"""Golden vectors for the stable AFD01 product-service protocol."""

from __future__ import annotations

import struct

import pytest

from satellite_debug_tool.core.product import (
    Availability,
    ControlMode,
    ProductServiceStore,
    SatelliteMode,
    TrackingPhase,
    U32UptimeUnwrapper,
)
from satellite_debug_tool.core.protocol import (
    CmdType,
    CodecError,
    FrameReceiverV2,
    ServiceCapabilities,
    ServiceComponentHealth,
    ServiceFastState,
    ServiceIdentity,
    ServiceLinkDetail,
    ServiceRfLockStatus,
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

    link_payload = struct.pack(
        "<BIIBffBfI",
        1,
        2000,
        0x7F,
        1,
        18250.0,
        28050.0,
        2,
        0.0,
        25544,
    ) + b"\x03ISS"
    link = _decode(CmdType.SERVICE_LINK_DETAIL, link_payload)
    assert isinstance(link, ServiceLinkDetail)
    assert link.modem_online is True
    assert link.satellite_id == 25544
    assert link.satellite_name == "ISS"

    rf_lock = _decode(
        CmdType.SERVICE_RF_LOCK_STATUS,
        struct.pack("<BIIB", 1, 2000, 0x07, 0x05),
    )
    assert isinstance(rf_lock, ServiceRfLockStatus)
    assert rf_lock.valid_mask == 0x07
    assert rf_lock.lock_mask == 0x05

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
    store.feed(
        ServiceLinkDetail(
            1, 20, 0x3F, True, 18250.0, 28050.0, 2, 0.0, 25544, ""
        ),
        received_wallclock=100.0,
    )
    store.feed(
        ServiceRfLockStatus(1, 20, 0x07, 0x05),
        received_wallclock=100.0,
    )
    snapshot = store.snapshot(now_wallclock=100.5)
    assert snapshot.source == "product_service"
    assert snapshot.identity.serial_number.value == "AFD01-A1B2"
    assert snapshot.operation.control_mode.value == ControlMode.AUTO
    assert snapshot.operation.tracking_phase.value == TrackingPhase.LOCKED
    assert snapshot.operation.snr_db.availability == Availability.VALID
    assert snapshot.operation.modem_online.value is True
    assert snapshot.operation.rx_lo_mhz.value == 18250.0
    assert snapshot.operation.satellite_mode.value == SatelliteMode.LEO_TLE
    assert snapshot.operation.satellite_id.value == 25544
    assert snapshot.operation.clock_pll_locked.value is True
    assert snapshot.operation.tx_pll_locked.value is False
    assert snapshot.operation.rx_pll_locked.value is True

    stale = store.snapshot(now_wallclock=102.0)
    assert stale.operation.snr_db.value == 6
    assert stale.operation.snr_db.availability == Availability.STALE
    assert stale.operation.modem_online.availability == Availability.VALID
    link_stale = store.snapshot(now_wallclock=104.0)
    assert link_stale.operation.modem_online.availability == Availability.STALE


def test_rf_lock_status_preserves_partial_and_stale_paths() -> None:
    store = ProductServiceStore()
    store.feed(
        ServiceRfLockStatus(1, 50, 0x01, 0x01),
        received_wallclock=100.0,
    )

    partial = store.snapshot(now_wallclock=100.1).operation
    assert partial.clock_pll_locked.value is True
    assert partial.clock_pll_locked.availability == Availability.VALID
    assert partial.tx_pll_locked.availability == Availability.UNSUPPORTED
    assert partial.rx_pll_locked.availability == Availability.UNSUPPORTED

    stale = store.snapshot(now_wallclock=101.1).operation
    assert stale.clock_pll_locked.value is True
    assert stale.clock_pll_locked.availability == Availability.STALE


def test_link_detail_rejects_trailing_or_invalid_utf8_data() -> None:
    fixed = struct.pack("<BIIBffBfI", 1, 20, 0, 0, 0.0, 0.0, 0, 0.0, 0)
    receiver = FrameReceiverV2()

    assert receiver.feed(build_frame(CmdType.SERVICE_LINK_DETAIL, fixed + b"\x01\xff")) == []
    assert receiver.decode_errors == 1

    receiver = FrameReceiverV2()
    assert receiver.feed(build_frame(CmdType.SERVICE_LINK_DETAIL, fixed + b"\x00\x00")) == []
    assert receiver.decode_errors == 1


def test_unsupported_service_schema_is_rejected() -> None:
    payload = struct.pack("<BII6B6f", 2, 20, 0xFFF, 0, 3, 1, 3, 3, 0, *([0.0] * 6))
    receiver = FrameReceiverV2()

    assert receiver.feed(build_frame(CmdType.SERVICE_FAST_STATE, payload)) == []
    assert receiver.decode_errors == 1


def test_unknown_control_mode_is_not_mapped_to_auto() -> None:
    store = ProductServiceStore()
    store.feed(ServiceFastState(1, 20, 0x01, 7, 0, False, 0, 0, False, 0, 0, 0, 0, 0, 0))

    assert store.snapshot().operation.control_mode.value == ControlMode.UNKNOWN


def _fast_snr(timestamp: int, snr_db: float) -> ServiceFastState:
    return ServiceFastState(
        1, timestamp, 1 << 11, 0, 0, False, 0, 0, False,
        0.0, 0.0, 0.0, 0.0, 0.0, snr_db,
    )


def test_u32_uptime_unwrapper_handles_rollover_and_rejects_old_packets() -> None:
    clock = U32UptimeUnwrapper()
    first = clock.add(0xFFFFFF00)
    wrapped = clock.add(0x00000100)

    assert first == 0xFFFFFF00
    assert wrapped == 0x100000100
    assert clock.add(0x00000080) is None
    assert clock.add(0x00000200) == 0x100000200

    clock.reset()
    assert clock.add(250) == 250


def test_service_snr_history_uses_uptime_seconds_prunes_and_resets() -> None:
    store = ProductServiceStore()
    store.feed(_fast_snr(1000, 10.0))
    store.feed(_fast_snr(301000, 11.0))
    store.feed(_fast_snr(302000, 12.0))

    times, values = store.snr_history()
    assert times.tolist() == pytest.approx([301.0, 302.0])
    assert values.tolist() == pytest.approx([11.0, 12.0])

    store.clear()
    store.feed(_fast_snr(250, 13.0))
    times, values = store.snr_history()
    assert times.tolist() == pytest.approx([0.25])
    assert values.tolist() == pytest.approx([13.0])


def test_service_snr_history_remains_monotonic_across_u32_rollover() -> None:
    store = ProductServiceStore()
    store.feed(_fast_snr(0xFFFFFF00, 10.0))
    store.feed(_fast_snr(0x00000100, 11.0))

    times, values = store.snr_history()
    assert times.tolist() == pytest.approx([0xFFFFFF00 / 1000.0, 0x100000100 / 1000.0])
    assert values.tolist() == pytest.approx([10.0, 11.0])
    assert times[1] > times[0]


def test_service_control_builders_reject_invalid_values() -> None:
    with pytest.raises(CodecError):
        build_service_set_control_mode(1, 7)
    with pytest.raises(CodecError):
        build_service_apply_rf(1, float("nan"), 29798.0, 0, 0)
    with pytest.raises(CodecError):
        build_service_apply_rf(1, 19798.0, 29798.0, 4, 0)
