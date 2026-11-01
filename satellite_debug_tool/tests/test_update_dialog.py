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

    def test_download_completion_only_prepares_handoff_request(
        self,
        qapp,
        tmp_settings,
        tmp_path,
        monkeypatch,
    ):
        from satellite_debug_tool.updater.checker import LatestRelease, Asset
        from satellite_debug_tool.updater.preparation import PreparedDownload
        import hashlib
        from satellite_debug_tool.ui import update_dialog

        install = tmp_path / "SatelliteTerminalManagerOps"
        install.mkdir()
        updater = install / "updater.exe"
        updater.write_bytes(b"PE-updater")
        volume = tmp_path / "release.7z"
        volume.write_bytes(b"archive")
        transaction = tmp_path / "updates" / "v2.0.0"
        transaction.mkdir(parents=True)
        manifest = tmp_path / "tree.json"
        manifest.write_bytes(b'{"schema":"test-manifest"}')
        package_asset = Asset("release.7z", "https://example.invalid/release.7z", volume.stat().st_size,
                              hashlib.sha256(volume.read_bytes()).hexdigest())
        tree_asset = Asset("tree.json", "https://example.invalid/tree.json", manifest.stat().st_size,
                           hashlib.sha256(manifest.read_bytes()).hexdigest())

        dlg = update_dialog.UpdateDialog(tmp_settings, auto_start=False)
        dlg._download_dir = transaction
        dlg._latest = LatestRelease(
            tag_name="v2.0.0",
            body="",
            html_url="https://example.invalid/release",
            assets=[package_asset], tree_manifest=tree_asset, installation_mode="automatic",
        )
        dlg._prepared_download = PreparedDownload(volume, package_asset, manifest, None, "automatic")
        monkeypatch.setattr(update_dialog, "_detect_install_dir", lambda: install)
        monkeypatch.setattr(update_dialog, "_detect_updater_exe", lambda: updater)

        dlg._do_launch_updater([volume])

        import time
        deadline = time.monotonic() + 3
        while dlg._prepare_thread is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert dlg._prepare_thread is None

        request = dlg.launch_request
        assert request is not None
        assert request.install_dir == install.resolve()
        assert request.volumes == (volume.resolve(),)
        assert request.status_path.is_file()
        assert request.package_sha256 == package_asset.sha256
        assert request.updater_executable != updater
        dlg.deleteLater()

    def test_close_keeps_worker_owner_until_actual_thread_exit(self, qapp, tmp_settings, monkeypatch):
        import threading
        import time
        from satellite_debug_tool.ui import update_dialog

        entered, release = threading.Event(), threading.Event()

        class BlockingChecker:
            def __init__(self, *args, **kwargs):
                self.cancelled = False

            def fetch_latest(self):
                entered.set()
                release.wait(3)
                raise update_dialog.UpdateCheckError("check_cancelled", "cancelled")

            def cancel(self):
                self.cancelled = True

        monkeypatch.setattr(update_dialog, "ReleaseChecker", BlockingChecker)
        dlg = update_dialog.UpdateDialog(tmp_settings, auto_start=False)
        dlg.start_check()
        assert entered.wait(1)
        thread = dlg._check_thread
        checker = dlg._check_source
        dlg.reject()
        assert thread.isRunning()
        assert dlg._check_thread is thread
        assert checker.cancelled
        release.set()
        deadline = time.monotonic() + 3
        while dlg._check_thread is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert dlg._check_thread is None
        assert dlg._close_result is None
        dlg.deleteLater()

    def test_manual_download_waits_for_manual_installation(self, qapp, tmp_settings, tmp_path):
        from satellite_debug_tool.ui import update_dialog
        from satellite_debug_tool.updater.checker import Asset
        from satellite_debug_tool.updater.preparation import PreparedDownload

        dlg = update_dialog.UpdateDialog(tmp_settings, auto_start=False)
        dlg._on_dl_finished(PreparedDownload(tmp_path / "application.zip", Asset("application.zip", "https://example.invalid/app.zip")))
        assert dlg._stack.currentIndex() == update_dialog._PAGE_READY
        assert dlg._btn_install.isHidden()
        assert dlg.launch_request is None
        dlg.deleteLater()

    def test_install_preparation_keeps_gui_responsive_and_cancels_before_handoff(
        self, qapp, tmp_settings, tmp_path, monkeypatch,
    ):
        import threading
        import time
        from PySide6.QtCore import QThread
        from satellite_debug_tool.ui import update_dialog
        from satellite_debug_tool.updater.checker import Asset, LatestRelease
        from satellite_debug_tool.updater.preparation import PreparedDownload

        gui_thread = QThread.currentThread()
        entered, release = threading.Event(), threading.Event()
        owners = []
        def prepare(**kwargs):
            owners.append(QThread.currentThread())
            entered.set()
            release.wait(3)
            assert kwargs["cancel_event"].is_set()
            raise update_dialog.DownloadCancelled()

        monkeypatch.setattr(update_dialog, "prepare_update_launch_request", prepare)
        monkeypatch.setattr(update_dialog, "_detect_install_dir", lambda: tmp_path / "app")
        monkeypatch.setattr(update_dialog, "_detect_updater_exe", lambda: tmp_path / "app/updater.exe")
        item = Asset("package.7z", "https://example.invalid/package.7z", 1, "0" * 64)
        dlg = update_dialog.UpdateDialog(tmp_settings, auto_start=False)
        dlg._download_dir = tmp_path
        dlg._latest = LatestRelease("2.0.0", assets=[item], tree_manifest=item, installation_mode="automatic")
        dlg._prepared_download = PreparedDownload(tmp_path / "package.7z", item, tmp_path / "tree.json", None, "automatic")
        dlg._do_launch_updater([tmp_path / "package.7z"])
        assert entered.wait(1)
        assert owners[0] != gui_thread
        dlg.reject()
        assert dlg._prepare_thread is not None
        assert dlg._cancel_event.is_set()
        release.set()
        deadline = time.monotonic() + 3
        while dlg._prepare_thread is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert dlg._prepare_thread is None
        assert dlg.launch_request is None
        dlg.deleteLater()


# ============================ SettingsDialog 更新设置往返 ============================


def test_background_check_delivers_result_on_gui_thread(qapp, tmp_settings, monkeypatch):
    import time
    from PySide6.QtCore import QThread
    from satellite_debug_tool.ui import update_dialog

    gui_thread = QThread.currentThread()
    observed = []

    class Checker:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_latest(self):
            return update_dialog.LatestRelease(tag_name="v999.0.0")

    monkeypatch.setenv("SATELLITE_UPDATE_CHECK", "1")
    monkeypatch.setattr(update_dialog, "ReleaseChecker", Checker)
    monkeypatch.setattr(tmp_settings, "persist_preferences", lambda: observed.append(("settings", QThread.currentThread())))
    thread = update_dialog.silent_background_check(
        tmp_settings, on_new_version=lambda latest: observed.append(("callback", QThread.currentThread())),
    )
    deadline = time.monotonic() + 3
    while (thread.isRunning() or len(observed) < 2) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert thread.wait(1000)
    assert [name for name, _ in observed] == ["settings", "callback"]
    assert all(owner == gui_thread for _, owner in observed)
    thread.deleteLater()


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
