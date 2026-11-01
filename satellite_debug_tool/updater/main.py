"""独立 Windows updater：可信输入、同盘准备、可恢复切换及新版健康握手。"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from typing import Optional

from satellite_debug_tool.package_names import WINDOWS_EXECUTABLE
from satellite_debug_tool.updater.applier import Applier, PayloadContract
from satellite_debug_tool.updater.handoff import UpdateLaunchRequest, load_update_launch_request, sha256_file
from satellite_debug_tool.updater.health import HEALTH_ENV, HEALTH_SCHEMA, StartupHealthContext, validate_health_message
from satellite_debug_tool.updater.recovery import (
    InstallJournal, InstallationLock, RecoveryError, recover_installation,
    register_login_recovery, remove_login_recovery, require_recovered_installation,
)
from satellite_debug_tool.updater.status import write_atomic_json, write_update_status


log = logging.getLogger("updater")


def _setup_logging(log_path: Optional[Path] = None) -> None:
    handlers = [logging.StreamHandler(sys.stderr)]
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [updater] %(message)s",
                        handlers=handlers)


def _show_windows_failure(stage: str, detail: str, log_path: Path) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,
            f"自动更新失败\n\n阶段：{stage}\n原因：{detail}\n\n日志：{log_path}",
            "Satellite Terminal Manager Updater", 0x10)
    except Exception:
        log.exception("Could not display update failure dialog")


def wait_for_pid(pid: int, timeout: float = 30.0) -> bool:
    """进程退出是明确证据；缺少 psutil 时显式失败，不用盲等推断。"""
    import psutil
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    try:
        process.wait(timeout=timeout)
        return True
    except psutil.TimeoutExpired:
        return False


def require_installation_idle(install_dir: Path) -> None:
    """同目录的其他主程序或运行库子进程仍存活时，保留原安装和配置。"""
    import psutil
    installation = Path(install_dir).resolve()
    for process in psutil.process_iter(attrs=["pid", "exe"]):
        executable = process.info.get("exe")
        if executable and installation in Path(executable).resolve().parents:
            raise RecoveryError(f"another installed application process is still running: PID {process.info['pid']}")


def _start_trial(request: UpdateLaunchRequest, context: StartupHealthContext):
    executable = request.install_dir / WINDOWS_EXECUTABLE
    if not executable.is_file():
        raise RecoveryError("installed executable is missing")
    health_request = context.directory / "health-request.json"
    write_atomic_json(health_request, {
        "schema": HEALTH_SCHEMA, "transaction_id": context.transaction_id,
        "target_version": context.target_version, "token": context.token,
        "timeout_s": context.timeout_s,
    })
    environment = dict(os.environ)
    environment[HEALTH_ENV] = str(health_request)
    options = {"close_fds": True, "env": environment, "cwd": str(request.transaction_dir)}
    if sys.platform == "win32":
        options["creationflags"] = 0x00000008
    else:
        options["start_new_session"] = True
    return subprocess.Popen([str(executable)], **options)


def _wait_for_health(process, context: StartupHealthContext, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RecoveryError("new application exited before health confirmation")
        try:
            message = json.loads(context.receipt_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        if not validate_health_message(message, context, pid=process.pid, state="ready"):
            raise RecoveryError("new application health receipt identity differs from the transaction")
        return message
    raise RecoveryError("new application health receipt timed out")


def _verify_inputs(request: UpdateLaunchRequest) -> dict:
    from satellite_debug_tool.updater.payload import read_manifest
    if sha256_file(request.volumes[0]) != request.package_sha256:
        raise RecoveryError("downloaded package changed after verification")
    if sha256_file(request.tree_manifest) != request.tree_manifest_sha256:
        raise RecoveryError("target file manifest changed after verification")
    manifest = read_manifest(request.tree_manifest)
    identity = manifest["identity"]
    if (identity["edition"] != "ops" or identity["platform"] != "windows"
            or str(identity["version"]).lstrip("v") != request.target_version.lstrip("v")):
        raise RecoveryError("target tree identity differs from the installation request")
    installed = json.loads((request.install_dir / "_internal/satellite_debug_tool/build_identity.json").read_text(encoding="utf-8"))
    for field in ("app_id", "edition", "architecture"):
        if installed.get(field) != identity[field]:
            raise RecoveryError(f"installed {field} differs from the trusted target")
    if {"win": "windows"}.get(installed.get("platform"), installed.get("platform")) != identity["platform"]:
        raise RecoveryError("installed platform differs from the trusted target")
    return manifest


def execute_update(request: UpdateLaunchRequest, *, wait_timeout: float = 30.0,
                   health_timeout: float = 30.0) -> int:
    """可测事务入口；CLI 另外限定正式 Windows 自动安装合同。"""
    journal = None
    trial = None
    phase = "waiting_for_app_exit"
    lock = InstallationLock(request.install_dir)
    acquired = False
    try:
        lock.__enter__()
        acquired = True
        require_recovered_installation(request.install_dir)
        if getattr(sys, "frozen", False) and Path(sys.executable).resolve() != request.updater_executable:
            raise RecoveryError("updater must run from its persistent transaction copy")
        write_update_status(request.status_path, phase, transaction_id=request.transaction_id,
                            target_version=request.target_version, install_dir=str(request.install_dir))
        if not wait_for_pid(request.main_pid, wait_timeout):
            write_update_status(request.status_path, "failed", stage=phase,
                                detail="main application is still running")
            return 2
        require_installation_idle(request.install_dir)
        phase = "verifying"
        manifest = _verify_inputs(request)
        request.work_dir.mkdir(parents=True, exist_ok=False)

        def progress(stage: str, value: float) -> None:
            log.info("%s %.3f", stage, value)

        applier = Applier(on_progress=progress)
        contract = PayloadContract(WINDOWS_EXECUTABLE, "updater.exe",
                                   edition="ops", target_version=request.target_version)
        phase = "preparing"
        if request.prepared_payload is None:
            extracted = applier.extract_to(request.volumes[0], request.work_dir / "extracted",
                max_expanded_bytes=sum(int(item["size"]) for item in manifest["files"]))
            payload = applier.resolve_payload_root(extracted, contract)
        else:
            payload = request.prepared_payload
        staging = applier.prepare_payload(request.install_dir, payload, request.transaction_id,
                                          tree_manifest=manifest, contract=contract)
        journal = InstallJournal.create(request.transaction_dir / "journal.json",
            transaction_id=request.transaction_id, install_dir=request.install_dir,
            staging_dir=staging, updater_executable=request.updater_executable,
            target_version=request.target_version)
        journal.capture_settings(request.settings_directory)
        phase = "registering_recovery"
        register_login_recovery(request.updater_executable, journal.path, request.install_dir)
        journal.save("recovery_ready")
        phase = "swapping"
        result = applier.install_prepared(journal)
        phase = "waiting_for_health"
        context = StartupHealthContext(request.transaction_dir, request.transaction_id,
            request.target_version, secrets.token_hex(32), request.settings_directory,
            health_timeout + 10.0)
        journal.save("trial_start_intent", configuration_trial_started=True)
        trial = _start_trial(request, context)
        import psutil
        created = psutil.Process(trial.pid).create_time()
        journal.save("trial_started", trial_pid=trial.pid, trial_created=created)
        write_update_status(request.status_path, phase, transaction_id=request.transaction_id,
                            target_version=request.target_version, backup_dir=str(result.backup_dir))
        _wait_for_health(trial, context, health_timeout)
        # journal 是提交事实；其持久化先于允许主程序操作设备。
        journal.save("committed")
        commit = context.message("committed")
        commit["pid"] = trial.pid
        write_atomic_json(context.commit_path, commit)
        write_update_status(request.status_path, "completed", transaction_id=request.transaction_id,
                            target_version=request.target_version, install_dir=str(result.install_dir),
                            backup_dir=str(result.backup_dir), application_pid=trial.pid)
        try:
            remove_login_recovery(request.install_dir)
        except OSError:
            log.exception("Completed installation retained its login cleanup entry")
        return 0
    except Exception as exc:
        log.exception("Update failed during %s", phase)
        recovery = "installation_preserved"
        trial_stopped = True
        if trial is not None and (journal is None or journal.data["state"] != "committed"):
            try:
                if trial.poll() is None:
                    trial.terminate()
                    try:
                        trial.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        trial.kill()
                        trial.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as stop_error:
                trial_stopped = False
                recovery = "recovery_required"
                exc = RecoveryError(f"{exc}; trial process stop: {stop_error}")
        if journal is not None and trial_stopped:
            try:
                recovery = journal.rollback()
            except Exception as recovery_error:
                recovery = "recovery_required"
                log.exception("Update recovery requires the persistent entry")
                exc = RecoveryError(f"{exc}; recovery: {recovery_error}")
        write_update_status(request.status_path, "failed", stage=phase, detail=str(exc),
                            recovery=recovery, transaction_id=request.transaction_id,
                            journal=str(journal.path) if journal is not None else "")
        _show_windows_failure(phase, str(exc), request.log_path)
        return 3
    finally:
        if acquired:
            lock.__exit__(None, None, None)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Satellite Terminal Manager Updater")
    intent = parser.add_mutually_exclusive_group(required=True)
    intent.add_argument("--request", type=Path, help="Frozen verified installation request")
    intent.add_argument("--recover", type=Path, help="Persistent interrupted-install journal")
    parser.add_argument("--wait-timeout", type=float, default=30.0)
    parser.add_argument("--health-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        raise SystemExit("automatic application installation currently supports Windows ops")
    if args.recover:
        args.recover = args.recover.resolve()
        # 登录恢复和手动恢复都先释放继承的安装目录占用，再允许 rename。
        os.chdir(args.recover.parent)
        _setup_logging(args.recover.parent / "updater.log")
        try:
            result = recover_installation(args.recover)
            journal = InstallJournal.load(args.recover)
            write_update_status(args.recover.parent / "update-status.json", result,
                                application_phase=journal.data.get("application_phase", "closed"),
                                transaction_id=journal.data["transaction_id"])
            return 0
        except Exception as exc:
            log.exception("Recovery failed")
            _show_windows_failure("recovery", str(exc), args.recover.parent / "updater.log")
            return 3
    request = load_update_launch_request(args.request)
    os.chdir(request.transaction_dir)
    _setup_logging(request.log_path)
    return execute_update(request, wait_timeout=max(0.1, args.wait_timeout),
                          health_timeout=max(0.1, args.health_timeout))


if __name__ == "__main__":
    sys.exit(main())
