"""在服务器本地将已核验 user 完整候选交付为不可变 HTTPS 下载文件。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from urllib.parse import urlsplit
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from satellite_debug_tool.edition import APP_ID, create_build_identity
from satellite_debug_tool.package_names import APPLICATION_EXECUTABLE, release_archive_name
from satellite_debug_tool.versioning import parse_version

DOWNLOAD_ROOT = Path("/srv/satellite-updates/public/downloads")
INCOMING_ROOT = Path("/srv/satellite-updates/incoming")
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,180}\Z")


def _component(value: str) -> str:
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value) or value in (".", ".."):
        raise ValueError("delivery artifact requires a safe basename")
    return value


def _directory(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("delivery directories require absolute paths")
    for current in reversed((path, *path.parents)):
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise ValueError("delivery parents require existing real directories")
    return path


def _open_file(path: Path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("delivery input requires a regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("delivery input requires a regular file")
    return os.fdopen(fd, "rb")


def _digest(path: Path) -> tuple[int, str]:
    digest, count = hashlib.sha256(), 0
    with _open_file(path) as source:
        while block := source.read(1024**2):
            count += len(block)
            digest.update(block)
    return count, digest.hexdigest()


def _member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute() or ".." in path.parts
            or any(":" in part for part in path.parts)):
        raise ValueError("delivery archive contains an unsafe path")
    return path


def verify_release_archive(archive: Path, identity: dict) -> None:
    """验证完整发行归档的 CRC、唯一身份和 edition 裁剪边界。"""
    if identity["platform"] == "mac":
        with zipfile.ZipFile(archive) as package:
            entries = package.infolist()
            names = [_member(item.filename).as_posix() for item in entries]
            if len(set(names)) != len(names):
                raise ValueError("delivery archive contains duplicate paths")
            if (identity["edition"] == "user"
                    and any(PurePosixPath(name).name in ("updater", "updater.exe", "release.config.json")
                            for name in names)):
                raise ValueError("user archive contains application update files")
            suffix = "/Contents/Resources/satellite_debug_tool/build_identity.json"
            matches = [item for item in entries if item.filename.endswith(suffix)]
            if len(matches) != 1 or matches[0].file_size > 64 * 1024 or stat.S_ISLNK(matches[0].external_attr >> 16):
                raise ValueError("Mac delivery requires a unique regular build identity")
            app = matches[0].filename[:-len(suffix)]
            if not app.endswith(".app") or app + "/Contents/MacOS/" + APPLICATION_EXECUTABLE not in names:
                raise ValueError("Mac delivery requires a complete application")
            if package.testzip() is not None:
                raise ValueError("delivery ZIP CRC verification failed")
            actual = json.loads(package.read(matches[0]))
    else:
        import py7zr
        from scripts.verify_update_archive import verify_archive
        with py7zr.SevenZipFile(archive) as package:
            entries = package.list()
            names = [_member(item.filename).as_posix() for item in entries]
            if len(set(names)) != len(names) or any(item.is_symlink for item in entries):
                raise ValueError("Windows delivery requires unique regular archive entries")
            if package.testzip() is not None:
                raise ValueError("delivery 7z CRC verification failed")
        root = verify_archive(archive, edition=identity["edition"], version=identity["version"])
        member = (root / "_internal/satellite_debug_tool/build_identity.json").as_posix()
        match = [item for item in entries if item.filename == member]
        if len(match) != 1 or match[0].uncompressed > 64 * 1024:
            raise ValueError("Windows delivery requires a bounded build identity")
        with tempfile.TemporaryDirectory(prefix="manual-delivery-identity-") as directory:
            with py7zr.SevenZipFile(archive) as package:
                package.extract(path=directory, targets=[member])
            actual = json.loads((Path(directory) / member).read_text(encoding="utf-8"))
    if actual != identity:
        raise ValueError("archive build identity differs from its candidate receipt")


def inspect_manual_delivery_candidate(candidate: Path, *, expected_commit: str) -> dict:
    """只读核验 user 候选，返回后续服务器本地交付所需的权威事实。"""
    candidate = _directory(candidate)
    receipt_path = candidate / "receipt.json"
    with _open_file(receipt_path) as stream:
        raw = stream.read(1024**2 + 1)
    if len(raw) > 1024**2:
        raise ValueError("candidate receipt exceeds its bound")
    receipt = json.loads(raw)
    if not isinstance(receipt, dict) or receipt.get("schema") != "satellite.release-candidate/v1":
        raise ValueError("manual delivery requires a release candidate receipt")
    identity = receipt.get("identity")
    if (not isinstance(identity, dict) or any(not isinstance(identity.get(key), str) for key in
            ("app_id", "edition", "version", "platform", "architecture", "build_id"))):
        raise ValueError("candidate requires a complete identity")
    canonical = create_build_identity(**{key: identity.get(key) for key in
        ("edition", "version", "platform", "architecture", "build_id")})
    if (identity != canonical or identity["app_id"] != APP_ID or identity["edition"] != "user"
            or identity["build_id"] != expected_commit or receipt.get("source_commit") != expected_commit):
        raise ValueError("manual delivery requires the expected user source identity")
    version = _component(identity["version"])
    parse_version(version, complete=True)
    if version.startswith(("v", "V")):
        raise ValueError("candidate version requires canonical SemVer without a prefix")
    rows = receipt.get("artifacts")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
        raise ValueError("candidate requires bounded complete artifacts")
    names, archives = set(), []
    for row in rows:
        name = _component(row["name"])
        size, digest = row.get("size"), row.get("sha256")
        if (name in names or name == "receipt.json" or type(size) is not int or not 0 < size <= 2 * 1024**3
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise ValueError("candidate artifact metadata is invalid")
        names.add(name)
        if _digest(candidate / name) != (size, digest):
            raise ValueError("candidate artifact differs from its receipt")
        if name.endswith((".7z", ".zip")) and name != "delta.zip":
            archives.append(row)
    extension = "7z" if identity["platform"] == "win" else "zip"
    expected_name = release_archive_name(edition="user", platform=identity["platform"],
        architecture=identity["architecture"], version=version, extension=extension)
    if len(archives) != 1 or archives[0]["name"] != expected_name:
        raise ValueError("candidate requires exactly one named complete platform archive")
    row = archives[0]
    source = candidate / row["name"]
    verify_release_archive(source, identity)
    return {"identity": identity, "source": source, "artifact": row,
            "receipt_sha256": hashlib.sha256(raw).hexdigest()}


def publish_manual_delivery(candidate: Path, *, expected_commit: str, output_root: Path,
                            public_base_url: str) -> dict:
    """校验服务器候选后本地复制；所有权根预建，只生成版本子目录和完整包。"""
    candidate, output_root = _directory(candidate), _directory(output_root)
    parsed = urlsplit(public_base_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not public_base_url.endswith("/")):
        raise ValueError("manual delivery requires an absolute HTTPS base ending in slash")
    inspected = inspect_manual_delivery_candidate(candidate, expected_commit=expected_commit)
    identity, source, row = inspected["identity"], inspected["source"], inspected["artifact"]
    version = identity["version"]
    directory = output_root / ("v" + version)
    try:
        directory.mkdir(mode=0o755)
    except FileExistsError:
        pass
    _directory(directory)
    os.chmod(directory, 0o755)
    target = directory / row["name"]
    expected = (row["size"], row["sha256"])
    temporary = directory / (".delivery-" + uuid.uuid4().hex)
    try:
        if target.exists() or target.is_symlink():
            if _digest(target) != expected:
                raise ValueError("immutable delivery already has different bytes")
            state = "reused"
        else:
            with _open_file(source) as src, temporary.open("xb") as dst:
                while block := src.read(1024**2):
                    dst.write(block)
                dst.flush()
                os.fsync(dst.fileno())
            if _digest(temporary) != expected:
                raise ValueError("delivery copy differs from the verified candidate")
            os.chmod(temporary, 0o644)
            try:
                os.link(temporary, target)
                state = "published"
            except FileExistsError:
                if _digest(target) != expected:
                    raise ValueError("immutable delivery already has different bytes")
                state = "reused"
        if os.name == "nt":
            os.chmod(target, 0o644)
        else:
            os.chmod(target, 0o644, follow_symlinks=False)
        if _digest(target) != expected:
            raise ValueError("final delivery differs from the verified candidate")
    finally:
        temporary.unlink(missing_ok=True)
    return {"schema": "satellite.manual-delivery/v1", "state": state, "identity": identity,
            "url": public_base_url + directory.name + "/" + row["name"],
            "path": str(target), "size": row["size"], "sha256": row["sha256"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--output-root", type=Path, default=DOWNLOAD_ROOT)
    parser.add_argument("--public-base-url", required=True)
    args = parser.parse_args()
    if args.output_root != DOWNLOAD_ROOT or INCOMING_ROOT not in args.candidate.parents:
        parser.error("server delivery requires the fixed incoming and public downloads roots")
    print(json.dumps(publish_manual_delivery(args.candidate, expected_commit=args.expected_commit,
        output_root=args.output_root, public_base_url=args.public_base_url), ensure_ascii=False))


if __name__ == "__main__":
    main()
