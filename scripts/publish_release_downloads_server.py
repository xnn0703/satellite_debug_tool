"""在 ECS 本机把两份 user 候选和两份正式 ops target 原子发布为完整下载目录。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Mapping

from tuf.api.metadata import Metadata

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from scripts.publish_manual_delivery import inspect_manual_delivery_candidate, verify_release_archive
from satellite_debug_tool.edition import APP_ID
from satellite_debug_tool.package_names import release_archive_name
from satellite_debug_tool.updater.payload import canonical_bytes
from satellite_debug_tool.updater.repository import target_path
from satellite_debug_tool.versioning import parse_version


REQUEST_SCHEMA = "satellite.release-download-request/v1"
RESULT_SCHEMA = "satellite.release-download-result/v1"
INDEX_SCHEMA = "satellite.release-download-index/v1"
MAX_REQUEST_BYTES = 256 * 1024
HASH64 = re.compile(r"[0-9a-f]{64}")
COMMIT40 = re.compile(r"[0-9a-f]{40}")
EXPECTED_USERS = {"windows_x86_64": ("win", "x86_64"), "macos_arm64": ("mac", "arm64")}
EXPECTED_OPS = {
    "windows_x86_64": "catalog/ops/stable/windows/x86_64.json",
    "macos_arm64": "catalog/ops/stable/macos/arm64.json",
}


def _digest(path: Path) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("release source requires a regular file")
        while block := stream.read(1024**2):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def _real_directory(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("release directory requires an absolute path")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("release directory requires a real directory")
    return path


def _regular_beneath(root: Path, relative: PurePosixPath) -> Path:
    current = _real_directory(root)
    for index, part in enumerate(relative.parts):
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("release source path contains a symbolic link")
        if index + 1 < len(relative.parts):
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("release source parent is not a directory")
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError("release source is not a regular file")
    return current


def _relative_directory(root: Path, value: str) -> Path:
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts or "\\" in value:
        raise ValueError("candidate path must be relative to the incoming root")
    current = _real_directory(root)
    for part in pure.parts:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("candidate path requires real directories")
    return current


def load_request(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("download request exceeds its size bound")
    request = json.loads(raw)
    if request.get("schema") != REQUEST_SCHEMA or raw != canonical_bytes(request):
        raise ValueError("unsupported download request")
    if not HASH64.fullmatch(str(request.get("request_id", ""))):
        raise ValueError("download request id is invalid")
    bound = dict(request)
    request_id = bound.pop("request_id")
    if request_id != hashlib.sha256(canonical_bytes(bound)).hexdigest():
        raise ValueError("download request id differs from its content")
    version = str(request.get("version", ""))
    parse_version(version, complete=True)
    if version.startswith(("v", "V")) or not COMMIT40.fullmatch(str(request.get("source_commit", ""))):
        raise ValueError("download release identity is invalid")
    if request.get("app_id") != APP_ID:
        raise ValueError("download request app identity differs")
    base = str(request.get("public_base_url", ""))
    if not re.fullmatch(r"https://[^/?#]+/downloads/", base):
        raise ValueError("download request requires the fixed HTTPS downloads base")
    users, ops = request.get("users"), request.get("ops")
    if not isinstance(users, dict) or set(users) != set(EXPECTED_USERS):
        raise ValueError("download request requires both user platforms")
    if not isinstance(ops, dict) or set(ops) != set(EXPECTED_OPS):
        raise ValueError("download request requires both ops platforms")
    for key, row in users.items():
        if (not isinstance(row, dict) or not isinstance(row.get("candidate"), str)
                or not HASH64.fullmatch(str(row.get("receipt_sha256", "")))):
            raise ValueError(f"user download request is invalid: {key}")
    for key, row in ops.items():
        if not isinstance(row, dict) or row.get("catalog") != EXPECTED_OPS[key]:
            raise ValueError(f"ops download request is invalid: {key}")
    return request


def _published_target(repository: Path, targets: Metadata, logical: str) -> tuple[Path, Any]:
    logical = target_path(logical)
    info = targets.signed.targets.get(logical)
    if info is None:
        raise ValueError(f"trusted target is missing: {logical}")
    pure = PurePosixPath(logical)
    stored = _regular_beneath(repository, PurePosixPath("targets") /
        pure.with_name(info.hashes["sha256"] + "." + pure.name))
    with stored.open("rb") as stream:
        info.verify_length_and_hashes(stream)
    return stored, info


def _ops_artifact(repository: Path, catalog_logical: str, *, version: str,
                  commit: str, platform: str, architecture: str) -> dict:
    root_path = _regular_beneath(repository, PurePosixPath("metadata/root.json"))
    targets_path = _regular_beneath(repository, PurePosixPath("metadata/targets.json"))
    root = Metadata.from_file(str(root_path))
    targets = Metadata.from_file(str(targets_path))
    root.signed.verify_delegate("targets", targets.signed_bytes, targets.signatures)
    catalog_path, catalog_info = _published_target(repository, targets, catalog_logical)
    if catalog_info.length > 1024**2:
        raise ValueError("release catalog exceeds its local bound")
    catalog = json.loads(catalog_path.read_bytes())
    identity = catalog.get("identity", {})
    expected = {"app_id": APP_ID, "edition": "ops", "version": version,
                "platform": platform, "architecture": architecture, "build_id": commit}
    if any(identity.get(key) != value for key, value in expected.items()):
        raise ValueError("ops catalog identity differs from the download request")
    if catalog.get("schema") != "satellite.update-catalog/v1" or catalog.get("channel") != "stable":
        raise ValueError("ops download requires a stable signed catalog")
    package_path, info = _published_target(repository, targets, catalog.get("package", ""))
    archive_identity = {**identity, "platform": {"windows": "win", "macos": "mac"}[platform]}
    verify_release_archive(package_path, archive_identity)
    extension = "7z" if platform == "windows" else "zip"
    name = release_archive_name(edition="ops", platform=platform, architecture=architecture,
                                version=version, extension=extension)
    return {"identity": identity, "source": package_path,
            "artifact": {"name": name, "size": info.length, "sha256": info.hashes["sha256"]},
            "catalog_sha256": catalog_info.hashes["sha256"]}


def inspect_release(request: Mapping[str, Any], *, incoming_root: Path,
                    repository: Path) -> list[dict[str, Any]]:
    artifacts = []
    for key, (platform, architecture) in EXPECTED_USERS.items():
        row = request["users"][key]
        candidate = _relative_directory(incoming_root, row["candidate"])
        inspected = inspect_manual_delivery_candidate(candidate, expected_commit=request["source_commit"])
        identity = inspected["identity"]
        if (identity["version"] != request["version"] or identity["platform"] != platform
                or identity["architecture"] != architecture):
            raise ValueError("user candidate identity differs from the download request")
        if inspected["receipt_sha256"] != row["receipt_sha256"]:
            raise ValueError("user candidate receipt differs from the download request")
        artifacts.append({**inspected, "edition": "user"})
    for key, catalog in EXPECTED_OPS.items():
        platform = "windows" if key.startswith("windows") else "macos"
        architecture = EXPECTED_USERS[key][1]
        artifacts.append({**_ops_artifact(repository, catalog, version=request["version"],
            commit=request["source_commit"], platform=platform, architecture=architecture), "edition": "ops"})
    if {(row["identity"]["edition"], row["identity"]["platform"], row["identity"]["architecture"])
            for row in artifacts} != {
                ("user", "win", "x86_64"), ("user", "mac", "arm64"),
                ("ops", "windows", "x86_64"), ("ops", "macos", "arm64")}:
        raise ValueError("release artifacts do not form the required four-package set")
    return artifacts


def _copy_immutable(source: Path, target: Path, expected: tuple[int, str]) -> None:
    linked = False
    # 公开目录不能借由 hard link + chmod 改写 incoming/TUF 源 inode 的权限。
    # 已经公开可读的正式 target 可直接链接；窄权限候选复制为独立公开文件。
    if source.stat().st_mode & stat.S_IROTH:
        try:
            os.link(source, target)
            linked = True
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
    if not linked:
        with source.open("rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst, 1024**2)
            dst.flush()
            os.fsync(dst.fileno())
        target.chmod(0o644)
    if _digest(target) != expected:
        raise ValueError("published download differs from its verified source")


def _expected_files(request: Mapping[str, Any], artifacts: list[dict[str, Any]]) -> dict[str, bytes | tuple[Path, tuple[int, str]]]:
    rows = []
    files: dict[str, bytes | tuple[Path, tuple[int, str]]] = {}
    for item in sorted(artifacts, key=lambda row: (row["identity"]["edition"], row["identity"]["platform"])):
        row = item["artifact"]
        files[row["name"]] = (item["source"], (row["size"], row["sha256"]))
        rows.append({"edition": item["identity"]["edition"],
                     "platform": {"win": "windows", "mac": "macos"}.get(
                         item["identity"]["platform"], item["identity"]["platform"]),
                     "architecture": item["identity"]["architecture"], "name": row["name"],
                     "url": request["public_base_url"] + "v" + request["version"] + "/" + row["name"],
                     "size": row["size"], "sha256": row["sha256"]})
    index = {"schema": INDEX_SCHEMA, "version": request["version"],
             "source_commit": request["source_commit"], "artifacts": rows}
    files["release.json"] = canonical_bytes(index)
    files["SHA256SUMS.txt"] = ("".join(f"{row['sha256']}  {row['name']}\n" for row in rows)).encode()
    return files


def _matches(directory: Path, files: Mapping[str, bytes | tuple[Path, tuple[int, str]]]) -> bool:
    if directory.is_symlink() or not directory.is_dir() or {path.name for path in directory.iterdir()} != set(files):
        return False
    for name, value in files.items():
        path = directory / name
        if isinstance(value, bytes):
            if path.is_symlink() or not path.is_file() or path.read_bytes() != value:
                return False
        elif _digest(path) != value[1]:
            return False
    return True


def publish_release(request: Mapping[str, Any], *, incoming_root: Path,
                    repository: Path, downloads_root: Path) -> dict[str, Any]:
    incoming_root, repository, downloads_root = map(_real_directory, (incoming_root, repository, downloads_root))
    artifacts = inspect_release(request, incoming_root=incoming_root, repository=repository)
    files = _expected_files(request, artifacts)
    destination = downloads_root / ("v" + request["version"])
    if destination.exists() or destination.is_symlink():
        if not _matches(destination, files):
            raise ValueError("immutable release download directory has different bytes")
        state = "reused"
    else:
        with tempfile.TemporaryDirectory(prefix=".release-download-", dir=downloads_root) as temporary:
            stage = Path(temporary) / destination.name
            stage.mkdir(mode=0o755)
            for name, value in files.items():
                target = stage / name
                if isinstance(value, bytes):
                    with target.open("xb") as stream:
                        stream.write(value)
                        stream.flush()
                        os.fsync(stream.fileno())
                    target.chmod(0o644)
                else:
                    _copy_immutable(value[0], target, value[1])
            # POSIX 目录 fsync 保证 rename 前目录项已经落盘；Windows 不允许用
            # os.open 打开目录，文件本身已在上方逐一 fsync，由 rename 提供原子可见性。
            if os.name != "nt":
                descriptor = os.open(stage, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            try:
                stage.rename(destination)
            except FileExistsError:
                if not _matches(destination, files):
                    raise ValueError("immutable release download directory has different bytes")
                state = "reused"
            else:
                state = "published"
    index = json.loads((destination / "release.json").read_bytes())
    return {"schema": RESULT_SCHEMA, "state": state, "request_id": request["request_id"],
            "request_sha256": hashlib.sha256(canonical_bytes(request)).hexdigest(),
            "version": request["version"], "source_commit": request["source_commit"],
            "release_index": request["public_base_url"] + destination.name + "/release.json",
            "sha256sums": request["public_base_url"] + destination.name + "/SHA256SUMS.txt",
            "artifacts": index["artifacts"], "published_at": datetime.now(timezone.utc).isoformat()}


def _write_result(path: Path, value: Mapping[str, Any]) -> None:
    _write_bytes(path, canonical_bytes(value), prefix=".download-result-")


def _write_bytes(path: Path, raw: bytes, *, prefix: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=prefix, dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o640)
    temporary.replace(path)


def process_requests(requests: Path, results: Path, archive: Path, *, incoming_root: Path,
                     repository: Path, downloads_root: Path) -> list[dict[str, Any]]:
    outcomes = []
    for path in sorted(Path(requests).glob("*.json")):
        raw = b""
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REQUEST_BYTES:
                raise ValueError("download request is not a bounded regular file")
            raw = path.read_bytes()
            request = load_request(raw)
            if path.stem != request["request_id"]:
                raise ValueError("download request filename differs from its id")
            outcome = publish_release(request, incoming_root=incoming_root,
                                      repository=repository, downloads_root=downloads_root)
        except Exception as error:
            safe_id = path.stem if HASH64.fullmatch(path.stem) else hashlib.sha256(path.name.encode()).hexdigest()
            outcome = {"schema": RESULT_SCHEMA, "state": "failed", "request_id": safe_id,
                       "request_sha256": hashlib.sha256(raw).hexdigest(), "error": str(error),
                       "published_at": datetime.now(timezone.utc).isoformat()}
        _write_result(Path(results) / path.name, outcome)
        Path(archive).mkdir(parents=True, exist_ok=True)
        archived = Path(archive) / (outcome["request_id"] + ".json")
        if archived.exists() and archived.read_bytes() != raw:
            raise ValueError("download request archive contains different bytes")
        if not archived.exists():
            _write_bytes(archived, raw, prefix=".download-request-")
        path.unlink()
        outcomes.append(outcome)
    return outcomes


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--incoming-root", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--downloads-root", type=Path, required=True)
    args = parser.parse_args(argv)
    outcomes = process_requests(args.requests, args.results, args.archive,
        incoming_root=args.incoming_root, repository=args.repository, downloads_root=args.downloads_root)
    print(json.dumps(outcomes, ensure_ascii=False))
    if any(row.get("state") not in ("published", "reused") for row in outcomes):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
