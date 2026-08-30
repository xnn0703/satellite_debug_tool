"""Golden vectors for the registered customer Product Service protocol."""

from __future__ import annotations

import struct

import pytest

import satellite_debug_tool.core.protocol as protocol

from satellite_debug_tool.core.product import (
    Availability,
    ControlMode,
    ExternalInsState,
    NavigationSource,
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
    ServiceExternalInsDiagnostics,
    ServiceHardwareIdentity,
    ServiceIdentity,
    ServiceLinkDetail,
    ServiceMountStatus,
    ServiceNavigationSourceInfo,
    ServiceRfLockStatus,
    ServiceSlowState,
    SERVICE_PERSISTED_DEVICE_MOUNT,
    build_frame,
    build_service_apply_rf,
    build_service_set_capture_profile,
    build_service_set_control_mode,
    build_service_set_device_mount,
    build_service_subscribe,
    decode_service_mount_status,
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

    hardware = _decode(
        CmdType.SERVICE_HARDWARE_IDENTITY,
        struct.pack(
            "<BIIIII6sB",
            1,
            1234,
            0x07,
            0x12345678,
            0x9ABCDEF0,
            0x0BADBEEF,
            bytes.fromhex("4A65A6999E4B"),
            1,
        ),
    )
    assert isinstance(hardware, ServiceHardwareIdentity)
    assert hardware.device_uid == "123456789ABCDEF00BADBEEF"
    assert hardware.mac_text == "4A:65:A6:99:9E:4B"
    assert hardware.mac_source == 1

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

    mount = build_service_set_device_mount(9, 12.5, -3.0, 1.5)
    assert mount[6:24] == struct.pack("<BIBfff", 1, 9, 5, 12.5, -3.0, 1.5)
    assert len(mount[6:-3]) == 18


def test_service_mount_status_decodes_the_fixed_frd1_contract() -> None:
    payload = struct.pack(
        "<BIII9fBBB",
        1,
        2500,
        0x7F,
        0x31445246,
        12.5,
        -3.0,
        1.5,
        0.0,
        180.0,
        -90.0,
        0.0,
        180.0,
        -90.0,
        2,
        1,
        0,
    )

    status = _decode(CmdType.SERVICE_MOUNT_STATUS, payload)

    assert isinstance(status, ServiceMountStatus)
    assert status.mount_contract_id == 0x31445246
    assert status.mount_yaw_deg == pytest.approx(12.5)
    assert status.expected_rbv_z_deg == pytest.approx(-90.0)
    assert status.rbv_verified is True
    assert status.restart_required is False
    assert SERVICE_PERSISTED_DEVICE_MOUNT == 1 << 7
    assert not hasattr(protocol, "SERVICE_APPLIED_DEVICE_MOUNT")


def _mount_status_payload(
    *,
    valid_mask: int = 0x7F,
    angles: tuple[float, ...] = (12.5, -3.0, 1.5, 0.0, 180.0, -90.0, 0.0, 180.0, -90.0),
    rbv_verified: int = 1,
    restart_required: int = 0,
) -> bytes:
    return struct.pack(
        "<BIII9fBBB",
        1,
        2500,
        valid_mask,
        0x31445246,
        *angles,
        2,
        rbv_verified,
        restart_required,
    )


@pytest.mark.parametrize("index", range(9))
def test_service_mount_status_rejects_each_non_finite_float(index: int) -> None:
    angles = [12.5, -3.0, 1.5, 0.0, 180.0, -90.0, 0.0, 180.0, -90.0]
    angles[index] = float("nan")

    with pytest.raises(CodecError, match="non-finite"):
        decode_service_mount_status(_mount_status_payload(angles=tuple(angles)))


def test_service_mount_status_rejects_infinite_float() -> None:
    angles = (float("inf"), -3.0, 1.5, 0.0, 180.0, -90.0, 0.0, 180.0, -90.0)

    with pytest.raises(CodecError, match="non-finite"):
        decode_service_mount_status(_mount_status_payload(angles=angles))


def test_service_mount_status_rejects_unknown_valid_mask_bits() -> None:
    with pytest.raises(CodecError, match="valid-mask"):
        decode_service_mount_status(_mount_status_payload(valid_mask=0x80))


@pytest.mark.parametrize(
    ("rbv_verified", "restart_required"),
    ((2, 0), (0, 2)),
)
def test_service_mount_status_rejects_non_boolean_flags(
    rbv_verified: int,
    restart_required: int,
) -> None:
    with pytest.raises(CodecError, match="boolean"):
        decode_service_mount_status(
            _mount_status_payload(
                rbv_verified=rbv_verified,
                restart_required=restart_required,
            )
        )


@pytest.mark.parametrize(
    "angles",
    (
        (181.0, 0.0, 0.0),
        (0.0, 91.0, 0.0),
        (0.0, 0.0, -181.0),
        (float("nan"), 0.0, 0.0),
    ),
)
def test_service_mount_builder_rejects_out_of_range_angles(angles) -> None:
    with pytest.raises(CodecError):
        build_service_set_device_mount(1, *angles)


def test_telemetry_generation_does_not_reuse_a_cleared_connection_waterline() -> None:
    store = ProductServiceStore()
    fast = ServiceFastState(
        1, 1, 0xFFF, 0, 0, False, 3, 3, False,
        0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
    )
    store.feed(fast)
    assert store.fast_telemetry_generation == 1
    assert store.fast_telemetry_cursor.epoch == 0
    assert store.fast_telemetry_cursor.timestamp_ms == 1

    store.clear()
    assert store.fast_telemetry_cursor.epoch == 1
    assert store.fast_telemetry_cursor.generation == 1
    assert store.fast_telemetry_cursor.timestamp_ms is None
    store.feed(fast)

    assert store.fast_telemetry_generation == 2
    assert store.fast_telemetry_cursor.epoch == 1
    assert store.fast_telemetry_cursor.timestamp_ms == 1

    mount = ServiceMountStatus(
        1, 1, 0x7F, 0x31445246,
        0.0, 0.0, 0.0,
        0.0, 180.0, -90.0,
        0.0, 180.0, -90.0,
        0, True, False,
    )
    store.feed(mount)
    assert store.mount_status_generation == 1
    assert store.mount_status_cursor.epoch == 1
    assert store.mount_status_cursor.timestamp_ms == 1
    store.clear()
    assert store.mount_status_record is None
    assert store.mount_status_cursor.epoch == 2
    assert store.mount_status_cursor.generation == 1
    assert store.mount_status_cursor.timestamp_ms is None
    store.feed(mount)
    assert store.mount_status_generation == 2


def test_navigation_source_and_external_ins_diagnostics_decode() -> None:
    source = _decode(
        CmdType.SERVICE_NAV_SOURCE_INFO,
        struct.pack("<BII7B", 1, 3000, 0x7F, 3, 3, 3, 3, 0x07, 0x0F, 0),
    )
    assert isinstance(source, ServiceNavigationSourceInfo)
    assert source.external_role_mask == 0x07
    assert source.capability_flags == 0x0F

    payload = struct.pack(
        "<BII9B3I3f4I14f",
        1,
        3000,
        0xFFF,
        3,
        0x07,
        1,
        4,
        1,
        3,
        56,
        50,
        18,
        1000,
        10000,
        1000,
        10.0,
        100.0,
        10.0,
        2,
        3,
        4,
        5,
        123.0,
        1.0,
        -2.0,
        0.2,
        0.1,
        0.1,
        0.4,
        0.5,
        0.8,
        0.05,
        0.06,
        0.07,
        0.25,
        0.5,
    )
    diagnostics = _decode(CmdType.SERVICE_EXTERNAL_INS_DIAGNOSTICS, payload)
    assert isinstance(diagnostics, ServiceExternalInsDiagnostics)
    assert diagnostics.online is True
    assert diagnostics.inspvax_hz == pytest.approx(10.0)
    assert diagnostics.yaw_std_deg == pytest.approx(0.2)
    assert diagnostics.differential_age_s == pytest.approx(0.5)


def test_service_store_distinguishes_external_ins_na_online_and_stale() -> None:
    store = ProductServiceStore()
    store.feed(
        ServiceNavigationSourceInfo(1, 100, 0x7F, 2, 1, 0, 0, 0, 0x01, 0),
        received_monotonic=10.0,
    )
    store.feed(
        ServiceExternalInsDiagnostics(
            1,
            100,
            0x01,
            0,
            0,
            False,
            0,
            False,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0.0,
            0.0,
            0.0,
            0,
            0,
            0,
            0,
            *([0.0] * 14),
        ),
        received_monotonic=10.0,
    )
    not_applicable = store.snapshot(now_monotonic=10.1)
    assert not_applicable.navigation_sources.external_ins_supported.value is True
    assert not_applicable.navigation_sources.external_ins_configured.value is False
    assert not_applicable.navigation_sources.external_role_mask.value == 0
    assert not_applicable.external_ins.source.value == NavigationSource.NONE
    assert not_applicable.external_ins.online.availability == Availability.UNSUPPORTED

    store.feed(
        ServiceNavigationSourceInfo(1, 200, 0x7F, 3, 3, 3, 3, 0x07, 0x0F, 0),
        received_monotonic=20.0,
    )
    configured = ServiceExternalInsDiagnostics(
        1,
        200,
        0xFFF,
        3,
        0x07,
        True,
        4,
        True,
        3,
        56,
        50,
        18,
        1000,
        10000,
        1000,
        10.0,
        100.0,
        10.0,
        2,
        3,
        4,
        5,
        123.0,
        1.0,
        -2.0,
        0.2,
        0.1,
        0.1,
        0.4,
        0.5,
        0.8,
        0.05,
        0.06,
        0.07,
        0.25,
        0.5,
    )
    store.feed(configured, received_monotonic=20.0)
    online = store.snapshot(now_monotonic=20.5)
    assert online.navigation_sources.external_ins_source.value == NavigationSource.BYNAV
    assert online.navigation_sources.external_ins_configured.value is True
    assert online.external_ins.state.value == ExternalInsState.YAW_ALIGNED
    assert online.external_ins.online.value is True
    assert online.external_ins.inspvax_hz.value == pytest.approx(10.0)

    stale = store.snapshot(now_monotonic=23.1)
    assert stale.external_ins.online.availability == Availability.STALE
    assert stale.external_ins.yaw_std_deg.availability == Availability.STALE


def test_service_store_overrides_legacy_and_marks_stale() -> None:
    store = ProductServiceStore()
    store.feed(
        ServiceIdentity(1, 10, 0x17, "afd01", "AFD01-A1B2", "0.0.130", "", 2),
        received_monotonic=100.0,
    )
    store.feed(
        ServiceHardwareIdentity(
            1,
            10,
            0x07,
            (0x12345678, 0x9ABCDEF0, 0x0BADBEEF),
            bytes.fromhex("4A65A6999E4B"),
            1,
        ),
        received_monotonic=100.0,
    )
    store.feed(
        ServiceFastState(1, 20, 0xFFF, 0, 3, True, 3, 3, False, 1, 2, 3, 4, 5, 6),
        received_monotonic=100.0,
    )
    store.feed(
        ServiceLinkDetail(
            1, 20, 0x3F, True, 18250.0, 28050.0, 2, 0.0, 25544, ""
        ),
        received_monotonic=100.0,
    )
    store.feed(
        ServiceRfLockStatus(1, 20, 0x07, 0x05),
        received_monotonic=100.0,
    )
    snapshot = store.snapshot(now_monotonic=100.5)
    assert snapshot.source == "product_service"
    assert snapshot.identity.serial_number.value == "AFD01-A1B2"
    assert snapshot.identity.device_uid.value == "123456789ABCDEF00BADBEEF"
    assert snapshot.identity.mac_address.value == "4A:65:A6:99:9E:4B"
    assert snapshot.identity.mac_source.value == 1
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

    stale = store.snapshot(now_monotonic=102.0)
    assert stale.operation.snr_db.value == 6
    assert stale.operation.snr_db.availability == Availability.STALE
    assert stale.operation.modem_online.availability == Availability.VALID
    link_stale = store.snapshot(now_monotonic=104.0)
    assert link_stale.operation.modem_online.availability == Availability.STALE


def test_service_fields_keep_their_record_receipt_time_when_timestamps_collide() -> None:
    store = ProductServiceStore()
    store.feed(
        ServiceFastState(1, 20, 0xFFF, 0, 3, True, 3, 3, False, 1, 2, 3, 4, 5, 6),
        received_monotonic=100.0,
    )
    store.feed(
        ServiceLinkDetail(
            1, 20, 0x3F, True, 18250.0, 28050.0, 2, 0.0, 25544, ""
        ),
        received_monotonic=101.0,
    )

    operation = store.snapshot(now_monotonic=101.0).operation

    assert operation.snr_db.received_monotonic_s == 100.0
    assert operation.modem_online.received_monotonic_s == 101.0


def test_rf_lock_status_preserves_partial_and_stale_paths() -> None:
    store = ProductServiceStore()
    store.feed(
        ServiceRfLockStatus(1, 50, 0x01, 0x01),
        received_monotonic=100.0,
    )

    partial = store.snapshot(now_monotonic=100.1).operation
    assert partial.clock_pll_locked.value is True
    assert partial.clock_pll_locked.availability == Availability.VALID
    assert partial.tx_pll_locked.availability == Availability.UNSUPPORTED
    assert partial.rx_pll_locked.availability == Availability.UNSUPPORTED

    stale = store.snapshot(now_monotonic=101.1).operation
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


def test_fast_tx_state_remains_authoritative_after_partial_slow_state() -> None:
    store = ProductServiceStore()
    store.feed(
        ServiceFastState(
            1, 20, 1 << 5, 0, 0, False, 0, 0, True,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        ),
        received_monotonic=10.0,
    )
    store.feed(
        ServiceSlowState(
            1, 20, 1 << 0,
            31.8, 0.0, 0.0, 0.0, 0.0, 0, 0, False,
        ),
        received_monotonic=10.0,
    )

    operation = store.snapshot(now_monotonic=10.1).operation

    assert operation.tx_enabled.value is True
    assert operation.tx_enabled.availability is Availability.VALID


def test_slow_tx_initializes_state_until_fast_tx_field_arrives() -> None:
    store = ProductServiceStore()
    store.feed(
        ServiceSlowState(
            1, 20, 1 << 7,
            0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, True,
        ),
        received_monotonic=10.0,
    )

    assert store.snapshot(now_monotonic=10.1).operation.tx_enabled.value is True


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
