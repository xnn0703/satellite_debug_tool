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


def test_main_window_runs_render_timers_only_for_active_page(
    qapplication_session, lifecycle_settings
) -> None:
    from satellite_debug_tool.ui.main_window import MainWindow

    qapp = qapplication_session
    lifecycle_settings.set("ui.active_tab_id", "live")
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
    window = MainWindow(settings=lifecycle_settings)

    assert window._playback is None
    assert window._log is None
    assert window._device is None
    assert window._production is None
    assert not window._live.presentation_ready
    assert window._customer._rf_control is None
    assert window._customer._playback is None
    assert window._customer._maintenance is None

    window._customer.set_page("rf")
    qapp.processEvents()
    first_rf = window._customer._rf_control
    assert first_rf is not None
    window._customer.set_page("overview")
    window._customer.set_page("rf")
    assert window._customer._rf_control is first_rf

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
