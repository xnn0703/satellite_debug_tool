"""M50 自动化测试完整采集协商与内容门禁回归。"""
from __future__ import annotations

from pathlib import Path
import struct

from satellite_debug_tool.core.comm import UdpEndpointBroker
from satellite_debug_tool.core.production.fleet import (
    FleetController,
    FleetDatagram,
    ProductionCaptureState,
)
from satellite_debug_tool.core.protocol import CmdType, build_frame
from satellite_debug_tool.core.session import EndpointSessionDirectory
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.tests.test_production_fleet import _fast_snr, _identity


ENDPOINT = ("192.168.1.12", 4004)


def _datagram(frame: bytes, sequence: int) -> FleetDatagram:
    return FleetDatagram(
        ENDPOINT,
        frame,
        1_000_000_000 + sequence,
        2_000_000_000 + sequence,
    )


def _capabilities(*, full_capture: bool = True) -> bytes:
    payload = struct.pack(
        "<BII4fBBB",
        1,
        10,
        0xFF,
        17700.0,
        21200.0,
        27500.0,
        31000.0,
        0x0F,
        0x03,
        0x03 if full_capture else 0x01,
    )
    return build_frame(CmdType.SERVICE_CAPABILITIES, payload)


def _capture_response(request_id: int) -> bytes:
    payload = struct.pack(
        "<BIBBIBffBBB",
        1,
        request_id,
        4,
        0,
        1 << 6,
        0,
        0.0,
        0.0,
        0,
        0,
        0,
    )
    return build_frame(CmdType.SERVICE_CONTROL_RESPONSE, payload)


def _data_report(timestamp_ms: int = 20) -> bytes:
    return build_frame(
        CmdType.DATA_REPORT,
        struct.pack("<IBBf", timestamp_ms, 1, 0, 12.0),
    )


def _capture_request_id(frame: bytes) -> int:
    schema, request_id, operation, _profile = struct.unpack("<BIBB", frame[6:13])
    assert schema == 1
    assert operation == 4
    return request_id


def _controller(monkeypatch):
    broker = UdpEndpointBroker(local_port=0)
    sent: list[bytes] = []
    monkeypatch.setattr(broker, "start_broker", lambda: True)
    monkeypatch.setattr(
        broker,
        "send_to",
        lambda endpoint, frame: sent.append(bytes(frame)) or endpoint == ENDPOINT,
    )
    directory = EndpointSessionDirectory(broker)
    controller = FleetController(
        discovery_cidr="192.168.1.0/24",
        local_port=0,
        broker=broker,
        session_directory=directory,
    )
    controller.start()
    controller.inject_datagram(_datagram(_identity("M50-FULL"), 1))
    controller.inject_datagram(_datagram(_capabilities(), 2))
    return controller, broker, sent


def test_production_requires_ack_and_real_analysis_frame_before_ready(
    qapplication_session, tmp_path: Path, monkeypatch
) -> None:
    controller, broker, sent = _controller(monkeypatch)
    try:
        assert controller.arm_batch_recording("M50-BATCH", tmp_path)
        session = controller.sessions()[0]
        request = sent[-1]
        assert request[3] == CmdType.SERVICE_CONTROL_REQUEST
        assert session.capture_state is ProductionCaptureState.REQUESTING
        assert not session.capture_ready

        # A frame preceding this request's exact ACK is not full-capture proof.
        controller.inject_datagram(_datagram(_data_report(15), 3))
        assert session.capture_state is ProductionCaptureState.REQUESTING
        assert not session.capture_ready

        controller.inject_datagram(
            _datagram(_capture_response(_capture_request_id(request)), 4)
        )
        assert session.capture_state is ProductionCaptureState.WAITING_DATA
        assert not session.capture_ready

        analysis_frame = _data_report()
        controller.inject_datagram(_datagram(analysis_frame, 5))
        assert session.capture_state is ProductionCaptureState.READY
        assert session.capture_ready

        assert controller.finalize_recordings()[ENDPOINT] is None
        restore_request = sent[-1]
        controller.inject_datagram(
            _datagram(_capture_response(_capture_request_id(restore_request)), 6)
        )
        path = controller.finalize_recordings()[ENDPOINT]
        assert path is not None
        sdb = DataImporter.open_sdb(path)
        assert any(type(item).__name__ == "DataReport" for item in sdb.iter_records())
        events = [
            item
            for item in sdb.metadata_events
            if item.get("type") == "production_capture_profile"
        ]
        assert [item["event"] for item in events] == [
            "requested",
            "confirmed",
            "data_observed",
            "restore_requested",
            "restored",
        ]
    finally:
        controller.stop()
        broker.stop_broker()


def test_production_rejects_summary_only_capture_capability(
    qapplication_session, tmp_path: Path, monkeypatch
) -> None:
    controller, broker, sent = _controller(monkeypatch)
    try:
        session = controller.sessions()[0]
        controller.inject_datagram(_datagram(_capabilities(full_capture=False), 3))
        sent_before = len(sent)
        assert controller.arm_batch_recording("M50-UNSUPPORTED", tmp_path)
        assert session.capture_state is ProductionCaptureState.UNSUPPORTED
        assert not session.capture_ready
        assert len(sent) == sent_before
    finally:
        controller.stop()
        broker.stop_broker()


def test_new_session_generation_requires_fresh_capture_ack_and_data(
    qapplication_session, tmp_path: Path, monkeypatch
) -> None:
    controller, broker, sent = _controller(monkeypatch)
    try:
        assert controller.arm_batch_recording("M50-RESTART", tmp_path)
        session = controller.sessions()[0]
        first_request = sent[-1]
        controller.inject_datagram(
            _datagram(_capture_response(_capture_request_id(first_request)), 3)
        )
        controller.inject_datagram(_datagram(_data_report(), 4))
        assert session.capture_ready

        session.core.generation_changed.emit(2, ENDPOINT)
        assert not session.capture_ready
        controller.inject_datagram(_datagram(_fast_snr(30, 11.0), 5))
        second_request = sent[-1]
        assert second_request != first_request
        assert session.capture_state is ProductionCaptureState.REQUESTING

        controller.inject_datagram(
            _datagram(_capture_response(_capture_request_id(second_request)), 6)
        )
        assert session.capture_state is ProductionCaptureState.WAITING_DATA
        controller.inject_datagram(_datagram(_data_report(40), 7))
        assert session.capture_ready
    finally:
        controller.stop()
        broker.stop_broker()


def test_abort_finalization_keeps_data_when_restore_ack_cannot_arrive(
    qapplication_session, tmp_path: Path, monkeypatch
) -> None:
    controller, broker, sent = _controller(monkeypatch)
    try:
        assert controller.arm_batch_recording("M50-ABORT", tmp_path)
        session = controller.sessions()[0]
        request = sent[-1]
        controller.inject_datagram(
            _datagram(_capture_response(_capture_request_id(request)), 3)
        )
        controller.inject_datagram(_datagram(_data_report(), 4))
        assert session.capture_ready

        path = controller.finalize_recordings(
            wait_for_capture_restore=False
        )[ENDPOINT]
        assert path is not None
        assert not session.recording_armed
        assert DataImporter.open_sdb(path).quality["complete"] is True
    finally:
        controller.stop()
        broker.stop_broker()
