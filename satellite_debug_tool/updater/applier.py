"""Applier — 准备 7z → 解压 → 原子替换安装目录（含 .bak rollback）。

流程：
    1. merge_volumes(volumes, output) ：复制单个 7z，或合并旧版分卷
    2. verify_7z_magic(path)         ：头 6 字节 = 377abcaf271c，否则视为合并坏掉
    3. extract_to(archive, staging)  ：py7zr 解压到 staging dir
    4. resolve_payload_root(staging, contract)：递归解析唯一完整应用载荷
    5. swap_install_dir(install, payload_root) ：
         - 把 install_dir 改名 .bak
         - 把已验证的 payload_root 移到 install 位置
         - 成功后保留 .bak（用户可手动验证后清掉）；失败把 .bak 改回 install_dir
"""
from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from satellite_debug_tool.updater.errors import UpdaterError


_7Z_MAGIC = b"\x37\x7a\xbc\xaf\x27\x1c"   # 7-Zip 文件头 6 字节


class ApplyError(UpdaterError):
    """应用更新失败（合并/校验/解压/替换/rollback 的统一包装）。"""


ProgressCallback = Callable[[str, float], None]    # (stage_text, percent_0_to_1)


@dataclass
class ApplyResult:
    """成功完成后返回，含新装路径 + 旧版备份路径。"""
    install_dir: Path
    backup_dir: Path


@dataclass(frozen=True)
class PayloadContract:
    """Files that identify exactly one installable application root."""

    executable_name: str
    updater_name: str
    runtime_dir_name: str = "_internal"
    edition: str | None = None
    target_version: str = ""


class Applier:
    """三步：merge → extract → swap。每步可独立调用，也可一站式 `apply()`。"""

    def __init__(
        self,
        on_progress: Optional[ProgressCallback] = None,
        *,
        heartbeat_interval: float = 5.0,
    ):
        self._on_progress = on_progress
        self._heartbeat_interval = max(0.01, float(heartbeat_interval))

    # ---- 阶段 1：准备归档 ----

    def merge_volumes(self, volumes: List[Path], output: Path,
                      chunk_size: int = 1024 * 1024) -> Path:
        """把一个或多个包文件写为单个 7z；旧分卷已按 001/002/... 排序。"""
        if not volumes:
            raise ApplyError("apply_no_volumes")
        self._progress("merge", 0.0)
        total = sum(v.stat().st_size for v in volumes if v.exists())
        if total == 0:
            raise ApplyError("apply_empty_volumes")
        written = 0
        try:
            with open(output, "wb") as out_f:
                for vol in volumes:
                    if not vol.exists():
                        raise ApplyError(
                            "apply_volume_missing",
                            path=str(vol),
                        )
                    with open(vol, "rb") as in_f:
                        while True:
                            chunk = in_f.read(chunk_size)
                            if not chunk:
                                break
                            out_f.write(chunk)
                            written += len(chunk)
                            self._progress("merge", min(0.99, written / total))
        except OSError as e:
            raise ApplyError("apply_merge_failed", str(e)) from e
        self._progress("merge", 1.0)
        return output

    @staticmethod
    def verify_7z_magic(path: Path) -> bool:
        """检测合并出来的文件确实是 7z 格式（防分卷顺序错乱）。"""
        try:
            with open(path, "rb") as f:
                head = f.read(6)
            return head == _7Z_MAGIC
        except OSError:
            return False

    # ---- 阶段 2：解压 ----

    def extract_to(self, archive: Path, staging: Path) -> Path:
        """py7zr 解压到 staging 目录（必须预先 mkdir）。返回 staging。"""
        if not archive.exists():
            raise ApplyError("apply_archive_missing", path=str(archive))
        if not self.verify_7z_magic(archive):
            raise ApplyError("apply_invalid_archive", path=str(archive))
        staging.mkdir(parents=True, exist_ok=True)
        self._progress("extract", 0.0)
        finished = threading.Event()
        started = time.monotonic()

        def report_heartbeat() -> None:
            while not finished.wait(self._heartbeat_interval):
                self._progress("extracting", time.monotonic() - started)

        heartbeat = threading.Thread(
            target=report_heartbeat,
            name="UpdaterExtractHeartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            import py7zr   # 延迟 import：updater 独立可执行不强依赖 py7zr 的子组件
            with py7zr.SevenZipFile(str(archive), mode="r") as z:
                z.extractall(path=str(staging))
        except Exception as e:
            raise ApplyError("apply_extract_failed", str(e)) from e
        finally:
            finished.set()
            heartbeat.join(timeout=self._heartbeat_interval + 0.5)
        self._progress("extract", 1.0)
        return staging

    def resolve_payload_root(
        self,
        staging: Path,
        contract: PayloadContract,
    ) -> Path:
        """Resolve exactly one complete application payload before old files move."""

        self._progress("validate", 0.0)
        candidates: list[Path] = []
        for executable in staging.rglob(contract.executable_name):
            root = executable.parent
            if not (root / contract.updater_name).is_file():
                continue
            if not (root / contract.runtime_dir_name).is_dir():
                continue
            candidates.append(root.resolve())
        unique = tuple(dict.fromkeys(candidates))
        if not unique:
            raise ApplyError(
                "apply_payload_missing",
                path=str(staging),
                executable=contract.executable_name,
            )
        if len(unique) != 1:
            raise ApplyError(
                "apply_payload_ambiguous",
                candidates=[str(path) for path in unique],
            )
        if contract.edition is not None:
            from satellite_debug_tool.edition import Edition, validate_payload_identity
            try:
                validate_payload_identity(unique[0], Edition(contract.edition), contract.target_version)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ApplyError("apply_payload_identity", str(exc)) from exc
        self._progress("validate", 1.0)
        return unique[0]

    # ---- 阶段 3：原子替换 + rollback ----

    def swap_install_dir(self, install_dir: Path, payload_root: Path,
                         backup_suffix: str = ".bak") -> ApplyResult:
        """把已验证的 payload_root 移到 install_dir，失败时回滚旧版本。

        Args:
            install_dir: 当前要被替换的安装目录（.app 或 onedir）
            payload_root: `resolve_payload_root()` 返回的唯一应用目录
            backup_suffix: 旧版备份后缀，默认 ".bak"
        """
        source = Path(payload_root)
        if not source.is_dir():
            raise ApplyError("apply_source_missing", path=str(payload_root))

        # 备份路径：与 install_dir 同级 + 时间戳，避免与历史 .bak 冲突
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup_dir = install_dir.parent / f"{install_dir.name}{backup_suffix}.{ts}"

        self._progress("swap", 0.0)
        # 步骤 a：旧 → backup
        if install_dir.exists():
            try:
                install_dir.rename(backup_dir)
            except OSError as e:
                raise ApplyError("apply_backup_failed", str(e)) from e
        # 步骤 b：source → install_dir
        try:
            # 跨设备 / 跨文件系统时 rename 会失败，用 shutil.move 兜底
            shutil.move(str(source), str(install_dir))
        except (OSError, shutil.Error) as e:
            # rollback：把 backup 改回 install_dir
            if backup_dir.exists() and not install_dir.exists():
                try:
                    backup_dir.rename(install_dir)
                except OSError:
                    pass
            raise ApplyError("apply_swap_failed_rollback_attempted", str(e)) from e

        self._progress("swap", 1.0)
        return ApplyResult(install_dir=install_dir, backup_dir=backup_dir)

    # ---- 一站式 ----

    def apply(
        self,
        volumes: List[Path],
        install_dir: Path,
        workdir: Path,
        *,
        payload_contract: PayloadContract,
    ) -> ApplyResult:
        """一站式：merge → verify → extract → swap。

        workdir 用于放合并后的 7z 和 staging 解压目录；调用方负责事后清理。
        """
        if payload_contract.edition is not None:
            import json
            from satellite_debug_tool.edition import Edition
            identity = install_dir / "_internal" / "satellite_debug_tool" / "build_identity.json"
            try:
                installed = Edition(json.loads(identity.read_text())["edition"]) if identity.exists() else Edition.OPS
                if installed is not Edition(payload_contract.edition):
                    raise ValueError("installed edition differs from updater request")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ApplyError("apply_payload_identity", str(exc)) from exc
        workdir.mkdir(parents=True, exist_ok=True)
        merged = workdir / "_merged.7z"
        staging = workdir / "_staging"
        self.merge_volumes(volumes, merged)
        self.extract_to(merged, staging)
        payload_root = self.resolve_payload_root(staging, payload_contract)
        return self.swap_install_dir(install_dir, payload_root)

    # ---- 内部 ----

    def _progress(self, stage: str, pct: float) -> None:
        if self._on_progress:
            self._on_progress(stage, pct)
