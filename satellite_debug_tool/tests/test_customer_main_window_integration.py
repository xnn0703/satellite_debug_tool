"""M25 customer/Engineering composition over the shared endpoint runtime."""

from __future__ import annotations

import math
import socket
import struct

from PySide6.QtCore import QElapsedTimer, QThread
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.protocol import CmdType, build_frame
from satellite_debug_tool.ui.engineering_session_host import ENGINEERING_SHARED_UDP
from satellite_debug_tool.ui.main_window import MainWindow
from satellite_debug_tool.ui.settings_dialog import (
    SETTINGS_SCOPE_CUSTOMER,
    SETTINGS_SCOPE_ENGINEERING,
    SETTINGS_SCOPE_PRODUCTION,
)


def _free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _configured_settings(tmp_path, monkeypatch) -> tuple[Settings, tuple[tuple[str, int], ...]]:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    reserved: list[socket.socket] = []
    try:
        for _index in range(3):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            reserved.append(sock)
        local_port, port_a, port_b = (
            int(sock.getsockname()[1]) for sock in reserved
        )
    finally:
        for sock in reserved:
            sock.close()
    endpoints = (("127.0.0.1", port_a), ("127.0.0.1", port_b))
    devices = [{"ip": ip, "port": port} for ip, port in endpoints]
    settings.set("device_udp.local_port", local_port)
    settings.set("customer.devices", devices)
    settings.set("customer.active_endpoint", devices[0])
    settings.save()
    return settings, endpoints


def _data_report(timestamp_ms: int, value: float) -> bytes:
    payload = struct.pack("<IBBf", int(timestamp_ms), 1, 0, float(value))
    return build_frame(CmdType.DATA_REPORT, payload)


def _wait_until(predicate, *, timeout_ms: int = 1000) -> bool:
    timer = QElapsedTimer()
    timer.start()
    while timer.elapsed() < timeout_ms:
        if predicate():
            return True
        QTest.qWait(5)
    return bool(predicate())


def test_two_endpoints_share_one_socket_route_once_and_switch_fixed_pages(
    qapplication_session,
    tmp_path,
    monkeypatch,
) -> None:
    qapp = qapplication_session
    settings, endpoints = _configured_settings(tmp_path, monkeypatch)
    window = MainWindow(settings=settings)
    directory = window._customer_devices
    endpoint_a, endpoint_b = endpoints

    try:
        runtime_a = directory.runtime(endpoint_a)
        runtime_b = directory.runtime(endpoint_b)
        assert runtime_a is not None and runtime_b is not None
        assert runtime_a is not runtime_b
        assert runtime_a.core is not runtime_b.core
        assert not window._udp_broker.isRunning()
        assert window._customer._bundle_registry.bundle_count == 1

        directory.attach(endpoint_a)
        directory.attach(endpoint_b)
        QTest.qWait(80)
        assert window._udp_broker.isRunning()
        assert window._udp_broker.demand_count == 2
        assert window._udp_broker.claim_count == 2
        assert window._udp_broker.active_local_port == int(
            settings.get("device_udp.local_port")
        )
        assert runtime_a.core.generation == 1
        assert runtime_b.core.generation == 1

        senders: list[socket.socket] = []
        try:
            for endpoint, value in zip(endpoints, (11.0, 22.0)):
                sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sender.bind(endpoint)
                sender.sendto(
                    _data_report(1000 + len(senders), value),
                    ("127.0.0.1", window._udp_broker.active_local_port),
                )
                senders.append(sender)
            assert _wait_until(
                lambda: (
                    runtime_a.core.receiver.frames_ok == 1
                    and runtime_b.core.receiver.frames_ok == 1
                )
            )
        finally:
            for sender in senders:
                sender.close()

        assert runtime_a.core.receiver.frames_ok == 1
        assert runtime_b.core.receiver.frames_ok == 1
        assert runtime_a.core.data_store.get_channel_by_id(0).get_latest()[1] == 11.0
        assert runtime_b.core.data_store.get_channel_by_id(0).get_latest()[1] == 22.0

        fixed_views: dict[tuple[tuple[str, int], str], object] = {}
        for endpoint in endpoints:
            directory.select_endpoint(endpoint)
            for page_id in ("overview", "rf", "maintenance"):
                view = window._customer.session_page(page_id, ensure=True)
                assert view is not None
                fixed_views[(endpoint, page_id)] = view
        assert window._customer._bundle_registry.bundle_count == 2

        generations = (runtime_a.core.generation, runtime_b.core.generation)
        settings.save = lambda: None  # isolate view-switch latency from disk fsync
        elapsed_ms: list[float] = []
        for index in range(200):
            target = endpoint_a if index % 2 == 0 else endpoint_b
            timer = QElapsedTimer()
            timer.start()
            directory.select_endpoint(target)
            elapsed_ms.append(timer.nsecsElapsed() / 1_000_000.0)
            assert window._customer.session_page("overview") is fixed_views[
                (target, "overview")
            ]
        ordered = sorted(elapsed_ms)
        p95 = ordered[math.ceil(len(ordered) * 0.95) - 1]
        assert p95 <= 10.0
        assert max(ordered) <= 50.0
        assert (runtime_a.core.generation, runtime_b.core.generation) == generations
        assert len(window._endpoint_directory.runtimes()) == 2

        window._set_engineering_session_mode(
            ENGINEERING_SHARED_UDP,
            persist=False,
        )
        window.unlock_engineering_for_session()
        qapp.processEvents()
        selected = directory.active_endpoint()
        assert selected is not None
        assert window._engineering_live_host.active_view is (
            window._customer_bundle_factory.bundle(selected).live
        )
        other = endpoint_b if selected == endpoint_a else endpoint_a
        directory.select_endpoint(other)
        qapp.processEvents()
        assert window._engineering_live_host.active_view is (
            window._customer_bundle_factory.bundle(other).live
        )
        assert window._engineering_live_host.active_view.fixed_endpoint == other
        assert (runtime_a.core.generation, runtime_b.core.generation) == generations
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()

    assert not window._udp_broker.isRunning()
    assert window._endpoint_directory.closed


def test_corrupt_device_settings_block_production_start_gate(
    qapplication_session,
    tmp_path,
    monkeypatch,
) -> None:
    qapp = qapplication_session
    settings, endpoints = _configured_settings(tmp_path, monkeypatch)
    window = MainWindow(settings=settings)
    try:
        settings._device_configuration_blocked = True
        settings._device_configuration_error = "injected durability uncertainty"

        production = window._ensure_production_workspace()
        participants, reason = production._evaluate_start_gate()
        assert participants == ()
        assert reason.strip()
    finally:
        settings._device_configuration_blocked = False
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_workspace_switch_controls_external_power_and_settings_scope(
    qapplication_session,
    tmp_path,
    monkeypatch,
) -> None:
    qapp = qapplication_session
    settings, _endpoints = _configured_settings(tmp_path, monkeypatch)
    window = MainWindow(settings=settings)

    class _AccessoryFactoryDouble:
        def __init__(self) -> None:
            self.active_calls: list[bool] = []

        def set_customer_active(self, active: bool) -> None:
            self.active_calls.append(bool(active))

    accessory_factory = _AccessoryFactoryDouble()
    window._customer_bundle_factory.set_customer_active = (
        accessory_factory.set_customer_active
    )
    scopes: list[str] = []

    class _SettingsDialogDouble:
        def __init__(self, *_args, scope: str, **_kwargs) -> None:
            scopes.append(scope)

        def exec(self):
            return 0

    monkeypatch.setattr(
        "satellite_debug_tool.ui.main_window.SettingsDialog",
        _SettingsDialogDouble,
    )

    try:
        for index in (0, 1, 2):
            window._workspace.setCurrentIndex(index)
            qapp.processEvents()
            window._on_open_settings()

        assert scopes == [
            SETTINGS_SCOPE_CUSTOMER,
            SETTINGS_SCOPE_ENGINEERING,
            SETTINGS_SCOPE_PRODUCTION,
        ]
        assert accessory_factory.active_calls[-2:] == [False, False]
        window._workspace.setCurrentIndex(0)
        qapp.processEvents()
        assert accessory_factory.active_calls[-1] is True
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_close_waits_for_background_update_thread_without_device_connection(
    qapplication_session,
    tmp_path,
    monkeypatch,
) -> None:
    qapp = qapplication_session
    settings, _endpoints = _configured_settings(tmp_path, monkeypatch)
    window = MainWindow(settings=settings)

    class _PendingUpdateThread(QThread):
        def run(self) -> None:
            while not self.isInterruptionRequested():
                self.msleep(5)

    thread = _PendingUpdateThread()
    thread.start()
    assert thread.isRunning()
    window._bg_check_thread = thread

    assert window.close()
    assert not thread.isRunning()
    assert window._bg_check_thread is None
    window.deleteLater()
    qapp.processEvents()


def test_close_rejects_worker_timeout_before_customer_teardown(
    qapplication_session,
    tmp_path,
    monkeypatch,
) -> None:
    qapp = qapplication_session
    settings, _endpoints = _configured_settings(tmp_path, monkeypatch)
    window = MainWindow(settings=settings)
    original_live_shutdown = window._live.shutdown
    original_customer_shutdown = window._customer.shutdown
    customer_shutdown_calls: list[bool] = []
    warnings: list[tuple[object, str, str]] = []

    try:
        monkeypatch.setattr(window._live, "shutdown", lambda: False)
        monkeypatch.setattr(
            window._customer,
            "shutdown",
            lambda: customer_shutdown_calls.append(True),
        )
        monkeypatch.setattr(
            QMessageBox,
            "warning",
            lambda parent, title, message: warnings.append((parent, title, message)),
        )

        assert not window.close()
        assert customer_shutdown_calls == []
        assert warnings
        assert not window._endpoint_directory.closed
    finally:
        monkeypatch.setattr(window._live, "shutdown", original_live_shutdown)
        monkeypatch.setattr(window._customer, "shutdown", original_customer_shutdown)
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_close_retains_directory_when_customer_shutdown_needs_recovery(
    qapplication_session,
    tmp_path,
    monkeypatch,
) -> None:
    qapp = qapplication_session
    settings, _endpoints = _configured_settings(tmp_path, monkeypatch)
    window = MainWindow(settings=settings)
    original_customer_shutdown = window._customer.shutdown
    original_directory_shutdown = window._customer_devices.shutdown
    directory_shutdown_calls: list[bool] = []
    warnings: list[tuple[object, str, str]] = []

    try:
        monkeypatch.setattr(window._customer, "shutdown", lambda: False)
        monkeypatch.setattr(
            window._customer_devices,
            "shutdown",
            lambda: directory_shutdown_calls.append(True),
        )
        monkeypatch.setattr(
            QMessageBox,
            "warning",
            lambda parent, title, message: warnings.append((parent, title, message)),
        )

        assert not window.close()
        assert directory_shutdown_calls == []
        assert warnings
        assert not window._endpoint_directory.closed
    finally:
        monkeypatch.setattr(
            window._customer,
            "shutdown",
            original_customer_shutdown,
        )
        monkeypatch.setattr(
            window._customer_devices,
            "shutdown",
            original_directory_shutdown,
        )
        window.close()
        window.deleteLater()
        qapp.processEvents()
