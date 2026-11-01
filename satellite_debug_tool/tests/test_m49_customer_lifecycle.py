"""M49 显式电源连接、布局及工具关闭/录制连续性回归。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QMessageBox
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.product import ProductValue
from satellite_debug_tool.core.production import FixtureRuntime
from satellite_debug_tool.core.comm import EndpointDatagram
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.tests.test_customer_main_window_integration import _configured_settings, _data_report
from satellite_debug_tool.tests.test_customer_overview_view import _LiveDouble, _SettingsDouble
from satellite_debug_tool.tests.test_m25_live_recorder_lifecycle import _composition, _meta, _capture_response, ENDPOINT
from satellite_debug_tool.tests.test_tcp_shell_worker import FakeAfd01Device
from satellite_debug_tool.tests.test_production_workspace import _dispose_created_production_widgets
from satellite_debug_tool.ui.customer_overview_view import CustomerOverviewView
from satellite_debug_tool.ui.customer_tcp_shell_dialog import CustomerTcpShellDialog
from satellite_debug_tool.ui.customer_workspace import CustomerWorkspace
from satellite_debug_tool.ui.main_window import MainWindow


def wait(app, predicate, seconds=5):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        app.processEvents()
        if predicate():
            return True
        time.sleep(.002)
    return bool(predicate())


def dispose(app, widget):
    widget.close()
    app.processEvents()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_saved_power_profile_waits_for_explicit_connection(qapplication_session, tmp_path, monkeypatch):
    settings, endpoints = _configured_settings(tmp_path, monkeypatch)
    records = settings.get('customer.devices')
    records[0]['external_power'] = {'host': '127.0.0.1', 'voltage_set_v': 24., 'current_set_a': 5.}
    settings.set('customer.devices', records)
    window = MainWindow(settings=settings)
    try:
        bundle = window._customer_bundle_factory.bundle(endpoints[0])
        monitor = bundle._power_monitor
        assert monitor.host == '127.0.0.1'
        assert monitor._worker is None
        for active in (True, False, True):
            bundle.set_customer_active(active)
            assert monitor._worker is None
            assert monitor.active is False
        starts = []
        monkeypatch.setattr(monitor, '_sync_worker', lambda: starts.append('explicit'))
        monitor.resume_monitoring()
        assert monitor.active
        assert starts == ['explicit']
        bundle.set_customer_active(False)
        bundle.set_customer_active(True)
        assert monitor.active is False
        assert starts == ['explicit']
    finally:
        dispose(qapplication_session, window)


def test_stale_overview_keeps_window_width_and_readable_snr(qapplication_session):
    live = _LiveDouble()
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)
    try:
        view.resize(1100, 760)
        view.show()
        qapplication_session.processEvents()
        width = view.width()
        snapshot = live.products.snapshot()
        snapshot = replace(snapshot, operation=replace(snapshot.operation, snr_db=ProductValue.stale(12., 1)))
        view._refresh_status(snapshot)
        view._refresh_snr(snapshot)
        for label in (*view._status_values.values(), *view._data_values.values()):
            label.setText('已断开，正在重新连接，等待设备返回有效状态（已过期）')
        for label in view._component_details.values():
            label.setText('在线 · 35.000 °C（已过期） · 123456.00 V（已过期） · 0x03（已过期）')
        qapplication_session.processEvents()
        assert view.width() == width
        assert view.minimumSizeHint().width() <= width
        assert view._snr_readout.value.text() == '12.00 dB'
        assert view._snr_readout.quality.isVisible()
        assert view._connect_btn.mapTo(view, view._connect_btn.rect().bottomRight()).x() < width
    finally:
        dispose(qapplication_session, view)


def test_connected_shell_close_is_one_async_transaction_with_final_drain(qapplication_session, tmp_path, monkeypatch):
    dialog = CustomerTcpShellDialog(Settings(config_directory=tmp_path))
    server = FakeAfd01Device()
    server.start()
    dialog.show()
    controller = dialog.workspace._controller
    try:
        controller.connect_device({'transport': 'tcp', 'host': '127.0.0.1', 'port': server.port})
        assert wait(qapplication_session, lambda: controller.state.value == 'connected' and dialog.workspace._active_ring is controller.rx_ring)
        workspace = dialog.workspace
        workspace._frame_timer.stop()
        payload = b'line\r\n' * 20000 + b'TAIL-M49\r\n'
        assert workspace._active_ring.push(payload).accepted == len(payload)
        prompts = []
        monkeypatch.setattr(QMessageBox, 'warning', lambda *a, **k: prompts.append(a) or int(QMessageBox.StandardButton.Yes))
        ticks = []
        timer = QTimer(); timer.setInterval(5); timer.timeout.connect(lambda: ticks.append(1)); timer.start()
        assert dialog.close() is False
        assert dialog.close_requested
        assert wait(qapplication_session, lambda: not dialog.isVisible())
        timer.stop()
        assert len(prompts) == 1
        assert workspace.shutdown_complete
        assert controller.worker is None
        assert workspace._log_writer is None
        assert workspace._active_ring.backlog_bytes == 0
        assert 'TAIL-M49' in workspace._terminal.toPlainText()
        assert ticks
    finally:
        dialog.workspace.shutdown()
        server.join(2)
        dispose(qapplication_session, dialog)


def test_main_window_exit_waits_for_shell_then_retries_close_automatically(qapplication_session, tmp_path, monkeypatch):
    settings, _ = _configured_settings(tmp_path, monkeypatch)
    window = MainWindow(settings=settings); window.show()
    dialog = window._customer.open_tcp_shell_dialog()
    server = FakeAfd01Device(); server.start()
    prompts = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a, **k: prompts.append(a) or QMessageBox.StandardButton.Yes)
    try:
        controller = dialog.workspace._controller
        controller.connect_device({'transport': 'tcp', 'host': '127.0.0.1', 'port': server.port})
        assert wait(qapplication_session, lambda: controller.state.value == 'connected')
        assert window.close() is False
        assert wait(qapplication_session, lambda: not window.isVisible())
        assert len(prompts) == 1
        assert dialog.workspace.shutdown_complete
    finally:
        server.join(2)
        dispose(qapplication_session, window)


@pytest.mark.parametrize("support_full", (False, True))
def test_opening_tools_preserves_runtime_recorder_and_all_raw_frames(qapplication_session, tmp_path, monkeypatch, support_full):
    broker, sessions, devices, factory, _ = _composition(tmp_path, monkeypatch)
    bundle = factory.bundle(ENDPOINT)
    assert devices.attach(ENDPOINT)
    runtime = sessions.runtime(ENDPOINT)
    runtime.feed_datagram(_meta('M49-RECORD', monotonic_s=1.))
    path = tmp_path / 'tools-continuity.sdb'
    if support_full:
        from satellite_debug_tool.core.product import CustomerRecordingState
        live = bundle.live
        live._customer_recording_path = path
        live._set_customer_recording_state(CustomerRecordingState.PREPARING)
        assert live._capture_profile_controller.request(True)
        request_id = live._capture_profile_controller.pending_request_id
        live._capture_profile_controller.feed_response(_capture_response(request_id))
        assert live.is_recording()
    else:
        assert bundle.live._start_recording(format_version=3, customer=False, filepath=path)
    recorder = bundle.live._recorder
    workspace = CustomerWorkspace(None, factory._settings, None, device_directory=devices,
                                  page_bundle_factory=factory, fixture_runtime=FixtureRuntime(),
                                  tcp_shell_session_directory=sessions)
    try:
        for index in range(8):
            runtime.feed_datagram(EndpointDatagram(ENDPOINT, _data_report(1000+index*10, float(index)),
                                                 int((1+index*.01)*1e9), int((1+index*.01)*1e9)))
            shell = workspace.open_tcp_shell_dialog()
            server = FakeAfd01Device(); server.start()
            shell.workspace._controller.connect_device(
                {'transport': 'tcp', 'host': '127.0.0.1', 'port': server.port}, endpoint=ENDPOINT,
            )
            assert wait(qapplication_session, lambda: shell.workspace._controller.state.value == 'connected')
            workspace.open_fixture_dialog()
            workspace._fixture_dialog.close()
            monkeypatch.setattr(QMessageBox, 'warning', lambda *a, **k: QMessageBox.StandardButton.Yes)
            shell.close()
            assert wait(qapplication_session, lambda: workspace._tcp_shell_dialog is None)
            server.join(2)
            assert bundle.live._recorder is recorder
            assert bundle.live.is_recording()
            assert runtime.recorder_count == 1
        assert bundle.live._stop_recording(restore_customer_profile=False)
        if support_full:
            assert bundle.live.prepare_session_shutdown()
        assert wait(qapplication_session, lambda: runtime.recorder_count == 0)
        reports = list(DataImporter.open_sdb(path).iter_data_reports())
        assert [r.timestamp for r in reports] == [1000+i*10 for i in range(8)]
    finally:
        workspace.shutdown()
        dispose(qapplication_session, workspace)
        factory.shutdown_all(); devices.shutdown(); sessions.shutdown(); broker.stop_broker()


def test_shell_close_cancel_preserves_connection(qapplication_session, tmp_path, monkeypatch):
    dialog = CustomerTcpShellDialog(Settings(config_directory=tmp_path))
    server = FakeAfd01Device(); server.start(); dialog.show()
    controller = dialog.workspace._controller
    try:
        controller.connect_device({'transport': 'tcp', 'host': '127.0.0.1', 'port': server.port})
        assert wait(qapplication_session, lambda: controller.state.value == 'connected')
        monkeypatch.setattr(QMessageBox, 'warning', lambda *a, **k: QMessageBox.StandardButton.Cancel)
        assert dialog.close() is False
        assert not dialog.close_requested
        assert controller.state.value == 'connected'
        assert dialog.isVisible()
        monkeypatch.setattr(QMessageBox, 'warning', lambda *a, **k: QMessageBox.StandardButton.Yes)
        dialog.close()
        assert wait(qapplication_session, lambda: not dialog.isVisible())
    finally:
        controller.shutdown()
        server.join(2)
        dispose(qapplication_session, dialog)


def test_automation_title_uses_stable_source_on_language_changes(qapplication_session, tmp_path, monkeypatch):
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
    from satellite_debug_tool.i18n import initialize_translation_manager, tr
    import os
    manager = initialize_translation_manager(qapplication_session)
    original_preference = manager.preference
    original_override = os.environ.get('SATELLITE_DEBUG_LOCALE')
    monkeypatch.delenv('SATELLITE_DEBUG_LOCALE', raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(ProductionWorkspace, '_configure_fleet', lambda self: None)
    workspace = ProductionWorkspace(Settings(config_directory=tmp_path/'settings'))
    try:
        for language in ('zh_CN', 'en_US', 'zh_CN'):
            manager.set_preference(language)
            workspace.retranslate_ui()
            assert workspace._title.text() == tr('Automated testing')
        assert workspace._title.text() == '自动化测试'
    finally:
        dispose(qapplication_session, workspace)
        if original_override is not None:
            monkeypatch.setenv('SATELLITE_DEBUG_LOCALE', original_override)
        manager.set_preference(original_preference)


def test_old_tool_finished_signal_keeps_current_dialog_owner(qapplication_session, tmp_path, monkeypatch):
    settings, _ = _configured_settings(tmp_path, monkeypatch)
    window = MainWindow(settings=settings)
    customer = window._customer
    try:
        old = customer.open_tcp_shell_dialog()
        assert old.close()
        current = customer.open_tcp_shell_dialog()
        old.finished.emit(0)
        assert customer._tcp_shell_dialog is current
        customer.open_fixture_dialog()
        old_fixture = customer._fixture_dialog
        assert old_fixture.close()
        customer.open_fixture_dialog()
        current_fixture = customer._fixture_dialog
        old_fixture.finished.emit(0)
        assert customer._fixture_dialog is current_fixture
    finally:
        dispose(qapplication_session, window)


def test_shell_log_close_failure_keeps_writer_and_reports_failed_intent(qapplication_session, tmp_path, monkeypatch):
    dialog = CustomerTcpShellDialog(Settings(config_directory=tmp_path))
    class Writer:
        failing = True
        def close(self):
            if self.failing:
                raise OSError('disk close failed')
    writer = Writer()
    dialog.workspace._log_writer = writer
    errors = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a, **k: errors.append(a[2]) or QMessageBox.StandardButton.Ok)
    try:
        assert dialog.close() is False
        assert not dialog.close_requested
        assert not dialog.workspace.shutdown_complete
        assert dialog.workspace._log_writer is writer
        assert 'disk close failed' in errors[0]
        writer.failing = False
        assert dialog.close()
        assert dialog.workspace.shutdown_complete
        assert dialog.workspace._log_writer is None
    finally:
        dispose(qapplication_session, dialog)


def test_shell_shutdown_waits_for_worker_completion_after_link_disconnect(qapplication_session, tmp_path, monkeypatch):
    dialog = CustomerTcpShellDialog(Settings(config_directory=tmp_path))
    workspace = dialog.workspace
    controller = workspace._controller
    monkeypatch.setattr(controller, 'request_shutdown', lambda: controller.disconnected.emit('closed'))
    try:
        assert dialog.close() is False
        workspace._flush_frame()
        assert not workspace.shutdown_complete
        controller.shutdown_finished.emit()
        assert wait(qapplication_session, lambda: workspace.shutdown_complete)
        assert wait(qapplication_session, lambda: not dialog.isVisible())
    finally:
        dispose(qapplication_session, dialog)
