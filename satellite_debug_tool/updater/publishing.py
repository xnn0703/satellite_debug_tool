"""静态 TUF 仓库的受控发布；文件先就位，timestamp 最后推进。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import threading
from typing import Callable, Mapping
import zipfile
from uuid import uuid4

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from securesystemslib.signer import CryptoSigner, Signer
from tuf.api.metadata import Metadata, MetaFile, Root, Snapshot, TargetFile, Targets, Timestamp
from tuf.api.serialization.json import JSONSerializer

from satellite_debug_tool.updater.repository import HttpsFetcher, atomic_json, https_base, repository_lock, safe_component, target_path
from satellite_debug_tool.versioning import compare_versions, parse_version


ROLES = ("root", "targets", "snapshot", "timestamp")
_SERIALIZER = JSONSerializer(compact=True)


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).replace(microsecond=0)


def _write_bytes(path: Path, value: bytes, *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        if path.read_bytes() != value:
            raise ValueError(f"immutable publication differs: {path.name}")
        return
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _next_metadata_version(directory: Path, role: str) -> int:
    versions = [int(path.name.split(".", 1)[0]) for path in directory.glob(f"*.{role}.json")
                if path.name.split(".", 1)[0].isdigit()]
    return max(versions, default=0) + 1


def load_signers(key_dir: Path, roles=ROLES) -> dict[str, Signer]:
    signers = {}
    for role in roles:
        path = Path(key_dir) / f"{role}.pem"
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise ValueError(f"signing key permissions require owner-only access: {role}")
        password = os.environ.get(f"SATELLITE_SIGNING_{role.upper()}_PASSWORD")
        key = load_pem_private_key(path.read_bytes(), password=password.encode() if password else None)
        signers[role] = CryptoSigner(key)
    return signers


@contextmanager
def promotion_lock(path: Path):
    """服务器续签与 SFTP 晋升使用同一个 O_EXCL 文件，持有者结束才释放。"""
    path = Path(path)
    owner = json.dumps({"schema": "satellite.publication-lock/v1", "owner": uuid4().hex}).encode()
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(owner)
    try:
        yield
    finally:
        if path.read_bytes() == owner:
            path.unlink()


def initialize_repository(
    repository: Path, signers: Mapping[str, Signer], *, app_id: str, repo_id: str,
    now: datetime | None = None,
) -> dict:
    """显式初始化，调用者负责在独立环境保管密钥；已存在仓库拒绝覆盖。"""
    repository = Path(repository)
    app_id, repo_id = safe_component(app_id), safe_component(repo_id)
    now = _now(now)
    with repository_lock(repository / "publish.lock"):
        metadata_dir = repository / "metadata"
        if (metadata_dir / "root.json").exists():
            raise ValueError("repository already initialized")
        root = Metadata(Root(version=1, expires=now + timedelta(days=365), consistent_snapshot=True,
                             unrecognized_fields={"app_id": app_id, "repo_id": repo_id}))
        for role in ROLES:
            root.signed.add_key(signers[role].public_key, role)
        root.sign(signers["root"])
        raw = root.to_bytes(_SERIALIZER)
        _write_bytes(metadata_dir / "1.root.json", raw, immutable=True)
        _write_bytes(metadata_dir / "root.json", raw)
        _publish_metadata(repository, Metadata(Targets(version=1, expires=now + timedelta(days=90))), signers, now=now)
        return root.to_dict()


def _publish_metadata(repository: Path, targets: Metadata, signers: Mapping[str, Signer], *, now: datetime) -> None:
    metadata_dir = repository / "metadata"
    root = Metadata.from_file(str(metadata_dir / "root.json"))
    if root.signed.expires <= now:
        raise ValueError("root renewal requires its authorized signing owner")
    targets.signed.version = max(targets.signed.version, _next_metadata_version(metadata_dir, "targets"))
    targets.sign(signers["targets"])
    root.signed.verify_delegate("targets", targets.signed_bytes, targets.signatures)
    targets_raw = targets.to_bytes(_SERIALIZER)
    snapshot_version = _next_metadata_version(metadata_dir, "snapshot")
    timestamp_version = 1
    if (metadata_dir / "timestamp.json").exists():
        timestamp_version = Metadata.from_file(str(metadata_dir / "timestamp.json")).signed.version + 1
    snapshot = Metadata(Snapshot(version=snapshot_version, expires=now + timedelta(days=30), meta={
        "targets.json": MetaFile.from_data(targets.signed.version, targets_raw, ["sha256"]),
    }))
    snapshot.sign(signers["snapshot"])
    root.signed.verify_delegate("snapshot", snapshot.signed_bytes, snapshot.signatures)
    snapshot_raw = snapshot.to_bytes(_SERIALIZER)
    timestamp = Metadata(Timestamp(version=timestamp_version, expires=now + timedelta(days=7),
        snapshot_meta=MetaFile.from_data(snapshot_version, snapshot_raw, ["sha256"])))
    timestamp.sign(signers["timestamp"])
    root.signed.verify_delegate("timestamp", timestamp.signed_bytes, timestamp.signatures)
    _write_bytes(metadata_dir / f"{targets.signed.version}.targets.json", targets_raw, immutable=True)
    _write_bytes(metadata_dir / "targets.json", targets_raw)
    _write_bytes(metadata_dir / f"{snapshot_version}.snapshot.json", snapshot_raw, immutable=True)
    _write_bytes(metadata_dir / "snapshot.json", snapshot_raw)
    _write_bytes(metadata_dir / "timestamp.json", timestamp.to_bytes(_SERIALIZER))


def _store_target(repository: Path, logical: str, source: Path) -> TargetFile:
    logical = target_path(logical)
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError("publish input requires a regular file")
    info = TargetFile.from_file(logical, str(source), ["sha256"])
    path = PurePosixPath(logical)
    destination = repository / "targets" / path.with_name(info.hashes["sha256"] + "." + path.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        with destination.open("rb") as stream:
            info.verify_length_and_hashes(stream)
        return info
    temporary = destination.with_name(destination.name + ".tmp")
    with source.open("rb") as src, temporary.open("wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    with temporary.open("rb") as stream:
        info.verify_length_and_hashes(stream)
    temporary.replace(destination)
    return info


def _validate_catalog(path: str, source: Path, targets: dict[str, TargetFile], app_id: str) -> tuple[str, dict]:
    data = json.loads(Path(source).read_text(encoding="utf-8"))
    identity = data.get("identity", {})
    version = str(identity.get("version", ""))
    parsed = parse_version(version, complete=True)
    if data.get("schema") != "satellite.update-catalog/v1" or identity.get("app_id") != app_id or identity.get("edition") != "ops":
        raise ValueError("online catalog requires the repository's ops application")
    channel, platform, architecture = data.get("channel"), identity.get("platform"), identity.get("architecture")
    if channel not in ("stable", "beta") or platform not in ("windows", "macos") or architecture not in ("x86_64", "arm64"):
        raise ValueError("catalog identity is unsupported")
    if channel == "stable" and parsed.prerelease:
        raise ValueError("stable catalog requires a stable version")
    if path != f"catalog/ops/{channel}/{platform}/{architecture}.json":
        raise ValueError("catalog path differs from release identity")
    references = [data["package"]]
    if platform == "windows":
        references.append(data["tree_manifest"])
    deltas = data.get("deltas", [])
    if not isinstance(deltas, list) or len(deltas) > 1 or (deltas and platform != "windows"):
        raise ValueError("catalog permits one Windows delta")
    for delta in deltas:
        references.extend([delta["package"], delta["base_manifest"]])
    for reference in references:
        if target_path(reference) not in targets:
            raise ValueError(f"catalog references an unpublished target: {reference}")
    # 版本记录单独保持不可变，换路径也不能偷偷替换同版安装字节。
    record = {"identity": identity, "package": targets[data["package"]].to_dict()}
    if platform == "windows":
        record["tree_manifest"] = targets[data["tree_manifest"]].to_dict()
    version_key = version.lstrip("vV").split("+", 1)[0]
    record_path = f"releases/ops/{platform}/{architecture}/{version_key}.json"
    return record_path, record


def publish_targets(
    repository: Path, inputs: Mapping[str, Path], signers: Mapping[str, Signer], *,
    now: datetime | None = None, expected_catalogs: Mapping[str, str | None] | None = None,
    precommit_probe: Callable[[Mapping[str, TargetFile]], None] | None = None,
) -> dict:
    repository, now = Path(repository), _now(now)
    with repository_lock(repository / "publish.lock"):
        metadata_dir = repository / "metadata"
        root = Metadata.from_file(str(metadata_dir / "root.json"))
        current = Metadata.from_file(str(metadata_dir / "targets.json"))
        root.signed.verify_delegate("targets", current.signed_bytes, current.signatures)
        target_files = dict(current.signed.targets)
        for logical, expected in (expected_catalogs or {}).items():
            existing = target_files.get(logical)
            actual = existing.hashes["sha256"] if existing else None
            if actual != expected:
                raise ValueError("release channel changed while preparing candidate publication")
        for logical, source in inputs.items():
            logical = target_path(logical)
            info = _store_target(repository, logical, Path(source))
            existing = target_files.get(logical)
            mutable = logical.startswith("catalog/") or logical == "bootstrap/sources.json"
            if existing is not None and existing != info and not mutable:
                raise ValueError(f"immutable target identity differs: {logical}")
            target_files[logical] = info
        probe_paths = set(inputs)
        for logical, source in inputs.items():
            if not logical.startswith("catalog/"):
                continue
            record_path, record = _validate_catalog(logical, Path(source), target_files, root.signed.unrecognized_fields["app_id"])
            record_bytes = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            info = TargetFile(len(record_bytes), {"sha256": hashlib.sha256(record_bytes).hexdigest()}, record_path)
            if record_path in target_files and target_files[record_path] != info:
                raise ValueError("same release version has different package bytes")
            local = repository / "records" / record_path
            _write_bytes(local, record_bytes, immutable=True)
            target_files[record_path] = _store_target(repository, record_path, local)
            probe_paths.add(record_path)
            catalog = json.loads(Path(source).read_text(encoding="utf-8"))
            probe_paths.add(catalog["package"])
            if catalog.get("tree_manifest"):
                probe_paths.add(catalog["tree_manifest"])
            for delta in catalog.get("deltas", []):
                probe_paths.update((delta["package"], delta["base_manifest"]))
        if precommit_probe is not None:
            precommit_probe({path: target_files[path] for path in sorted(probe_paths)})
        metadata = Metadata(Targets(version=current.signed.version + 1, expires=now + timedelta(days=90), targets=target_files))
        _publish_metadata(repository, metadata, signers, now=now)
        receipt = {"schema": "satellite.publish-receipt/v1", "app_id": root.signed.unrecognized_fields["app_id"],
                   "repo_id": root.signed.unrecognized_fields["repo_id"], "targets_version": metadata.signed.version,
                   "published_at": now.isoformat(), "targets": {p: target_files[p].to_dict() for p in inputs}}
        atomic_json(repository / "last-publish.json", receipt)
        return receipt


def refresh_metadata(repository: Path, signers: Mapping[str, Signer], *, now: datetime | None = None) -> dict:
    """无新版本时刷新有效期；root 由离线维护单独续签。"""
    return publish_targets(repository, {}, signers, now=now)


def _read_published_target(repository: Path, info: TargetFile) -> bytes:
    path = PurePosixPath(info.path)
    stored = repository / "targets" / path.with_name(info.hashes["sha256"] + "." + path.name)
    with stored.open("rb") as stream:
        info.verify_length_and_hashes(stream)
    if info.length > 16 * 1024 * 1024:
        raise ValueError("published JSON exceeds bounded size")
    return stored.read_bytes()


def https_target_probe(base_url: str) -> Callable[[Mapping[str, TargetFile]], None]:
    """正式 URL 流式回读；入口权限/路径或字节错误发生在渠道推进之前。"""
    import urllib.parse
    base_url = https_base(base_url)
    fetcher = HttpsFetcher(15.0, threading.Event())
    def verify(targets: Mapping[str, TargetFile]) -> None:
        for info in targets.values():
            path = PurePosixPath(target_path(info.path))
            hashed_path = str(path.with_name(info.hashes["sha256"] + "." + path.name))
            url = base_url + urllib.parse.quote(hashed_path, safe="/")
            digest = hashlib.sha256()
            size = 0
            for block in fetcher.fetch(url):
                size += len(block)
                if size > info.length:
                    raise ValueError("public target exceeds its trusted length")
                digest.update(block)
            if size != info.length or digest.hexdigest() != info.hashes["sha256"]:
                raise ValueError("public target differs from publication bytes")
    return verify


def publish_candidate(
    repository: Path, candidate: Path, signers: Mapping[str, Signer], **options,
) -> dict:
    """把回执指定的字节冻结到发布者私有目录，上传账号后续写入不改变本次签名。"""
    from satellite_debug_tool.updater.payload import sha256_file
    candidate = Path(candidate).resolve(strict=True)
    receipt = candidate / "receipt.json"
    if receipt.is_symlink() or receipt.stat().st_size > 1024 * 1024:
        raise ValueError("candidate receipt requires a bounded regular file")
    with receipt.open("rb") as stream:
        receipt_bytes = stream.read(1024 * 1024 + 1)
    if len(receipt_bytes) > 1024 * 1024:
        raise ValueError("candidate receipt exceeds size limit")
    document = json.loads(receipt_bytes)
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 4:
        raise ValueError("candidate requires a bounded artifact list")
    names = [safe_component(item["name"]) for item in artifacts]
    upload_owner = candidate / ".upload-local-lock"
    if upload_owner.exists() and (upload_owner.is_symlink() or not upload_owner.is_file()):
        raise ValueError("candidate upload ownership requires a regular local file")
    entries = {p.name for p in candidate.iterdir()} - {".upload-local-lock"}
    if len(set(names)) != len(names) or "receipt.json" in names or entries != {*names, "receipt.json"}:
        raise ValueError("candidate directory differs from receipt artifact set")
    with tempfile.TemporaryDirectory(prefix="verified-candidate-") as temporary:
        frozen = Path(temporary)
        for item, name in zip(artifacts, names):
            size = item.get("size")
            if type(size) is not int or not 0 < size <= 2 * 1024**3:
                raise ValueError("candidate artifact exceeds size limit")
            source = candidate / name
            if source.is_symlink():
                raise ValueError("candidate requires regular artifact files")
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as src, (frozen / name).open("wb") as dst:
                source_stat = os.fstat(src.fileno())
                if not stat.S_ISREG(source_stat.st_mode):
                    raise ValueError("candidate requires regular artifact files")
                if source_stat.st_size != size:
                    raise ValueError("candidate artifact differs from its receipt")
                remaining = size
                while remaining:
                    block = src.read(min(remaining, 1024 * 1024))
                    if not block:
                        raise ValueError("candidate artifact differs from its receipt")
                    dst.write(block)
                    remaining -= len(block)
                if src.read(1):
                    raise ValueError("candidate artifact differs from its receipt")
            if (frozen / name).stat().st_size != item["size"] or sha256_file(frozen / name) != item["sha256"]:
                raise ValueError("candidate artifact differs from its receipt")
        (frozen / "receipt.json").write_bytes(receipt_bytes)
        return _publish_frozen_candidate(repository, frozen, signers, **options)


def _publish_frozen_candidate(
    repository: Path, candidate: Path, signers: Mapping[str, Signer], *, expected_commit: str,
    channel: str = "stable", min_os: str, min_updater_version: str = "1.0.0",
    config_schema_min: int = 1, config_schema_max: int = 1, notes: str = "",
    manual_root: Path | None = None,
    public_target_base_url: str | None = None,
    precommit_probe: Callable[[Mapping[str, TargetFile]], None] | None = None,
    publication_mode: str = "online",
) -> dict:
    """直接消费构建机回执；正式签名 owner 在该入口绑定批准的源码 SHA。"""
    from satellite_debug_tool.edition import create_build_identity
    from satellite_debug_tool.updater.payload import canonical_bytes, read_manifest, sha256_file
    repository, candidate = Path(repository), Path(candidate).resolve(strict=True)
    receipt_path = candidate / "receipt.json"
    if receipt_path.is_symlink() or receipt_path.stat().st_size > 1024 * 1024:
        raise ValueError("candidate receipt requires a bounded regular file")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("schema") != "satellite.release-candidate/v1":
        raise ValueError("unsupported candidate receipt schema")
    identity = receipt["identity"]
    canonical = create_build_identity(**{k: identity[k] for k in ("edition", "version", "platform", "architecture", "build_id")})
    if identity != canonical or identity["build_id"] != expected_commit or receipt.get("source_commit") != expected_commit:
        raise ValueError("candidate source differs from the approved exact commit")
    root = Metadata.from_file(str(repository / "metadata/root.json"))
    if identity["app_id"] != root.signed.unrecognized_fields["app_id"]:
        raise ValueError("candidate application differs from repository")
    records = receipt.get("artifacts")
    if not isinstance(records, list) or not 1 <= len(records) <= 4:
        raise ValueError("candidate requires a bounded artifact list")
    artifacts = {}
    for item in records:
        name = safe_component(item["name"])
        path = candidate / name
        if name in artifacts or path.is_symlink() or not path.is_file() or path.stat().st_size != item["size"] or sha256_file(path) != item["sha256"]:
            raise ValueError("candidate artifact differs from its receipt")
        artifacts[name] = path
    if {p.name for p in candidate.iterdir()} != {*artifacts, "receipt.json"}:
        raise ValueError("candidate directory differs from receipt artifact set")
    platform = {"win": "windows", "mac": "macos"}[identity["platform"]]
    identity = {**identity, "platform": platform}
    archive_suffix = ".7z" if platform == "windows" else ".zip"
    packages = [p for n, p in artifacts.items() if n.endswith(archive_suffix) and n != "delta.zip"]
    if len(packages) != 1:
        raise ValueError("candidate requires one complete platform archive")
    if identity["edition"] == "user":
        if manual_root is None:
            raise ValueError("user publication requires a separate manual delivery root")
        if Path(manual_root).resolve().is_relative_to(repository.resolve()):
            raise ValueError("manual delivery root must be separate from the public repository")
        destination = Path(manual_root) / identity["app_id"] / platform / identity["architecture"] / identity["version"]
        with repository_lock(Path(manual_root) / "delivery.lock"):
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if (destination / "receipt.json").read_bytes() != receipt_path.read_bytes():
                    raise ValueError("manual delivery version already contains different bytes")
                for name, path in artifacts.items():
                    if sha256_file(destination / name) != sha256_file(path):
                        raise ValueError("manual delivery artifact differs")
            else:
                with tempfile.TemporaryDirectory(prefix=".delivery-", dir=destination.parent) as temporary:
                    staging = Path(temporary)
                    for name, path in {**artifacts, "receipt.json": receipt_path}.items():
                        shutil.copyfile(path, staging / name)
                    staging.rename(destination)
        return {"schema": "satellite.manual-delivery/v1", "identity": identity, "directory": str(destination)}
    if channel not in ("stable", "beta"):
        raise ValueError("unsupported publication channel")
    from satellite_debug_tool.updater.checker import _os_tuple
    _os_tuple(min_os)
    parse_version(min_updater_version, complete=True)
    if type(config_schema_min) is not int or type(config_schema_max) is not int or not 1 <= config_schema_min <= config_schema_max:
        raise ValueError("publication requires an explicit supported configuration schema range")
    if publication_mode not in ("online", "prepare"):
        raise ValueError("unsupported publication mode")
    if publication_mode == "prepare" and (precommit_probe is not None or public_target_base_url is not None):
        raise ValueError("prepared publication commits through the SFTP promotion boundary")
    if publication_mode == "online" and precommit_probe is None:
        if public_target_base_url is None:
            raise ValueError("online publication requires public HTTPS target readback")
        precommit_probe = https_target_probe(public_target_base_url)
    version = identity["version"]
    prefix = f"packages/ops/{platform}/{identity['architecture']}/{version}"
    inputs = {f"{prefix}/{name}": path for name, path in artifacts.items()}
    logical = f"catalog/ops/{channel}/{platform}/{identity['architecture']}.json"
    existing_targets = Metadata.from_file(str(repository / "metadata/targets.json")).signed.targets
    previous_info = existing_targets.get(logical)
    previous = json.loads(_read_published_target(repository, previous_info)) if previous_info else None
    if previous and compare_versions(version, previous["identity"]["version"]) < 0:
        raise ValueError("candidate version precedes the current release channel")
    catalog = {"schema": "satellite.update-catalog/v1", "identity": identity, "channel": channel, "update_protocol": 1,
               "min_updater_version": min_updater_version, "min_os": min_os,
               "config_schema": {"min": config_schema_min, "max": config_schema_max},
               "package_format": "7z" if platform == "windows" else "zip",
               "layout_version": "windows-onedir-v1" if platform == "windows" else "macos-app-v1",
               "installation_mode": "automatic",
               "package": f"{prefix}/{packages[0].name}", "notes": notes, "deltas": []}
    if platform in {"windows", "macos"}:
        tree = read_manifest(artifacts["payload-manifest.json"])
        if tree["identity"] != identity:
            raise ValueError("candidate tree identity differs from receipt")
        if canonical_bytes(tree) != artifacts["payload-manifest.json"].read_bytes():
            raise ValueError("candidate tree requires canonical bytes")
        catalog["tree_manifest"] = f"{prefix}/payload-manifest.json"
        if "delta.zip" in artifacts:
            if previous is None:
                raise ValueError("delta candidate requires a previous published baseline")
            with zipfile.ZipFile(artifacts["delta.zip"]) as archive:
                if archive.getinfo("delta.json").file_size > 16 * 1024 * 1024:
                    raise ValueError("delta descriptor exceeds size limit")
                descriptor = json.loads(archive.read("delta.json"))
            # 重试同一个版本保留原 baseline；新版本只接受当前渠道上一正式版。
            if previous["identity"]["version"] == version:
                prior_deltas = previous.get("deltas", [])
                if len(prior_deltas) != 1:
                    raise ValueError("published complete-only release requires a new version to add delta")
                base_path = prior_deltas[0]["base_manifest"]
                base_build = prior_deltas[0]["base_build_id"]
            else:
                base_path = previous["tree_manifest"]
                base_build = previous["identity"]["build_id"]
            base_digest = existing_targets[base_path].hashes["sha256"]
            tree_digest = sha256_file(artifacts["payload-manifest.json"])
            if descriptor.get("schema") != "satellite.update-delta/v1" or descriptor.get("base_manifest_sha256") != base_digest or descriptor.get("target_manifest_sha256") != tree_digest:
                raise ValueError("delta differs from previous published baseline or candidate tree")
            if artifacts["delta.zip"].stat().st_size < packages[0].stat().st_size:
                catalog["deltas"] = [{"package": f"{prefix}/delta.zip", "base_manifest": base_path,
                    "base_manifest_sha256": base_digest, "target_manifest_sha256": tree_digest, "base_build_id": base_build}]
    with tempfile.TemporaryDirectory(prefix="catalog-") as temporary:
        catalog_path = Path(temporary) / "catalog.json"
        encoded = canonical_bytes(catalog)
        if len(encoded) > 256 * 1024:
            raise ValueError("release catalog exceeds client size limit")
        catalog_path.write_bytes(encoded)
        inputs[logical] = catalog_path
        return publish_targets(repository, inputs, signers,
                               expected_catalogs={logical: previous_info.hashes["sha256"] if previous_info else None},
                               precommit_probe=precommit_probe)


def refresh_online_metadata(
    repository: Path, signers: Mapping[str, Signer], *, refresh_snapshot: bool = False,
    now: datetime | None = None,
) -> dict:
    """日常续签只持有 timestamp，周维护另提供 snapshot；targets/root 权限保持独立。"""
    repository, now = Path(repository), _now(now)
    with repository_lock(repository / "publish.lock"):
        directory = repository / "metadata"
        root = Metadata.from_file(str(directory / "root.json"))
        timestamp = Metadata.from_file(str(directory / "timestamp.json"))
        root.signed.verify_delegate("timestamp", timestamp.signed_bytes, timestamp.signatures)
        snapshot_info = timestamp.signed.snapshot_meta
        snapshot_raw = (directory / f"{snapshot_info.version}.snapshot.json").read_bytes()
        snapshot_info.verify_length_and_hashes(snapshot_raw)
        snapshot = Metadata.from_bytes(snapshot_raw)
        root.signed.verify_delegate("snapshot", snapshot.signed_bytes, snapshot.signatures)
        target_info = snapshot.signed.meta["targets.json"]
        targets_raw = (directory / f"{target_info.version}.targets.json").read_bytes()
        target_info.verify_length_and_hashes(targets_raw)
        targets = Metadata.from_bytes(targets_raw)
        root.signed.verify_delegate("targets", targets.signed_bytes, targets.signatures)
        if root.signed.expires <= now or targets.signed.expires <= now:
            raise ValueError("root/targets renewal requires its authorized signing owner")
        if refresh_snapshot:
            snapshot.signed.version = _next_metadata_version(directory, "snapshot")
            snapshot.signed.expires = min(now + timedelta(days=30), targets.signed.expires, root.signed.expires)
            snapshot.sign(signers["snapshot"])
            root.signed.verify_delegate("snapshot", snapshot.signed_bytes, snapshot.signatures)
            snapshot_raw = snapshot.to_bytes(_SERIALIZER)
            _write_bytes(directory / f"{snapshot.signed.version}.snapshot.json", snapshot_raw, immutable=True)
            _write_bytes(directory / "snapshot.json", snapshot_raw)
        if snapshot.signed.expires <= now:
            raise ValueError("snapshot renewal requires its authorized signing owner")
        timestamp.signed.version += 1
        timestamp.signed.expires = min(now + timedelta(days=7), snapshot.signed.expires, targets.signed.expires, root.signed.expires)
        timestamp.signed.snapshot_meta = MetaFile.from_data(snapshot.signed.version, snapshot_raw, ["sha256"])
        timestamp.sign(signers["timestamp"])
        root.signed.verify_delegate("timestamp", timestamp.signed_bytes, timestamp.signatures)
        _write_bytes(directory / "timestamp.json", timestamp.to_bytes(_SERIALIZER))
        return {"schema": "satellite.renewal-receipt/v1", "timestamp_version": timestamp.signed.version,
                "snapshot_version": snapshot.signed.version, "targets_version": targets.signed.version,
                "timestamp_expires": timestamp.signed.expires.isoformat(), "targets_expires": targets.signed.expires.isoformat(),
                "root_expires": root.signed.expires.isoformat()}


def rotate_root(repository: Path, old_signer: Signer, new_signers: Mapping[str, Signer], *, now: datetime | None = None) -> dict:
    repository, now = Path(repository), _now(now)
    with repository_lock(repository / "publish.lock"):
        metadata_dir = repository / "metadata"
        old = Metadata.from_file(str(metadata_dir / "root.json"))
        def matches_keys(metadata: Metadata) -> bool:
            return all(metadata.signed.roles[role].keyids == [new_signers[role].public_key.keyid]
                       and metadata.signed.roles[role].threshold == 1 for role in ROLES)

        if old_signer.public_key.keyid not in old.signed.roles["root"].keyids:
            # root 指针已更新而下层角色尚未全部发布：同一组新密钥继续收尾。
            if not matches_keys(old):
                raise ValueError("root rotation keys differ from the active or pending rotation")
            old.signed.verify_delegate("root", old.signed_bytes, old.signatures)
            root = old
        else:
            pending = metadata_dir / f"{old.signed.version + 1}.root.json"
            if pending.exists():
                root = Metadata.from_file(str(pending))
                if (root.signed.version != old.signed.version + 1 or not matches_keys(root)
                        or root.signed.unrecognized_fields != old.signed.unrecognized_fields
                        or not root.signed.consistent_snapshot):
                    raise ValueError("root rotation keys differ from the pending rotation")
            else:
                root = Metadata(Root(version=old.signed.version + 1, expires=now + timedelta(days=365), consistent_snapshot=True,
                                     unrecognized_fields=dict(old.signed.unrecognized_fields)))
                for role in ROLES:
                    root.signed.add_key(new_signers[role].public_key, role)
                root.sign(old_signer)
                root.sign(new_signers["root"], append=True)
            old.signed.verify_delegate("root", root.signed_bytes, root.signatures)
            root.signed.verify_delegate("root", root.signed_bytes, root.signatures)
            raw = root.to_bytes(_SERIALIZER)
            _write_bytes(pending, raw, immutable=True)
            _write_bytes(metadata_dir / "root.json", raw)
        current = Metadata.from_file(str(metadata_dir / "targets.json"))
        current.signed.version += 1
        current.signed.expires = now + timedelta(days=90)
        _publish_metadata(repository, current, new_signers, now=now)
        return root.to_dict()
