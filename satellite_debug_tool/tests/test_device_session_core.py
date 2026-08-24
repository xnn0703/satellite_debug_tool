"""Shared device-session authority and telemetry contracts."""

from __future__ import annotations

import struct

import pytest

from satellite_debug_tool.core.data import TelemetrySeriesStore
from satellite_debug_tool.core.product import (
    Availability,
    ControlMode,
    ProductSnapshotResolver,
    ProductSource,
    ProductSourceState,
    ValueQuality,
)
from satellite_debug_tool.core.profile import CHANNEL_ROLE_SNR, ProfileStore
from satellite_debug_tool.core.product import LegacyV2Projector, ProductServiceStore
from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    ChannelSample,
    CmdType,
    CommandResponse,
    DataReport,
    MetaInfo,
    ProfileSemanticChannelEntry,
    ProfileSemanticsReport,
    ServiceIdentity,
    ServiceControlOp,
    ServiceControlResponse,
    RespCode,
    build_frame,
    build_request_meta_info,
)
from satellite_debug_tool.core.data import StateStore
from satellite_debug_tool.core.session import (
    CaptureProfileController,
    CaptureProfileResult,
    DeviceSessionCore,
    DebugController,
    DebugRequestResult,
    ProductControlController,
    ProductControlStatus,
    ProductSubscriptionController,
    SessionAuthorityError,
    SessionRegistry,
)


def test_registry_returns_one_authority_per_endpoint(qapplication_session) -> None:
    registry = SessionRegistry()
    first = registry.get_or_create(("192.168.1.12", 4004), owner="live")
    second = registry.get_or_create(("192.168.1.12", 4004), owner="fleet")

    assert first is second
    assert registry.owners(first) == frozenset({"live", "fleet"})

    competing = DeviceSessionCore()
    with pytest.raises(SessionAuthorityError):
        registry.register(("192.168.1.12", 4004), competing, owner="other")


def test_session_core_parses_once_and_updates_canonical_store(qapplication_session) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    observed: list[object] = []
    latest_seen_by_observer: list[tuple[float, float]] = []

    def observe(record: object) -> None:
        observed.append(record)
        latest_seen_by_observer.append(
            core.data_store.get_channel_by_id(7).get_latest()
        )

    core.record_received.connect(observe)
    payload = struct.pack("<IBBf", 1250, 1, 7, 12.5)

    records = core.feed_bytes(build_frame(CmdType.DATA_REPORT, payload))

    assert len(records) == 1
    assert observed == list(records)
    assert latest_seen_by_observer == [(1250.0, pytest.approx(12.5))]
    assert core.data_store.frame_count == 1
    assert core.data_store.get_channel_by_id(7).get_latest() == pytest.approx(
        (1250.0, 12.5)
    )


def test_session_core_owns_meta_info_request(qapplication_session) -> None:
    sent: list[bytes] = []
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    core.attach_transport(object(), lambda frame: not sent.append(frame))

    assert core.request_meta_info()
    assert sent == [build_request_meta_info()]


def test_product_control_registers_context_before_synchronous_response(
    qapplication_session,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    controller = ProductControlController(core)
    statuses: list[ProductControlStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))

    def send_with_immediate_response(_frame: bytes) -> bool:
        pending = controller.pending
        assert pending is not None
        core.product_store.feed(
            ServiceControlResponse(
                1,
                pending.request_id,
                ServiceControlOp.SET_CONTROL_MODE,
                0,
                1,
                1,
                19798.0,
                29798.0,
                2,
                3,
                False,
            )
        )
        return True

    core.attach_transport(object(), send_with_immediate_response)

    assert controller.request_control_mode(ControlMode.MANUAL)
    assert controller.pending is not None
    assert controller.pending.response_received
    assert statuses == [
        ProductControlStatus.WAITING_RESPONSE,
        ProductControlStatus.WAITING_READBACK,
    ]


def test_debug_control_registers_context_before_synchronous_ack(
    qapplication_session,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    controller = DebugController(core)
    results: list[tuple[bool, bool, str]] = []
    controller.request_finished.connect(
        lambda target, ok, result: results.append((target, ok, result))
    )

    def send_with_immediate_ack(_frame: bytes) -> bool:
        core.apply_records(
            (CommandResponse(RespCode.SUCCESS, "DEBUG_ENABLE=1"),)
        )
        return True

    core.attach_transport(object(), send_with_immediate_ack)
    controller.set_connected(True)
    controller.request(True)

    assert controller.enabled
    assert controller.pending_target is None
    assert results == [(True, True, DebugRequestResult.ACK.value)]


def test_capture_profile_registers_context_before_synchronous_ack(
    qapplication_session,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    controller = CaptureProfileController(core)
    results: list[tuple[bool, bool, str]] = []
    controller.finished.connect(
        lambda target, ok, result: results.append((target, ok, result))
    )

    def send_with_immediate_ack(_frame: bytes) -> bool:
        assert controller.pending_request_id is not None
        core.product_store.feed(
            ServiceControlResponse(
                1,
                controller.pending_request_id,
                ServiceControlOp.SET_CAPTURE_PROFILE,
                0,
                0,
                0,
                0.0,
                0.0,
                0,
                0,
                False,
            )
        )
        return True

    core.attach_transport(object(), send_with_immediate_ack)

    assert controller.request(True)
    assert controller.pending_request_id is None
    assert results == [(True, True, CaptureProfileResult.ACK.value)]


def test_product_subscription_registers_context_before_synchronous_ack(
    qapplication_session,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    controller = ProductSubscriptionController(core)

    def send_with_immediate_ack(_frame: bytes) -> bool:
        assert controller.pending_request_id is not None
        core.product_store.feed(
            ServiceControlResponse(
                1,
                controller.pending_request_id,
                ServiceControlOp.SUBSCRIBE,
                0,
                0,
                0,
                0.0,
                0.0,
                0,
                0,
                False,
            )
        )
        return True

    core.attach_transport(object(), send_with_immediate_ack)

    assert controller.send_now()
    assert controller.confirmed
    assert controller.pending_request_id is None


def test_product_source_resolver_never_mixes_service_and_legacy(
    qapplication_session,
) -> None:
    profiles = ProfileStore()
    profiles.apply_meta(MetaInfo(2, "legacy-fw", "afd01", "LEGACY-SN"))
    profiles.apply_channel_define(
        "afd01",
        1,
        [ChannelDefEntry(0, 1, 0, 0, "snr", "dB", -100.0, 100.0)],
    )
    profiles.apply_profile_semantics(
        "afd01",
        ProfileSemanticsReport(
            1,
            channels=[ProfileSemanticChannelEntry(0, [CHANNEL_ROLE_SNR])],
        ),
    )
    data = TelemetrySeriesStore()
    data.update(DataReport(1000, [ChannelSample(0, 18.0)]))
    service = ProductServiceStore()
    resolver = ProductSnapshotResolver(
        service,
        LegacyV2Projector(profiles, data, StateStore(), stale_after_s=float("inf")),
        discovery_timeout_s=2.0,
    )
    resolver.reset(now_monotonic=10.0)

    pending = resolver.snapshot(now_monotonic=10.5)
    assert resolver.state is ProductSourceState.PENDING
    assert pending.operation.snr_db.availability is Availability.PENDING

    legacy = resolver.snapshot(now_monotonic=12.1)
    assert legacy.source == "legacy_v2"
    assert legacy.operation.snr_db.value == pytest.approx(18.0)
    assert legacy.operation.snr_db.source is ProductSource.LEGACY_V2
    assert legacy.operation.snr_db.quality is ValueQuality.DERIVED

    service.feed(ServiceIdentity(1, 2000, 0x03, "afd01", "PRODUCT-SN", "", "", 2))
    product = resolver.snapshot(now_monotonic=12.2)
    assert product.source == "product_service"
    assert product.identity.serial_number.value == "PRODUCT-SN"
    assert product.operation.snr_db.availability is Availability.UNSUPPORTED
    assert product.operation.snr_db.source is ProductSource.PRODUCT_SERVICE


def test_telemetry_window_reports_device_time_gaps() -> None:
    store = TelemetrySeriesStore(buffer_capacity=16)
    for timestamp in (0, 100, 200, 1000):
        store.update(DataReport(timestamp, [ChannelSample(3, float(timestamp))]))

    window = store.query_window(3, gap_threshold=300.0)

    assert window.timestamps.tolist() == [0.0, 100.0, 200.0, 1000.0]
    assert window.gap_indices == (3,)
