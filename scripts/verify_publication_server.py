"""在 ECS 本机从已封口候选晋升公开文件，并生成完整校验回执。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from satellite_debug_tool.updater.payload import canonical_bytes
from satellite_debug_tool.updater.repository import safe_component, target_path

REQUEST_SCHEMA = "satellite.publication-stage-request/v2"
RESULT_SCHEMA = "satellite.publication-stage-result/v2"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_FILES = 4096
MAX_FILE_BYTES = 2 * 1024**3
HASH = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[0-9a-f]{64}")


def _sha256_file(path: Path) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("publication source requires a regular file")
        while block := stream.read(1024 * 1024):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def _target_file(root: Path, relative: str, *, require_exists: bool = True) -> Path:
    """Resolve one publication path without following a symlink component."""
    pure = PurePosixPath(target_path(relative))
    if not str(pure).startswith(("metadata/", "targets/")) or pure.name in ("root.json", "timestamp.json"):
        raise ValueError("verification path is outside immutable publication files")
    current = root
    for index, part in enumerate(pure.parts):
        current /= part
        if index + 1 == len(pure.parts) and not current.exists():
            if require_exists:
                raise FileNotFoundError(current)
            return current
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("verification path contains a symbolic link")
        if index + 1 < len(pure.parts):
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("verification path parent is not a directory")
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError("verification target is not a regular file")
    return current


def _candidate_directory(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if not relative or pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise ValueError("candidate source must be relative to the incoming root")
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("incoming root must be a real directory")
    current = root
    for part in pure.parts:
        safe_component(part)
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("candidate source requires real directories")
    return current


def _candidate_artifact(incoming_root: Path, app_id: str, source: Mapping[str, Any],
                        expected: tuple[int, str]) -> Path:
    candidate = _candidate_directory(incoming_root, source["candidate"])
    receipt = candidate / "receipt.json"
    info = receipt.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 1024**2:
        raise ValueError("candidate receipt requires a bounded regular file")
    raw = receipt.read_bytes()
    if hashlib.sha256(raw).hexdigest() != source["receipt_sha256"]:
        raise ValueError("candidate receipt differs from the stage request")
    document = json.loads(raw)
    identity = document.get("identity", {})
    if (document.get("schema") != "satellite.release-candidate/v1"
            or identity.get("app_id") != app_id or identity.get("edition") != "ops"
            or identity.get("build_id") != document.get("source_commit")):
        raise ValueError("candidate identity differs from the stage request")
    name = safe_component(source["artifact"])
    rows = document.get("artifacts")
    if not isinstance(rows, list):
        raise ValueError("candidate receipt artifacts are invalid")
    matches = [row for row in rows if isinstance(row, dict) and row.get("name") == name]
    if len(matches) != 1 or (matches[0].get("size"), matches[0].get("sha256")) != expected:
        raise ValueError("candidate artifact differs from the publication file")
    artifact = candidate / name
    info = artifact.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("candidate artifact is not a regular file")
    if _sha256_file(artifact) != expected:
        raise ValueError("candidate artifact bytes differ from its receipt")
    return artifact


def _copy_immutable(source: Path, destination: Path, expected: tuple[int, str]) -> None:
    """Copy into a distinct inode and create the immutable name without replacement."""
    if destination.exists():
        if _sha256_file(destination) != expected:
            raise ValueError("immutable publication target has different bytes")
        return
    with tempfile.NamedTemporaryFile(prefix=".publication-stage-", dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as input_stream:
            shutil.copyfileobj(input_stream, output, 1024**2)
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o644)
    try:
        if _sha256_file(temporary) != expected:
            raise ValueError("staged publication target differs from its candidate")
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if _sha256_file(destination) != expected:
                raise ValueError("immutable publication target has different bytes")
    finally:
        temporary.unlink(missing_ok=True)


def load_request(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("verification request exceeds its size bound")
    request = json.loads(raw)
    if request.get("schema") != REQUEST_SCHEMA or raw != canonical_bytes(request):
        raise ValueError("unsupported verification request")
    if not IDENTIFIER.fullmatch(str(request.get("request_id", ""))):
        raise ValueError("verification request id is invalid")
    if not IDENTIFIER.fullmatch(str(request.get("bundle_id", ""))):
        raise ValueError("verification bundle id is invalid")
    safe_component(request.get("app_id", ""))
    files = request.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("verification request file count is invalid")
    names: set[str] = set()
    total = 0
    for row in files:
        if not isinstance(row, dict):
            raise ValueError("verification file row is invalid")
        name = target_path(row.get("path", ""))
        size, digest = row.get("size"), row.get("sha256", "")
        source = row.get("source")
        if source is not None and (not isinstance(source, dict)
                or set(source) != {"candidate", "receipt_sha256", "artifact"}
                or not isinstance(source.get("candidate"), str)
                or not HASH.fullmatch(str(source.get("receipt_sha256", "")))
                or not isinstance(source.get("artifact"), str)):
            raise ValueError("verification candidate source is invalid")
        if (name in names or not name.startswith(("metadata/", "targets/"))
                or PurePosixPath(name).name in ("root.json", "timestamp.json")
                or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES
                or not HASH.fullmatch(str(digest))):
            raise ValueError("verification file identity is invalid")
        names.add(name)
        total += size
    if request.get("file_count") != len(files) or request.get("total_bytes") != total:
        raise ValueError("verification request totals differ from its files")
    return request


def verify_request(raw: bytes, public_apps_root: Path,
                   incoming_root: Path | None = None) -> dict[str, Any]:
    request = load_request(raw)
    root = Path(public_apps_root) / safe_component(request["app_id"])
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("publication application root is not a real directory")
    for row in request["files"]:
        expected = (row["size"], row["sha256"])
        destination = _target_file(root, row["path"], require_exists=False)
        if row.get("source") is not None:
            if incoming_root is None:
                raise ValueError("candidate staging requires the incoming root")
            source = _candidate_artifact(Path(incoming_root), request["app_id"], row["source"], expected)
            _copy_immutable(source, destination, expected)
        actual = _sha256_file(_target_file(root, row["path"]))
        if actual != expected:
            raise ValueError(f"publication file differs from request: {row['path']}")
    return {
        "schema": RESULT_SCHEMA,
        "state": "verified",
        "request_id": request["request_id"],
        "request_sha256": hashlib.sha256(raw).hexdigest(),
        "bundle_id": request["bundle_id"],
        "app_id": request["app_id"],
        "file_count": request["file_count"],
        "total_bytes": request["total_bytes"],
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


def failure_result(raw: bytes, request_name: str, error: Exception) -> dict[str, Any]:
    try:
        request = json.loads(raw)
    except Exception:
        request = {}
    return {
        "schema": RESULT_SCHEMA,
        "state": "failed",
        "request_id": request.get("request_id", request_name),
        "request_sha256": hashlib.sha256(raw).hexdigest(),
        "bundle_id": request.get("bundle_id", ""),
        "app_id": request.get("app_id", ""),
        "error": str(error),
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_result(path: Path, value: Mapping[str, Any]) -> None:
    raw = canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".verification-", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o640)
    temporary.replace(path)


def _archive_request(request_path: Path, archive: Path | None) -> None:
    if archive is None:
        return
    archive.mkdir(parents=True, exist_ok=True)
    destination = archive / request_path.name
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != request_path.read_bytes():
            raise ValueError("verification archive contains a different request")
        request_path.unlink()
        return
    raw = request_path.read_bytes()
    with tempfile.NamedTemporaryFile(prefix=".request-archive-", dir=archive, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o640)
    temporary.replace(destination)
    request_path.unlink()


def process_requests(requests: Path, results: Path, public_apps_root: Path,
                     archive: Path | None = None,
                     incoming_root: Path | None = None) -> list[dict[str, Any]]:
    requests, results = Path(requests), Path(results)
    outcomes = []
    for request_path in sorted(requests.glob("*.json")):
        try:
            info = request_path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REQUEST_BYTES:
                raise ValueError("verification request is not a bounded regular file")
            raw = request_path.read_bytes()
            request = load_request(raw)
            if request_path.stem != request["request_id"]:
                raise ValueError("verification request filename differs from its id")
            result_path = results / (request["request_id"] + ".json")
            if result_path.exists():
                previous = json.loads(result_path.read_bytes())
                if (previous.get("state") == "verified"
                        and previous.get("request_sha256") == hashlib.sha256(raw).hexdigest()):
                    outcomes.append(previous)
                    _archive_request(request_path, Path(archive) if archive is not None else None)
                    continue
            outcome = verify_request(raw, public_apps_root, incoming_root)
            _write_result(result_path, outcome)
        except Exception as exc:
            raw = request_path.read_bytes() if request_path.is_file() else b""
            outcome = failure_result(raw, request_path.stem, exc)
            _write_result(results / (request_path.stem + ".json"), outcome)
        outcomes.append(outcome)
        _archive_request(request_path, Path(archive) if archive is not None else None)
    return outcomes


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--public-apps-root", required=True, type=Path)
    parser.add_argument("--incoming-root", required=True, type=Path)
    args = parser.parse_args(argv)
    outcomes = process_requests(args.requests, args.results, args.public_apps_root, args.archive,
                                args.incoming_root)
    print(json.dumps(outcomes, ensure_ascii=False))
    if any(row.get("state") != "verified" for row in outcomes):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
