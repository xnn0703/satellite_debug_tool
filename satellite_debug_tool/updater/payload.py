"""可信 Windows 文件树与一跳文件级增量；安装目录由安装事务独占。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from threading import Event
from typing import Any, Mapping
import zipfile

TREE_SCHEMA = "satellite.update-tree/v1"
DELTA_SCHEMA = "satellite.update-delta/v1"
MAX_FILES = 50000
MAX_TREE_BYTES = 16 * 1024**3
MAX_MANIFEST_BYTES = 16 * 1024**2
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class PayloadError(ValueError):
    """文件树或增量合同校验失败。"""


def _cancel(event: Event | None) -> None:
    if event is not None and event.is_set():
        from satellite_debug_tool.updater.downloader import DownloadCancelled
        raise DownloadCancelled()


def sha256_file(path: Path, cancel_event: Event | None = None) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            _cancel(cancel_event)
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    validate_manifest(manifest)
    return hashlib.sha256(canonical_bytes(manifest)).hexdigest()


def safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value or ":" in value:
        raise PayloadError("invalid payload path")
    parts = value.split("/")
    if any(p in {"", ".", ".."} or p.endswith((" ", ".")) or p.split(".", 1)[0].casefold() in _RESERVED for p in parts):
        raise PayloadError(f"invalid payload path: {value}")
    if any(ord(c) < 32 or c in '<>"|?*' for c in value) or PurePosixPath(value).is_absolute():
        raise PayloadError(f"invalid payload path: {value}")
    return value


def _identity(value: Any) -> dict:
    fields = ("app_id", "edition", "platform", "architecture", "version", "build_id")
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k] for k in fields):
        raise PayloadError("payload identity requires app, edition, platform, architecture, version and build")
    if value["edition"] not in {"user", "ops"} or value["platform"] not in {"windows", "macos"}:
        raise PayloadError("file-tree payload requires a supported application platform")
    if value["architecture"] not in {"x86_64", "arm64"}:
        raise PayloadError("unsupported payload architecture")
    return value


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, dict]:
    if not isinstance(manifest, dict) or manifest.get("schema") != TREE_SCHEMA:
        raise PayloadError("unsupported file-tree schema")
    _identity(manifest.get("identity"))
    rows = manifest.get("files")
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_FILES:
        raise PayloadError("invalid file-tree count")
    index: dict[str, dict] = {}
    folded: set[str] = set()
    directories: set[str] = set()
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            raise PayloadError("invalid file-tree entry")
        name = safe_path(row.get("path"))
        size = row.get("size")
        kind = row.get("kind")
        if kind == "file":
            if type(size) is not int or size < 0 or not _HASH.fullmatch(str(row.get("sha256", ""))):
                raise PayloadError(f"invalid file metadata: {name}")
        elif kind == "symlink" and manifest["identity"]["platform"] == "macos":
            target = row.get("target")
            if (not isinstance(target, str) or not target or PurePosixPath(target).is_absolute()
                    or "\x00" in target):
                raise PayloadError(f"invalid symbolic link metadata: {name}")
            resolved = PurePosixPath(name).parent.joinpath(target)
            depth = 0
            for part in resolved.parts:
                depth += -1 if part == ".." else (0 if part == "." else 1)
                if depth < 0:
                    raise PayloadError(f"symbolic link escapes payload: {name}")
            if set(row) != {"path", "kind", "target"}:
                raise PayloadError(f"invalid symbolic link fields: {name}")
        else:
            raise PayloadError(f"invalid file metadata: {name}")
        key = name.casefold()
        if key in folded:
            raise PayloadError(f"duplicate payload path: {name}")
        folded.add(key)
        for parent in PurePosixPath(name).parents:
            if str(parent) != ".":
                directories.add(str(parent).casefold())
        index[name] = row
        if kind == "file":
            total += size
    if folded & directories or total > MAX_TREE_BYTES:
        raise PayloadError("conflicting paths or excessive expanded payload size")
    return index


def read_manifest(path: Path) -> dict:
    if Path(path).stat().st_size > MAX_MANIFEST_BYTES:
        raise PayloadError("file-tree manifest exceeds size limit")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise PayloadError("invalid file-tree JSON") from exc
    validate_manifest(data)
    return data


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    validate_manifest(manifest)
    Path(path).write_bytes(canonical_bytes(manifest))


def _files(root: Path, *, allow_symlinks: bool = False) -> dict[str, Path]:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise PayloadError("payload root must be a directory")
    result: dict[str, Path] = {}
    for p in root.rglob("*"):
        info = p.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            if not allow_symlinks:
                raise PayloadError(f"payload link is unsupported: {p.name}")
            name = safe_path(p.relative_to(root).as_posix())
            target = os.readlink(p)
            resolved = (p.parent / target).resolve()
            if resolved != root.resolve() and root.resolve() not in resolved.parents:
                raise PayloadError(f"payload link escapes root: {name}")
            result[name] = p
            continue
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise PayloadError("payload contains a special file")
        name = safe_path(p.relative_to(root).as_posix())
        result[name] = p
        if len(result) > MAX_FILES:
            raise PayloadError("payload file count exceeds limit")
    return result


def build_manifest(root: Path, identity: Mapping[str, Any], cancel_event: Event | None = None) -> dict:
    identity = dict(identity)
    identity["platform"] = {"win": "windows", "mac": "macos"}.get(
        identity.get("platform"), identity.get("platform"))
    rows = []
    macos = identity.get("platform") == "macos"
    for name, path in sorted(_files(Path(root), allow_symlinks=macos).items()):
        _cancel(cancel_event)
        if path.is_symlink():
            rows.append({"path": name, "kind": "symlink", "target": os.readlink(path)})
        else:
            rows.append({"path": name, "kind": "file", "size": path.stat().st_size, "sha256": sha256_file(path, cancel_event)})
    manifest = {"schema": TREE_SCHEMA, "identity": identity, "files": rows}
    validate_manifest(manifest)
    return manifest


def verify_tree(root: Path, manifest: Mapping[str, Any], cancel_event: Event | None = None) -> None:
    expected = validate_manifest(manifest)
    actual = _files(Path(root), allow_symlinks=manifest["identity"]["platform"] == "macos")
    if set(actual) != set(expected):
        raise PayloadError("payload file set differs from the trusted manifest")
    for name, row in expected.items():
        _cancel(cancel_event)
        p = actual[name]
        if row["kind"] == "symlink":
            if not p.is_symlink() or os.readlink(p) != row["target"]:
                raise PayloadError(f"payload link differs from the trusted manifest: {name}")
        elif p.is_symlink() or p.stat().st_size != row["size"] or sha256_file(p, cancel_event) != row["sha256"]:
            raise PayloadError(f"payload file differs from the trusted manifest: {name}")


def _same_product(base: Mapping, target: Mapping) -> None:
    for key in ("app_id", "edition", "platform", "architecture"):
        if base["identity"][key] != target["identity"][key]:
            raise PayloadError("delta identities differ")
    if target["identity"]["edition"] != "ops":
        raise PayloadError("delta delivery requires the ops edition")


def create_delta(base_root: Path, target_root: Path, archive: Path, base_manifest: Mapping, target_manifest: Mapping) -> dict:
    """发行侧生成增量；输入是两份完整的已验证发行文件树。"""
    base = validate_manifest(base_manifest)
    target = validate_manifest(target_manifest)
    _same_product(base_manifest, target_manifest)
    verify_tree(base_root, base_manifest)
    verify_tree(target_root, target_manifest)
    changed = sorted(name for name, row in target.items() if base.get(name) != row)
    descriptor = {"schema": DELTA_SCHEMA, "base_manifest_sha256": manifest_sha256(base_manifest),
                  "target_manifest_sha256": manifest_sha256(target_manifest), "changed": changed}
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("delta.json", canonical_bytes(descriptor))
        for name in changed:
            zf.write(Path(target_root) / name, "files/" + name)
    return descriptor


def apply_delta(base_root: Path, archive: Path, base_manifest: Mapping, target_manifest: Mapping,
                destination: Path, cancel_event: Event | None = None) -> Path:
    """从原目录复制并验证到新目录；原安装、配置及录制保持完整。"""
    base = validate_manifest(base_manifest)
    target = validate_manifest(target_manifest)
    _same_product(base_manifest, target_manifest)
    verify_tree(base_root, base_manifest, cancel_event)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise PayloadError("delta destination must be a fresh directory")
    if destination.resolve().is_relative_to(Path(base_root).resolve()):
        raise PayloadError("delta destination must be separate from the base installation")
    changed = sorted(name for name, row in target.items() if base.get(name) != row)
    expected_descriptor = {"schema": DELTA_SCHEMA, "base_manifest_sha256": manifest_sha256(base_manifest),
                           "target_manifest_sha256": manifest_sha256(target_manifest), "changed": changed}
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        expected_names = {"delta.json", *("files/" + n for n in changed)}
        if len(names) != len(set(names)) or set(names) != expected_names:
            raise PayloadError("delta archive entries differ from the target change set")
        if any(i.flag_bits & 1 or stat.S_ISLNK(i.external_attr >> 16) for i in infos):
            raise PayloadError("delta requires regular unencrypted files")
        if zf.getinfo("delta.json").file_size > MAX_MANIFEST_BYTES:
            raise PayloadError("delta descriptor exceeds size limit")
        if json.loads(zf.read("delta.json")) != expected_descriptor:
            raise PayloadError("delta baseline or target differs from the trusted manifests")
        for name in changed:
            if zf.getinfo("files/" + name).file_size != target[name]["size"]:
                raise PayloadError("delta expanded length differs from the target manifest")
        destination.mkdir(parents=True)
        try:
            for name, row in target.items():
                _cancel(cancel_event)
                dest = destination / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                if name in changed:
                    with zf.open("files/" + name) as src, dest.open("xb") as dst:
                        total = 0
                        while True:
                            _cancel(cancel_event)
                            block = src.read(1024 * 1024)
                            if not block:
                                break
                            total += len(block)
                            if total > row["size"]:
                                raise PayloadError("delta expanded file exceeds trusted length")
                            dst.write(block)
                else:
                    shutil.copy2(Path(base_root) / name, dest)
            verify_tree(destination, target_manifest, cancel_event)
        except BaseException:
            shutil.rmtree(destination)
            raise
    return destination
