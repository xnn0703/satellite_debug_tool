"""安装恢复入口、重复恢复与真实主窗口健康门禁。"""
from dataclasses import replace
import json
import os
from pathlib import Path
import plistlib
import sys
from types import SimpleNamespace

import pytest

from satellite_debug_tool.tests._updater_helpers import make_request
from satellite_debug_tool.updater.health import (
    HEALTH_ENV, HEALTH_SCHEMA, StartupHealthContext, install_startup_health_gate, startup_health_context,
)
from satellite_debug_tool.updater.recovery import InstallationLock, RecoveryError
from satellite_debug_tool.updater.status import write_atomic_json


def test_installation_lock_excludes_second_owner_and_releases(tmp_path):
    install = tmp_path / "app"
    install.mkdir()
    with InstallationLock(install):
        with pytest.raises(RecoveryError, match="another updater"):
            with InstallationLock(install):
                pass
    with InstallationLock(install):
        pass


class FakeWindowsMutexApi:
    def __init__(self, result=0, create_succeeds=True):
        self.result = result
        self.create_succeeds = create_succeeds
        self.names = []
        self.closed = []
        self.released = []
    def CreateMutexW(self, security, owner, name):
        assert security is None and owner is False
        self.names.append(name)
        return len(self.names) if self.create_succeeds else 0
    def WaitForSingleObject(self, handle, timeout):
        assert timeout == 0
        return self.result
    def ReleaseMutex(self, handle):
        self.released.append(handle)
        return True
    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True
    def GetLastError(self): return 5


def test_windows_startup_lock_works_with_readonly_installation(tmp_path, monkeypatch):
    from satellite_debug_tool.updater import recovery
    install = tmp_path / "Program Files" / "SatelliteTerminalTool"
    install.mkdir(parents=True)
    api = FakeWindowsMutexApi()
    monkeypatch.setattr(recovery, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(recovery, "_windows_mutex_api", lambda: api)
    def readonly(*_args, **_kwargs):
        raise PermissionError("installation directory is read-only")
    monkeypatch.setattr(Path, "open", readonly)
    with InstallationLock(install):
        recovery.require_recovered_installation(install)
        with pytest.raises(RecoveryError, match="another updater"):
            with InstallationLock(install):
                pytest.fail("same installation acquired twice")
    with InstallationLock(install):
        pass
    assert api.names[0] == api.names[1]
    assert api.names[0].startswith("Global\\SatelliteTerminalToolUpdate-")
    assert api.released == api.closed == [1, 2]


@pytest.mark.parametrize("result,create_succeeds", [(0x102, True), (0xFFFFFFFF, True), (0, False)])
def test_windows_mutex_contention_and_system_failures_block_admission(tmp_path, monkeypatch, result, create_succeeds):
    from satellite_debug_tool.updater import recovery
    api = FakeWindowsMutexApi(result, create_succeeds)
    monkeypatch.setattr(recovery, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(recovery, "_windows_mutex_api", lambda: api)
    with pytest.raises(RecoveryError):
        with InstallationLock(tmp_path / "installed"):
            pytest.fail("mutex failure bypassed installation admission")
    assert api.closed == ([1] if create_succeeds else [])
    assert api.released == []


def test_windows_mutex_abandoned_owner_allows_journal_recovery(tmp_path, monkeypatch):
    from satellite_debug_tool.updater import recovery
    api = FakeWindowsMutexApi(0x80)
    monkeypatch.setattr(recovery, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(recovery, "_windows_mutex_api", lambda: api)
    with InstallationLock(tmp_path / "installed"):
        pass
    assert api.released == api.closed == [1]


def test_unfinished_journal_blocks_normal_start_before_login_recovery(tmp_path):
    from satellite_debug_tool.tests.test_updater_applier import prepare
    from satellite_debug_tool.updater.recovery import (
        journal_pointer_path, pending_journal, require_recovered_installation, recover_installation,
    )
    install, _, _, journal = prepare(tmp_path)
    assert pending_journal(install).path == journal.path
    with pytest.raises(RecoveryError, match="requires recovery"):
        require_recovered_installation(install)
    recover_installation(journal.path)
    require_recovered_installation(install)
    assert not journal_pointer_path(install).exists()


def test_windows_persistent_login_entry_is_registered_read_back_and_removed(tmp_path, monkeypatch):
    from satellite_debug_tool.updater import recovery
    from satellite_debug_tool.tests.test_updater_applier import prepare
    install, _, _, journal = prepare(tmp_path)
    values = {}
    class Key:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
    fake = SimpleNamespace(HKEY_CURRENT_USER="HKCU", KEY_SET_VALUE=2, KEY_QUERY_VALUE=1, REG_SZ=1,
        CreateKeyEx=lambda *_args: Key(), OpenKey=lambda *_args: Key(),
        SetValueEx=lambda _key, name, _zero, _kind, value: values.__setitem__(name, (value, 1)),
        QueryValueEx=lambda _key, name: values[name],
        FlushKey=lambda _key: None,
        DeleteValue=lambda _key, name: values.pop(name))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(recovery, "sys", SimpleNamespace(platform="win32"))
    # 保持真实命令较短，避免 pytest 的长目录名本身超过 Windows 260 限制。
    monkeypatch.setattr(recovery.subprocess, "list2cmdline", lambda args: '"updater.exe" --recover "journal.json"')
    recovery.register_login_recovery(Path(journal.data["updater_executable"]), journal.path, install)
    assert len(values) == 1
    assert " --recover " in next(iter(values.values()))[0]
    recovery.remove_login_recovery(install)
    assert values == {}


def test_login_entry_failure_is_explicit(tmp_path, monkeypatch):
    from satellite_debug_tool.updater import recovery
    from satellite_debug_tool.tests.test_updater_applier import prepare
    install, _, _, journal = prepare(tmp_path)
    monkeypatch.setattr(recovery, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace())
    monkeypatch.setattr(recovery.subprocess, "list2cmdline", lambda _args: "x" * 261)
    with pytest.raises(RecoveryError, match="limit"):
        recovery.register_login_recovery(Path(journal.data["updater_executable"]), journal.path, install)


def test_typical_windows_maintenance_paths_fit_login_command_limit():
    from pathlib import PureWindowsPath
    from satellite_debug_tool.updater.recovery import recovery_command
    maintenance = PureWindowsPath(r"C:\Users\tester\.satellite_debug_tool\updates\transactions") / ("a" * 32)
    command = recovery_command(maintenance / "runtime" / "updater.exe", maintenance / "journal.json")
    assert len(command) <= 260
    assert "--recover" in command


def test_startup_health_context_binds_version_and_transaction(tmp_path, monkeypatch):
    from satellite_debug_tool.edition import Edition
    request = make_request(tmp_path)
    health_path = request.transaction_dir / "health-request.json"
    write_atomic_json(health_path, {"schema": HEALTH_SCHEMA, "transaction_id": request.transaction_id,
                                  "target_version": request.target_version, "token": "b" * 64})
    monkeypatch.setenv(HEALTH_ENV, str(health_path))
    context = startup_health_context(Edition.OPS, "1.9.1")
    assert context.transaction_id == request.transaction_id
    process_receipt = json.loads((context.directory / "health-process.json").read_text())
    assert process_receipt["state"] == "started"
    assert process_receipt["pid"] == os.getpid()
    assert process_receipt["created"] > 0
    assert HEALTH_ENV not in os.environ
    monkeypatch.setenv(HEALTH_ENV, str(health_path))
    with pytest.raises(RuntimeError, match="identity"):
        startup_health_context(Edition.OPS, "1.9.0")


@pytest.mark.parametrize("executable", ["SatelliteTerminalManager.exe", "SatelliteDebugTool.exe"])
def test_frozen_windows_health_requires_declared_manager_executable(tmp_path, monkeypatch, executable):
    from satellite_debug_tool.edition import Edition
    from satellite_debug_tool.updater import health

    request = make_request(tmp_path)
    path = request.transaction_dir / "health-request.json"
    write_atomic_json(path, {"schema": HEALTH_SCHEMA, "transaction_id": request.transaction_id,
                            "target_version": request.target_version, "token": "b" * 64})
    monkeypatch.setenv(HEALTH_ENV, str(path))
    monkeypatch.setattr(health, "sys", SimpleNamespace(frozen=True, executable=str(request.install_dir / executable)))
    if executable == "SatelliteTerminalManager.exe":
        assert startup_health_context(Edition.OPS, "1.9.1").transaction_id == request.transaction_id
    else:
        with pytest.raises(RuntimeError, match="executable"):
            startup_health_context(Edition.OPS, "1.9.1")


@pytest.mark.parametrize("executable", ["SatelliteTerminalManager", "SatelliteTerminalManagerOps"])
def test_frozen_macos_health_requires_info_plist_executable(tmp_path, monkeypatch, executable):
    from satellite_debug_tool.edition import Edition
    from satellite_debug_tool.updater import health

    request = make_request(tmp_path)
    install = tmp_path / "Applications/SatelliteTerminalManagerOps.app"
    macos = install / "Contents/MacOS"
    macos.mkdir(parents=True)
    (install / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": "SatelliteTerminalManager",
    }))
    (macos / "SatelliteTerminalManager").write_bytes(b"application")
    request = replace(request, install_dir=install.resolve())
    write_atomic_json(request.request_path, request.to_json())
    health_path = request.transaction_dir / "health-request.json"
    write_atomic_json(health_path, {"schema": HEALTH_SCHEMA, "transaction_id": request.transaction_id,
                                  "target_version": request.target_version, "token": "b" * 64})
    monkeypatch.setenv(HEALTH_ENV, str(health_path))
    monkeypatch.setattr(health, "sys", SimpleNamespace(
        frozen=True, executable=str(macos / executable),
    ))
    if executable == "SatelliteTerminalManager":
        assert startup_health_context(Edition.OPS, "1.9.1").transaction_id == request.transaction_id
    else:
        with pytest.raises(RuntimeError, match="executable"):
            startup_health_context(Edition.OPS, "1.9.1")


def test_health_environment_remains_until_process_receipt_is_durable(tmp_path, monkeypatch):
    from satellite_debug_tool.edition import Edition
    from satellite_debug_tool.updater import health
    request = make_request(tmp_path)
    path = request.transaction_dir / "health-request.json"
    write_atomic_json(path, {"schema": HEALTH_SCHEMA, "transaction_id": request.transaction_id,
                            "target_version": request.target_version, "token": "b" * 64})
    monkeypatch.setenv(HEALTH_ENV, str(path))
    def failed_write(*_args):
        assert os.environ[HEALTH_ENV] == str(path)
        raise OSError("process receipt write failed")
    monkeypatch.setattr(health, "write_atomic_json", failed_write)
    with pytest.raises(OSError):
        startup_health_context(Edition.OPS, "1.9.1")
    assert os.environ[HEALTH_ENV] == str(path)


def test_health_initializes_real_window_without_device_connections_and_waits_for_commit(
    qapplication_session, tmp_path, monkeypatch,
):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.core.comm.udp_endpoint_broker import UdpEndpointBroker
    from satellite_debug_tool.ui.main_window import MainWindow
    monkeypatch.setenv("HOME", str(tmp_path))
    # 初始客户设备的配置租约不得启动采集；若构造路径回归则稳定失败。
    monkeypatch.setattr(UdpEndpointBroker, "start_broker",
                        lambda *_args: pytest.fail("health initialization started device transport"))
    settings = Settings(config_directory=tmp_path / "config")
    settings.set("customer.devices", [{"ip": "127.0.0.1", "port": 49999}])
    settings.set("customer.active_endpoint", {"ip": "127.0.0.1", "port": 49999})
    settings.set("update.auto_check", False)
    window = MainWindow(settings=settings)
    context = StartupHealthContext(tmp_path, "a" * 32, "1.9.1", "b" * 64, None, 10)
    gate = install_startup_health_gate(qapplication_session, window, settings, context)
    window.show()
    qapplication_session.processEvents()
    assert not window.isEnabled()
    assert not window._udp_broker.isRunning()
    assert window._customer_devices.attachment(("127.0.0.1", 49999)) is None
    receipt = json.loads(context.receipt_path.read_text())
    assert receipt["state"] == "ready"
    assert receipt["pid"] == os.getpid()
    commit = context.message("committed")
    write_atomic_json(context.commit_path, commit)
    gate.observe_commit()
    assert window.isEnabled()
    assert window.close()
    qapplication_session.processEvents()


def test_wrong_commit_keeps_window_controls_closed(qapplication_session, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QWidget
    window = QWidget()
    settings = SimpleNamespace(device_configuration_blocked=False)
    exits = []
    monkeypatch.setattr(qapplication_session, "exit", exits.append)
    context = StartupHealthContext(tmp_path, "a" * 32, "1.9.1", "b" * 64, None, 10)
    gate = install_startup_health_gate(qapplication_session, window, settings, context)
    qapplication_session.processEvents()
    message = context.message("committed")
    message["transaction_id"] = "c" * 32
    write_atomic_json(context.commit_path, message)
    gate.observe_commit()
    assert not window.isEnabled()
    assert exits == [1]
    window.close()
