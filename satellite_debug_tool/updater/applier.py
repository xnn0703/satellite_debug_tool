"""完整包与增量共用的同盘 staging、载荷验证和 journal 目录切换。"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from satellite_debug_tool.updater.errors import UpdaterError
from satellite_debug_tool.updater.recovery import InstallJournal
from satellite_debug_tool.updater.status import sync_file


_7Z_MAGIC = b"\x37\x7a\xbc\xaf\x27\x1c"
MAX_ARCHIVE_ENTRIES = 50000
MAX_EXPANDED_BYTES = 8 * 1024 ** 3


class ApplyError(UpdaterError):
    """安装输入、staging 或目录切换失败。"""


ProgressCallback = Callable[[str, float], None]


@dataclass(frozen=True)
class ApplyResult:
    install_dir: Path
    backup_dir: Path


@dataclass(frozen=True)
class PayloadContract:
    executable_name: str
    updater_name: str
    runtime_dir_name: str = "_internal"
    edition: str | None = None
    target_version: str = ""


def _archive_path(name: str) -> str:
    normalized = name.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if normalized.startswith("/") or any(part == ".." for part in parts):
        raise ValueError("archive member escapes the payload directory")
    for part in parts:
        if ":" in part or part.endswith((" ", ".")) or "\x00" in part:
            raise ValueError("archive member has an unsupported Windows path")
        stem = part.split(".", 1)[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(COM|LPT)[1-9]", stem):
            raise ValueError("archive member uses a reserved Windows path")
    return "/".join(parts)


class Applier:
    def __init__(self, on_progress: Optional[ProgressCallback] = None, *,
                 heartbeat_interval: float = 5.0):
        self._on_progress = on_progress
        self._heartbeat_interval = max(0.01, float(heartbeat_interval))

    @staticmethod
    def verify_7z_magic(path: Path) -> bool:
        try:
            with Path(path).open("rb") as stream:
                return stream.read(6) == _7Z_MAGIC
        except OSError:
            return False

    def extract_to(self, archive: Path, staging: Path, *,
                   max_expanded_bytes: int = MAX_EXPANDED_BYTES) -> Path:
        """预检路径、类型和展开预算后才提取；Windows 包只含普通文件和目录。"""
        if not archive.is_file():
            raise ApplyError("apply_archive_missing", path=str(archive))
        if not self.verify_7z_magic(archive):
            raise ApplyError("apply_invalid_archive", path=str(archive))
        staging.mkdir(parents=True, exist_ok=False)
        self._progress("extract", 0.0)
        finished = threading.Event()
        started = time.monotonic()

        def heartbeat() -> None:
            while not finished.wait(self._heartbeat_interval):
                self._progress("extracting", time.monotonic() - started)

        worker = threading.Thread(target=heartbeat, name="UpdaterExtractHeartbeat", daemon=True)
        worker.start()
        try:
            import py7zr
            with py7zr.SevenZipFile(str(archive), mode="r") as seven_zip:
                members = seven_zip.list()
                if len(members) > MAX_ARCHIVE_ENTRIES:
                    raise ValueError("archive entry budget exceeded")
                total = 0
                seen = set()
                for member in members:
                    key = _archive_path(member.filename).casefold()
                    if key in seen:
                        raise ValueError("archive has duplicate or case-conflicting paths")
                    seen.add(key)
                    if getattr(member, "is_symlink", False):
                        raise ValueError("Windows update payload contains a symbolic link")
                    if not (member.is_directory or getattr(member, "is_file", False)):
                        raise ValueError("archive member has an unsupported file type")
                    total += int(member.uncompressed or 0)
                    if total > max_expanded_bytes:
                        raise ValueError("archive expanded size budget exceeded")
                if shutil.disk_usage(staging.parent).free < total:
                    raise OSError("insufficient space to extract update payload")
                seven_zip.extractall(path=str(staging))
            # 提取器生成的对象也必须遵守同一 Windows 普通文件合同。
            for entry in staging.rglob("*"):
                if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
                    raise ValueError("extracted payload contains an unsupported file type")
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise ApplyError("apply_extract_failed", str(exc)) from exc
        finally:
            finished.set()
            worker.join(timeout=self._heartbeat_interval + 0.5)
        self._progress("extract", 1.0)
        return staging

    def resolve_payload_root(self, staging: Path, contract: PayloadContract) -> Path:
        self._progress("validate", 0.0)
        candidates = []
        for executable in staging.rglob(contract.executable_name):
            root = executable.parent
            if ((root / contract.updater_name).is_file()
                    and (root / contract.runtime_dir_name).is_dir()):
                candidates.append(root.resolve())
        unique = tuple(dict.fromkeys(candidates))
        if not unique:
            raise ApplyError("apply_payload_missing", path=str(staging),
                             executable=contract.executable_name)
        if len(unique) != 1:
            raise ApplyError("apply_payload_ambiguous", candidates=[str(p) for p in unique])
        self._validate_identity(unique[0], contract)
        self._progress("validate", 1.0)
        return unique[0]

    @staticmethod
    def _validate_identity(root: Path, contract: PayloadContract) -> None:
        if contract.edition is None:
            return
        from satellite_debug_tool.edition import Edition, validate_payload_identity
        try:
            validate_payload_identity(root, Edition(contract.edition), contract.target_version)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ApplyError("apply_payload_identity", str(exc)) from exc

    def prepare_payload(self, install_dir: Path, payload_root: Path, transaction_id: str, *,
                        tree_manifest: dict, contract: PayloadContract) -> Path:
        """跨盘复制在旧安装移动前完成，完整复制后再次核对目标树。"""
        from satellite_debug_tool.updater.payload import verify_tree
        install = Path(install_dir).resolve()
        source = Path(payload_root).resolve()
        if re.fullmatch(r"[a-f0-9]{32}", transaction_id) is None:
            raise ApplyError("apply_transaction_identity")
        staging = install.parent / f".{install.name}.staging.{transaction_id}"
        if source == install or install in source.parents:
            raise ApplyError("apply_source_overlaps_installation")
        if not source.is_dir():
            raise ApplyError("apply_source_missing", path=str(source))
        if contract.edition is not None:
            try:
                identity = install / "_internal" / "satellite_debug_tool" / "build_identity.json"
                installed_edition = json.loads(identity.read_text(encoding="utf-8"))["edition"]
                if installed_edition != contract.edition:
                    raise ValueError("installed edition differs from updater request")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ApplyError("apply_payload_identity", str(exc)) from exc
        if staging.exists():
            raise ApplyError("apply_staging_exists", path=str(staging))
        self._progress("prepare", 0.0)
        try:
            verify_tree(source, tree_manifest)
            identity_file = source / "_internal/satellite_debug_tool/build_identity.json"
            packed_identity = json.loads(identity_file.read_text(encoding="utf-8"))
            packed_identity["platform"] = {"win": "windows"}.get(packed_identity.get("platform"), packed_identity.get("platform"))
            for field, value in tree_manifest["identity"].items():
                if packed_identity.get(field) != value:
                    raise ValueError(f"payload build identity differs from the trusted manifest: {field}")
            total = sum(int(entry["size"]) for entry in tree_manifest["files"])
            if shutil.disk_usage(install.parent).free < total:
                raise OSError("insufficient space to prepare update beside the installation")
            shutil.copytree(source, staging)
            verify_tree(staging, tree_manifest)
            self._validate_identity(staging, contract)
            for entry in tree_manifest["files"]:
                sync_file(staging / entry["path"])
        except Exception as exc:
            if staging.exists():
                shutil.rmtree(staging)
            raise ApplyError("apply_prepare_failed", str(exc)) from exc
        self._progress("prepare", 1.0)
        return staging

    def install_prepared(self, journal: InstallJournal) -> ApplyResult:
        """同盘 rename 是唯一切换操作；日志与恢复入口在此前已经持久化。"""
        if journal.data["state"] != "recovery_ready":
            raise ApplyError("apply_recovery_required")
        install = journal.install_dir
        staging = journal.staging_dir
        backup = journal.backup_dir
        if not staging.is_dir() or staging.stat().st_dev != install.parent.stat().st_dev:
            raise ApplyError("apply_same_filesystem_required")
        if backup.exists():
            raise ApplyError("apply_backup_exists")
        self._progress("swap", 0.0)
        try:
            journal.save("backup_intent")
            if install.exists():
                install.rename(backup)
            journal.save("old_backed_up")
            journal.save("switch_intent")
            staging.rename(install)
            journal.save("switched")
        except Exception as exc:
            try:
                journal.rollback()
            except Exception as recovery:
                raise ApplyError("apply_recovery_failed", str(recovery), original=str(exc),
                                 journal=str(journal.path)) from exc
            raise ApplyError("apply_swap_failed_restored", str(exc)) from exc
        self._progress("swap", 1.0)
        return ApplyResult(install, backup)

    def _progress(self, stage: str, value: float) -> None:
        if self._on_progress is not None:
            self._on_progress(stage, value)
