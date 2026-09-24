from __future__ import annotations

from pathlib import Path
import statistics
import time

import pytest
from PySide6.QtTest import QTest


@pytest.fixture
def lifecycle_settings(tmp_path: Path, monkeypatch):
    from satellite_debug_tool.core.config import Settings

    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    settings.set("production.discovery_cidr", "127.0.0.1/32")
    settings.set("production.local_port", 0)
    return settings


def _configure_customer_endpoint(settings) -> None:
    endpoint = {"ip": "127.0.0.1", "port": 4004}
    settings.set("customer.devices", [endpoint])
    settings.set("customer.active_endpoint", endpoint)


def test_main_window_runs_render_timers_only_for_active_page(
    qapplication_session, lifecycle_settings
) -> None:
    from satellite_debug_tool.ui.main_window import MainWindow

    qapp = qapplication_session
    lifecycle_settings.set("ui.active_tab_id", "live")
    _configure_customer_endpoint(lifecycle_settings)
    window = MainWindow(settings=lifecycle_settings)
    overview = window._customer.overview
    live = window._live
    assert not live.presentation_ready

    customer_ticks = 0
    live_ticks = 0

    def count_customer_tick() -> None:
        nonlocal customer_ticks
        customer_ticks += 1

    def count_live_tick() -> None:
        nonlocal live_ticks
        live_ticks += 1

    overview._timer.timeout.connect(count_customer_tick)
    live._update_timer.timeout.connect(count_live_tick)

    assert overview._timer.isActive()
    assert not live._update_timer.isActive()
    assert not live._heavy_timer.isActive()
    QTest.qWait(260)
    assert customer_ticks > 0
    assert live_ticks == 0

    customer_before = customer_ticks
    window.unlock_engineering_for_session()
    qapp.processEvents()
    assert live.presentation_ready
    assert not overview._timer.isActive()
    assert live._update_timer.isActive()
    assert live._heavy_timer.isActive()
    QTest.qWait(260)
    assert customer_ticks == customer_before
    assert live_ticks > 0

    live_before = live_ticks
    window._tabs.setCurrentIndex(3)
    qapp.processEvents()
    assert not live._update_timer.isActive()
    assert not live._heavy_timer.isActive()
    QTest.qWait(260)
    assert live_ticks == live_before

    window._workspace.setCurrentIndex(0)
    qapp.processEvents()
    assert overview._timer.isActive()
    assert not live._update_timer.isActive()

    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_heavy_pages_are_built_on_first_access(
    qapplication_session, lifecycle_settings
) -> None:
    from satellite_debug_tool.ui.main_window import MainWindow

    qapp = qapplication_session
    lifecycle_settings.set("ui.active_tab_id", "live")
    _configure_customer_endpoint(lifecycle_settings)
    window = MainWindow(settings=lifecycle_settings)

    assert window._playback is None
    assert window._log is None
    assert window._device is None
    assert window._production is None
    assert not window._live.presentation_ready
    assert window._customer._rf_control is None
    assert window._customer._iperf is None
    assert window._customer._playback is None
    assert window._customer._maintenance is None

    window._customer.set_page("rf")
    qapp.processEvents()
    first_rf = window._customer._rf_control
    assert first_rf is not None
    window._customer.set_page("overview")
    window._customer.set_page("rf")
    assert window._customer._rf_control is first_rf

    window._customer.set_page("iperf")
    qapp.processEvents()
    first_iperf = window._customer._iperf
    assert first_iperf is not None
    window._customer.set_page("overview")
    window._customer.set_page("iperf")
    assert window._customer._iperf is first_iperf

    window.unlock_engineering_for_session()
    assert window._live.presentation_ready
    window._tabs.setCurrentIndex(1)
    qapp.processEvents()
    first_playback = window._playback
    assert first_playback is not None
    window._tabs.setCurrentIndex(0)
    window._tabs.setCurrentIndex(1)
    assert window._playback is first_playback

    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_active_iperf_keeps_external_power_monitor_active_across_workspaces(
    qapplication_session, lifecycle_settings
) -> None:
    from dataclasses import replace

    from satellite_debug_tool.core.iperf_test import IperfTestPhase
    from satellite_debug_tool.ui.main_window import MainWindow

    qapp = qapplication_session
    _configure_customer_endpoint(lifecycle_settings)
    window = MainWindow(settings=lifecycle_settings)
    endpoint = window._customer_devices.active_endpoint()
    bundle = window._customer_bundle_factory.bundle(endpoint)
    window.unlock_engineering_for_session()
    window._workspace.setCurrentIndex(1)
    assert not bundle._power_monitor.active

    bundle._iperf_store.set_snapshot(
        replace(bundle._iperf_store.snapshot, phase=IperfTestPhase.RUNNING)
    )
    bundle._iperf_controller.active_changed.emit(True)
    assert bundle._power_monitor.active

    bundle._iperf_store.set_snapshot(
        replace(bundle._iperf_store.snapshot, phase=IperfTestPhase.STOPPED)
    )
    bundle._iperf_controller.active_changed.emit(False)
    assert not bundle._power_monitor.active

    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_prebuilt_lazy_child_activates_when_host_becomes_visible(
    qapplication_session
) -> None:
    from PySide6.QtWidgets import QWidget

    from satellite_debug_tool.ui.lazy_view_host import LazyViewHost

    class _LifecycleView(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.active = False
            self.activations = 0

        def activate_view(self) -> None:
            if self.active:
                return
            self.active = True
            self.activations += 1

        def deactivate_view(self) -> None:
            self.active = False

    child = _LifecycleView()
    host = LazyViewHost(lambda: child)

    assert host.ensure_view() is child
    assert not child.active
    host.activate_view()

    assert child.active
    assert child.activations == 1

    host.deactivate_view()
    assert not child.active
    host.deleteLater()


def test_production_subpage_lifecycle_limits_fixture_plotting(
    qapplication_session, lifecycle_settings
) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    qapp = qapplication_session
    workspace = ProductionWorkspace(lifecycle_settings)
    assert workspace._fixture_debug is None

    workspace.activate_view()
    assert workspace._fixture_debug is None

    workspace._switch_subpage(1)
    qapp.processEvents()
    fixture = workspace._fixture_debug
    assert fixture is not None
    assert fixture._plot_timer.isActive()

    workspace.deactivate_view()
    assert not fixture._plot_timer.isActive()

    workspace.shutdown()
    workspace.close()


def test_live_presentation_hydrates_data_received_before_first_access(
    qapplication_session, lifecycle_settings
) -> None:
    from satellite_debug_tool.core.protocol import ChannelSample, DataReport
    from satellite_debug_tool.ui.main_window import MainWindow

    qapp = qapplication_session
    window = MainWindow(settings=lifecycle_settings)
    live = window._live
    core = live.session_core()
    store = live.data_store()

    store.update(DataReport(timestamp=2500, samples=[ChannelSample(0, 12.5)]))
    assert not live.presentation_ready
    assert not hasattr(live, "_chart")

    window.unlock_engineering_for_session()
    qapp.processEvents()

    assert live.presentation_ready
    assert live.session_core() is core
    assert live.data_store() is store
    assert live._channel_panel.channel_names() == ["ch_00"]
    assert live._channel_panel._rows["ch_00"]._value_label.text() == "12.50"

    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_deferred_live_session_connects_before_engineering_ui_exists(
    qapplication_session, lifecycle_settings, monkeypatch
) -> None:
    from PySide6.QtCore import QObject, Signal

    from satellite_debug_tool.ui import live_view as live_module

    class _UdpWorker(QObject):
        connected = Signal()
        disconnected = Signal()
        error = Signal(str)
        data_received = Signal(bytes)

        def __init__(self) -> None:
            super().__init__()
            self.sent: list[bytes] = []

        def connect(self, _config: dict) -> bool:
            self.connected.emit()
            return True

        def disconnect(self) -> None:
            self.disconnected.emit()

        def send(self, frame: bytes) -> bool:
            self.sent.append(bytes(frame))
            return True

    monkeypatch.setattr(live_module, "UdpWorker", _UdpWorker)
    live = live_module.LiveView(
        lifecycle_settings,
        defer_presentation=True,
    )

    assert not live.presentation_ready
    assert live.connect_udp("192.168.1.88", 4004, 0, auto_debug=False)
    assert live.is_connected()
    assert live.session_core().connected
    assert not hasattr(live, "_connect_btn")

    live.disconnect_device()
    assert not live.is_connected()
    assert not live.session_core().connected
    live.deleteLater()


def test_unmanaged_live_reaps_worker_before_reconnect_disconnect_and_shutdown(
    qapplication_session, lifecycle_settings, monkeypatch
) -> None:
    from PySide6.QtCore import QThread, Signal

    from satellite_debug_tool.ui import live_view as live_module

    class _ThreadWorker(QThread):
        connected = Signal()
        disconnected = Signal()
        error = Signal(str)
        data_received = Signal(bytes)

        instances = []

        def __init__(self) -> None:
            super().__init__()
            self._running = False
            self.instances.append(self)

        def connect(self, _config: dict) -> bool:
            self._running = True
            self.start()
            self.connected.emit()
            return True

        def disconnect(self) -> None:
            self._running = False
            self.disconnected.emit()

        def send(self, _frame: bytes) -> bool:
            return self._running

        def run(self) -> None:
            while self._running:
                self.msleep(5)

    monkeypatch.setattr(live_module, "UdpWorker", _ThreadWorker)
    live = live_module.LiveView(lifecycle_settings, defer_presentation=True)
    config = {
        "type": "udp",
        "remote_ip": "127.0.0.1",
        "remote_port": 4004,
        "local_port": 0,
    }

    assert live._connect_transport(config)
    first = _ThreadWorker.instances[-1]
    assert first.isRunning()

    assert live._connect_transport(config)
    second = _ThreadWorker.instances[-1]
    assert second is not first
    assert not first.isRunning()
    assert live._worker is second
    assert live.session_core().transport is second

    assert live.disconnect_device()
    assert not second.isRunning()
    assert live._worker is None
    assert not live.session_core().connected

    assert live._connect_transport(config)
    third = _ThreadWorker.instances[-1]
    assert third.isRunning()
    assert live.shutdown()
    assert not third.isRunning()
    assert live._worker is None
    live.deleteLater()


def test_unmanaged_live_retains_worker_when_bounded_wait_does_not_stop_it(
    qapplication_session, lifecycle_settings, monkeypatch
) -> None:
    from PySide6.QtCore import QObject, Signal

    from satellite_debug_tool.ui import live_view as live_module

    class _StuckWorker(QObject):
        connected = Signal()
        disconnected = Signal()
        error = Signal(str)
        data_received = Signal(bytes)

        instances = []

        def __init__(self) -> None:
            super().__init__()
            self.running = True
            self.delete_requested = False
            self.instances.append(self)

        def connect(self, _config: dict) -> bool:
            self.connected.emit()
            return True

        def disconnect(self) -> None:
            self.disconnected.emit()

        def isRunning(self) -> bool:  # noqa: N802 - mirrors QThread
            return self.running

        def wait(self, _timeout_ms: int) -> bool:
            return False

        def deleteLater(self) -> None:  # noqa: N802 - mirrors QObject
            self.delete_requested = True

        def send(self, _frame: bytes) -> bool:
            return False

    monkeypatch.setattr(live_module, "UdpWorker", _StuckWorker)
    live = live_module.LiveView(lifecycle_settings, defer_presentation=True)
    config = {
        "type": "udp",
        "remote_ip": "127.0.0.1",
        "remote_port": 4004,
        "local_port": 0,
    }

    assert live._connect_transport(config)
    worker = _StuckWorker.instances[-1]
    assert not live.disconnect_device()
    assert live._worker is worker
    assert not worker.delete_requested

    assert not live._connect_transport(config)
    assert len(_StuckWorker.instances) == 1
    assert live._worker is worker

    worker.running = False
    assert live.shutdown()
    assert worker.delete_requested
    assert live._worker is None
    live.deleteLater()


def test_two_hundred_workspace_switches_reuse_session_and_store(
    qapplication_session, lifecycle_settings
) -> None:
    from satellite_debug_tool.core.protocol import ChannelSample, DataReport
    from satellite_debug_tool.ui.main_window import MainWindow

    qapp = qapplication_session
    window = MainWindow(settings=lifecycle_settings)
    live = window._live
    core = live.session_core()
    store = live.data_store()
    store.update(DataReport(timestamp=1000, samples=[ChannelSample(0, 1.0)]))

    window.unlock_engineering_for_session()
    window.unlock_production_for_session()
    assert window._production is not None
    assert window._production._view_active
    window._workspace.setCurrentIndex(0)
    qapp.processEvents()
    assert not window._production._view_active

    elapsed_ms: list[float] = []
    for index in range(200):
        started = time.perf_counter()
        window._workspace.setCurrentIndex((index + 1) % 3)
        qapp.processEvents()
        elapsed_ms.append((time.perf_counter() - started) * 1000.0)

    assert live.session_core() is core
    assert live.data_store() is store
    assert store.frame_count == 1
    assert statistics.quantiles(elapsed_ms, n=20)[18] < 100.0

    window._workspace.setCurrentIndex(0)
    window.close()
    window.deleteLater()
    qapp.processEvents()
