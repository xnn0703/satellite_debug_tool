"""用户/运维装配、更新身份与历史全量迁移边界。"""

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from satellite_debug_tool.edition import Edition, validate_payload_identity
from satellite_debug_tool.updater.applier import Applier, ApplyError, PayloadContract
from satellite_debug_tool.updater.checker import Asset, LatestRelease


def identity(root, edition, version="1.8.0"):
    path = root / "_internal/satellite_debug_tool/build_identity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"edition": edition, "version": version, "platform": "win"}))


def test_release_editions_use_trusted_identity_and_user_has_manual_delivery():
    release = LatestRelease("1.8.0", assets=[Asset("ops.7z", "https://example.test/ops")],
        identity={"app_id": "satellite-terminal-tool", "edition": "ops", "platform": "windows",
                  "architecture": "x86_64", "version": "1.8.0", "build_id": "abc"})
    assert release.assets_for_platform("win", edition=Edition.USER) == []
    assert [a.name for a in release.assets_for_platform("win", edition=Edition.OPS)] == ["ops.7z"]
    assert release.assets_for_platform("mac", edition=Edition.USER) == []


@pytest.mark.parametrize("edition", list(Edition))
def test_edition_payload_cross_install_is_rejected_before_swap(tmp_path, edition):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "SatelliteTerminalManager.exe").write_bytes(b"exe")
    (staging / "updater.exe").write_bytes(b"updater")
    identity(staging, "ops" if edition is Edition.USER else "user")
    contract = PayloadContract("SatelliteTerminalManager.exe", "updater.exe", edition=edition.value, target_version="v1.8.0")
    with pytest.raises(ApplyError, match="identity"):
        Applier().resolve_payload_root(staging, contract)
    identity(staging, edition.value)
    assert Applier().resolve_payload_root(staging, contract) == staging.resolve()


def test_only_legacy_full_payload_through_170_can_omit_identity(tmp_path):
    validate_payload_identity(tmp_path, Edition.OPS, "v1.7.0")
    for edition, version in ((Edition.USER, "v1.7.0"), (Edition.OPS, "v1.8.0")):
        with pytest.raises(ValueError):
            validate_payload_identity(tmp_path, edition, version)


def test_windows_update_rejects_other_platform_and_incomplete_identity(tmp_path):
    identity(tmp_path, "user")
    path = tmp_path / "_internal/satellite_debug_tool/build_identity.json"
    for data in ({"edition": "user", "version": "1.8.0", "platform": "mac"}, {"edition": "user"}):
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError):
            validate_payload_identity(tmp_path, Edition.USER, "v1.8.0")


def test_update_request_cannot_override_installed_identity(tmp_path):
    installed = tmp_path / "installed"
    identity(installed, "user")
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ApplyError, match="identity"):
        Applier().prepare_payload(installed, source, "a" * 32, tree_manifest={},
                        contract=PayloadContract("SatelliteTerminalManager.exe", "updater.exe", edition="ops", target_version="1.8.0"))
    assert not (tmp_path / "work").exists()


def test_user_settings_are_separate_and_operations_settings_are_preserved(tmp_path, monkeypatch):
    from satellite_debug_tool.core.config import Settings
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ops = Settings(edition=Edition.OPS)
    ops.set("production.last_operator", "operator")
    ops.save()
    saved = ops._config_file.read_bytes()
    user = Settings(edition=Edition.USER)
    assert Path(user.config_directory) == Path(ops.config_directory) / "user"
    user.set("ui.theme", "light")
    user.save()
    assert ops._config_file.read_bytes() == saved


def test_user_runtime_has_four_pages_and_zero_operations_modules(tmp_path):
    script = r'''
import sys
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.edition import Edition
from satellite_debug_tool.ui.user_window import UserWindow
app = QApplication([])
with patch('pathlib.Path.home', return_value=Path(sys.argv[1])):
    settings = Settings(edition=Edition.USER)
    settings.set('customer.devices', [{'ip':'127.0.0.1','port':4004}])
    settings.set('customer.active_endpoint', {'ip':'127.0.0.1','port':4004})
    settings.set("ui.active_tab_id", "device")
    settings.set("ui.edition", "ops")
    settings.set("update.auto_check", True)
    window = UserWindow(settings)
    assert not hasattr(window, '_update_button')
    window._on_check_update_clicked()
    window._kick_silent_update_check()
    window._on_silent_check_found_new(object())
    window._pending_update_request = object()
    window._launch_pending_update()
    assert window._pending_update_request is None
    assert window._bg_check_thread is None
    window.retranslate_ui()
    from satellite_debug_tool import __version__
    assert window._version_label.text() == f'v{__version__}'
    assert not any(name.startswith(('satellite_debug_tool.updater', 'satellite_debug_tool.release_config',
                                   'satellite_debug_tool.ui.update_dialog')) for name in sys.modules)
    assert window._customer._page_ids == ['overview','rf','playback','maintenance']
    assert window._customer._fixture_button is None
    assert window._customer._tcp_shell_dialog is None
    assert window._customer._tcp_shell_button.isEnabled()
    assert 'satellite_debug_tool.core.tcp_shell.worker' not in sys.modules
    shell = window._customer.open_tcp_shell_dialog()
    assert window._customer.open_tcp_shell_dialog() is shell
    assert shell.workspace._controller._device_directory is window._customer_devices
    assert shell.workspace._controller._session_directory is window._endpoint_directory
    assert shell.workspace._controller.worker is None
    assert shell.workspace._type_combo.count() == 2
    shell.workspace._type_combo.setCurrentIndex(1)
    assert shell.workspace._config_stack.currentIndex() == 1
    assert shell.workspace._baud_combo.currentText() == '115200'
    assert shell.workspace._ymodem_box.isEnabled()
    assert shell.workspace._controller._session_root == Path(settings.config_directory) / 'tcp_shell_sessions'
    assert shell.close()
    app.processEvents()
    assert window._customer._tcp_shell_dialog is None
    reopened = window._customer.open_tcp_shell_dialog()
    assert reopened is not shell
    for page in window._customer._page_ids:
        window._customer.set_page(page)
        app.processEvents()
    bundle = window._customer_bundle_factory.bundle(('127.0.0.1',4004))
    assert bundle._power_monitor is None and bundle._iperf_controller is None
    overview = bundle._overview
    assert overview._root_layout.indexOf(overview._signal_panel) >= 0
    assert overview._root_layout.indexOf(overview._component_panel) == overview._root_layout.count() - 1
    assert set(overview._component_widgets) == {'converter', 'tx_array', 'rx_array'}
    assert bundle._maintenance._ota_status.text() == '空闲'
    assert bundle._maintenance._mount_controller is None
    assert not hasattr(bundle._maintenance, '_mount_preview')
    forbidden = ('ui.main_window','ui.production_workspace','ui.device_view','core.external_power_monitor',
                 'core.iperf_test','core.motion_platform','core.production.reporting')
    loaded = [n for n in sys.modules if n.startswith('satellite_debug_tool.')]
    assert not any(n.startswith('satellite_debug_tool.'+key) for n in loaded for key in forbidden)
    for action in (window._customer.open_fixture_dialog,bundle.tracking_view,bundle.live.ensure_presentation):
        try: action()
        except RuntimeError: pass
        else: raise AssertionError('operations entry opened in user edition')
    assert window.close()
    assert window._customer._tcp_shell_dialog is None
    assert reopened.workspace._controller.worker is None
    app.processEvents()
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_user_entry_rejects_operations_cli():
    result = subprocess.run([sys.executable, "-m", "satellite_debug_tool.main_user", "--production"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "customer features only" in result.stderr


def test_user_frozen_contract_requires_shell_and_rejects_operations(tmp_path):
    from scripts.edition_policy import USER_SHELL_MODULES
    from scripts.verify_edition_package import verify

    identity(tmp_path, "user")
    toc = tmp_path / "Analysis-00.toc"
    names = sorted(USER_SHELL_MODULES)
    def write_toc(modules):
        toc.write_text(repr([(name, "/source.py", "PYMODULE") for name in modules]))

    write_toc(names[1:])
    with pytest.raises(ValueError, match="requires Shell"):
        verify(tmp_path, "user", toc)
    write_toc(names)
    verify(tmp_path, "user", toc)
    write_toc(names + ["satellite_debug_tool.core.motion_platform.controller"])
    with pytest.raises(ValueError, match="operations modules"):
        verify(tmp_path, "user", toc)


def test_update_policy_belongs_to_build_identity(monkeypatch):
    from types import SimpleNamespace
    from satellite_debug_tool import edition

    user = SimpleNamespace(edition=Edition.USER)
    ops = SimpleNamespace(edition=Edition.OPS)
    assert edition.application_update_policy(user) is edition.ApplicationUpdatePolicy.MANUAL
    assert edition.application_update_policy(ops) is edition.ApplicationUpdatePolicy.ONLINE
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(edition, "build_edition", lambda: Edition.USER)
    assert edition.edition_of(ops) is Edition.USER
    assert not edition.application_online_update_enabled(ops)
    monkeypatch.setattr(edition, "build_edition", lambda: Edition.OPS)
    assert edition.edition_of(user) is Edition.OPS
    assert edition.application_online_update_enabled(user)


@pytest.mark.parametrize("module", ["satellite_debug_tool.updater.checker", "satellite_debug_tool.release_config",
                                   "satellite_debug_tool.ui.update_dialog", "tuf.ngclient", "securesystemslib.signer"])
def test_user_frozen_contract_rejects_application_update_modules(tmp_path, module):
    from scripts.edition_policy import USER_SHELL_MODULES
    from scripts.verify_edition_package import verify

    identity(tmp_path, "user")
    toc = tmp_path / "Analysis-00.toc"
    toc.write_text(repr([(name, "/source.py", "PYMODULE") for name in (*USER_SHELL_MODULES, module)]))
    with pytest.raises(ValueError, match="operations modules"):
        verify(tmp_path, "user", toc)


@pytest.mark.parametrize("name", ["updater.exe", "updater", "release.config.json"])
def test_user_frozen_contract_rejects_application_update_files(tmp_path, name):
    from scripts.verify_edition_package import verify

    identity(tmp_path, "user")
    (tmp_path / name).write_bytes(b"update")
    with pytest.raises(ValueError, match="application update files"):
        verify(tmp_path, "user", None)


def test_build_identity_records_exact_source_and_normalized_architecture():
    from satellite_debug_tool.edition import create_build_identity

    value = create_build_identity(edition=Edition.USER, version="1.8.1", platform="win",
                                  architecture="AMD64", build_id="a" * 40)
    assert value == {"app_id": "satellite-terminal-tool", "edition": "user", "version": "1.8.1",
                     "platform": "win", "architecture": "x86_64", "build_id": "a" * 40}
    with pytest.raises(ValueError, match="exact source commit"):
        create_build_identity(edition=Edition.OPS, version="1.8.1", platform="win",
                              architecture="x86_64", build_id="main")


@pytest.mark.parametrize("edition", list(Edition))
def test_update_settings_follow_fixed_edition_policy(qapplication_session, tmp_path, edition):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.ui.settings_dialog import SettingsDialog, SETTINGS_SCOPE_CUSTOMER

    settings = Settings(edition=edition, config_directory=tmp_path)
    settings.set("update.auto_check", True)
    settings.set("update.check_interval_hours", 24)
    settings.set("update.skip_version", "1.8.2")
    dialog = SettingsDialog(settings, scope=SETTINGS_SCOPE_CUSTOMER)
    dialog.show()
    assert dialog._update_section.isVisible() is (edition is Edition.OPS)
    dialog.retranslate_ui()
    assert dialog._update_section.isVisible() is (edition is Edition.OPS)
    dialog._cb_auto_check.setChecked(False)
    dialog._spin_interval.setValue(48)
    dialog._on_reset_skip_version()
    dialog._on_accept()
    if edition is Edition.USER:
        assert settings.get("update.auto_check") is True
        assert settings.get("update.check_interval_hours") == 24
        assert settings.get("update.skip_version") == "1.8.2"
    else:
        assert settings.get("update.auto_check") is False
        assert settings.get("update.check_interval_hours") == 48
        assert settings.get("update.skip_version") == ""


def test_pending_update_validation_failure_keeps_window_owner_and_records_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from satellite_debug_tool.ui.application_window_actions import ApplicationWindowActions

    request = SimpleNamespace(status_path=tmp_path / "status.json", target_version="1.8.2",
                              install_dir=tmp_path / "install", log_path=tmp_path / "updater.log")
    owner = ApplicationWindowActions()
    owner._settings = SimpleNamespace(edition=Edition.OPS)
    owner._pending_update_request = request
    def fail(value):
        raise ValueError("package changed before handoff")
    monkeypatch.setattr("satellite_debug_tool.updater.handoff.launch_updater", fail)
    shown = []
    monkeypatch.setattr("satellite_debug_tool.ui.application_window_actions.QMessageBox.critical",
                        lambda *arguments: shown.append(arguments[-1]))
    assert owner._launch_pending_update() is False
    assert owner._pending_update_request is None
    proof = json.loads(request.status_path.read_text())
    assert proof["state"] == "failed" and proof["stage"] == "launching_updater"
    assert "package changed" in proof["detail"] and "package changed" in shown[0]


def test_background_shutdown_cancels_network_owner_and_retains_running_thread():
    from types import SimpleNamespace
    from satellite_debug_tool.ui.application_window_actions import ApplicationWindowActions

    calls = []
    thread = SimpleNamespace(update_checker=SimpleNamespace(cancel=lambda: calls.append("cancel")),
        isRunning=lambda: True, requestInterruption=lambda: calls.append("interrupt"),
        quit=lambda: calls.append("quit"), wait=lambda timeout: False)
    owner = ApplicationWindowActions()
    owner._bg_check_thread = thread
    assert owner._shutdown_background_update_check(1) is False
    assert calls == ["cancel", "interrupt", "quit"]
    assert owner._bg_check_thread is thread


def test_user_bootstrap_ignores_operations_health_transaction(tmp_path):
    import os
    destination = tmp_path / "smoke.json"
    env = dict(os.environ, SATELLITE_UPDATE_HEALTH_REQUEST="foreign-operations-health-context")
    result = subprocess.run([sys.executable, "-m", "satellite_debug_tool.main_user", "--smoke-test",
                             f"--smoke-output={destination}"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    proof = json.loads(destination.read_text())
    assert proof["application_update_policy"] == "manual"
    assert proof["application_update_modules"] == []
