"""在服务器本机验证待发布文件，生成与请求绑定的只读回执。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from satellite_debug_tool.updater.payload import canonical_bytes
from satellite_debug_tool.updater.repository import safe_component, target_path

REQUEST_SCHEMA = "satellite.publication-verification-request/v1"
RESULT_SCHEMA = "satellite.publication-verification-result/v1"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_FILES = 4096
MAX_FILE_BYTES = 2 * 1024**3
HASH = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[0-9a-f]{64}")


def _sha256_file(path: Path) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def _regular_file(root: Path, relative: str) -> Path:
    """Resolve one request path without following a symlink at any component."""
    pure = PurePosixPath(target_path(relative))
    if not str(pure).startswith(("metadata/", "targets/")) or pure.name in ("root.json", "timestamp.json"):
        raise ValueError("verification path is outside immutable publication files")
    current = root
    for index, part in enumerate(pure.parts):
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("verification path contains a symbolic link")
        if index + 1 < len(pure.parts):
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("verification path parent is not a directory")
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError("verification target is not a regular file")
    return current


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


def verify_request(raw: bytes, public_apps_root: Path) -> dict[str, Any]:
    request = load_request(raw)
    root = Path(public_apps_root) / safe_component(request["app_id"])
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("publication application root is not a real directory")
    for row in request["files"]:
        actual = _sha256_file(_regular_file(root, row["path"]))
        if actual != (row["size"], row["sha256"]):
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
                     archive: Path | None = None) -> list[dict[str, Any]]:
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
            outcome = verify_request(raw, public_apps_root)
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
    args = parser.parse_args(argv)
    outcomes = process_requests(args.requests, args.results, args.public_apps_root, args.archive)
    print(json.dumps(outcomes, ensure_ascii=False))
    if any(row.get("state") != "verified" for row in outcomes):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
