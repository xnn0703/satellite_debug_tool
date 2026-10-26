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


def test_release_editions_select_exact_assets_and_old_full_selects_operations():
    names = ["satellite_terminal_tool-user-windows-v1.8.0.7z", "satellite_debug_tool-win-v1.8.0.7z",
             "other-win-v1.8.0.7z", "satellite_debug_tool-win-v1.9.0.7z"]
    release = LatestRelease("v1.8.0", assets=[Asset(name, "https://example.test/" + name) for name in names])
    assert [a.name for a in release.assets_for_platform("win", edition=Edition.USER)] == names[:1]
    assert [a.name for a in release.assets_for_platform("win", edition=Edition.OPS)] == names[1:2]
    # 实际发布只放两种正式资产；旧全量更新器的平台正则只能命中 ops。
    assert [n for n in names[:2] if re.search(r"-win-.*\.7z$", n)] == names[1:2]
    assert release.assets_for_platform("mac", edition=Edition.USER) == []


@pytest.mark.parametrize("edition", list(Edition))
def test_edition_payload_cross_install_is_rejected_before_swap(tmp_path, edition):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "SatelliteDebugTool.exe").write_bytes(b"exe")
    (staging / "updater.exe").write_bytes(b"updater")
    identity(staging, "ops" if edition is Edition.USER else "user")
    contract = PayloadContract("SatelliteDebugTool.exe", "updater.exe", edition=edition.value, target_version="v1.8.0")
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
    with pytest.raises(ApplyError, match="identity"):
        Applier().apply([], installed, tmp_path / "work",
                        payload_contract=PayloadContract("SatelliteDebugTool.exe", "updater.exe", edition="ops", target_version="1.8.0"))
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
    window = UserWindow(settings)
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
