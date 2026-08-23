"""updater 独立可执行入口（M11 P6）。

由主程序触发：主程序下载完发布资产后，调 `subprocess.Popen` 把本可执行启动起来，
然后退出自己。本可执行：
  1. 等主进程退出（psutil.wait_for_pid，无则盲等 3s）
  2. self-relocate 到 OS 临时目录（避免被替换流程删掉自身）
  3. 调 Applier.apply() 完成 merge + extract + swap
  4. 启动新版主程序（mac: `open <install>.app`；win: 直接 spawn 新 exe）
  5. 退出自身

命令行：
    updater --pid <PID> --install-dir <PATH> --volumes <PATH1> <PATH2> ...
            --workdir <PATH> [--restart-cmd <CMD>] [--log <PATH>]

退出码：
    0  成功
    1  通用失败
    2  超时（主进程一直没退出）
    3  替换失败（已尝试 rollback）
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional


# ---------- 日志 ----------

def _setup_logging(log_path: Optional[Path] = None) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_path:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [updater] %(message)s",
        handlers=handlers,
    )


log = logging.getLogger("updater")


# ---------- 等主进程退出 ----------

def wait_for_pid(pid: int, timeout: float = 30.0) -> bool:
    """等 pid 对应的进程退出。psutil 优先；不可用时回退 OS 探测；都不行就盲等 3s。"""
    try:
        import psutil
    except ImportError:
        log.warning("psutil is not installed; waiting 3 seconds")
        time.sleep(3.0)
        return True

    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        log.info("PID %s no longer exists", pid)
        return True

    log.info("Waiting for PID %s to exit (timeout=%ss)", pid, timeout)
    try:
        p.wait(timeout=timeout)
        log.info("PID %s exited", pid)
        return True
    except psutil.TimeoutExpired:
        log.error("Timed out waiting for PID %s", pid)
        return False


# ---------- self-relocate ----------

def _is_running_from_install_dir(install_dir: Path) -> bool:
    """当前 updater 可执行是否就在 install_dir 内？是 → 需要 self-relocate。"""
    try:
        my_path = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
        install_resolved = install_dir.resolve()
        return install_resolved in my_path.parents or my_path == install_resolved
    except OSError:
        return False


def self_relocate_and_relaunch(install_dir: Path, argv: List[str]) -> None:
    """把 updater 复制到 OS 临时目录并重新启动。

    updater 位于 install_dir 内且为冻结程序时执行搬迁并启动临时副本。
    """
    if not _is_running_from_install_dir(install_dir):
        return
    if not getattr(sys, "frozen", False):
        # 开发模式跑 python -m，不需要 relocate
        return

    src_exe = Path(sys.executable).resolve()
    src_dir = src_exe.parent
    tmp_root = Path(tempfile.mkdtemp(prefix="satellite_updater_"))
    runtime_dir = src_dir / "_internal"
    if runtime_dir.is_dir():
        # 兼容旧 onedir updater。
        dst_dir = tmp_root / src_dir.name
        log.info("self-relocate onedir: %s -> %s", src_dir, dst_dir)
        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
        dst_exe = dst_dir / src_exe.name
    else:
        # 当前发行使用 onefile，避免把 Python 运行目录嵌入主 .app。
        dst_exe = tmp_root / src_exe.name
        log.info("self-relocate onefile: %s -> %s", src_exe, dst_exe)
        shutil.copy2(src_exe, dst_exe)

    # 临时副本以明确的 relocated 状态启动。
    new_argv = [str(dst_exe)] + argv + ["--relocated"]
    log.info(f"relaunch: {new_argv}")
    if sys.platform.startswith("win"):
        # DETACHED_PROCESS = 0x00000008
        subprocess.Popen(new_argv, creationflags=0x00000008, close_fds=True)
    else:
        subprocess.Popen(new_argv, start_new_session=True, close_fds=True)
    sys.exit(0)


# ---------- 启动新版主程序 ----------

def restart_app(install_dir: Path, restart_cmd: Optional[str] = None) -> None:
    """升级完成后启动新版主程序。"""
    if restart_cmd:
        log.info("Using user-specified restart command: %s", restart_cmd)
        subprocess.Popen(restart_cmd, shell=True, close_fds=True)
        return

    if sys.platform.startswith("darwin") and install_dir.suffix == ".app":
        log.info(f"open .app: {install_dir}")
        subprocess.Popen(["open", str(install_dir)], close_fds=True)
        return

    # Windows / Linux：找 install_dir 下的同名可执行（onedir 模式约定）
    name = install_dir.name
    candidates = [
        install_dir / f"{name}.exe",
        install_dir / name,
        install_dir / "SatelliteDebugTool.exe",
        install_dir / "SatelliteDebugTool",
    ]
    for c in candidates:
        if c.exists():
            log.info("Launching: %s", c)
            if sys.platform.startswith("win"):
                subprocess.Popen([str(c)], creationflags=0x00000008, close_fds=True)
            else:
                subprocess.Popen([str(c)], start_new_session=True, close_fds=True)
            return
    log.warning("No launchable executable found in install_dir=%s", install_dir)


# ---------- CLI ----------

def main(argv: Optional[List[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="satellite_debug_tool updater")
    parser.add_argument("--pid", type=int, required=True,
                        help="PID of the main application to wait for")
    parser.add_argument("--install-dir", type=Path, required=True,
                        help="Target install directory (macOS .app or Windows onedir)")
    parser.add_argument("--volumes", type=Path, nargs="+", required=True,
                        help="Ordered list of 7z volume files")
    parser.add_argument("--workdir", type=Path, required=True,
                        help="Temporary work directory for merge and extraction")
    parser.add_argument("--restart-cmd", type=str, default=None,
                        help="Restart command after update (defaults to the platform install target)")
    parser.add_argument("--log", type=Path, default=None,
                        help="Log file path")
    parser.add_argument("--wait-timeout", type=float, default=30.0,
                        help="Seconds to wait for the main process to exit")
    parser.add_argument("--relocated", action="store_true",
                        help="Internal: updater is running from its relocated runtime")
    args = parser.parse_args(arguments)

    _setup_logging(args.log)

    # install_dir 中的冻结 updater 先搬迁到临时运行目录。
    if not args.relocated:
        self_relocate_and_relaunch(args.install_dir, arguments)

    # 等主进程退出
    if not wait_for_pid(args.pid, args.wait_timeout):
        log.error("Main process did not exit before the timeout; update aborted")
        return 2

    # 应用更新
    from satellite_debug_tool.updater.applier import Applier, ApplyError
    try:
        applier = Applier(on_progress=lambda stage, pct: log.info(f"{stage} {pct*100:.1f}%"))
        result = applier.apply(args.volumes, args.install_dir, args.workdir)
        log.info("Update completed; previous version backup: %s", result.backup_dir)
    except ApplyError as e:
        log.error("Update failed: %s", e)
        return 3

    # 启动新版
    restart_app(args.install_dir, args.restart_cmd)

    # 清理 workdir（保留 backup_dir 让用户验证）
    try:
        shutil.rmtree(args.workdir, ignore_errors=True)
    except OSError:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
