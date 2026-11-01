"""真实归档/文件树加模拟新进程的完整安装与恢复验收。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from satellite_debug_tool.tests._updater_helpers import make_request
from satellite_debug_tool.updater import main as updater_main
from satellite_debug_tool.updater import recovery
from satellite_debug_tool.updater.recovery import InstallJournal
from satellite_debug_tool.updater.status import write_atomic_json


_show_windows_failure = updater_main._show_windows_failure


@pytest.fixture(autouse=True)
def platform_effects(monkeypatch):
    """事务测试观察 OS 意图，提示窗口和登录启动项使用独立的边界测试。"""
    effects = SimpleNamespace(failures=[], registered=[], removed=[])
    monkeypatch.setattr(updater_main, "_show_windows_failure", lambda *args: effects.failures.append(args))
    monkeypatch.setattr(updater_main, "register_login_recovery", lambda *args: effects.registered.append(args))
    for module in (updater_main, recovery):
        monkeypatch.setattr(module, "remove_login_recovery", lambda path: effects.removed.append(path))
    return effects


def assert_failure_reported(effects, request, stage):
    status = json.loads(request.status_path.read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["stage"] == stage
    assert status["detail"]
    assert effects.failures == [(stage, status["detail"], request.log_path)]


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_failure_dialog_presents_stage_detail_and_log_on_windows(monkeypatch, tmp_path, platform):
    import ctypes

    calls = []
    monkeypatch.setattr(updater_main, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(
        MessageBoxW=lambda *args: calls.append(args),
    )), raising=False)
    log_path = tmp_path / "updater.log"
    _show_windows_failure("verifying", "package changed", log_path)
    expected = [(None, f"自动更新失败\n\n阶段：verifying\n原因：package changed\n\n日志：{log_path}",
                 "Satellite Terminal Manager Updater", 0x10)] if platform == "win32" else []
    assert calls == expected


class TrialProcess:
    pid = 987654
    returncode = None
    def __init__(self, executable):
        self.executable = executable
        self.terminated = False
    def poll(self): return self.returncode
    def create_time(self): return 100.0
    def exe(self): return str(self.executable)
    def is_running(self): return self.returncode is None
    def status(self): return "running"
    def terminate(self):
        self.terminated = True
        self.returncode = -1
    def kill(self): self.terminate()
    def wait(self, timeout=5): return self.returncode


def install_trial(monkeypatch, request, mode="ready", on_start=None):
    import psutil
    trial = TrialProcess(request.install_dir / "SatelliteTerminalManager.exe")
    monkeypatch.setattr(updater_main, "wait_for_pid", lambda *_args: True)
    monkeypatch.setattr(psutil, "Process", lambda _pid: trial)
    monkeypatch.setattr(psutil, "process_iter", lambda **_kwargs: [])
    def start(_request, context):
        write_atomic_json(context.directory / "health-request.json", {
            "schema": "satellite.update-health/v1", "transaction_id": context.transaction_id,
            "target_version": context.target_version, "token": context.token,
        })
        if on_start is not None:
            on_start()
        if mode == "exit":
            trial.returncode = 1
        elif mode != "timeout":
            message = context.message("ready")
            message["pid"] = trial.pid
            if mode == "wrong_token":
                message["token"] = "different"
            if mode == "wrong_version":
                message["target_version"] = "0.0.1"
            if mode == "wrong_pid":
                message["pid"] += 1
            write_atomic_json(context.receipt_path, message)
        return trial
    monkeypatch.setattr(updater_main, "_start_trial", start)
    return trial


def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        updater_main.main(["--help"])
    assert exc.value.code == 0


def test_wait_pid_requires_real_exit():
    assert updater_main.wait_for_pid(2**30, timeout=0.1)


def test_trial_launch_uses_maintenance_cwd(tmp_path, monkeypatch):
    from satellite_debug_tool.updater.health import StartupHealthContext
    request = make_request(tmp_path)
    monkeypatch.chdir(request.install_dir)
    context = StartupHealthContext(request.transaction_dir, request.transaction_id,
                                   request.target_version, "b" * 64, request.settings_directory, 10)
    calls = []
    monkeypatch.setattr(updater_main.subprocess, "Popen", lambda command, **options: calls.append((command, options)))
    updater_main._start_trial(request, context)
    assert calls[0][0] == [str(request.install_dir / "SatelliteTerminalManager.exe")]
    assert calls[0][1]["cwd"] == str(request.transaction_dir)


def test_recovery_cli_releases_installation_cwd_before_rename(tmp_path, monkeypatch, platform_effects):
    from types import SimpleNamespace
    from satellite_debug_tool.tests.test_updater_applier import prepare
    from satellite_debug_tool.updater.applier import Applier
    install, _, _, journal = prepare(tmp_path)
    Applier().install_prepared(journal)
    monkeypatch.chdir(install)
    monkeypatch.setattr(updater_main, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(updater_main, "_setup_logging", lambda *_args: None)
    rename = Path.rename
    observed = []
    def checked_rename(path, destination):
        observed.append(Path.cwd())
        assert Path.cwd() == journal.path.parent
        return rename(path, destination)
    monkeypatch.setattr(Path, "rename", checked_rename)
    assert updater_main.main(["--recover", str(journal.path)]) == 0
    assert len(observed) == 2
    assert (install / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"
    assert platform_effects.failures == []
    assert platform_effects.registered == []
    assert platform_effects.removed == [install]


def test_recovery_cli_reports_failure_once(tmp_path, monkeypatch, platform_effects):
    detail = "persistent recovery journal cannot be read"

    def fail_recovery(_path):
        raise recovery.RecoveryError(detail)

    monkeypatch.setattr(updater_main, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(updater_main, "_setup_logging", lambda *_args: None)
    monkeypatch.setattr(updater_main, "recover_installation", fail_recovery)
    assert updater_main.main(["--recover", str(tmp_path / "journal.json")]) == 3
    assert platform_effects.failures == [("recovery", detail, tmp_path / "updater.log")]
    assert platform_effects.registered == []
    assert platform_effects.removed == []


def test_other_installed_process_prevents_configuration_snapshot(tmp_path, monkeypatch, platform_effects):
    import psutil
    from types import SimpleNamespace
    request = make_request(tmp_path)
    install_trial(monkeypatch, request)
    monkeypatch.setattr(psutil, "process_iter", lambda **_kwargs: [SimpleNamespace(
        info={"pid": 456, "exe": str(request.install_dir / "SatelliteTerminalManager.exe")})])
    assert updater_main.execute_update(request) == 3
    assert not (request.transaction_dir / "configuration-backup").exists()
    assert (request.install_dir / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"
    assert_failure_reported(platform_effects, request, "waiting_for_app_exit")
    assert "PID 456" in platform_effects.failures[0][1]
    assert platform_effects.registered == []
    assert platform_effects.removed == []


def test_wait_timeout_keeps_installation(tmp_path, monkeypatch, platform_effects):
    request = make_request(tmp_path)
    monkeypatch.setattr(updater_main, "wait_for_pid", lambda *_args: False)
    assert updater_main.execute_update(request) == 2
    status = json.loads(request.status_path.read_text())
    assert status["state"] == "failed"
    assert status["stage"] == "waiting_for_app_exit"
    assert (request.install_dir / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"
    assert platform_effects.failures == []
    assert platform_effects.registered == []
    assert platform_effects.removed == []


@pytest.mark.parametrize("prepared", [False, True])
def test_complete_and_prepared_tree_share_health_transaction(tmp_path, monkeypatch, prepared, platform_effects):
    request = make_request(tmp_path, prepared=prepared)
    trial = install_trial(monkeypatch, request)
    assert updater_main.execute_update(request, health_timeout=0.2) == 0
    assert (request.install_dir / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.1"
    journal = InstallJournal.load(request.transaction_dir / "journal.json")
    assert journal.data["state"] == "committed"
    assert (journal.backup_dir / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"
    status = json.loads(request.status_path.read_text())
    commit = json.loads((request.transaction_dir / "health-commit.json").read_text())
    assert status["state"] == "completed"
    assert commit["pid"] == trial.pid
    assert commit["transaction_id"] == request.transaction_id
    assert not trial.terminated
    assert platform_effects.failures == []
    assert platform_effects.registered == [(request.updater_executable,
                                            request.transaction_dir / "journal.json", request.install_dir)]
    assert platform_effects.removed == [request.install_dir]


@pytest.mark.parametrize("mode", ["exit", "timeout", "wrong_token", "wrong_pid", "wrong_version"])
def test_spawn_and_invalid_receipts_cannot_mark_success(tmp_path, monkeypatch, mode, platform_effects):
    """旧实现 Popen 成功立即 completed；这些已启动的新进程都必须回退。"""
    request = make_request(tmp_path)
    trial = install_trial(monkeypatch, request, mode)
    assert updater_main.execute_update(request, health_timeout=0.02) == 3
    assert (request.install_dir / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"
    status = json.loads(request.status_path.read_text())
    assert status["state"] == "failed"
    assert status["recovery"] == "rolled_back"
    assert not (request.transaction_dir / "health-commit.json").exists()
    assert trial.terminated or trial.returncode is not None
    assert_failure_reported(platform_effects, request, "waiting_for_health")
    assert len(platform_effects.registered) == 1
    assert platform_effects.removed == [request.install_dir]


def test_recovery_registration_failure_precedes_old_directory_move(tmp_path, monkeypatch, platform_effects):
    request = make_request(tmp_path)
    install_trial(monkeypatch, request)
    def failure(*_args):
        raise OSError("login recovery registration refused")
    monkeypatch.setattr(updater_main, "register_login_recovery", failure)
    assert updater_main.execute_update(request) == 3
    assert (request.install_dir / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"
    assert not list(tmp_path.glob("install.bak.*"))
    assert_failure_reported(platform_effects, request, "registering_recovery")
    assert platform_effects.failures[0][1] == "login recovery registration refused"
    assert platform_effects.registered == []
    assert platform_effects.removed == [request.install_dir]


@pytest.mark.parametrize("target", ["package", "manifest", "prepared_tree"])
def test_inputs_reverified_after_download_and_handoff(tmp_path, monkeypatch, target, platform_effects):
    request = make_request(tmp_path, prepared=target == "prepared_tree")
    install_trial(monkeypatch, request)
    if target == "package":
        request.volumes[0].write_bytes(b"changed package")
    elif target == "manifest":
        request.tree_manifest.write_bytes(b"changed manifest")
    else:
        (request.prepared_payload / "SatelliteTerminalManager.exe").write_bytes(b"changed tree")
    assert updater_main.execute_update(request) == 3
    assert (request.install_dir / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"
    assert not list(tmp_path.glob("install.bak.*"))
    assert_failure_reported(platform_effects, request, "preparing" if target == "prepared_tree" else "verifying")
    assert platform_effects.registered == []
    assert platform_effects.removed == []


def test_trial_configuration_rollback_preserves_recordings(tmp_path, monkeypatch, platform_effects):
    request = make_request(tmp_path)
    recording = request.settings_directory / "important.sdb"
    recording.write_bytes(b"recorded-data")
    original = (request.settings_directory / "settings.json").read_bytes()
    def migrate():
        (request.settings_directory / "settings.json").write_text('{"schema":2}')
        (request.settings_directory / "settings.json.bak").write_text("new backup")
    install_trial(monkeypatch, request, "exit", migrate)
    assert updater_main.execute_update(request) == 3
    assert (request.settings_directory / "settings.json").read_bytes() == original
    assert not (request.settings_directory / "settings.json.bak").exists()
    assert recording.read_bytes() == b"recorded-data"
    assert_failure_reported(platform_effects, request, "waiting_for_health")
    assert len(platform_effects.registered) == 1
    assert platform_effects.removed == [request.install_dir]
    journal = InstallJournal.load(request.transaction_dir / "journal.json")
    (request.settings_directory / "settings.json").write_text('{"user_changed_after_recovery":true}')
    journal.rollback()
    assert "user_changed_after_recovery" in (request.settings_directory / "settings.json").read_text()
    assert platform_effects.removed == [request.install_dir, request.install_dir]


def test_trial_settings_restored_when_process_identity_lookup_fails(tmp_path, monkeypatch, platform_effects):
    import psutil
    request = make_request(tmp_path)
    settings_file = request.settings_directory / "settings.json"
    original = settings_file.read_bytes()
    trial = install_trial(monkeypatch, request, on_start=lambda: settings_file.write_text('{"schema":2}'))
    def lookup_failure(_pid):
        raise psutil.AccessDenied(trial.pid)
    monkeypatch.setattr(psutil, "Process", lookup_failure)
    assert updater_main.execute_update(request) == 3
    assert trial.terminated
    assert settings_file.read_bytes() == original
    assert (request.install_dir / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"
    assert_failure_reported(platform_effects, request, "waiting_for_health")
    assert len(platform_effects.registered) == 1
    assert platform_effects.removed == [request.install_dir]


@pytest.mark.parametrize("binding", ["environment", "receipt", "unknown", "denied", "unresolved_exe"])
def test_recovery_confirms_trial_exit_when_updater_dies_before_pid_journal(
    tmp_path, monkeypatch, binding, platform_effects,
):
    import psutil
    from satellite_debug_tool.updater.health import HEALTH_ENV
    from satellite_debug_tool.updater.recovery import recover_installation, RecoveryError
    request = make_request(tmp_path)
    settings_file = request.settings_directory / "settings.json"
    original_settings = settings_file.read_bytes()
    trial = install_trial(monkeypatch, request, on_start=lambda: settings_file.write_text('{"schema":2}'))
    trial.info = {"pid": trial.pid, "exe": None if binding == "unresolved_exe" else trial.exe(),
                  "name": "SatelliteTerminalManager.exe"}
    if binding == "denied":
        def denied(): raise psutil.AccessDenied(trial.pid)
        trial.environ = denied
    else:
        trial.environ = lambda: ({HEALTH_ENV: str(request.transaction_dir / "health-request.json")}
                                  if binding == "environment" else {})
    monkeypatch.setattr(psutil, "process_iter", lambda **_kwargs: [trial] if trial.poll() is None else [])
    # 初次事务仍需以原应用已退出为前提；仅在启动新版后让扫描观察到该进程。
    monkeypatch.setattr(updater_main, "require_installation_idle", lambda *_args: None)
    original_start = updater_main._start_trial
    class UpdaterPowerLoss(BaseException): pass
    def interrupted_start(request, context):
        original_start(request, context)
        if binding == "receipt":
            started = context.message("started")
            started.update(pid=trial.pid, created=trial.create_time(), executable=trial.exe())
            write_atomic_json(context.directory / "health-process.json", started)
        raise UpdaterPowerLoss()
    monkeypatch.setattr(updater_main, "_start_trial", interrupted_start)
    with pytest.raises(UpdaterPowerLoss):
        updater_main.execute_update(request)
    journal_path = request.transaction_dir / "journal.json"
    assert "trial_pid" not in InstallJournal.load(journal_path).data
    rename = Path.rename
    renamed = []
    def track_rename(path, destination):
        renamed.append(path)
        return rename(path, destination)
    monkeypatch.setattr(Path, "rename", track_rename)
    if binding in {"environment", "receipt"}:
        assert recover_installation(journal_path) == "rolled_back"
        assert trial.terminated
        assert len(renamed) == 2
        assert settings_file.read_bytes() == original_settings
    else:
        with pytest.raises(RecoveryError):
            recover_installation(journal_path)
        assert not trial.terminated
        assert renamed == []
        assert settings_file.read_text() == '{"schema":2}'
        assert (request.install_dir / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.1"
    assert platform_effects.failures == []
    assert len(platform_effects.registered) == 1
    assert platform_effects.removed == ([request.install_dir] if binding in {"environment", "receipt"} else [])


def test_failed_install_keeps_lock_through_rollback(tmp_path, monkeypatch, platform_effects):
    from satellite_debug_tool.updater.recovery import InstallationLock, RecoveryError
    request = make_request(tmp_path)
    install_trial(monkeypatch, request, "exit")
    original = InstallJournal.rollback
    observed = []
    def rollback(self):
        with pytest.raises(RecoveryError):
            with InstallationLock(request.install_dir):
                pytest.fail("another updater entered before recovery")
        observed.append(True)
        return original(self)
    monkeypatch.setattr(InstallJournal, "rollback", rollback)
    assert updater_main.execute_update(request) == 3
    assert observed
    assert_failure_reported(platform_effects, request, "waiting_for_health")
    assert len(platform_effects.registered) == 1
    assert platform_effects.removed == [request.install_dir]


@pytest.mark.parametrize("alive", [True, False])
def test_recovery_after_committed_journal_before_commit_delivery(tmp_path, monkeypatch, alive, platform_effects):
    import psutil
    from satellite_debug_tool.updater.recovery import recover_installation
    request = make_request(tmp_path)
    trial = install_trial(monkeypatch, request)
    assert updater_main.execute_update(request) == 0
    commit = request.transaction_dir / "health-commit.json"
    commit.unlink()
    if not alive:
        def absent(_pid): raise psutil.NoSuchProcess(trial.pid)
        monkeypatch.setattr(psutil, "Process", absent)
    assert recover_installation(request.transaction_dir / "journal.json") == "committed"
    journal = InstallJournal.load(request.transaction_dir / "journal.json")
    assert journal.data["application_phase"] == ("ready" if alive else "closed")
    assert commit.exists() is alive
    assert (request.install_dir / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.1"
    assert platform_effects.failures == []
    assert len(platform_effects.registered) == 1
    assert platform_effects.removed == [request.install_dir, request.install_dir]
