"""发布者本地签名并经纯 SFTP 晋升；私钥与本地账本始终留在本机。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tuf.api.metadata import Metadata, TargetFile
from scripts.upload_release import CANDIDATE_ROOT, _attributes, _local_digest, upload_file
from satellite_debug_tool.updater.payload import canonical_bytes
from satellite_debug_tool.updater.publishing import _write_bytes, load_signers, publish_candidate
from satellite_debug_tool.updater.repository import https_base, repository_lock, safe_component, target_path
from scripts.verify_publication_server import REQUEST_SCHEMA, RESULT_SCHEMA

PUBLIC_ROOT = "/srv/satellite-updates/public/v1/apps"
STATE_ROOT = "/srv/satellite-updates/receipts"
VERIFY_REQUEST_ROOT = STATE_ROOT + "/verification/requests"
VERIFY_RESULT_ROOT = STATE_ROOT + "/verification/results"
MAX_METADATA = 64 * 1024**2
RANGE_BLOCK = 4096


def _read(sftp, path: str, limit: int = MAX_METADATA) -> bytes | None:
    info = _attributes(sftp, path)
    if info is None:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError("remote publication metadata requires a bounded regular file")
    with sftp.open(path, "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("remote publication metadata exceeds its bound")
    return data


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _remote_root(app_id: str) -> str:
    return PUBLIC_ROOT + "/" + safe_component(app_id)


def fetch_candidate(sftp, remote: str, output: Path) -> Path:
    """从非公开 incoming 区下载已封口候选，逐文件验证 receipt 后原子交付。"""
    remote_path, allowed = PurePosixPath(remote), PurePosixPath(CANDIDATE_ROOT)
    if str(remote_path) != remote or allowed not in remote_path.parents or ".." in remote_path.parts:
        raise ValueError("candidate download must be inside the fixed incoming root")
    current = PurePosixPath("/")
    for part in remote_path.parts[1:]:
        current /= safe_component(part)
        info = _attributes(sftp, str(current))
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise ValueError("candidate download requires real remote directories")
    raw = _read(sftp, remote + "/receipt.json", 1024**2)
    if raw is None:
        raise ValueError("remote candidate receipt is missing")
    document = json.loads(raw)
    rows = document.get("artifacts")
    if document.get("schema") != "satellite.release-candidate/v1" or not isinstance(rows, list) or not 1 <= len(rows) <= 4:
        raise ValueError("unsupported remote candidate receipt")
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("candidate download requires a fresh output directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".candidate-fetch-", dir=output.parent) as temp:
        staging, names = Path(temp), set()
        for row in rows:
            name, size, digest = safe_component(row["name"]), row.get("size"), row.get("sha256", "")
            if name in names or name == "receipt.json" or type(size) is not int or not 0 < size <= 2 * 1024**3 or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("remote candidate artifact metadata is invalid")
            names.add(name)
            info = _attributes(sftp, remote + "/" + name)
            if info is None or not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise ValueError("remote candidate artifact differs from receipt")
            actual, count = hashlib.sha256(), 0
            with sftp.open(remote + "/" + name, "rb") as src, (staging / name).open("xb") as dst:
                src.prefetch(file_size=src.stat().st_size, max_concurrent_requests=32)
                while block := src.read(min(1024**2, size - count + 1)):
                    count += len(block)
                    if count > size:
                        raise ValueError("remote candidate artifact exceeds receipt length")
                    actual.update(block)
                    dst.write(block)
            if count != size or actual.hexdigest() != digest:
                raise ValueError("remote candidate artifact differs from receipt")
        (staging / "receipt.json").write_bytes(raw)
        staging.rename(output)
    return output / "receipt.json"


def _ensure_directory(sftp, path: str, root: str) -> None:
    destination, allowed = PurePosixPath(path), PurePosixPath(root)
    if destination != allowed and allowed not in destination.parents:
        raise ValueError("publication path is outside its fixed root")
    current = PurePosixPath("/")
    public_root = PurePosixPath(PUBLIC_ROOT)
    for part in destination.parts[1:]:
        current /= safe_component(part)
        info = _attributes(sftp, str(current))
        if info is None:
            if current != allowed and allowed not in current.parents:
                raise ValueError("server publication parent directory must already exist")
            # 公开目录由独立 Nginx 身份遍历；私有发布状态继续限制到属主/组。
            mode = 0o755 if current == public_root or public_root in current.parents else 0o750
            sftp.mkdir(str(current), mode=mode)
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("publication directories must be real directories")


def _sync_remote(sftp, repository: Path, stage: Path, *, initial: bool) -> str | None:
    """验证并吸收在线低权续签；根/targets 身份和版本继续由本地可信根约束。"""
    directory = stage / "metadata"
    shutil.copytree(repository / "metadata", directory)
    root = Metadata.from_file(str(directory / "root.json"))
    app_id = root.signed.unrecognized_fields["app_id"]
    remote = _remote_root(app_id)
    raw_timestamp = _read(sftp, remote + "/metadata/timestamp.json")
    if initial:
        if raw_timestamp is not None:
            raise ValueError("initial publication requires an empty remote channel")
        # 初始化只上传当前 root 与本次新发行，源仓库必须还是空 targets。
        if Metadata.from_file(str(directory / "targets.json")).signed.targets:
            raise ValueError("initial publication requires an initialized empty local repository")
        return None
    if raw_timestamp is None:
        raise ValueError("remote channel is missing; initial publication requires --initial")
    for _ in range(32):
        version = root.signed.version + 1
        raw = _read(sftp, remote + f"/metadata/{version}.root.json")
        if raw is None:
            break
        candidate = Metadata.from_bytes(raw)
        if (candidate.signed.version != version or not candidate.signed.consistent_snapshot
                or candidate.signed.unrecognized_fields != root.signed.unrecognized_fields):
            raise ValueError("remote root chain identity differs")
        root.signed.verify_delegate("root", candidate.signed_bytes, candidate.signatures)
        candidate.signed.verify_delegate("root", candidate.signed_bytes, candidate.signatures)
        (directory / f"{version}.root.json").write_bytes(raw)
        (directory / "root.json").write_bytes(raw)
        root = candidate
    else:
        raise ValueError("remote root rotation chain exceeds publication bound")
    if root.signed.expires <= datetime.now(timezone.utc):
        raise ValueError("trusted root requires renewal before publication")
    timestamp = Metadata.from_bytes(raw_timestamp)
    root.signed.verify_delegate("timestamp", timestamp.signed_bytes, timestamp.signatures)
    local_timestamp = Metadata.from_file(str(directory / "timestamp.json"))
    if timestamp.signed.version < local_timestamp.signed.version:
        raise ValueError("remote timestamp precedes the local publication ledger")
    snapshot_ref = timestamp.signed.snapshot_meta
    raw_snapshot = _read(sftp, remote + f"/metadata/{snapshot_ref.version}.snapshot.json")
    if raw_snapshot is None:
        raise ValueError("remote snapshot is missing")
    snapshot_ref.verify_length_and_hashes(raw_snapshot)
    snapshot = Metadata.from_bytes(raw_snapshot)
    root.signed.verify_delegate("snapshot", snapshot.signed_bytes, snapshot.signatures)
    if snapshot.signed.version != snapshot_ref.version:
        raise ValueError("remote snapshot version differs")
    target_ref = snapshot.signed.meta["targets.json"]
    raw_targets = _read(sftp, remote + f"/metadata/{target_ref.version}.targets.json")
    if raw_targets is None:
        raise ValueError("remote targets metadata is missing")
    target_ref.verify_length_and_hashes(raw_targets)
    targets = Metadata.from_bytes(raw_targets)
    root.signed.verify_delegate("targets", targets.signed_bytes, targets.signatures)
    if targets.signed.version != target_ref.version:
        raise ValueError("remote targets version differs")
    local_targets = Metadata.from_file(str(directory / "targets.json"))
    if targets.signed.version < local_targets.signed.version:
        raise ValueError("remote targets precede the local publication ledger")
    # 此入口持 targets 签名权，可恢复过期 targets；客户端和在线低权续签仍拒绝过期事实。
    for name, raw in (("timestamp.json", raw_timestamp), ("snapshot.json", raw_snapshot),
                      (f"{snapshot_ref.version}.snapshot.json", raw_snapshot), ("targets.json", raw_targets),
                      (f"{target_ref.version}.targets.json", raw_targets)):
        (directory / name).write_bytes(raw)
    # 上一正式 catalog 和文件树足够构造一跳增量合同，历史完整包无需下载到发布机。
    for logical, info in targets.signed.targets.items():
        if logical.endswith(".json") and info.length <= 16 * 1024**2:
            path = PurePosixPath(target_path(logical))
            hashed = str(path.with_name(info.hashes["sha256"] + "." + path.name))
            raw = _read(sftp, remote + "/targets/" + hashed, 16 * 1024**2)
            if raw is None:
                raise ValueError("remote published JSON target is missing")
            info.verify_length_and_hashes(raw)
            local = stage / "targets" / hashed
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(raw)
    return _sha(raw_timestamp)


def prepare_bundle(sftp, repository: Path, candidate: Path, output: Path, signers: dict, *,
                   expected_commit: str, min_os: str, public_base_url: str, initial: bool = False,
                   channel: str = "stable", notes: str = "", min_updater_version: str = "1.0.0",
                   config_schema_min: int = 1, config_schema_max: int = 1) -> Path:
    """产生不可变 bundle；本地正式账本与远端 timestamp 都保持原状态。"""
    repository, output = Path(repository).resolve(), Path(output).resolve()
    base = https_base(public_base_url)
    if output.exists() or output.is_relative_to(repository):
        raise ValueError("publication bundle requires a fresh directory outside the local repository")
    output.parent.mkdir(parents=True, exist_ok=True)
    with repository_lock(repository / "publish.lock"), tempfile.TemporaryDirectory(prefix=".publication-", dir=output.parent) as tmp:
        bundle = Path(tmp)
        stage = bundle / "repository"
        expected = _sync_remote(sftp, repository, stage, initial=initial)
        root = Metadata.from_file(str(stage / "metadata/root.json"))
        app_id, repo_id = (root.signed.unrecognized_fields[key] for key in ("app_id", "repo_id"))
        receipt = json.loads((Path(candidate) / "receipt.json").read_text(encoding="utf-8"))
        if receipt.get("identity", {}).get("edition") != "ops":
            raise ValueError("online publication bundle requires the ops edition")
        result = publish_candidate(stage, candidate, signers, expected_commit=expected_commit, min_os=min_os,
            publication_mode="prepare", channel=channel, notes=notes, min_updater_version=min_updater_version,
            config_schema_min=config_schema_min, config_schema_max=config_schema_max)
        files = []
        for directory in (stage / "targets", stage / "metadata"):
            for path in sorted(directory.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(stage).as_posix()
                if directory.name == "metadata" and path.name in ("targets.json", "snapshot.json"):
                    continue
                size, digest = _local_digest(path)
                files.append({"path": relative, "size": size, "sha256": digest})
        document = {"schema": "satellite.publication-bundle/v1", "app_id": app_id, "repo_id": repo_id,
                    "local_repository": str(repository),
                    "public_base_url": base, "expected_timestamp_sha256": expected,
                    "timestamp_sha256": _sha((stage / "metadata/timestamp.json").read_bytes()),
                    "files": files, "publish_receipt": result}
        (bundle / "publication.json").write_bytes(canonical_bytes(document))
        bundle.rename(output)
    return output / "publication.json"


def _load_bundle(path: Path) -> tuple[dict, str]:
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_METADATA:
        raise ValueError("publication bundle requires a bounded regular manifest")
    raw = path.read_bytes()
    document = json.loads(raw)
    if document.get("schema") != "satellite.publication-bundle/v1" or raw != canonical_bytes(document):
        raise ValueError("unsupported publication bundle")
    safe_component(document["app_id"])
    safe_component(document["repo_id"])
    https_base(document["public_base_url"])
    names = set()
    for row in document["files"]:
        name = target_path(row["path"])
        if name in names or not name.startswith(("metadata/", "targets/")):
            raise ValueError("publication bundle contains an invalid file set")
        names.add(name)
        local = path.parent / "repository" / name
        if local.is_symlink() or _local_digest(local) != (row["size"], row["sha256"]):
            raise ValueError("publication bundle bytes changed after signing")
    if "metadata/timestamp.json" not in names or document["timestamp_sha256"] != _sha((path.parent / "repository/metadata/timestamp.json").read_bytes()):
        raise ValueError("publication timestamp differs from bundle identity")
    return document, _sha(raw)


@contextmanager
def _remote_lock(sftp, path: str, bundle_id: str, recover: bool):
    owner = canonical_bytes({"schema": "satellite.publication-lock/v1", "owner": bundle_id})
    current = _read(sftp, path, 1024)
    if current is not None:
        if not recover or current != owner:
            raise FileExistsError("publication lock is held; same bundle recovery requires --recover-lock after confirming the old publisher stopped")
        sftp.remove(path)
    with sftp.open(path, "wx") as stream:
        stream.write(owner)
        stream.flush()
    try:
        yield
    finally:
        # 断线时此操作可失败；保留 owner，后续只能显式恢复同一 bundle。
        if _read(sftp, path, 1024) == owner:
            sftp.remove(path)


class PublicRangeFetcher:
    """Read one exact HTTPS range; a server that ignores Range is not accepted."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def fetch_range(self, url: str, start: int, end: int) -> bytes:
        request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            if getattr(response, "status", None) != 206:
                raise ValueError("public publication range request was not honored")
            expected = f"bytes {start}-{end}/"
            if not response.headers.get("Content-Range", "").startswith(expected):
                raise ValueError("public publication Content-Range differs")
            data = response.read(end - start + 2)
        if len(data) != end - start + 1:
            raise ValueError("public publication range length differs")
        return data


def _immutable_rows(document: dict) -> list[dict]:
    return [row for row in document["files"]
            if row["path"] not in ("metadata/timestamp.json", "metadata/root.json")]


def _verification_request(document: dict, bundle_id: str) -> tuple[dict, bytes]:
    files = [{"path": row["path"], "size": row["size"], "sha256": row["sha256"]}
             for row in _immutable_rows(document)]
    request = {
        "schema": REQUEST_SCHEMA,
        "request_id": uuid.uuid4().hex + uuid.uuid4().hex,
        "bundle_id": bundle_id,
        "app_id": document["app_id"],
        "file_count": len(files),
        "total_bytes": sum(row["size"] for row in files),
        "files": files,
    }
    return request, canonical_bytes(request)


def _require_remote_directory(sftp, path: str) -> None:
    info = _attributes(sftp, path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise ValueError("server verification directory is unavailable")


def _request_server_verification(sftp, document: dict, bundle_id: str, *, timeout: float = 120.0) -> dict:
    """Submit a small request through SFTP and consume the root-owned server result."""
    _require_remote_directory(sftp, VERIFY_REQUEST_ROOT)
    _require_remote_directory(sftp, VERIFY_RESULT_ROOT)
    request, raw = _verification_request(document, bundle_id)
    request_digest = hashlib.sha256(raw).hexdigest()
    destination = VERIFY_REQUEST_ROOT + "/" + request["request_id"] + ".json"
    temporary = destination + ".submission-" + bundle_id
    with sftp.open(temporary, "wx") as stream:
        stream.write(raw)
        stream.flush()
    sftp.posix_rename(temporary, destination)
    result_path = VERIFY_RESULT_ROOT + "/" + request["request_id"] + ".json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result_raw = _read(sftp, result_path, 1024**2)
        if result_raw is None:
            time.sleep(0.2)
            continue
        result = json.loads(result_raw)
        if result_raw != canonical_bytes(result) or result.get("schema") != RESULT_SCHEMA:
            raise ValueError("server verification result is unsupported")
        expected = {
            "request_id": request["request_id"],
            "request_sha256": request_digest,
            "bundle_id": bundle_id,
            "app_id": document["app_id"],
        }
        if any(result.get(key) != value for key, value in expected.items()):
            raise ValueError("server verification result differs from this request")
        if result.get("state") != "verified":
            raise ValueError("server publication verification failed: " + str(result.get("error", "unknown error")))
        if (result.get("file_count"), result.get("total_bytes")) != (
                request["file_count"], request["total_bytes"]):
            raise ValueError("server verification result totals differ")
        return result
    raise TimeoutError("server publication verification result timed out")


def _probe_public_samples(document: dict, repository: Path, fetcher=None) -> None:
    """Prove Nginx maps each target to the locally signed bytes without a full public download."""
    fetcher = fetcher or PublicRangeFetcher()
    for row in _immutable_rows(document):
        size = row["size"]
        if size == 0:
            continue
        starts = sorted({0, max(0, (size - RANGE_BLOCK) // 2), max(0, size - RANGE_BLOCK)})
        source = repository / row["path"]
        with source.open("rb") as stream:
            for start in starts:
                end = min(size, start + RANGE_BLOCK) - 1
                stream.seek(start)
                expected = stream.read(end - start + 1)
                if hasattr(fetcher, "fetch_range"):
                    actual = fetcher.fetch_range(document["public_base_url"] + row["path"], start, end)
                else:
                    # Test-only compatibility for the historical FetcherInterface substitute.
                    entire = b"".join(fetcher.fetch(document["public_base_url"] + row["path"]))
                    actual = entire[start:end + 1]
                if actual != expected:
                    raise ValueError("public publication range differs from signed bundle bytes")


def _record_local_commit(document: dict, manifest: Path) -> None:
    """远端提交后推进本地受信版本围栏；重试已发布 bundle 也补齐该步骤。"""
    repository = Path(document["local_repository"])
    with repository_lock(repository / "publish.lock"):
        root = Metadata.from_file(str(repository / "metadata/root.json"))
        if (root.signed.unrecognized_fields.get("app_id") != document["app_id"]
                or root.signed.unrecognized_fields.get("repo_id") != document["repo_id"]):
            raise ValueError("local publication ledger identity differs from bundle")
        current = Metadata.from_file(str(repository / "metadata/timestamp.json"))
        source = manifest.parent / "repository/metadata"
        timestamp = Metadata.from_file(str(source / "timestamp.json"))
        if current.signed.version > timestamp.signed.version:
            raise ValueError("local publication ledger already records a later timestamp")
        for row in document["files"]:
            if row["path"].startswith("metadata/") and row["path"] not in ("metadata/root.json", "metadata/timestamp.json"):
                _write_bytes(repository / row["path"], (manifest.parent / "repository" / row["path"]).read_bytes(), immutable=True)
        snapshot_name = f"{timestamp.signed.snapshot_meta.version}.snapshot.json"
        snapshot = Metadata.from_file(str(source / snapshot_name))
        target_name = f"{snapshot.signed.meta['targets.json'].version}.targets.json"
        for name, origin in (("root.json", "root.json"), ("targets.json", target_name),
                             ("snapshot.json", snapshot_name), ("timestamp.json", "timestamp.json")):
            _write_bytes(repository / "metadata" / name, (source / origin).read_bytes())


def commit_bundle(sftp, manifest: Path, *, fetcher=None, recover_lock: bool = False) -> dict:
    """只上传公开签名数据；ECS 完整校验与 HTTPS 分段探测成功后切换 timestamp。"""
    manifest = Path(manifest).resolve(strict=True)
    with repository_lock(manifest.parent / "commit.lock"):
        document, identity = _load_bundle(manifest)
        remote = _remote_root(document["app_id"])
        _ensure_directory(sftp, remote, PUBLIC_ROOT)
        _ensure_directory(sftp, STATE_ROOT, STATE_ROOT)
        lock = STATE_ROOT + "/" + document["app_id"] + ".publication.lock"
        with _remote_lock(sftp, lock, identity, recover_lock):
            timestamp_path = remote + "/metadata/timestamp.json"
            before = _read(sftp, timestamp_path)
            current_digest = None if before is None else _sha(before)
            if current_digest == document["timestamp_sha256"]:
                _record_local_commit(document, manifest)
                return {"state": "published", "bundle_id": identity, "reused": True}
            if current_digest != document["expected_timestamp_sha256"]:
                raise ValueError("remote channel changed after this publication was prepared")
            mutable = {"metadata/timestamp.json", "metadata/root.json"}
            for row in document["files"]:
                if row["path"] in mutable:
                    continue
                destination = remote + "/" + row["path"]
                _ensure_directory(sftp, str(PurePosixPath(destination).parent), PUBLIC_ROOT)
                upload_file(sftp, manifest.parent / "repository" / row["path"], destination,
                            size=row["size"], sha256=row["sha256"])
            verification = _request_server_verification(sftp, document, identity)
            _probe_public_samples(document, manifest.parent / "repository", fetcher)
            # timestamp 最后提交；root.json 是供在线低权续签读取的公开根指针。
            for name in ("metadata/root.json", "metadata/timestamp.json"):
                row = next(row for row in document["files"] if row["path"] == name)
                destination = remote + "/" + name
                temporary = destination + ".publication-" + identity
                _ensure_directory(sftp, str(PurePosixPath(destination).parent), PUBLIC_ROOT)
                upload_file(sftp, manifest.parent / "repository" / name, temporary,
                            size=row["size"], sha256=row["sha256"])
                # OpenSSH posix-rename 提供同文件系统原子替换；不支持时明确失败。
                sftp.posix_rename(temporary, destination)
            _record_local_commit(document, manifest)
            return {"state": "published", "bundle_id": identity, "timestamp_sha256": document["timestamp_sha256"],
                    "verification_request_id": verification["request_id"], "reused": False}


def _connect(args):
    config = json.loads(args.config.read_text(encoding="utf-8"))
    host, username, port = config.get("host", ""), config.get("username", ""), config.get("port", 22)
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host)
            or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username)
            or type(port) is not int or not 1 <= port <= 65535
            or config.get("public_root") != PUBLIC_ROOT or config.get("state_root") != STATE_ROOT):
        raise ValueError("publication SSH configuration differs from fixed path contract")
    import paramiko
    client = paramiko.SSHClient()
    client.load_host_keys(str(args.known_hosts.resolve(strict=True)))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(host, port=port, username=username, key_filename=str(args.identity_file.resolve(strict=True)),
                       look_for_keys=False, allow_agent=False, timeout=20, banner_timeout=20, auth_timeout=20)
        transport = client.get_transport()
        if transport is None:
            raise ConnectionError("publication SSH transport is unavailable")
        # 完整 HTTPS 回读期间 SFTP 空闲；由同一 SSH owner 保持协议活动直至最终提交。
        transport.set_keepalive(30)
    except Exception:
        client.close()
        raise
    return client


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "prepare", "commit"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--key-dir", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--remote-candidate")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-commit")
    parser.add_argument("--min-os")
    parser.add_argument("--min-updater-version", default="1.0.0")
    parser.add_argument("--config-schema-min", type=int, default=1)
    parser.add_argument("--config-schema-max", type=int, default=1)
    parser.add_argument("--public-base-url")
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    parser.add_argument("--notes-file", type=Path)
    parser.add_argument("--initial", action="store_true")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--recover-lock", action="store_true")
    args = parser.parse_args(argv)
    client = _connect(args)
    try:
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(60)
            if args.command == "fetch":
                if args.remote_candidate is None or args.output is None:
                    parser.error("fetch requires --remote-candidate and --output")
                result = {"state": "candidate-downloaded", "receipt": str(fetch_candidate(sftp, args.remote_candidate, args.output))}
            elif args.command == "prepare":
                if any(getattr(args, key) is None for key in ("repository", "key_dir", "candidate", "output", "expected_commit", "min_os", "public_base_url")):
                    parser.error("prepare requires repository, key-dir, candidate, output, expected-commit, min-os and public-base-url")
                result = {"state": "prepared", "bundle": str(prepare_bundle(sftp, args.repository, args.candidate, args.output,
                    load_signers(args.key_dir, ("targets", "snapshot", "timestamp")), expected_commit=args.expected_commit,
                    min_os=args.min_os, public_base_url=args.public_base_url, initial=args.initial, channel=args.channel,
                    min_updater_version=args.min_updater_version, config_schema_min=args.config_schema_min,
                    config_schema_max=args.config_schema_max,
                    notes=args.notes_file.read_text(encoding="utf-8") if args.notes_file else ""))}
            else:
                if args.bundle is None:
                    parser.error("commit requires --bundle")
                result = commit_bundle(sftp, args.bundle, recover_lock=args.recover_lock)
            print(json.dumps(result, ensure_ascii=False))
    finally:
        client.close()


if __name__ == "__main__":
    main()
