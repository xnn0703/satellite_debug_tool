"""安装 journal、跨进程安装锁和 Windows 登录恢复入口。"""

from __future__ import annotations

from contextlib import AbstractContextManager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from typing import Any

from satellite_debug_tool.package_names import WINDOWS_EXECUTABLE
from satellite_debug_tool.updater.handoff import sha256_file
from satellite_debug_tool.updater.status import sync_file, write_atomic_json


JOURNAL_SCHEMA = "satellite.update-journal/v1"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_owned_windows_mutexes: set[str] = set()
_windows_mutex_guard = threading.Lock()


class RecoveryError(RuntimeError):
    """恢复的必要证据或文件操作未完成。"""


def journal_pointer_path(install_dir: Path) -> Path:
    install = Path(install_dir).resolve()
    return install.parent / f".{install.name}.update-journal.json"


def pending_journal(install_dir: Path) -> "InstallJournal | None":
    """固定位置只保存 journal 地址；执行事实继续由 journal 独占。"""
    pointer = journal_pointer_path(install_dir)
    if not pointer.exists():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        if data.get("schema") != "satellite.update-pointer/v1":
            raise ValueError("unsupported installation journal pointer")
        journal = InstallJournal.load(Path(data["journal"]))
        if journal.install_dir != Path(install_dir).resolve():
            raise ValueError("journal pointer refers to a different installation")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RecoveryError(f"installation recovery pointer requires inspection: {pointer}: {exc}") from exc
    return None if journal.data["state"] in {"committed", "rolled_back"} else journal


def require_recovered_installation(install_dir: Path) -> None:
    journal = pending_journal(install_dir)
    if journal is not None:
        command = subprocess.list2cmdline([journal.data["updater_executable"], "--recover", str(journal.path)])
        raise RecoveryError(f"application update requires recovery before startup; run: {command}")


def _windows_mutex_api():
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32")
    api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.ReleaseMutex.argtypes = [wintypes.HANDLE]
    api.ReleaseMutex.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.GetLastError.argtypes = []
    api.GetLastError.restype = wintypes.DWORD
    return api


class InstallationLock(AbstractContextManager):
    """Windows 使用系统互斥量，普通启动无需写入 Program Files 等安装目录。"""

    def __init__(self, install_dir: Path):
        self.path = Path(install_dir).parent / f".{Path(install_dir).name}.update.lock"
        self._stream = None
        identity = os.path.normcase(str(Path(install_dir).resolve()))
        self._mutex_name = "Global\\SatelliteTerminalToolUpdate-" + hashlib.sha256(identity.encode()).hexdigest()
        self._mutex = None
        self._mutex_api = None

    def __enter__(self):
        if sys.platform == "win32":
            with _windows_mutex_guard:
                if self._mutex_name in _owned_windows_mutexes:
                    raise RecoveryError("another updater owns this installation")
                api = _windows_mutex_api()
                handle = api.CreateMutexW(None, False, self._mutex_name)
                if not handle:
                    raise RecoveryError(f"installation mutex could not be opened: Windows error {api.GetLastError()}")
                result = api.WaitForSingleObject(handle, 0)
                if result not in {0x00000000, 0x00000080}:  # acquired / abandoned owner
                    error = api.GetLastError()
                    api.CloseHandle(handle)
                    if result == 0x00000102:
                        raise RecoveryError("another updater owns this installation")
                    raise RecoveryError(f"installation mutex acquisition failed: Windows error {error}")
                self._mutex_api, self._mutex = api, handle
                _owned_windows_mutexes.add(self._mutex_name)
            return self
        try:
            self._stream = self.path.open("a+b")
            import fcntl
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self._stream is not None:
                self._stream.close()
            self._stream = None
            if isinstance(exc, BlockingIOError):
                raise RecoveryError("another updater owns this installation") from exc
            raise RecoveryError(f"installation lock could not be acquired: {exc}") from exc
        return self

    def __exit__(self, *_args):
        if self._mutex is not None:
            with _windows_mutex_guard:
                released = self._mutex_api.ReleaseMutex(self._mutex)
                error = self._mutex_api.GetLastError()
                self._mutex_api.CloseHandle(self._mutex)
                self._mutex = None
                _owned_windows_mutexes.discard(self._mutex_name)
                if not released:
                    raise RecoveryError(f"installation mutex release failed: Windows error {error}")
        if self._stream is not None:
            import fcntl
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None


def recovery_name(install_dir: Path) -> str:
    identity = os.path.normcase(str(Path(install_dir).resolve()))
    return "SatelliteTerminalToolUpdate-" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def recovery_command(executable, journal_path) -> str:
    command = subprocess.list2cmdline([str(executable), "--recover", str(journal_path)])
    if len(command.encode("utf-16-le")) // 2 > 260:
        raise RecoveryError("recovery command exceeds Windows Run limit; use manual installation")
    return command


def register_login_recovery(executable: Path, journal_path: Path, install_dir: Path) -> None:
    """HKCU Run 保持到事务终态，避免 RunOnce 在执行前删除造成恢复缺口。"""
    if sys.platform != "win32":
        return
    import winreg
    executable = Path(executable).resolve()
    journal_path = Path(journal_path).resolve()
    if not executable.is_file() or not journal_path.is_file():
        raise RecoveryError("persistent recovery executable and journal are required")
    if Path(install_dir).resolve() in executable.parents:
        raise RecoveryError("recovery executable must be outside the installation")
    command = recovery_command(executable, journal_path)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                           winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
        name = recovery_name(install_dir)
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
        if winreg.QueryValueEx(key, name) != (command, winreg.REG_SZ):
            raise RecoveryError("Windows login recovery registration readback failed")
        winreg.FlushKey(key)


def remove_login_recovery(install_dir: Path) -> None:
    if sys.platform != "win32":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, recovery_name(install_dir))
            winreg.FlushKey(key)
    except FileNotFoundError:
        return


class InstallJournal:
    def __init__(self, path: Path, data: dict[str, Any]):
        self.path = Path(path).resolve()
        self.data = dict(data)
        self._validate()

    @classmethod
    def create(cls, path: Path, *, transaction_id: str, install_dir: Path,
               staging_dir: Path, updater_executable: Path, target_version: str) -> "InstallJournal":
        install = Path(install_dir).resolve()
        journal = cls(path, {
            "schema": JOURNAL_SCHEMA,
            "transaction_id": transaction_id,
            "install_dir": str(install),
            "staging_dir": str(Path(staging_dir).resolve()),
            "backup_dir": str(install.parent / f"{install.name}.bak.{transaction_id}"),
            "failed_dir": str(install.parent / f"{install.name}.failed.{transaction_id}"),
            "updater_executable": str(Path(updater_executable).resolve()),
            "target_version": str(target_version),
            "state": "prepared",
            "original_exists": install.is_dir(),
            "settings": [],
        })
        journal.save("prepared")
        return journal

    @classmethod
    def load(cls, path: Path) -> "InstallJournal":
        return cls(path, json.loads(Path(path).read_text(encoding="utf-8")))

    def _validate(self) -> None:
        d = self.data
        if d.get("schema") != JOURNAL_SCHEMA or re.fullmatch(r"[a-f0-9]{32}", str(d.get("transaction_id"))) is None:
            raise RecoveryError("update recovery journal identity is invalid")
        install = Path(d["install_dir"]).resolve()
        if install == self.path.parent or install in self.path.parents:
            raise RecoveryError("update journal must be outside the installation")
        token = d["transaction_id"]
        expected = {
            "backup_dir": install.parent / f"{install.name}.bak.{token}",
            "failed_dir": install.parent / f"{install.name}.failed.{token}",
            "staging_dir": install.parent / f".{install.name}.staging.{token}",
        }
        for key, value in expected.items():
            if Path(d[key]).resolve() != value:
                raise RecoveryError(f"unexpected recovery path: {key}")
        executable = Path(d["updater_executable"]).resolve()
        if self.path.parent not in executable.parents or install in executable.parents:
            raise RecoveryError("update recovery executable is outside the maintenance directory")
        for entry in d.get("settings", []):
            path = Path(entry["path"])
            copy = Path(entry["backup"])
            if path.name not in {"settings.json", "settings.json.bak"}:
                raise RecoveryError("unsupported configuration recovery file")
            if copy.parent.resolve() != self.path.parent / "configuration-backup":
                raise RecoveryError("configuration backup is outside the transaction")
            if install in path.resolve().parents:
                raise RecoveryError("configuration recovery path overlaps the installation")

    @property
    def install_dir(self) -> Path:
        return Path(self.data["install_dir"])

    @property
    def backup_dir(self) -> Path:
        return Path(self.data["backup_dir"])

    @property
    def staging_dir(self) -> Path:
        return Path(self.data["staging_dir"])

    def save(self, state: str, **facts: Any) -> None:
        self.data.update(facts)
        self.data["state"] = state
        self._validate()
        write_atomic_json(self.path, self.data)
        pointer = journal_pointer_path(self.install_dir)
        if state in {"committed", "rolled_back"}:
            if pointer.exists():
                data = json.loads(pointer.read_text(encoding="utf-8"))
                if Path(data["journal"]).resolve() == self.path:
                    pointer.unlink()
        else:
            if pointer.exists():
                data = json.loads(pointer.read_text(encoding="utf-8"))
                if Path(data["journal"]).resolve() != self.path:
                    raise RecoveryError("another journal owns the pending installation")
            write_atomic_json(pointer, {"schema": "satellite.update-pointer/v1", "journal": str(self.path)})

    def capture_settings(self, directory: Path | None) -> None:
        if directory is None:
            return
        records = []
        backup_root = self.path.parent / "configuration-backup"
        backup_root.mkdir(exist_ok=True)
        for name in ("settings.json", "settings.json.bak"):
            source = Path(directory).resolve() / name
            backup = backup_root / name
            exists = source.is_file()
            digest = ""
            if exists:
                shutil.copy2(source, backup)
                sync_file(backup)
                digest = sha256_file(source)
                if sha256_file(backup) != digest:
                    raise RecoveryError("configuration snapshot changed during backup")
            records.append({"path": str(source), "backup": str(backup),
                            "existed": exists, "sha256": digest})
        self.save(self.data["state"], settings=records)

    def restore_settings(self) -> None:
        for item in self.data["settings"]:
            path = Path(item["path"])
            backup = Path(item["backup"])
            if item["existed"]:
                if sha256_file(backup) != item["sha256"]:
                    raise RecoveryError("configuration backup digest differs from journal")
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f".{path.name}.update-restore")
                shutil.copy2(backup, temporary)
                sync_file(temporary)
                os.replace(temporary, path)
            else:
                path.unlink(missing_ok=True)

    def stop_trial_process(self) -> None:
        if not self.data.get("configuration_trial_started", False):
            return
        from satellite_debug_tool.updater.health import HEALTH_ENV, StartupHealthContext, validate_health_message
        import psutil
        expected = self.install_dir / WINDOWS_EXECUTABLE
        health_path = self.path.parent / "health-request.json"

        def stop_identity(pid, created) -> None:
            try:
                process = psutil.Process(int(pid))
                if abs(process.create_time() - float(created)) > 0.01:
                    # PID 复用表示该进程已经退出；另一个进程交给安装目录扫描确认。
                    return
                if Path(process.exe()).resolve() != expected:
                    raise RecoveryError("trial process executable differs from the installation")
                process.terminate()
                try:
                    process.wait(timeout=5)
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            except psutil.NoSuchProcess:
                return

        def installed_processes():
            for process in psutil.process_iter(attrs=["pid", "exe", "name"]):
                executable = process.info.get("exe")
                if (not executable and str(process.info.get("name", "")).casefold() == expected.name.casefold()):
                    raise RecoveryError("application process executable requires recovery confirmation")
                if executable and self.install_dir in Path(executable).resolve().parents:
                    yield process

        try:
            pid = self.data.get("trial_pid")
            if pid is not None:
                stop_identity(pid, self.data["trial_created"])
            process_receipt = self.path.parent / "health-process.json"
            if process_receipt.exists():
                receipt = json.loads(process_receipt.read_text(encoding="utf-8"))
                health = json.loads(health_path.read_text(encoding="utf-8"))
                context = StartupHealthContext(self.path.parent, self.data["transaction_id"],
                    self.data["target_version"], health["token"], None, 1)
                if (not validate_health_message(receipt, context, pid=int(receipt["pid"]), state="started")
                        or Path(receipt["executable"]).resolve() != expected):
                    raise RecoveryError("trial startup process receipt differs from the transaction")
                stop_identity(receipt["pid"], receipt["created"])
            # Popen 已创建 EXE、尚未执行到 Python 回执的窗口，由精确启动环境绑定。
            for process in installed_processes():
                try:
                    binding = process.environ().get(HEALTH_ENV)
                    if (Path(process.exe()).resolve() != expected or not binding
                            or Path(binding).resolve() != health_path):
                        raise RecoveryError("installed application process requires recovery confirmation")
                    stop_identity(process.pid, process.create_time())
                except psutil.NoSuchProcess:
                    continue
            if any(installed_processes()):
                raise RecoveryError("installed application processes are still running during recovery")
        except psutil.Error as exc:
            raise RecoveryError(f"trial process could not be stopped: {exc}") from exc

    def resume_committed_trial(self) -> str:
        """提交记录先落盘后的中断，可向仍存活的同一试运行进程重发准入回执。"""
        from satellite_debug_tool.updater.health import StartupHealthContext, validate_health_message
        import psutil
        pid = self.data.get("trial_pid")
        created = self.data.get("trial_created")
        if pid is None or created is None:
            return "closed"
        try:
            process = psutil.Process(int(pid))
            if (abs(process.create_time() - float(created)) > 0.01
                    or Path(process.exe()).resolve() != self.install_dir / WINDOWS_EXECUTABLE
                    or not process.is_running() or process.status() == psutil.STATUS_ZOMBIE):
                return "closed"
        except psutil.NoSuchProcess:
            return "closed"
        except psutil.Error as exc:
            raise RecoveryError(f"committed application process could not be inspected: {exc}") from exc
        health = json.loads((self.path.parent / "health-request.json").read_text(encoding="utf-8"))
        context = StartupHealthContext(self.path.parent, self.data["transaction_id"],
                                       self.data["target_version"], health["token"], None, 1)
        receipt = json.loads(context.receipt_path.read_text(encoding="utf-8"))
        if not validate_health_message(receipt, context, pid=int(pid), state="ready"):
            raise RecoveryError("committed application's ready receipt differs from journal")
        commit = context.message("committed")
        commit["pid"] = int(pid)
        write_atomic_json(context.commit_path, commit)
        return "ready"

    def rollback(self) -> str:
        """根据文件事实回退；每次 rename 前记录意图，允许重复恢复。"""
        if self.data["state"] == "committed":
            phase = self.resume_committed_trial()
            self.save("committed", application_phase=phase)
            remove_login_recovery(self.install_dir)
            return "committed"
        if self.data["state"] == "rolled_back":
            remove_login_recovery(self.install_dir)
            return self.data["state"]
        self.stop_trial_process()
        source_state = self.data.get("rollback_from", self.data["state"])
        self.save("rollback_intent", rollback_from=source_state)
        if self.backup_dir.is_dir():
            if self.install_dir.exists():
                failed = Path(self.data["failed_dir"])
                if failed.exists():
                    raise RecoveryError("failed-install destination already exists")
                self.install_dir.rename(failed)
            self.backup_dir.rename(self.install_dir)
        elif self.data["original_exists"] and not self.install_dir.is_dir():
            raise RecoveryError("installed application and its recovery backup are both missing")
        elif (self.data["original_exists"]
              and source_state in {"old_backed_up", "switch_intent", "switched", "trial_start_intent", "trial_started"}
              and not Path(self.data["failed_dir"]).is_dir()):
            raise RecoveryError("verified previous installation backup is missing")
        elif not self.data["original_exists"] and self.install_dir.is_dir():
            failed = Path(self.data["failed_dir"])
            self.install_dir.rename(failed)
        if self.data.get("configuration_trial_started", False):
            self.restore_settings()
        self.save("rolled_back")
        remove_login_recovery(self.install_dir)
        return "rolled_back"


def recover_installation(journal_path: Path) -> str:
    journal = InstallJournal.load(journal_path)
    with InstallationLock(journal.install_dir):
        return journal.rollback()
