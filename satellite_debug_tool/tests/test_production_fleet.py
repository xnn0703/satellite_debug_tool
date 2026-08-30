"""M19 shared UDP hub, identity isolation, and pre-armed recording tests."""

from __future__ import annotations

import os
import math
from pathlib import Path
import socket
import struct
import time

import pytest

from satellite_debug_tool.core.production import (
    DeviceSessionState,
    FleetConfigurationError,
    FleetController,
    FleetDatagram,
    UdpFleetHub,
)
from satellite_debug_tool.core.protocol import CmdType, build_frame
from satellite_debug_tool.io.data_recorder import SDB_FOOTER, SDB_MAGIC


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _identity(
    serial_number: str,
    *,
    model: str = "AFD01",
    timestamp: int = 1,
    service_protocol: int = 2,
) -> bytes:
    payload = struct.pack("<BII", 1, timestamp, 0x17)
    for text in (model, serial_number, "0.0.130", "0.0.3"):
        encoded = text.encode("utf-8")
        payload += bytes([len(encoded)]) + encoded
    payload += bytes([service_protocol])
    return build_frame(CmdType.SERVICE_IDENTITY, payload)


def _fast_snr(timestamp: int, snr_db: float, *, valid: bool = True) -> bytes:
    payload = struct.pack(
        "<BII6B6f",
        1,
        timestamp,
        (1 << 11) if valid else 0,
        0,
        3,
        1,
        3,
        4,
        1,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        snr_db,
    )
    return build_frame(CmdType.SERVICE_FAST_STATE, payload)


def _hardware_identity(
    uid_words: tuple[int, int, int],
    *,
    mac: bytes = bytes.fromhex("4A65A6999E4B"),
    timestamp: int = 1,
) -> bytes:
    payload = struct.pack(
        "<BIIIII6sB",
        1,
        timestamp,
        0x07,
        *uid_words,
        mac,
        1,
    )
    return build_frame(CmdType.SERVICE_HARDWARE_IDENTITY, payload)


def _datagram(endpoint: tuple[str, int], data: bytes, sequence: int = 1) -> FleetDatagram:
    return FleetDatagram(
        endpoint=endpoint,
        data=data,
        wall_time_ns=1_000_000_000 + sequence,
        monotonic_ns=2_000_000_000 + sequence,
    )


def _controller(max_devices: int = 4) -> FleetController:
    return FleetController(
        discovery_cidr="192.168.1.0/24",
        local_port=0,
        device_port=4004,
        max_devices=max_devices,
    )


def test_hub_configuration_rejects_unsafe_scan_ranges() -> None:
    with pytest.raises(FleetConfigurationError, match="1024 hosts"):
        UdpFleetHub(discovery_cidr="10.0.0.0/16", local_port=0)
    with pytest.raises(FleetConfigurationError, match="IPv4"):
        UdpFleetHub(discovery_cidr="::1/128", local_port=0)
    with pytest.raises(FleetConfigurationError, match="max_devices"):
        FleetController(
            discovery_cidr="127.0.0.1/32",
            local_port=0,
            max_devices=5,
        )


def test_one_hub_socket_discovers_and_preserves_source_endpoint(qapp) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(2.0)
    device_port = server.getsockname()[1]
    hub = UdpFleetHub(
        discovery_cidr="127.0.0.1/32",
        local_port=0,
        device_port=device_port,
        discovery_interval_s=0.2,
    )
    received: list[FleetDatagram] = []
    hub.datagram_received.connect(received.append)
    try:
        assert hub.start_hub()
        probe, host_endpoint = server.recvfrom(2048)
        assert probe[3] == CmdType.SERVICE_CONTROL_REQUEST
        assert probe[12] == 1
        server.sendto(_identity("AFD01-LOOPBACK"), host_endpoint)
        deadline = time.monotonic() + 2.0
        while not received and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert len(received) == 1
        assert received[0].endpoint == ("127.0.0.1", device_port)
        assert received[0].data[3] == CmdType.SERVICE_IDENTITY
        assert hub.statistics.received_datagrams == 1
    finally:
        assert hub.stop_hub()
        server.close()


def test_controller_assigns_four_stable_slots_and_rejects_the_fifth() -> None:
    controller = _controller()
    rejected: list[str] = []
    controller.endpoint_rejected.connect(rejected.append)
    for index in range(1, 6):
        endpoint = (f"192.168.1.{10 + index}", 4004)
        controller._on_datagram(
            _datagram(endpoint, _identity(f"AFD01-{index}"), index)
        )

    assert [session.slot for session in controller.sessions()] == [1, 2, 3, 4]
    assert [session.serial_number for session in controller.sessions()] == [
        "AFD01-1",
        "AFD01-2",
        "AFD01-3",
        "AFD01-4",
    ]
    assert rejected == ["192.168.1.15:4004"]
    controller.stop()


def test_discovery_datagram_is_decoded_once_before_session_application() -> None:
    controller = _controller()
    endpoint = ("192.168.1.12", 4004)

    controller._on_datagram(_datagram(endpoint, _identity("AFD01-ONCE")))

    session = controller.sessions()[0]
    assert session.serial_number == "AFD01-ONCE"
    assert session.receiver.frames_ok == 0
    controller.stop()


def test_identified_device_is_promoted_from_discovery_to_capture_rate() -> None:
    controller = _controller()
    sent: list[tuple[tuple[str, int], bytes]] = []

    def capture_send(endpoint: tuple[str, int], frame: bytes) -> bool:
        sent.append((endpoint, frame))
        return True

    controller.hub.send_to = capture_send
    endpoint = ("192.168.1.12", 4004)

    controller._on_datagram(_datagram(endpoint, _identity("AFD01-RATE")))

    assert len(sent) == 1
    assert sent[0][0] == endpoint
    assert sent[0][1][3] == CmdType.SERVICE_CONTROL_REQUEST
    assert sent[0][1][12] == 20
    controller.stop()


def test_session_keeps_full_snr_samples_on_workstation_timeline() -> None:
    from satellite_debug_tool.core.production import DeviceSession

    endpoint = ("192.168.1.12", 4004)
    session = DeviceSession(endpoint, 1)
    origin_ns = 2_000_000_000
    for index in range(120):
        session.feed_datagram(
            FleetDatagram(
                endpoint=endpoint,
                data=_fast_snr(index * 50, 10.0 + index / 100.0),
                wall_time_ns=1_000_000_000 + index * 50_000_000,
                monotonic_ns=origin_ns + index * 50_000_000,
            )
        )
    session.feed_datagram(
        FleetDatagram(
            endpoint=endpoint,
            data=_fast_snr(6000, 99.0, valid=False),
            wall_time_ns=7_000_000_000,
            monotonic_ns=origin_ns + 6_000_000_000,
        )
    )
    session.feed_datagram(
        FleetDatagram(
            endpoint=endpoint,
            data=_fast_snr(6050, math.nan),
            wall_time_ns=7_050_000_000,
            monotonic_ns=origin_ns + 6_050_000_000,
        )
    )

    history = session.snr_history(
        window_s=300.0,
        now_monotonic_ns=origin_ns + 6_050_000_000,
    )
    assert len(history) == 120
    assert history[0].monotonic_ns == origin_ns
    assert history[-1].device_uptime_ms == 5950
    assert history[-1].value_db == pytest.approx(11.19)

    recent = session.snr_history(
        window_s=0.2,
        now_monotonic_ns=origin_ns + 5_950_000_000,
    )
    assert [sample.device_uptime_ms for sample in recent] == [5750, 5800, 5850, 5900, 5950]
    session.clear_snr_history()
    assert session.snr_history(now_monotonic_ns=origin_ns + 6_000_000_000) == ()


def test_valid_zero_snr_is_preserved_as_device_evidence() -> None:
    from satellite_debug_tool.core.production import DeviceSession

    endpoint = ("192.168.1.12", 4004)
    session = DeviceSession(endpoint, 1)
    session.feed_datagram(
        FleetDatagram(
            endpoint=endpoint,
            data=_fast_snr(100, 0.0, valid=True),
            wall_time_ns=1_000_000_000,
            monotonic_ns=2_000_000_000,
        )
    )

    history = session.snr_history(
        window_s=1.0,
        now_monotonic_ns=2_000_000_000,
    )
    assert len(history) == 1
    assert history[0].value_db == 0.0


def test_duplicate_serial_number_marks_both_sessions_conflicted() -> None:
    controller = _controller()
    conflicts: list[str] = []
    controller.identity_conflict.connect(conflicts.append)
    controller._on_datagram(
        _datagram(("192.168.1.12", 4004), _identity("AFD01-DUP"), 1)
    )
    controller._on_datagram(
        _datagram(("192.168.1.13", 4004), _identity("AFD01-DUP"), 2)
    )

    assert len(controller.sessions()) == 2
    assert all(
        session.state == DeviceSessionState.CONFLICT
        for session in controller.sessions()
    )
    assert "duplicate serial number" in conflicts[0]

    # Repeated identity traffic is only fresh transport evidence.  Neither an
    # identical record nor the same identity with a newer device timestamp may
    # erase an already-proven collision.
    first_endpoint = ("192.168.1.12", 4004)
    controller._on_datagram(
        _datagram(first_endpoint, _identity("AFD01-DUP"), 3)
    )
    assert all(
        session.state == DeviceSessionState.CONFLICT
        for session in controller.sessions()
    )
    controller._on_datagram(
        _datagram(
            first_endpoint,
            _identity("AFD01-DUP", timestamp=2),
            4,
        )
    )
    assert all(
        session.state == DeviceSessionState.CONFLICT
        for session in controller.sessions()
    )
    controller.stop()


def test_uid_is_primary_for_offline_endpoint_migration() -> None:
    controller = _controller()
    migrations: list[tuple[str, str, str]] = []
    controller.endpoint_migrated.connect(
        lambda identity, old, new: migrations.append((identity, old, new))
    )
    uid = (0x12345678, 0x9ABCDEF0, 0x0BADBEEF)
    old_endpoint = ("192.168.1.12", 4004)
    new_endpoint = ("192.168.1.22", 4004)
    controller._on_datagram(_datagram(old_endpoint, _hardware_identity(uid), 1))
    controller._on_datagram(_datagram(old_endpoint, _identity("AFD01-202607N001"), 2))
    original = controller.sessions()[0]

    controller._on_datagram(
        _datagram(new_endpoint, _hardware_identity(uid), 4_000_000_002)
    )

    assert controller.sessions() == (original,)
    assert original.endpoint == new_endpoint
    assert original.device_uid == "123456789ABCDEF00BADBEEF"
    assert original.identity_key == "uid:123456789ABCDEF00BADBEEF"
    assert migrations == [
        ("AFD01-202607N001", "192.168.1.12:4004", "192.168.1.22:4004")
    ]
    controller.stop()


def test_same_serial_with_different_uid_is_a_conflict() -> None:
    controller = _controller()
    conflicts: list[str] = []
    controller.identity_conflict.connect(conflicts.append)
    first = ("192.168.1.12", 4004)
    second = ("192.168.1.13", 4004)
    controller._on_datagram(
        _datagram(first, _hardware_identity((1, 2, 3), mac=bytes.fromhex("020000000001")), 1)
    )
    controller._on_datagram(_datagram(first, _identity("AFD01-202607N001"), 2))
    controller._on_datagram(
        _datagram(second, _hardware_identity((4, 5, 6), mac=bytes.fromhex("020000000002")), 3)
    )
    controller._on_datagram(_datagram(second, _identity("AFD01-202607N001"), 4))

    assert all(session.state == DeviceSessionState.CONFLICT for session in controller.sessions())
    assert any("different MCU UIDs" in conflict for conflict in conflicts)
    controller.stop()


def test_duplicate_mac_with_different_uid_is_a_conflict() -> None:
    controller = _controller()
    conflicts: list[str] = []
    controller.identity_conflict.connect(conflicts.append)
    mac = bytes.fromhex("020000000001")
    controller._on_datagram(
        _datagram(("192.168.1.12", 4004), _hardware_identity((1, 2, 3), mac=mac), 1)
    )
    controller._on_datagram(
        _datagram(("192.168.1.13", 4004), _hardware_identity((4, 5, 6), mac=mac), 2)
    )

    assert all(session.state == DeviceSessionState.CONFLICT for session in controller.sessions())
    assert any("duplicate MAC address" in conflict for conflict in conflicts)
    controller.stop()


def test_offline_device_keeps_slot_and_recorder_when_endpoint_changes(
    tmp_path: Path,
) -> None:
    controller = _controller()
    migrations: list[tuple[str, str, str]] = []
    controller.endpoint_migrated.connect(
        lambda serial, old, new: migrations.append((serial, old, new))
    )
    output_root = tmp_path / "PILOT-MIGRATE"
    controller.arm_batch_recording("PILOT-MIGRATE", output_root)
    old_endpoint = ("192.168.1.12", 4004)
    new_endpoint = ("192.168.1.22", 4004)
    controller._on_datagram(
        _datagram(old_endpoint, _identity("AFD01-MOVE"), 1)
    )
    original = controller.sessions()[0]

    controller._on_datagram(
        _datagram(new_endpoint, _identity("AFD01-MOVE"), 4_000_000_001)
    )

    assert controller.sessions() == (original,)
    assert original.slot == 1
    assert original.endpoint == new_endpoint
    assert original.recording_armed
    assert migrations == [
        ("AFD01-MOVE", "192.168.1.12:4004", "192.168.1.22:4004")
    ]
    controller.stop()


def test_unsupported_hardware_never_becomes_identified() -> None:
    controller = _controller()
    controller._on_datagram(
        _datagram(
            ("192.168.1.12", 4004),
            _identity("ESA01-001", model="ESA01"),
        )
    )
    session = controller.sessions()[0]
    assert session.state == DeviceSessionState.UNSUPPORTED
    assert session.serial_number == "ESA01-001"
    controller.stop()


def test_afd01c_protocol_v8_becomes_identified() -> None:
    controller = _controller()
    controller._on_datagram(
        _datagram(
            ("192.168.1.12", 4004),
            _identity(
                "AFD01C-001",
                model="AFD01C",
                service_protocol=8,
            ),
        )
    )

    session = controller.sessions()[0]
    assert session.state == DeviceSessionState.IDENTIFIED
    assert session.hardware_type == "AFD01C"
    assert session.serial_number == "AFD01C-001"
    controller.stop()


def test_supported_afd01c_without_serial_waits_for_identity_configuration() -> None:
    controller = _controller()
    controller._on_datagram(
        _datagram(
            ("192.168.1.12", 4004),
            _identity("", model="AFD01C", service_protocol=8),
        )
    )

    session = controller.sessions()[0]
    assert session.state == DeviceSessionState.IDENTITY_PENDING
    assert session.hardware_type == "AFD01C"
    assert session.serial_number == ""
    controller.stop()


@pytest.mark.parametrize(
    "serial_number",
    ("-", "unknown", "AFD01-dev", "未配置"),
)
def test_placeholder_serial_never_becomes_a_production_identity(
    serial_number: str,
) -> None:
    controller = _controller()
    controller._on_datagram(
        _datagram(
            ("192.168.1.12", 4004),
            _identity(serial_number, model="AFD01", service_protocol=8),
        )
    )

    session = controller.sessions()[0]
    assert session.state is DeviceSessionState.IDENTITY_PENDING
    assert session.serial_number == ""
    assert session.identity_key == ""
    controller.stop()


def test_afd01c_requires_product_service_v8() -> None:
    controller = _controller()
    controller._on_datagram(
        _datagram(
            ("192.168.1.12", 4004),
            _identity(
                "AFD01C-OLD",
                model="AFD01C",
                service_protocol=7,
            ),
        )
    )

    assert controller.sessions()[0].state == DeviceSessionState.UNSUPPORTED
    controller.stop()


def test_afd01c_without_serial_does_not_hide_unsupported_protocol() -> None:
    controller = _controller()
    controller._on_datagram(
        _datagram(
            ("192.168.1.12", 4004),
            _identity("", model="AFD01C", service_protocol=7),
        )
    )

    assert controller.sessions()[0].state == DeviceSessionState.UNSUPPORTED
    controller.stop()


def test_prearmed_fleet_records_the_first_valid_device_datagram(
    tmp_path: Path,
) -> None:
    controller = _controller()
    output_root = tmp_path / "PILOT-001"
    assert controller.arm_batch_recording("PILOT-001", output_root)
    endpoint = ("192.168.1.12", 4004)
    first_frame = _identity("AFD01-REC")

    controller._on_datagram(_datagram(endpoint, first_frame))
    session = controller.sessions()[0]
    assert session.recording_armed
    assert session.serial_number == "AFD01-REC"

    recordings = controller.finalize_recordings()
    path = recordings[endpoint]
    assert path is not None
    assert path.parent == output_root / "devices" / "AFD01-REC" / "evidence"
    contents = path.read_bytes()
    assert contents.startswith(SDB_MAGIC)
    assert contents.endswith(SDB_FOOTER)
    assert first_frame in contents
    assert session.recording_complete is True
    controller.stop()


def test_fleet_rejects_a_second_batch_with_direct_evidence_reason(
    tmp_path: Path,
) -> None:
    controller = _controller()
    assert controller.arm_batch_recording("PILOT-001", tmp_path / "PILOT-001")

    with pytest.raises(
        RuntimeError,
        match="fleet evidence recording already belongs to another batch",
    ):
        controller.arm_batch_recording("PILOT-002", tmp_path / "PILOT-002")

    controller.stop()


def test_participant_freeze_requires_created_evidence_recording() -> None:
    controller = _controller()

    with pytest.raises(
        RuntimeError,
        match="batch evidence recording has not been created",
    ):
        controller.freeze_batch_participants(("uid:001",))

    controller.stop()
