"""Customer support recording is gated by exact capture-profile confirmation."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from satellite_debug_tool.core.config import Settings
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
        ServiceResultCode.SUCCESS,
        0,
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
    assert view._handshake is not None
    assert not view._handshake.is_ready
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
    assert worker.sent[-1][11] == ServiceControlOp.SET_CAPTURE_PROFILE
    assert worker.sent[-1][12] == 1
    assert not view.is_recording()

    view._on_capture_response(_response(request_id + 1))
    assert not view.is_recording()
    assert view._capture_pending_id == request_id

    view._on_capture_response(_response(request_id))
    assert view.is_recording()
    assert view._customer_recording

    view._on_data_received(worker.sent[0])
    view.toggle_customer_recording()
    assert not view.is_recording()
    assert worker.sent[-1][11] == ServiceControlOp.SET_CAPTURE_PROFILE
    assert worker.sent[-1][12] == 0

    sdb = DataImporter.open_sdb(target)
    assert sdb.version == 3
    assert sdb.metadata["capture_profile"] == "support_full"
    assert sdb.quality["complete"] is True


def test_full_capture_timeout_requests_customer_profile_restore(
    app, settings: Settings
) -> None:
    view, worker = _ready_view(settings)
    view.toggle_customer_recording()
    assert worker.sent[-1][12] == 1

    view._on_capture_timeout()

    assert not view.is_recording()
    assert view._capture_pending_target is False
    assert worker.sent[-1][11] == ServiceControlOp.SET_CAPTURE_PROFILE
    assert worker.sent[-1][12] == 0


def test_customer_recording_restores_previous_debug_state(
    app, settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    view, worker = _ready_view(settings)
    view._debug_enabled = True
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
    assert view._debug_pending_target is True

    view._on_debug_command_response(
        CommandResponse(code=RespCode.SUCCESS, msg="DEBUG_ENABLE=1")
    )
    assert view.is_debug_enabled()
