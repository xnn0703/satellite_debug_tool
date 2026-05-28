"""SettingsDialog 单测：弹窗读写 paths 的行为，以及各 view 接入 paths 默认目录。

不实际弹窗，只直接调用 SettingsDialog 的内部状态做行为校验。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    """构造一个隔离的 Settings 实例（用 tmp 目录代替 ~/.satellite_debug_tool）。

    用 HOME 环境变量重定向，比 monkeypatch Path.home 更稳（Path.home() 内部
    会读 os.environ["HOME"]，而直接 monkeypatch Path 类方法在某些跑测序顺序下
    被其他测试导入的 Path 实例绕过）。
    """
    from satellite_debug_tool.core.config import Settings
    monkeypatch.setenv("HOME", str(tmp_path))
    s = Settings()
    return s


class TestPathsSchema:
    def test_default_paths_section_exists(self, tmp_settings):
        """DEFAULT_CONFIG 含 paths section，默认 3 个 key 是空串。"""
        s = tmp_settings
        assert s.get("paths.recording_dir") == ""
        assert s.get("paths.log_dir") == ""
        assert s.get("paths.firmware_dir") == ""

    def test_set_and_persist(self, tmp_settings, tmp_path):
        """set + save 后能从 settings.json 读回。"""
        s = tmp_settings
        s.set("paths.recording_dir", "/data/rec")
        s.set("paths.log_dir", "/data/log")
        s.set("paths.firmware_dir", "/fw")
        s.save()
        with open(tmp_path / ".satellite_debug_tool" / "settings.json", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk["paths"]["recording_dir"] == "/data/rec"
        assert on_disk["paths"]["log_dir"] == "/data/log"
        assert on_disk["paths"]["firmware_dir"] == "/fw"


class TestSettingsDialog:
    def test_init_loads_current_values(self, qapp, tmp_settings):
        """弹窗打开时 LineEdit 显示 settings 当前值。"""
        from satellite_debug_tool.ui.settings_dialog import SettingsDialog
        tmp_settings.set("paths.recording_dir", "/rec")
        tmp_settings.set("paths.log_dir", "/log")
        tmp_settings.set("paths.firmware_dir", "/fw")
        dlg = SettingsDialog(tmp_settings)
        assert dlg._recording_edit.text() == "/rec"
        assert dlg._log_edit.text() == "/log"
        assert dlg._firmware_edit.text() == "/fw"
        dlg.deleteLater()

    def test_accept_writes_to_settings(self, qapp, tmp_settings):
        """点确定（_on_accept）把 LineEdit 值写入 settings 并 save。"""
        from satellite_debug_tool.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(tmp_settings)
        dlg._recording_edit.setText("/new/rec")
        dlg._log_edit.setText("/new/log")
        dlg._firmware_edit.setText("/new/fw")
        dlg._on_accept()
        assert tmp_settings.get("paths.recording_dir") == "/new/rec"
        assert tmp_settings.get("paths.log_dir") == "/new/log"
        assert tmp_settings.get("paths.firmware_dir") == "/new/fw"
        dlg.deleteLater()

    def test_accept_strips_whitespace(self, qapp, tmp_settings):
        """LineEdit 首尾空格被 strip 掉。"""
        from satellite_debug_tool.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(tmp_settings)
        dlg._recording_edit.setText("  /path/with/spaces  ")
        dlg._on_accept()
        assert tmp_settings.get("paths.recording_dir") == "/path/with/spaces"
        dlg.deleteLater()

    def test_reject_does_not_write(self, qapp, tmp_settings):
        """点取消（reject）不修改 settings。"""
        from satellite_debug_tool.ui.settings_dialog import SettingsDialog
        tmp_settings.set("paths.recording_dir", "/original")
        dlg = SettingsDialog(tmp_settings)
        dlg._recording_edit.setText("/changed_but_not_saved")
        dlg.reject()
        # _on_accept 没被调用，settings 仍为原值
        assert tmp_settings.get("paths.recording_dir") == "/original"
        dlg.deleteLater()


class TestViewsAcceptSettings:
    """验证 4 个 view 都能接受 settings 入参（不报错），且 None 时也能工作。"""

    def test_playback_view_with_settings(self, qapp, tmp_settings):
        from satellite_debug_tool.ui.playback_view import PlaybackView
        pv = PlaybackView(settings=tmp_settings)
        assert pv._settings is tmp_settings
        pv.deleteLater()

    def test_playback_view_without_settings(self, qapp):
        from satellite_debug_tool.ui.playback_view import PlaybackView
        pv = PlaybackView()
        assert pv._settings is None
        pv.deleteLater()

    def test_log_view_with_settings(self, qapp, tmp_settings):
        from satellite_debug_tool.ui.log_view import LogView
        lv = LogView(settings=tmp_settings)
        assert lv._settings is tmp_settings
        lv.deleteLater()

    def test_device_view_with_settings(self, qapp, tmp_settings):
        from satellite_debug_tool.ui.device_view import DeviceView
        dv = DeviceView(settings=tmp_settings)
        assert dv._settings is tmp_settings
        dv.deleteLater()
