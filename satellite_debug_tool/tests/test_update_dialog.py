"""UpdateDialog 单测：should_check_in_background 策略 + 弹窗构造 + 设置往返。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

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
    monkeypatch.setenv("HOME", str(tmp_path))
    from satellite_debug_tool.core.config import Settings
    return Settings()


# ============================ should_check_in_background ============================


class TestShouldCheck:
    def test_disabled_when_auto_check_false(self, tmp_settings, monkeypatch):
        from satellite_debug_tool.ui.update_dialog import should_check_in_background
        monkeypatch.setenv("SATELLITE_UPDATE_CHECK", "1")
        tmp_settings.set("update.auto_check", False)
        assert should_check_in_background(tmp_settings) is False

    def test_disabled_when_environment_state_is_zero(self, tmp_settings, monkeypatch):
        from satellite_debug_tool.ui.update_dialog import should_check_in_background
        monkeypatch.setenv("SATELLITE_UPDATE_CHECK", "0")
        tmp_settings.set("update.auto_check", True)
        assert should_check_in_background(tmp_settings) is False

    def test_enabled_first_time(self, tmp_settings, monkeypatch):
        from satellite_debug_tool.ui.update_dialog import should_check_in_background
        monkeypatch.setenv("SATELLITE_UPDATE_CHECK", "1")
        tmp_settings.set("update.auto_check", True)
        tmp_settings.set("update.last_check_iso", "")
        assert should_check_in_background(tmp_settings) is True

    def test_disabled_within_interval(self, tmp_settings, monkeypatch):
        from satellite_debug_tool.ui.update_dialog import should_check_in_background
        monkeypatch.setenv("SATELLITE_UPDATE_CHECK", "1")
        tmp_settings.set("update.auto_check", True)
        tmp_settings.set("update.check_interval_hours", 24)
        # 1 小时前刚查过 → 24h 间隔内不查
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        tmp_settings.set("update.last_check_iso", recent)
        assert should_check_in_background(tmp_settings) is False

    def test_enabled_after_interval(self, tmp_settings, monkeypatch):
        from satellite_debug_tool.ui.update_dialog import should_check_in_background
        monkeypatch.setenv("SATELLITE_UPDATE_CHECK", "1")
        tmp_settings.set("update.auto_check", True)
        tmp_settings.set("update.check_interval_hours", 24)
        long_ago = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        tmp_settings.set("update.last_check_iso", long_ago)
        assert should_check_in_background(tmp_settings) is True

    def test_legacy_naive_timestamp_is_interpreted_as_utc(
        self,
        tmp_settings,
        monkeypatch,
    ):
        from satellite_debug_tool.ui.update_dialog import should_check_in_background

        monkeypatch.setenv("SATELLITE_UPDATE_CHECK", "1")
        tmp_settings.set("update.auto_check", True)
        tmp_settings.set("update.check_interval_hours", 24)
        legacy_recent = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        ).isoformat()
        tmp_settings.set("update.last_check_iso", legacy_recent)

        assert should_check_in_background(tmp_settings) is False

    def test_invalid_last_iso_triggers_check(self, tmp_settings, monkeypatch):
        from satellite_debug_tool.ui.update_dialog import should_check_in_background
        monkeypatch.setenv("SATELLITE_UPDATE_CHECK", "1")
        tmp_settings.set("update.auto_check", True)
        tmp_settings.set("update.last_check_iso", "not-iso-junk")
        assert should_check_in_background(tmp_settings) is True


# ============================ UpdateDialog ============================


class TestUpdateDialogConstruct:
    def test_construct_no_auto_start(self, qapp, tmp_settings):
        """auto_start=False 时不发起检查，纯构造。"""
        from satellite_debug_tool.ui.update_dialog import UpdateDialog
        dlg = UpdateDialog(tmp_settings, auto_start=False)
        # 默认应显示 CHECKING 页（idx=0）
        assert dlg._stack.currentIndex() == 0
        dlg.deleteLater()


# ============================ SettingsDialog 更新设置往返 ============================


class TestSettingsDialogUpdateSection:
    def test_initial_values_loaded(self, qapp, tmp_settings):
        tmp_settings.set("update.auto_check", False)
        tmp_settings.set("update.check_interval_hours", 12)
        tmp_settings.set("update.skip_version", "v0.5.0")
        from satellite_debug_tool.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(tmp_settings)
        assert dlg._cb_auto_check.isChecked() is False
        assert dlg._spin_interval.value() == 12
        assert "v0.5.0" in dlg._lbl_skipped.text()
        assert dlg._btn_reset_skip.isEnabled() is True
        dlg.deleteLater()

    def test_accept_writes_update_settings(self, qapp, tmp_settings):
        from satellite_debug_tool.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(tmp_settings)
        dlg._cb_auto_check.setChecked(False)
        dlg._spin_interval.setValue(48)
        dlg._on_accept()
        assert tmp_settings.get("update.auto_check") is False
        assert tmp_settings.get("update.check_interval_hours") == 48
        dlg.deleteLater()

    def test_reset_skip_version_clears(self, qapp, tmp_settings):
        tmp_settings.set("update.skip_version", "v1.2.3")
        from satellite_debug_tool.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(tmp_settings)
        dlg._on_reset_skip_version()
        assert tmp_settings.get("update.skip_version") == ""
        from satellite_debug_tool.i18n import tr
        assert dlg._lbl_skipped.text() == tr(
            "Skipped version: {version}",
            version=tr("None"),
        )
        assert dlg._btn_reset_skip.isEnabled() is False
        dlg.deleteLater()
