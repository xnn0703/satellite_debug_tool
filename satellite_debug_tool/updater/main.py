"""updater 独立可执行入口（M11 P6）。

由主程序触发：主程序下载完所有分卷后，调 `subprocess.Popen` 把本可执行启动起来，
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
        log.warning("psutil 未安装，盲等 3s")
        time.sleep(3.0)
        return True

    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        log.info(f"PID {pid} 已不存在，无需等待")
        return True

    log.info(f"等 PID {pid} 退出，超时 {timeout}s...")
    try:
        p.wait(timeout=timeout)
        log.info(f"PID {pid} 已退出")
        return True
    except psutil.TimeoutExpired:
        log.error(f"等待 PID {pid} 超时")
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
    """把整个 updater 二进制（PyInstaller onedir）复制到 OS 临时目录并重新启动。

    若已不在 install_dir 内则 no-op。
    """
    if not _is_running_from_install_dir(install_dir):
        return
    if not getattr(sys, "frozen", False):
        # 开发模式跑 python -m，不需要 relocate
        return

    src_exe = Path(sys.executable).resolve()
    src_dir = src_exe.parent
    tmp_root = Path(tempfile.mkdtemp(prefix="satellite_updater_"))
    log.info(f"self-relocate: {src_dir} → {tmp_root}")

    # onedir：复制整个目录（含 _internal、updater 本体）
    dst_dir = tmp_root / src_dir.name
    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
    dst_exe = dst_dir / src_exe.name

    # 重启自己：保持原 argv，加 --no-relocate 防止无限递归
    new_argv = [str(dst_exe)] + argv + ["--no-relocate"]
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
        log.info(f"重启命令（用户指定）: {restart_cmd}")
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
            log.info(f"启动: {c}")
            if sys.platform.startswith("win"):
                subprocess.Popen([str(c)], creationflags=0x00000008, close_fds=True)
            else:
                subprocess.Popen([str(c)], start_new_session=True, close_fds=True)
            return
    log.warning(f"未找到可启动的可执行，install_dir={install_dir}")


# ---------- CLI ----------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="satellite_debug_tool updater")
    parser.add_argument("--pid", type=int, required=True,
                        help="等待退出的主程序 PID")
    parser.add_argument("--install-dir", type=Path, required=True,
                        help="目标安装目录（mac .app 或 win onedir）")
    parser.add_argument("--volumes", type=Path, nargs="+", required=True,
                        help="按顺序的 7z 分卷文件列表")
    parser.add_argument("--workdir", type=Path, required=True,
                        help="临时工作目录（合并 + 解压用）")
    parser.add_argument("--restart-cmd", type=str, default=None,
                        help="升级完成后的重启命令（默认按平台启动 install_dir）")
    parser.add_argument("--log", type=Path, default=None,
                        help="日志文件路径")
    parser.add_argument("--wait-timeout", type=float, default=30.0,
                        help="等主进程退出的超时秒")
    parser.add_argument("--no-relocate", action="store_true",
                        help="（内部）已 self-relocate 过，跳过再次 relocate")
    args = parser.parse_args(argv)

    _setup_logging(args.log)

    # 优先 self-relocate（如果在 install_dir 内运行）
    if not args.no_relocate:
        self_relocate_and_relaunch(args.install_dir, sys.argv[1:])

    # 等主进程退出
    if not wait_for_pid(args.pid, args.wait_timeout):
        log.error("主进程未在超时内退出，放弃升级")
        return 2

    # 应用更新
    from satellite_debug_tool.updater.applier import Applier, ApplyError
    try:
        applier = Applier(on_progress=lambda stage, pct: log.info(f"{stage} {pct*100:.1f}%"))
        result = applier.apply(args.volumes, args.install_dir, args.workdir)
        log.info(f"升级成功；旧版备份: {result.backup_dir}")
    except ApplyError as e:
        log.error(f"升级失败: {e}")
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
