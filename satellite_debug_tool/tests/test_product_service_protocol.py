"""Golden vectors for the stable AFD01 product-service protocol."""

from __future__ import annotations

import struct

import pytest

from satellite_debug_tool.core.product import (
    Availability,
    ControlMode,
    ProductServiceStore,
    TrackingPhase,
)
from satellite_debug_tool.core.protocol import (
    CmdType,
    CodecError,
    FrameReceiverV2,
    ServiceCapabilities,
    ServiceComponentHealth,
    ServiceFastState,
    ServiceIdentity,
    ServiceSlowState,
    build_frame,
    build_service_apply_rf,
    build_service_set_capture_profile,
    build_service_set_control_mode,
    build_service_subscribe,
)


def _decode(cmd: int, payload: bytes):
    records = FrameReceiverV2().feed(build_frame(cmd, payload))
    assert len(records) == 1
    return records[0]


def test_service_identity_and_fixed_records_decode() -> None:
    identity_payload = struct.pack("<BII", 1, 1234, 0x17)
    for text in ("afd01", "AFD01-A1B2", "0.0.130", ""):
        encoded = text.encode()
        identity_payload += bytes([len(encoded)]) + encoded
    identity_payload += b"\x02"
    identity = _decode(CmdType.SERVICE_IDENTITY, identity_payload)
    assert isinstance(identity, ServiceIdentity)
    assert identity.serial_number == "AFD01-A1B2"
    assert identity.valid_mask == 0x17

    fast_payload = struct.pack(
        "<BII6B6f",
        1,
        2000,
        0xFFF,
        0,
        3,
        1,
        3,
        3,
        1,
        1.0,
        2.0,
        3.0,
        120.0,
        35.0,
        18.5,
    )
    fast = _decode(CmdType.SERVICE_FAST_STATE, fast_payload)
    assert isinstance(fast, ServiceFastState)
    assert fast.locked is True
    assert fast.snr_db == 18.5

    slow_payload = struct.pack(
        "<BII5f3B", 1, 2000, 0xFF, 31.8, 118.8, 12.0, 19798.0, 29798.0, 2, 3, 1
    )
    slow = _decode(CmdType.SERVICE_SLOW_STATE, slow_payload)
    assert isinstance(slow, ServiceSlowState)
    assert slow.tx_frequency_mhz == 29798.0

    item = struct.pack("<BBffI", 0x0F, 1, 42.0, 12.1, 0x24)
    components = _decode(
        CmdType.SERVICE_COMPONENT_HEALTH,
        struct.pack("<BI", 1, 2000) + item * 3,
    )
    assert isinstance(components, ServiceComponentHealth)
    assert components.tx_array.version == 0x24

    capabilities = _decode(
        CmdType.SERVICE_CAPABILITIES,
        struct.pack(
            "<BII4fBBB", 1, 2000, 0xFF, 17700.0, 21200.0, 27500.0, 31000.0, 0x0F, 0x03, 0x03
        ),
    )
    assert isinstance(capabilities, ServiceCapabilities)
    assert capabilities.feature_flags == 0x03


def test_service_control_builders_include_context() -> None:
    subscribe = build_service_subscribe(0x12345678, 10)
    assert subscribe[3] == CmdType.SERVICE_CONTROL_REQUEST
    assert subscribe[6:13] == struct.pack("<BIBB", 1, 0x12345678, 0, 10)

    apply_rf = build_service_apply_rf(7, 19798.0, 29798.0, 2, 3)
    assert apply_rf[6:12] == struct.pack("<BIB", 1, 7, 2)
    assert struct.unpack("<ffBB", apply_rf[12:22]) == (19798.0, 29798.0, 2, 3)

    capture = build_service_set_capture_profile(8, True)
    assert capture[6:13] == struct.pack("<BIBB", 1, 8, 4, 1)


def test_service_store_overrides_legacy_and_marks_stale() -> None:
    store = ProductServiceStore()
    store.feed(
        ServiceIdentity(1, 10, 0x17, "afd01", "AFD01-A1B2", "0.0.130", "", 2),
        received_wallclock=100.0,
    )
    store.feed(
        ServiceFastState(1, 20, 0xFFF, 0, 3, True, 3, 3, False, 1, 2, 3, 4, 5, 6),
        received_wallclock=100.0,
    )
    snapshot = store.snapshot(now_wallclock=100.5)
    assert snapshot.source == "product_service"
    assert snapshot.identity.serial_number.value == "AFD01-A1B2"
    assert snapshot.operation.control_mode.value == ControlMode.AUTO
    assert snapshot.operation.tracking_phase.value == TrackingPhase.LOCKED
    assert snapshot.operation.snr_db.availability == Availability.VALID

    stale = store.snapshot(now_wallclock=102.0)
    assert stale.operation.snr_db.value == 6
    assert stale.operation.snr_db.availability == Availability.STALE


def test_unsupported_service_schema_is_rejected() -> None:
    payload = struct.pack("<BII6B6f", 2, 20, 0xFFF, 0, 3, 1, 3, 3, 0, *([0.0] * 6))
    receiver = FrameReceiverV2()

    assert receiver.feed(build_frame(CmdType.SERVICE_FAST_STATE, payload)) == []
    assert receiver.decode_errors == 1


def test_unknown_control_mode_is_not_mapped_to_auto() -> None:
    store = ProductServiceStore()
    store.feed(ServiceFastState(1, 20, 0x01, 7, 0, False, 0, 0, False, 0, 0, 0, 0, 0, 0))

    assert store.snapshot().operation.control_mode.value == ControlMode.UNKNOWN


def test_service_control_builders_reject_invalid_values() -> None:
    with pytest.raises(CodecError):
        build_service_set_control_mode(1, 7)
    with pytest.raises(CodecError):
        build_service_apply_rf(1, float("nan"), 29798.0, 0, 0)
    with pytest.raises(CodecError):
        build_service_apply_rf(1, 19798.0, 29798.0, 4, 0)
