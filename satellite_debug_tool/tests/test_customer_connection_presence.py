"""Customer connection intent stays active until a real device appears."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from satellite_debug_tool.core.comm import DeviceConnectionPhase
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.protocol import (
    CmdType,
    ServiceControlOp,
    ServiceControlResponse,
    ServiceFastState,
    ServiceResultCode,
    build_frame,
)
from satellite_debug_tool.ui.live_view import (
    DISCOVERY_SLOW_INTERVAL_MS,
    LiveView,
)


class _WorkerDouble:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, frame: bytes) -> bool:
        self.sent.append(bytes(frame))
        return True


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return Settings()


def _identity(timestamp: int = 100) -> bytes:
    payload = struct.pack("<BII", 1, timestamp, 0x17)
    for text in ("AFD01", "AFD01-LATE", "0.0.130", ""):
        encoded = text.encode("utf-8")
        payload += bytes([len(encoded)]) + encoded
    payload += b"\x02"
    return build_frame(CmdType.SERVICE_IDENTITY, payload)


def _subscription_count(frames: list[bytes]) -> int:
    return sum(
        frame[3] == CmdType.SERVICE_CONTROL_REQUEST and frame[11] == 0
        for frame in frames
    )


def _connected_view(settings: Settings) -> tuple[LiveView, _WorkerDouble]:
    view = LiveView(settings)
    worker = _WorkerDouble()
    view._worker = worker
    view._on_connected()
    return view, worker


def test_transport_waits_for_valid_device_frame(app, settings: Settings) -> None:
    view, _worker = _connected_view(settings)

    assert view.is_connected()
    assert not view.is_device_online()
    assert view.connection_phase() == DeviceConnectionPhase.WAITING

    view._on_data_received(b"not-a-frame")
    assert view.connection_phase() == DeviceConnectionPhase.WAITING

    view._on_data_received(_identity())
    assert view.is_device_online()
    assert view.connection_phase() == DeviceConnectionPhase.ONLINE
    view._on_disconnected()


def test_product_discovery_continues_after_three_attempts(
    app, settings: Settings
) -> None:
    view, worker = _connected_view(settings)
    initial = _subscription_count(worker.sent)

    for _ in range(12):
        view._retry_product_subscription()

    assert _subscription_count(worker.sent) == initial + 12
    assert view._product_subscribe_timer.interval() == DISCOVERY_SLOW_INTERVAL_MS
    view._on_disconnected()


def test_identity_does_not_confirm_product_subscription(
    app, settings: Settings
) -> None:
    view, _worker = _connected_view(settings)

    view._on_data_received(_identity())

    assert view.product_store().service_available
    assert not view.product_store().telemetry_ready
    assert not view._product_subscription_confirmed
    assert view._product_subscribe_timer.isActive()
    view._on_disconnected()


def test_exact_subscribe_response_confirms_product_subscription(
    app, settings: Settings
) -> None:
    view, _worker = _connected_view(settings)
    request_id = view._product_subscribe_pending_id
    assert request_id is not None

    view.product_store().feed(ServiceControlResponse(
        1,
        request_id,
        ServiceControlOp.SUBSCRIBE,
        ServiceResultCode.SUCCESS,
        0,
        0,
        0.0,
        0.0,
        0,
        0,
        False,
    ))

    assert view._product_subscription_confirmed
    assert not view._product_subscribe_timer.isActive()
    view._on_disconnected()


def test_fast_telemetry_confirms_product_subscription(
    app, settings: Settings
) -> None:
    view, _worker = _connected_view(settings)

    view.product_store().feed(ServiceFastState(
        1, 1, 0, 0, 0, False, 0, 0, False,
        0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
    ))

    assert view._product_subscription_confirmed
    assert not view._product_subscribe_timer.isActive()
    view._on_disconnected()


def test_online_timeout_keeps_transport_and_reconnects(
    app, settings: Settings, monkeypatch
) -> None:
    view, worker = _connected_view(settings)
    phases: list[str] = []
    view.device_connection_phase_changed.connect(phases.append)
    view._on_data_received(_identity())
    assert view.product_store().service_available
    before = _subscription_count(worker.sent)

    view._last_valid_frame_at = 10.0
    monkeypatch.setattr(
        "satellite_debug_tool.ui.live_view.time.monotonic", lambda: 13.1
    )
    view._check_device_activity()

    assert view.is_connected()
    assert not view.is_device_online()
    assert view.connection_phase() == DeviceConnectionPhase.RECONNECTING
    assert not view.product_store().service_available
    assert _subscription_count(worker.sent) == before + 1

    view._on_data_received(_identity(200))
    assert view.connection_phase() == DeviceConnectionPhase.ONLINE
    assert phases[-2:] == ["reconnecting", "online"]
    view._on_disconnected()
