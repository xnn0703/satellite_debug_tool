"""Customer support recording is gated by exact capture-profile confirmation."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from satellite_debug_tool.core.comm import DeviceConnectionPhase
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.external_power_monitor import (
    EXTERNAL_POWER_SAMPLE_EVENT,
    ExternalPowerSample,
)
from satellite_debug_tool.core.production.power_supply import PowerIdentity
from satellite_debug_tool.core.product import CustomerRecordingState
from satellite_debug_tool.core.protocol import (
    CmdType,
    CommandResponse,
    RespCode,
    ServiceCapabilities,
    ServiceControlOp,
    ServiceControlResponse,
    ServiceResultCode,
    SubCmd,
)
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.ui.live_view import LiveView


class _WorkerDouble:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, frame: bytes) -> bool:
        self.sent.append(frame)
        return True


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return Settings()


def _ready_view(settings: Settings) -> tuple[LiveView, _WorkerDouble]:
    view = LiveView(settings)
    worker = _WorkerDouble()
    view._worker = worker
    view._is_connected = True
    view._device_connection_phase = DeviceConnectionPhase.ONLINE
    view.product_store().feed(
        ServiceCapabilities(
            1,
            10,
            0xFF,
            17700.0,
            21200.0,
            27500.0,
            31000.0,
            0x0F,
            0x03,
            0x03,
        )
    )
    return view, worker


def _response(request_id: int) -> ServiceControlResponse:
    return ServiceControlResponse(
        1,
        request_id,
        ServiceControlOp.SET_CAPTURE_PROFILE,
        ServiceResultCode.ACCEPTED,
        1 << 6,
        0,
        0.0,
        0.0,
        0,
        0,
        False,
    )


def test_product_subscription_starts_before_profile_handshake_ready(
    app, settings: Settings
) -> None:
    view = LiveView(settings)
    worker = _WorkerDouble()
    view._worker = worker

    view._on_connected()

    assert any(
        frame[3] == CmdType.SERVICE_CONTROL_REQUEST
        and frame[11] == ServiceControlOp.SUBSCRIBE
        for frame in worker.sent
    )
    assert view.session_core().handshake is not None
    assert not view.session_core().handshake.is_ready
    view._on_disconnected()


def test_customer_recording_waits_for_exact_ack_and_restores_on_stop(
    app, settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    view, worker = _ready_view(settings)
    target = tmp_path / "support.sdb"
    monkeypatch.setattr(
        "satellite_debug_tool.ui.live_view.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(target), ""),
    )

    view.toggle_customer_recording()
    request_id = view._capture_pending_id
    assert request_id is not None
    assert view.customer_recording_state() == CustomerRecordingState.PREPARING
    assert worker.sent[-1][11] == ServiceControlOp.SET_CAPTURE_PROFILE
    assert worker.sent[-1][12] == 1
    assert not view.is_recording()

    view._on_capture_response(_response(request_id + 1))
    assert not view.is_recording()
    assert view._capture_pending_id == request_id

    view._on_capture_response(_response(request_id))
    assert view.is_recording()
    assert view._customer_recording
    assert view.customer_recording_state() == CustomerRecordingState.ACTIVE

    view._on_data_received(worker.sent[0])
    view.toggle_customer_recording()
    assert not view.is_recording()
    assert view.customer_recording_state() == CustomerRecordingState.RESTORING
    assert worker.sent[-1][11] == ServiceControlOp.SET_CAPTURE_PROFILE
    assert worker.sent[-1][12] == 0

    sdb = DataImporter.open_sdb(target)
    assert sdb.version == 3
    assert sdb.metadata["capture_profile"] == "support_full"
    assert sdb.quality["complete"] is True


def test_customer_recording_persists_external_power_metadata(
    app, settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    view, _worker = _ready_view(settings)
    target = tmp_path / "support-power.sdb"
    monkeypatch.setattr(
        "satellite_debug_tool.ui.live_view.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(target), ""),
    )
    view.toggle_customer_recording()
    request_id = view._capture_pending_id
    assert request_id is not None
    view._on_capture_response(_response(request_id))

    sample = ExternalPowerSample(
        host_timestamp_ns=1_800_000_000_123_456_789,
        monotonic_ns=123,
        connection_generation=2,
        identity=PowerIdentity(
            "GW-INSTEK", "PSW 80-27", "PSW1234", "1.70", "raw"
        ),
        voltage_v=12.04,
        current_a=1.25,
        power_w=15.05,
        output_enabled=True,
        operation_condition=1,
        questionable_condition=0,
        protection_tripped=False,
    )
    assert view.record_external_power_sample(sample)

    view.toggle_customer_recording()
    sdb = DataImporter.open_sdb(target)
    power_events = [
        event
        for event in sdb.metadata_events
        if event.get("event") == EXTERNAL_POWER_SAMPLE_EVENT
    ]
    assert len(power_events) == 1
    assert power_events[0]["host_timestamp_ns"] == sample.host_timestamp_ns
    assert power_events[0]["connection_generation"] == 2
    assert power_events[0]["voltage_v"] == pytest.approx(12.04)
    assert power_events[0]["current_a"] == pytest.approx(1.25)
    assert power_events[0]["power_w"] == pytest.approx(15.05)

def test_full_capture_timeout_requests_customer_profile_restore(
    app, settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    view, worker = _ready_view(settings)
    target = tmp_path / "support_timeout.sdb"
    monkeypatch.setattr(
        "satellite_debug_tool.ui.live_view.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(target), ""),
    )
    view.toggle_customer_recording()
    assert worker.sent[-1][12] == 1

    view._on_capture_timeout()

    assert not view.is_recording()
    assert view._capture_pending_target is False
    assert view.customer_recording_state() == CustomerRecordingState.RESTORING
    assert worker.sent[-1][11] == ServiceControlOp.SET_CAPTURE_PROFILE
    assert worker.sent[-1][12] == 0


def test_customer_recording_restores_previous_debug_state(
    app, settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    view, worker = _ready_view(settings)
    view.request_debug_mode(True)
    view._debug_controller.feed_response(
        CommandResponse(code=RespCode.SUCCESS, msg="DEBUG_ENABLE=1")
    )
    worker.sent.clear()
    target = tmp_path / "support_debug_restore.sdb"
    monkeypatch.setattr(
        "satellite_debug_tool.ui.live_view.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(target), ""),
    )

    view.toggle_customer_recording()
    start_request_id = view._capture_pending_id
    assert start_request_id is not None
    view._on_capture_response(_response(start_request_id))
    assert view.is_recording()

    view.toggle_customer_recording()
    restore_request_id = view._capture_pending_id
    assert restore_request_id is not None
    view._on_capture_response(_response(restore_request_id))
    app.processEvents()

    assert worker.sent[-1][6] == SubCmd.DEBUG_ENABLE
    assert worker.sent[-1][7] == 1
    assert view._debug_controller.pending_target is True

    view._debug_controller.feed_response(
        CommandResponse(code=RespCode.SUCCESS, msg="DEBUG_ENABLE=1")
    )
    assert view.is_debug_enabled()


def test_customer_recording_can_be_armed_before_device_is_online(
    app, settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    view = LiveView(settings)
    worker = _WorkerDouble()
    view._worker = worker
    view._is_connected = True
    view._device_connection_phase = DeviceConnectionPhase.WAITING
    view.product_store().feed(
        ServiceCapabilities(
            1,
            10,
            0xFF,
            17700.0,
            21200.0,
            27500.0,
            31000.0,
            0x0F,
            0x03,
            0x03,
        )
    )
    target = tmp_path / "armed.sdb"
    monkeypatch.setattr(
        "satellite_debug_tool.ui.live_view.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(target), ""),
    )

    view.toggle_customer_recording()

    assert view.customer_recording_state() == CustomerRecordingState.ARMED
    assert view._capture_pending_id is None
    assert not target.exists()

    view._mark_device_activity()

    assert view.customer_recording_state() == CustomerRecordingState.PREPARING
    assert view._capture_pending_id is not None


def test_cancelling_armed_recording_does_not_create_a_file(
    app, settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    view = LiveView(settings)
    view._worker = _WorkerDouble()
    view._is_connected = True
    view._device_connection_phase = DeviceConnectionPhase.WAITING
    target = tmp_path / "cancelled.sdb"
    monkeypatch.setattr(
        "satellite_debug_tool.ui.live_view.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(target), ""),
    )

    view.toggle_customer_recording()
    view.toggle_customer_recording()

    assert view.customer_recording_state() == CustomerRecordingState.IDLE
    assert not target.exists()


def test_disconnect_clears_armed_recording_intent(
    app, settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    view = LiveView(settings)
    view._worker = _WorkerDouble()
    view._is_connected = True
    view._device_connection_phase = DeviceConnectionPhase.WAITING
    target = tmp_path / "disconnect.sdb"
    monkeypatch.setattr(
        "satellite_debug_tool.ui.live_view.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(target), ""),
    )

    view.toggle_customer_recording()
    view._on_disconnected()

    assert view.customer_recording_state() == CustomerRecordingState.IDLE
    assert not target.exists()


def test_unsupported_full_capture_does_not_create_or_arm_a_recording(
    app, settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    view = LiveView(settings)
    worker = _WorkerDouble()
    view._worker = worker
    view._is_connected = True
    view._device_connection_phase = DeviceConnectionPhase.ONLINE
    view.product_store().feed(
        ServiceCapabilities(
            1,
            10,
            0xFF,
            17700.0,
            21200.0,
            27500.0,
            31000.0,
            0x0F,
            0x03,
            0x01,
        )
    )
    target = tmp_path / "unsupported.sdb"
    monkeypatch.setattr(
        "satellite_debug_tool.ui.live_view.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(target), ""),
    )

    view.toggle_customer_recording()

    assert view.customer_recording_state() == CustomerRecordingState.IDLE
    assert view._capture_pending_id is None
    assert not target.exists()
    assert not worker.sent


def test_active_recording_marks_outage_and_renegotiates_after_reconnect(
    app, settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    view, worker = _ready_view(settings)
    target = tmp_path / "reconnect.sdb"
    monkeypatch.setattr(
        "satellite_debug_tool.ui.live_view.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(target), ""),
    )
    view.toggle_customer_recording()
    start_request_id = view._capture_pending_id
    assert start_request_id is not None
    view._on_capture_response(_response(start_request_id))

    view._set_device_connection_phase(DeviceConnectionPhase.RECONNECTING)
    assert not view._customer_capture_confirmed
    view._mark_device_activity()
    resume_request_id = view._capture_pending_id
    assert resume_request_id is not None
    assert resume_request_id != start_request_id
    view._on_capture_response(_response(resume_request_id))
    assert view._customer_capture_confirmed

    view.toggle_customer_recording()
    sdb = DataImporter.open_sdb(target)
    assert [event["event"] for event in sdb.metadata_events] == [
        "device_offline",
        "device_online",
    ]
