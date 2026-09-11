"""Shared device-session authority and telemetry contracts."""

from __future__ import annotations

import hashlib
from pathlib import Path
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
from satellite_debug_tool.core.security import FirmwarePackage
from satellite_debug_tool.core.product import LegacyV2Projector, ProductServiceStore
from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    ChannelSample,
    CmdType,
    CommandResponse,
    DataReport,
    MetaInfo,
    ParaTableReport,
    ProfileSemanticCapabilityEntry,
    ProfileSemanticsReport,
    ProfileSemanticChannelEntry,
    ServiceIdentity,
    ServiceHardwareIdentity,
    ServiceMountStatus,
    ServiceNavigationSourceInfo,
    ServiceFastState,
    ServiceSlowState,
    ServiceControlOp,
    ServiceControlResponse,
    RespCode,
    build_frame,
    build_device_reboot,
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
    MountConfigurationController,
    MountConfigurationStatus,
    OtaController,
    OtaState,
    OtaStatus,
    OTA_MAX_IMAGE_BYTES,
    ParameterController,
    ParameterOperation,
    ParameterStatus,
    ProductSubscriptionController,
    SessionAuthorityError,
    SessionRegistry,
    SUBSCRIPTION_KEEPALIVE_INTERVAL_MS,
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


def test_replacing_live_transport_starts_a_new_connection_epoch(
    qapplication_session,
) -> None:
    first_transport = object()
    second_transport = object()
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    generations: list[int] = []
    core.generation_changed.connect(
        lambda generation, _endpoint: generations.append(generation)
    )
    core.attach_transport(first_transport, lambda _frame: True)
    core.apply_records((MetaInfo(2, "0.0.130", "afd01", "AFD01-001"),))
    owner = object()
    assert core.try_acquire_device_transaction(owner)

    core.attach_transport(second_transport, lambda _frame: True)

    assert generations == [1]
    assert core.transport is second_transport
    assert core.meta_info is None
    assert not core.device_transaction_active


def test_connected_endpoint_cannot_change_outside_begin_connection(
    qapplication_session,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    core.attach_transport(object(), lambda _frame: True)

    with pytest.raises(ValueError, match="begin_connection"):
        core.bind_endpoint(("192.168.1.99", 4004))

    core.begin_connection(
        endpoint=("192.168.1.99", 4004),
        transport=object(),
        sender=lambda _frame: True,
        handshake_enabled=False,
    )
    assert core.endpoint == ("192.168.1.99", 4004)
    assert core.generation == 1


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


def test_product_control_requires_matching_telemetry_after_ack(
    qapplication_session,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    controller = ProductControlController(core)
    statuses: list[ProductControlStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))
    core.attach_transport(object(), lambda _frame: True)

    # This old matching state must not complete a later request after ACK.
    core.product_store.feed(
        ServiceFastState(
            1, 100, 0xFFF, 1, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )
    assert controller.request_control_mode(ControlMode.MANUAL)
    pending = controller.pending
    assert pending is not None
    core.product_store.feed(
        ServiceControlResponse(
            1, pending.request_id, ServiceControlOp.SET_CONTROL_MODE, 0, 1,
            1, 19798.0, 29798.0, 2, 3, False,
        )
    )

    assert controller.pending is not None
    assert controller.pending.response_received
    assert statuses[-1] is ProductControlStatus.WAITING_READBACK

    # UDP may deliver a duplicate or an older datagram after the ACK.  A later
    # host arrival alone does not prove that the device produced new state.
    for timestamp in (100, 99):
        core.product_store.feed(
            ServiceFastState(
                1, timestamp, 0xFFF, 1, 0, False, 3, 3, False,
                0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
            )
        )
        assert controller.pending is not None
        assert statuses[-1] is ProductControlStatus.WAITING_READBACK

    core.product_store.feed(
        ServiceFastState(
            1, 101, 0xFFF, 1, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )

    assert controller.pending is None
    assert statuses[-1] is ProductControlStatus.APPLIED


def test_product_control_establishes_device_time_before_applied_proof(
    qapplication_session,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    controller = ProductControlController(core)
    statuses: list[ProductControlStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))
    core.attach_transport(object(), lambda _frame: True)

    assert controller.request_control_mode(ControlMode.MANUAL)
    pending = controller.pending
    assert pending is not None
    core.product_store.feed(
        ServiceControlResponse(
            1, pending.request_id, ServiceControlOp.SET_CONTROL_MODE, 0, 1,
            1, 19798.0, 29798.0, 2, 3, False,
        )
    )

    # With no device timestamp at ACK, the first arrival only establishes a
    # waterline because it may have been queued before the request.
    core.product_store.feed(
        ServiceFastState(
            1, 100, 0xFFF, 1, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )
    assert controller.pending is not None
    assert statuses[-1] is ProductControlStatus.WAITING_READBACK

    core.product_store.feed(
        ServiceFastState(
            1, 101, 0xFFF, 1, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )
    assert controller.pending is None
    assert statuses[-1] is ProductControlStatus.APPLIED


@pytest.mark.parametrize(
    ("operation", "applied_mask"),
    (
        (ServiceControlOp.SET_CONTROL_MODE, 0),
        (ServiceControlOp.APPLY_RF, 0x0E),
        (ServiceControlOp.SET_TX_ENABLE, 0),
    ),
)
def test_product_control_rejects_success_without_operation_applied_mask(
    qapplication_session,
    operation: ServiceControlOp,
    applied_mask: int,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    controller = ProductControlController(core)
    statuses: list[ProductControlStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))
    core.attach_transport(object(), lambda _frame: True)

    if operation is ServiceControlOp.SET_CONTROL_MODE:
        assert controller.request_control_mode(ControlMode.MANUAL)
    elif operation is ServiceControlOp.APPLY_RF:
        assert controller.request_rf(19798.0, 29798.0, 2, 3)
    else:
        assert controller.request_tx_enable(
            True,
            confirmed_scope=core.device_scope(),
        )
    pending = controller.pending
    assert pending is not None

    core.product_store.feed(
        ServiceControlResponse(
            1,
            pending.request_id,
            operation,
            0,
            applied_mask,
            1,
            19798.0,
            29798.0,
            2,
            3,
            True,
        )
    )

    assert controller.pending is None
    assert statuses[-1] is ProductControlStatus.INTERNAL_ERROR
    assert not core.device_transaction_active


def test_product_control_cancels_when_telemetry_epoch_changes_after_ack(
    qapplication_session,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    controller = ProductControlController(core)
    statuses: list[ProductControlStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))
    core.attach_transport(object(), lambda _frame: True)

    assert controller.request_control_mode(ControlMode.MANUAL)
    pending = controller.pending
    assert pending is not None
    core.product_store.feed(
        ServiceControlResponse(
            1, pending.request_id, ServiceControlOp.SET_CONTROL_MODE, 0, 1,
            1, 19798.0, 29798.0, 2, 3, False,
        )
    )
    core.product_store.clear()

    assert controller.pending is None
    assert statuses[-1] is ProductControlStatus.SESSION_CHANGED
    assert not core.device_transaction_active


def test_apply_rf_requires_new_slow_telemetry_after_ack(
    qapplication_session,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    controller = ProductControlController(core)
    core.attach_transport(object(), lambda _frame: True)
    core.product_store.feed(
        ServiceSlowState(
            1, 100, 0xFF, 31.8, 118.8, 10.0, 19798.0, 29798.0, 2, 3, False
        )
    )
    assert controller.request_rf(19798.0, 29798.0, 2, 3)
    pending = controller.pending
    assert pending is not None
    core.product_store.feed(
        ServiceControlResponse(
            1, pending.request_id, ServiceControlOp.APPLY_RF, 0, 0x1E,
            1, 19798.0, 29798.0, 2, 3, False,
        )
    )

    # A later FAST_STATE must not make a pre-ACK SLOW_STATE look applied.
    core.product_store.feed(
        ServiceFastState(
            1, 101, 0xFFF, 1, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )
    assert controller.pending is not None

    core.product_store.feed(
        ServiceSlowState(
            1, 102, 0xFF, 31.8, 118.8, 10.0, 19798.0, 29798.0, 2, 3, False
        )
    )
    assert controller.pending is None


def test_mount_and_product_controls_share_one_atomic_device_transaction(
    qapplication_session,
) -> None:
    sent: list[bytes] = []
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    core.attach_transport(object(), lambda frame: not sent.append(frame))
    core.product_store.feed(_hardware_identity(timestamp=1, uid_seed=0x10))
    rf = ProductControlController(core)
    mount = MountConfigurationController(core)
    second_product = ProductControlController(core)
    mount_statuses: list[MountConfigurationStatus] = []
    product_statuses: list[ProductControlStatus] = []
    mount.status_changed.connect(lambda status, _values: mount_statuses.append(status))
    second_product.status_changed.connect(
        lambda status, _values: product_statuses.append(status)
    )

    assert rf.request_rf(19798.0, 29798.0, 2, 3)
    assert core.device_transaction_active
    assert not mount.request_mount(12.5, -3.0, 1.5)
    assert mount_statuses[-1] is MountConfigurationStatus.BUSY
    assert len(sent) == 1

    rf._on_timeout()
    assert not core.device_transaction_active
    assert mount.request_mount(12.5, -3.0, 1.5)
    assert core.device_transaction_active
    assert not second_product.request_control_mode(ControlMode.MANUAL)
    assert product_statuses[-1] is ProductControlStatus.BUSY
    assert len(sent) == 2

    mount._on_timeout()
    assert not core.device_transaction_active


def test_ota_parameters_product_and_mount_share_one_bidirectional_transaction(
    qapplication_session,
) -> None:
    profile = ProfileStore(cache=None)
    profile.apply_meta(MetaInfo(2, "0.0.140", "afd01", "AFD01-001"))
    profile.apply_profile_semantics(
        "afd01",
        ProfileSemanticsReport(
            table_ver=1,
            capabilities=[
                ProfileSemanticCapabilityEntry("parameters", True),
                ProfileSemanticCapabilityEntry("ota", True),
                ProfileSemanticCapabilityEntry("command_response_context", True),
            ],
        ),
    )
    sent: list[bytes] = []
    core = DeviceSessionCore(
        endpoint=("192.168.1.12", 4004),
        profile_store=profile,
    )
    core.attach_transport(object(), lambda frame: not sent.append(frame))
    ota = OtaController(core)
    parameters = ParameterController(core)
    product = ProductControlController(core)
    mount = MountConfigurationController(core)
    ota_statuses: list[OtaStatus] = []
    parameter_statuses: list[ParameterStatus] = []
    ota.status_changed.connect(lambda status, _values: ota_statuses.append(status))
    parameters.status_changed.connect(
        lambda status, _values: parameter_statuses.append(status)
    )
    token = ota.configure_engineering_file(b"A" * 64, "afd01.bin")
    assert token is not None
    selected_crc = ota.crc32

    assert ota.start(pause_debug=False)
    assert core.device_transaction_active
    assert ota.configure_engineering_file(b"B" * 64, "other.bin") is None
    assert ota.artifact_token == token
    assert ota.crc32 == selected_crc
    assert not product.request_control_mode(ControlMode.MANUAL)
    assert not mount.request_mount(0.0, 0.0, 0.0)
    assert not parameters.request_table()
    assert parameter_statuses[-1] is ParameterStatus.TRANSACTION_ACTIVE
    ota.abort()
    assert core.device_transaction_active
    assert ota.state is OtaState.TERMINATING
    ota._on_response_timeout()
    assert not core.device_transaction_active

    assert parameters.request_table()
    assert core.device_transaction_active
    assert not product.request_control_mode(ControlMode.MANUAL)
    assert not mount.request_mount(0.0, 0.0, 0.0)
    assert not ota.start(pause_debug=False)
    assert ota_statuses[-1] is OtaStatus.TRANSACTION_ACTIVE
    core.apply_records((ParaTableReport(table_ver=1, params=[]),))
    assert not core.device_transaction_active

    assert product.request_control_mode(ControlMode.MANUAL)
    assert core.device_transaction_active
    assert not parameters.request_table()
    assert not ota.start(pause_debug=False)
    assert ota_statuses[-1] is OtaStatus.TRANSACTION_ACTIVE
    product._on_timeout()
    assert not core.device_transaction_active

    assert ota.configure_engineering_file(b"A", "x" * 256) is None
    assert ota_statuses[-1] is OtaStatus.BEGIN_SEND_FAILED
    assert not core.device_transaction_active

    assert not parameters.write("x" * 256, 0, "1")
    assert not core.device_transaction_active


def test_engineering_ota_rejects_image_beyond_u16_sequence_capacity(
    qapplication_session,
) -> None:
    profile = ProfileStore(cache=None)
    profile.apply_meta(MetaInfo(2, "0.0.140", "afd01", "AFD01-001"))
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004), profile_store=profile)
    core.attach_transport(object(), lambda _frame: True)
    ota = OtaController(core)
    statuses: list[tuple[OtaStatus, dict[str, object]]] = []
    ota.status_changed.connect(
        lambda status, values: statuses.append((status, values))
    )

    assert ota.configure_engineering_file(
        b"A" * (OTA_MAX_IMAGE_BYTES + 1),
        "oversized.bin",
    ) is None
    assert statuses[-1] == (
        OtaStatus.ARTIFACT_TOO_LARGE,
        {"maximum": OTA_MAX_IMAGE_BYTES},
    )
    assert not ota.has_file
    assert not ota.active
    assert not core.device_transaction_active


def test_ota_codec_failure_finishes_transaction_and_releases_lease(
    qapplication_session,
) -> None:
    profile = ProfileStore(cache=None)
    profile.apply_meta(MetaInfo(2, "0.0.140", "afd01", "AFD01-001"))
    profile.apply_profile_semantics(
        "afd01",
        ProfileSemanticsReport(
            table_ver=1,
            capabilities=[
                ProfileSemanticCapabilityEntry("ota", True),
                ProfileSemanticCapabilityEntry("command_response_context", True),
            ],
        ),
    )
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004), profile_store=profile)
    core.attach_transport(object(), lambda _frame: True)
    ota = OtaController(core)
    statuses: list[OtaStatus] = []
    ota.status_changed.connect(lambda status, _values: statuses.append(status))
    assert ota.configure_engineering_file(b"A", "firmware.bin") is not None
    assert ota.start(pause_debug=False)
    assert core.device_transaction_active

    ota._sequence = 0x10000
    ota._total_chunks = 0x10001
    ota._send_current_chunk()

    assert statuses[-1] is OtaStatus.CHUNK_SEND_FAILED
    assert not ota.active
    assert not core.device_transaction_active


def _ota_product_session(
    *,
    firmware: str = "0.0.130",
    serial_number: str = "AFD01-001",
    product_serial_number: str | None = None,
    debug_firmware: str | None = None,
    product_firmware: str | None = None,
) -> tuple[DeviceSessionCore, OtaController, list[OtaStatus]]:
    debug_version = debug_firmware or firmware
    product_version = product_firmware or firmware
    profile = ProfileStore(cache=None)
    profile.apply_meta(MetaInfo(2, debug_version, "afd01", serial_number))
    profile.apply_profile_semantics(
        "afd01",
        ProfileSemanticsReport(
            table_ver=1,
            capabilities=[
                ProfileSemanticCapabilityEntry("ota", True),
                ProfileSemanticCapabilityEntry("command_response_context", True),
            ],
        ),
    )
    core = DeviceSessionCore(
        endpoint=("192.168.1.12", 4004),
        profile_store=profile,
    )
    core.attach_transport(object(), lambda _frame: True)
    core.apply_records(
        (
            MetaInfo(2, debug_version, "afd01", serial_number),
            ServiceIdentity(
                1,
                10,
                0x17,
                "AFD01",
                product_serial_number or serial_number,
                product_version,
                "",
                8,
            ),
        )
    )
    controller = OtaController(core)
    statuses: list[OtaStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))
    return core, controller, statuses


def _verified_package(version: str, *, policy: str = "upgrade_only") -> FirmwarePackage:
    firmware = f"AFD01-{version}".encode("ascii")
    return FirmwarePackage(
        path=Path("release.sfpkg"),
        product="AFD01",
        hardware_types=("afd01",),
        version=version,
        version_policy=policy,
        firmware_name="afd01.bin",
        firmware=firmware,
        firmware_sha256=hashlib.sha256(firmware).hexdigest(),
        key_id="test-release",
        key_label="Test release",
    )


def test_verified_ota_confirmation_cannot_survive_raw_image_replacement(
    qapplication_session,
) -> None:
    _core, ota, statuses = _ota_product_session()
    confirmed = ota.configure_verified_package(_verified_package("0.0.131"))
    assert confirmed is not None

    replacement = ota.configure_engineering_file(b"RAW", "engineering.bin")
    assert replacement is not None
    assert replacement != confirmed
    assert not ota.start(pause_debug=False, required_artifact=confirmed)
    assert statuses[-1] is OtaStatus.ARTIFACT_MISMATCH


def test_customer_ota_requires_non_placeholder_immutable_identity(
    qapplication_session,
) -> None:
    _core, ota, statuses = _ota_product_session(serial_number="AFD01-dev")

    assert ota.configure_verified_package(_verified_package("0.0.131")) is None
    assert statuses[-1] is OtaStatus.IDENTITY_UNAVAILABLE


def test_customer_ota_rejects_conflicting_debug_and_product_firmware(
    qapplication_session,
) -> None:
    _core, ota, statuses = _ota_product_session(
        debug_firmware="2.0.0",
        product_firmware="1.0.0",
    )

    assert ota.configure_verified_package(_verified_package("2.0.0")) is None
    assert statuses[-1] is OtaStatus.FIRMWARE_FACTS_CONFLICT


def test_customer_ota_rejects_conflicting_debug_and_product_serials(
    qapplication_session,
) -> None:
    _core, ota, statuses = _ota_product_session(
        serial_number="AFD01-001",
        product_serial_number="AFD01-999",
    )

    assert ota.configure_verified_package(_verified_package("0.0.131")) is None
    assert statuses[-1] is OtaStatus.IDENTITY_FACTS_CONFLICT


def test_verified_ota_requires_same_identity_and_exact_target_version(
    qapplication_session,
) -> None:
    core, ota, statuses = _ota_product_session()
    token = ota.configure_verified_package(_verified_package("0.0.131"))
    assert token is not None
    assert ota.start(pause_debug=False, required_artifact=token)
    ota._enter_wait_reboot()

    core.end_connection()
    assert ota.active
    core.begin_connection(
        endpoint=("192.168.1.12", 4004),
        transport=object(),
        sender=lambda _frame: True,
        handshake_enabled=False,
    )
    core.apply_records((MetaInfo(2, "0.0.131", "afd01", "AFD01-001"),))

    assert ota.active
    assert statuses[-1] is not OtaStatus.TARGET_VERSION_CONFIRMED
    core.apply_records(
        (
            ServiceIdentity(
                1,
                11,
                0x17,
                "AFD01",
                "AFD01-001",
                "0.0.131",
                "",
                8,
            ),
        )
    )

    assert statuses[-1] is OtaStatus.TARGET_VERSION_CONFIRMED
    assert not ota.active


@pytest.mark.parametrize("first_source", ("product", "debug"))
def test_verified_ota_result_is_independent_of_firmware_source_order(
    qapplication_session,
    first_source: str,
) -> None:
    core, ota, statuses = _ota_product_session()
    token = ota.configure_verified_package(_verified_package("0.0.131"))
    assert token is not None
    assert ota.start(pause_debug=False, required_artifact=token)
    ota._enter_wait_reboot()

    old_product = ServiceIdentity(
        1,
        11,
        0x17,
        "AFD01",
        "AFD01-001",
        "0.0.130",
        "",
        8,
    )
    target_debug = MetaInfo(2, "0.0.131", "afd01", "AFD01-001")
    records = (
        (old_product, target_debug)
        if first_source == "product"
        else (target_debug, old_product)
    )
    for record in records:
        core.apply_records((record,))

    assert ota.active
    assert statuses[-1] is not OtaStatus.TARGET_VERSION_CONFIRMED

    core.apply_records(
        (
            ServiceIdentity(
                1,
                12,
                0x17,
                "AFD01",
                "AFD01-001",
                "0.0.131",
                "",
                8,
            ),
        )
    )

    assert statuses[-1] is OtaStatus.TARGET_VERSION_CONFIRMED
    assert not ota.active


def test_verified_ota_reports_returned_firmware_source_conflict_on_timeout(
    qapplication_session,
) -> None:
    core, ota, statuses = _ota_product_session()
    token = ota.configure_verified_package(_verified_package("0.0.131"))
    assert token is not None
    assert ota.start(pause_debug=False, required_artifact=token)
    ota._enter_wait_reboot()
    core.apply_records(
        (
            ServiceIdentity(
                1,
                11,
                0x17,
                "AFD01",
                "AFD01-001",
                "0.0.130",
                "",
                8,
            ),
            MetaInfo(2, "0.0.131", "afd01", "AFD01-001"),
        )
    )
    assert ota.active

    ota._finish_reboot_timeout()

    assert statuses[-1] is OtaStatus.FIRMWARE_FACTS_CONFLICT
    assert not ota.active


def test_verified_ota_waits_for_every_captured_identity_fact(
    qapplication_session,
) -> None:
    core, ota, statuses = _ota_product_session()
    original_hardware_identity = ServiceHardwareIdentity(
        1,
        10,
        0x01,
        (0x10, 0x11, 0x12),
        b"\x00\x00\x00\x00\x00\x00",
        0,
    )
    core.apply_records((original_hardware_identity,))
    token = ota.configure_verified_package(_verified_package("0.0.131"))
    assert token is not None
    assert ota.start(pause_debug=False, required_artifact=token)
    ota._enter_wait_reboot()

    core.apply_records(
        (
            MetaInfo(2, "0.0.131", "afd01", "AFD01-001"),
            ServiceIdentity(
                1,
                11,
                0x17,
                "AFD01",
                "AFD01-001",
                "0.0.131",
                "",
                8,
            ),
        )
    )
    assert ota.active

    core.apply_records(
        (
            ServiceHardwareIdentity(
                1,
                11,
                0x01,
                original_hardware_identity.uid_words,
                b"\x00\x00\x00\x00\x00\x00",
                0,
            ),
        )
    )

    assert statuses[-1] is OtaStatus.TARGET_VERSION_CONFIRMED
    assert not ota.active


def test_verified_ota_rejects_a_different_reconnect_endpoint(
    qapplication_session,
) -> None:
    core, ota, statuses = _ota_product_session()
    token = ota.configure_verified_package(_verified_package("0.0.131"))
    assert token is not None
    assert ota.start(pause_debug=False, required_artifact=token)
    ota._enter_wait_reboot()

    core.end_connection()
    core.begin_connection(
        endpoint=("192.168.1.99", 4004),
        transport=object(),
        sender=lambda _frame: True,
        handshake_enabled=False,
    )

    assert statuses[-1] is OtaStatus.SESSION_CHANGED
    assert not ota.active
    assert not ota.has_file


def test_verified_ota_does_not_accept_wrong_target_or_changed_identity(
    qapplication_session,
) -> None:
    core, ota, statuses = _ota_product_session()
    token = ota.configure_verified_package(_verified_package("0.0.131"))
    assert token is not None
    assert ota.start(pause_debug=False, required_artifact=token)
    ota._enter_wait_reboot()

    core.apply_records(
        (
            MetaInfo(2, "0.0.130", "afd01", "AFD01-001"),
            ServiceIdentity(
                1,
                11,
                0x17,
                "AFD01",
                "AFD01-001",
                "0.0.130",
                "",
                8,
            ),
        )
    )
    assert ota.active
    ota._finish_reboot_timeout()
    assert statuses[-1] is OtaStatus.TARGET_VERSION_NOT_CONFIRMED

    core, ota, statuses = _ota_product_session()
    token = ota.configure_verified_package(_verified_package("0.0.131"))
    assert token is not None
    assert ota.start(pause_debug=False, required_artifact=token)
    ota._enter_wait_reboot()
    core.apply_records((MetaInfo(2, "0.0.131", "afd01", "AFD01-001"),))
    assert ota.active
    core.apply_records(
        (
            ServiceIdentity(
                1,
                11,
                0x17,
                "AFD01",
                "AFD01-OTHER",
                "0.0.131",
                "",
                8,
            ),
        )
    )
    assert statuses[-1] is OtaStatus.RETURN_IDENTITY_CHANGED


def test_same_version_package_never_claims_application_without_reboot_proof(
    qapplication_session,
) -> None:
    core, ota, statuses = _ota_product_session()
    token = ota.configure_verified_package(
        _verified_package("0.0.130", policy="allow_same")
    )
    assert token is not None
    assert ota.start(pause_debug=False, required_artifact=token)
    ota._enter_wait_reboot()

    core.apply_records(
        (
            MetaInfo(2, "0.0.130", "afd01", "AFD01-001"),
            ServiceIdentity(
                1,
                11,
                0x17,
                "AFD01",
                "AFD01-001",
                "0.0.130",
                "",
                8,
            ),
        )
    )

    assert statuses[-1] is OtaStatus.APPLICATION_UNCONFIRMED
    assert not ota.active


def test_device_transaction_is_released_when_send_fails(
    qapplication_session,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    core.attach_transport(object(), lambda _frame: False)
    controller = ProductControlController(core)
    statuses: list[ProductControlStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))

    assert not controller.request_control_mode(ControlMode.MANUAL)
    assert statuses[-1] is ProductControlStatus.SEND_FAILED
    assert not core.device_transaction_active


@pytest.mark.parametrize("operation", ("read", "write"))
@pytest.mark.parametrize("transition", ("generation", "disconnect"))
def test_parameter_transaction_is_cancelled_when_connection_authority_ends(
    qapplication_session,
    operation: str,
    transition: str,
) -> None:
    endpoint = ("192.168.1.12", 4004)
    profile = ProfileStore(cache=None)
    profile.apply_meta(MetaInfo(2, "0.0.140", "afd01", "AFD01-001"))
    profile.apply_profile_semantics(
        "afd01",
        ProfileSemanticsReport(
            table_ver=1,
            capabilities=[
                ProfileSemanticCapabilityEntry("parameters", True),
                ProfileSemanticCapabilityEntry("command_response_context", True),
            ],
        ),
    )
    core = DeviceSessionCore(endpoint=endpoint, profile_store=profile)
    core.attach_transport(object(), lambda _frame: True)
    controller = ParameterController(core)
    statuses: list[ParameterStatus] = []
    row_statuses: list[ParameterStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))
    controller.parameter_status_changed.connect(
        lambda _name, status, _values: row_statuses.append(status)
    )

    if operation == "read":
        assert controller.request_table()
    else:
        assert controller.write("modem_baud", 0, "115200")
    assert core.device_transaction_active

    if transition == "generation":
        core.begin_connection(
            endpoint=endpoint,
            transport=object(),
            sender=lambda _frame: True,
            handshake_enabled=False,
        )
    else:
        core.end_connection()

    assert controller.operation is ParameterOperation.IDLE
    assert not controller.read_pending
    assert not core.device_transaction_active
    observed = row_statuses if operation == "write" else statuses
    assert observed[-1] is ParameterStatus.SESSION_CHANGED


def test_parameter_write_ignores_unrelated_error_response(
    qapplication_session,
) -> None:
    profile = ProfileStore(cache=None)
    profile.apply_meta(MetaInfo(2, "0.0.140", "afd01", "AFD01-001"))
    profile.apply_profile_semantics(
        "afd01",
        ProfileSemanticsReport(
            table_ver=1,
            capabilities=[
                ProfileSemanticCapabilityEntry("parameters", True),
                ProfileSemanticCapabilityEntry("command_response_context", True),
            ],
        ),
    )
    core = DeviceSessionCore(profile_store=profile)
    core.attach_transport(object(), lambda _frame: True)
    controller = ParameterController(core)
    row_statuses: list[ParameterStatus] = []
    controller.parameter_status_changed.connect(
        lambda _name, status, _values: row_statuses.append(status)
    )

    assert controller.write("modem_baud", 0, "115200")
    core.apply_records(
        (CommandResponse(RespCode.INTERNAL_ERROR, "DEBUG_ENABLE=failed"),)
    )

    assert controller.operation is ParameterOperation.WRITING
    assert core.device_transaction_active
    assert row_statuses[-1] is ParameterStatus.WRITE_AWAITING

    core.apply_records(
        (CommandResponse(RespCode.INTERNAL_ERROR, "PARA_SET=modem_baud"),)
    )
    assert controller.operation is ParameterOperation.IDLE
    assert not core.device_transaction_active
    assert row_statuses[-1] is ParameterStatus.WRITE_ERROR


def test_parameter_reset_ignores_unrelated_error_response(
    qapplication_session,
) -> None:
    profile = ProfileStore(cache=None)
    profile.apply_meta(MetaInfo(2, "0.0.140", "afd01", "AFD01-001"))
    profile.apply_profile_semantics(
        "afd01",
        ProfileSemanticsReport(
            table_ver=1,
            capabilities=[
                ProfileSemanticCapabilityEntry("parameters", True),
                ProfileSemanticCapabilityEntry("command_response_context", True),
            ],
        ),
    )
    core = DeviceSessionCore(profile_store=profile)
    core.attach_transport(object(), lambda _frame: True)
    controller = ParameterController(core)
    statuses: list[ParameterStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))

    assert controller.reset_parameters()
    core.apply_records((CommandResponse(RespCode.INTERNAL_ERROR, "OTA_END=failed"),))

    assert controller.operation is ParameterOperation.RESETTING
    assert core.device_transaction_active

    core.apply_records(
        (CommandResponse(RespCode.INTERNAL_ERROR, "PARA_RESET=failed"),)
    )
    assert controller.operation is ParameterOperation.IDLE
    assert not core.device_transaction_active
    assert statuses[-1] is ParameterStatus.RESET_FAILED


def test_parameter_verify_send_failure_finishes_write_and_releases_transaction(
    qapplication_session,
) -> None:
    profile = ProfileStore(cache=None)
    profile.apply_meta(MetaInfo(2, "0.0.140", "afd01", "AFD01-001"))
    profile.apply_profile_semantics(
        "afd01",
        ProfileSemanticsReport(
            table_ver=1,
            capabilities=[
                ProfileSemanticCapabilityEntry("parameters", True),
                ProfileSemanticCapabilityEntry("command_response_context", True),
            ],
        ),
    )
    sends: list[bytes] = []

    def send(frame: bytes) -> bool:
        sends.append(frame)
        return len(sends) == 1

    core = DeviceSessionCore(profile_store=profile)
    core.attach_transport(object(), send)
    controller = ParameterController(core)
    row_statuses: list[ParameterStatus] = []
    controller.parameter_status_changed.connect(
        lambda _name, status, _values: row_statuses.append(status)
    )

    assert controller.write("modem_baud", 0, "115200")
    core.apply_records((CommandResponse(RespCode.SUCCESS, "PARA_SET=modem_baud"),))
    assert controller.operation is ParameterOperation.VERIFYING

    controller._request_verify_table()

    assert controller.operation is ParameterOperation.IDLE
    assert controller.pending_name is None
    assert not controller.read_pending
    assert not core.device_transaction_active
    assert row_statuses[-1] is ParameterStatus.WRITE_READBACK_SEND_FAILED


def test_product_control_is_cancelled_when_device_session_changes(
    qapplication_session,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    controller = ProductControlController(core)
    statuses: list[ProductControlStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))
    core.attach_transport(object(), lambda _frame: True)

    assert controller.request_control_mode(ControlMode.MANUAL)
    pending = controller.pending
    assert pending is not None
    core.product_store.feed(
        ServiceControlResponse(
            1, pending.request_id, ServiceControlOp.SET_CONTROL_MODE, 0, 1,
            1, 19798.0, 29798.0, 2, 3, False,
        )
    )
    assert controller.pending is not None
    assert controller.pending.response_received

    core.end_connection()
    assert controller.pending is None
    assert statuses[-1] is ProductControlStatus.SESSION_CHANGED

    core.attach_transport(object(), lambda _frame: True)
    core.product_store.feed(
        ServiceFastState(
            1, 102, 0xFFF, 1, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )
    assert statuses[-1] is ProductControlStatus.SESSION_CHANGED


def test_tx_enable_rejects_confirmation_scope_from_previous_connection(
    qapplication_session,
) -> None:
    endpoint = ("192.168.1.12", 4004)
    sent: list[bytes] = []
    core = DeviceSessionCore(endpoint=endpoint)
    core.attach_transport(object(), lambda _frame: True)
    controller = ProductControlController(core)
    statuses: list[ProductControlStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))
    confirmed_scope = core.device_scope()

    core.begin_connection(
        endpoint=endpoint,
        transport=object(),
        sender=lambda frame: not sent.append(frame),
        handshake_enabled=False,
    )

    assert not controller.request_tx_enable(True, confirmed_scope=confirmed_scope)
    assert sent == []
    assert controller.pending is None
    assert statuses[-1] is ProductControlStatus.SESSION_CHANGED
    assert not core.device_transaction_active

    # The rejected confirmation did not consume a request ID or poison the
    # controller; a confirmation captured from the current connection starts at 1.
    assert controller.request_tx_enable(True, confirmed_scope=core.device_scope())
    assert int.from_bytes(sent[-1][7:11], "little") == 1
    controller._on_timeout()


def _mount_status(
    *,
    timestamp: int,
    yaw: float,
    pitch: float,
    roll: float,
    verified: bool,
    restart_required: bool,
) -> ServiceMountStatus:
    return ServiceMountStatus(
        1,
        timestamp,
        0x7F,
        0x31445246,
        yaw,
        pitch,
        roll,
        0.0,
        180.0,
        -90.0,
        0.0,
        180.0,
        -90.0,
        0,
        verified,
        restart_required,
    )


def _identity(*, timestamp: int, serial_number: str) -> ServiceIdentity:
    return ServiceIdentity(
        1,
        timestamp,
        0x17,
        "AFD01",
        serial_number,
        "0.0.140",
        "",
        8,
    )


def _hardware_identity(*, timestamp: int, uid_seed: int) -> ServiceHardwareIdentity:
    return ServiceHardwareIdentity(
        1,
        timestamp,
        0x01,
        (uid_seed, uid_seed + 1, uid_seed + 2),
        b"\x00\x00\x00\x00\x00\x00",
        0,
    )


def _navigation_source(
    *,
    timestamp: int,
    external_source: int = 3,
    external_roles: int = 0x07,
    configured: bool = True,
    attitude_owner: int = 1,
) -> ServiceNavigationSourceInfo:
    flags = 0x0F if configured else 0x01
    return ServiceNavigationSourceInfo(
        1,
        timestamp,
        0x7F,
        2,
        1,
        attitude_owner,
        external_source,
        external_roles,
        flags,
        0,
    )


def test_mount_configuration_fails_closed_without_uid_or_serial_number(
    qapplication_session,
) -> None:
    sent: list[bytes] = []
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    core.attach_transport(object(), lambda frame: not sent.append(frame))
    controller = MountConfigurationController(core)
    statuses: list[MountConfigurationStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))

    assert not controller.request_mount(0.0, 0.0, 0.0)
    assert controller.pending is None
    assert statuses == [MountConfigurationStatus.IDENTITY_UNAVAILABLE]
    assert sent == []
    assert not core.device_transaction_active


def test_mount_configuration_rejects_duplicate_and_older_udp_readback(
    qapplication_session,
) -> None:
    sent: list[bytes] = []
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    core.attach_transport(object(), lambda frame: not sent.append(frame))
    core.product_store.feed(_hardware_identity(timestamp=1, uid_seed=0x10))
    core.product_store.feed(
        _mount_status(
            timestamp=100,
            yaw=12.5,
            pitch=-3.0,
            roll=1.5,
            verified=False,
            restart_required=True,
        )
    )
    controller = MountConfigurationController(core)

    assert controller.request_mount(12.5, -3.0, 1.5)
    pending = controller.pending
    assert pending is not None
    core.product_store.feed(
        ServiceControlResponse(
            1,
            pending.request_id,
            ServiceControlOp.SET_DEVICE_MOUNT,
            0,
            1 << 7,
            0,
            0.0,
            0.0,
            0,
            0,
            False,
        )
    )

    for timestamp in (100, 99):
        core.product_store.feed(
            _mount_status(
                timestamp=timestamp,
                yaw=12.5,
                pitch=-3.0,
                roll=1.5,
                verified=False,
                restart_required=True,
            )
        )
        assert controller.pending is not None
        assert build_device_reboot() not in sent

    core.product_store.feed(
        _mount_status(
            timestamp=101,
            yaw=12.5,
            pitch=-3.0,
            roll=1.5,
            verified=False,
            restart_required=True,
        )
    )
    assert sent[-1] == build_device_reboot()
    controller._on_timeout()
    assert not core.device_transaction_active


def test_mount_configuration_requires_ack_readback_reboot_and_final_readback(
    qapplication_session,
) -> None:
    sent: list[bytes] = []
    endpoint = ("192.168.1.12", 4004)
    core = DeviceSessionCore(endpoint=endpoint)
    core.attach_transport(object(), lambda frame: not sent.append(frame))
    core.product_store.feed(_identity(timestamp=1, serial_number="AFD01-001"))
    core.product_store.feed(_hardware_identity(timestamp=1, uid_seed=0x10))
    core.product_store.feed(_navigation_source(timestamp=1, configured=False))
    controller = MountConfigurationController(core)
    statuses: list[MountConfigurationStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))

    assert controller.request_mount(12.5, -3.0, 1.5)
    pending = controller.pending
    assert pending is not None
    core.product_store.feed(
        ServiceControlResponse(
            1,
            pending.request_id,
            ServiceControlOp.SET_DEVICE_MOUNT,
            0,
            1 << 7,
            0,
            0.0,
            0.0,
            0,
            0,
            False,
        )
    )
    assert statuses[-1] is MountConfigurationStatus.WAITING_READBACK

    core.product_store.feed(
        _mount_status(
            timestamp=2,
            yaw=12.5,
            pitch=-3.0,
            roll=1.5,
            verified=False,
            restart_required=True,
        )
    )
    assert sent[-1] == build_device_reboot()
    assert statuses[-1] is MountConfigurationStatus.WAITING_RESTART

    core.end_connection()
    assert controller.pending is not None
    core.begin_connection(
        endpoint=endpoint,
        transport=object(),
        sender=lambda frame: not sent.append(frame),
        handshake_enabled=False,
    )
    core.product_store.feed(_identity(timestamp=3, serial_number="AFD01-001"))
    core.product_store.feed(_hardware_identity(timestamp=3, uid_seed=0x10))
    core.product_store.feed(_navigation_source(timestamp=3, configured=False))
    core.product_store.feed(
        _mount_status(
            # The explicit reboot/connection epoch permits the device uptime
            # timestamp to restart below the pre-reboot value.
            timestamp=1,
            yaw=12.5,
            pitch=-3.0,
            roll=1.5,
            verified=False,
            restart_required=False,
        )
    )

    assert controller.pending is None
    assert statuses[-1] is MountConfigurationStatus.APPLIED


def test_mount_configuration_waits_for_bynav_rbv_verification(
    qapplication_session,
) -> None:
    sent: list[bytes] = []
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    core.attach_transport(object(), lambda frame: not sent.append(frame))
    core.product_store.feed(_hardware_identity(timestamp=1, uid_seed=0x10))
    core.product_store.feed(_navigation_source(timestamp=1))
    controller = MountConfigurationController(core)
    statuses: list[MountConfigurationStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))

    assert controller.request_mount(0.0, 0.0, 0.0)
    pending = controller.pending
    assert pending is not None
    core.product_store.feed(
        ServiceControlResponse(
            1,
            pending.request_id,
            ServiceControlOp.SET_DEVICE_MOUNT,
            0,
            1 << 7,
            0,
            0.0,
            0.0,
            0,
            0,
            False,
        )
    )
    core.product_store.feed(
        _mount_status(
            timestamp=2,
            yaw=0.0,
            pitch=0.0,
            roll=0.0,
            verified=False,
            restart_required=True,
        )
    )
    assert sent[-1] == build_device_reboot()
    assert controller.pending is not None

    # 最终是否需要 RBV 由重启后的权威导航配置重算，
    # 不继承发起请求时的判断。
    core.product_store.feed(_hardware_identity(timestamp=3, uid_seed=0x10))
    core.product_store.feed(_navigation_source(timestamp=3))
    core.product_store.feed(
        _mount_status(
            timestamp=3,
            yaw=0.0,
            pitch=0.0,
            roll=0.0,
            verified=True,
            restart_required=False,
        )
    )
    assert controller.pending is None
    assert statuses[-1] is MountConfigurationStatus.APPLIED


def test_mount_configuration_ignores_dynamic_owner_and_requires_attitude_role(
    qapplication_session,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    sent: list[bytes] = []
    core.attach_transport(object(), lambda frame: not sent.append(frame))
    core.product_store.feed(_hardware_identity(timestamp=1, uid_seed=0x10))
    core.product_store.feed(
        _navigation_source(
            timestamp=1,
            external_roles=0x03,
            attitude_owner=3,
        )
    )
    controller = MountConfigurationController(core)

    assert controller.request_mount(0.0, 0.0, 0.0)
    pending = controller.pending
    assert pending is not None
    core.product_store.feed(
        ServiceControlResponse(
            1,
            pending.request_id,
            ServiceControlOp.SET_DEVICE_MOUNT,
            0,
            1 << 7,
            0,
            0.0,
            0.0,
            0,
            0,
            False,
        )
    )
    core.product_store.feed(
        _mount_status(
            timestamp=2,
            yaw=0.0,
            pitch=0.0,
            roll=0.0,
            verified=False,
            restart_required=True,
        )
    )
    assert sent[-1] == build_device_reboot()
    core.product_store.feed(_hardware_identity(timestamp=3, uid_seed=0x10))
    core.product_store.feed(
        _navigation_source(
            timestamp=3,
            external_roles=0x03,
            attitude_owner=3,
        )
    )
    core.product_store.feed(
        _mount_status(
            timestamp=3,
            yaw=0.0,
            pitch=0.0,
            roll=0.0,
            verified=False,
            restart_required=False,
        )
    )

    assert controller.pending is None


def test_mount_configuration_does_not_skip_required_restart(
    qapplication_session,
) -> None:
    sent: list[bytes] = []
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    core.attach_transport(object(), lambda frame: not sent.append(frame))
    core.product_store.feed(_hardware_identity(timestamp=1, uid_seed=0x10))
    controller = MountConfigurationController(core)

    assert controller.request_mount(8.0, -2.0, 1.0)
    pending = controller.pending
    assert pending is not None
    core.product_store.feed(
        ServiceControlResponse(
            1,
            pending.request_id,
            ServiceControlOp.SET_DEVICE_MOUNT,
            0,
            1 << 7,
            0,
            0.0,
            0.0,
            0,
            0,
            False,
        )
    )
    core.product_store.feed(
        _mount_status(
            timestamp=2,
            yaw=8.0,
            pitch=-2.0,
            roll=1.0,
            verified=True,
            restart_required=False,
        )
    )

    assert controller.pending is not None
    assert sent[-1] != build_device_reboot()


def test_mount_configuration_reacquires_transaction_and_rejects_identity_change(
    qapplication_session,
) -> None:
    sent: list[bytes] = []
    endpoint = ("192.168.1.12", 4004)
    core = DeviceSessionCore(endpoint=endpoint)
    core.attach_transport(object(), lambda frame: not sent.append(frame))
    core.product_store.feed(_identity(timestamp=1, serial_number="AFD01-001"))
    core.product_store.feed(_hardware_identity(timestamp=1, uid_seed=0x10))
    controller = MountConfigurationController(core)
    statuses: list[MountConfigurationStatus] = []
    controller.status_changed.connect(lambda status, _values: statuses.append(status))

    assert controller.request_mount(8.0, -2.0, 1.0)
    pending = controller.pending
    assert pending is not None
    core.product_store.feed(
        ServiceControlResponse(
            1,
            pending.request_id,
            ServiceControlOp.SET_DEVICE_MOUNT,
            0,
            1 << 7,
            0,
            0.0,
            0.0,
            0,
            0,
            False,
        )
    )
    core.product_store.feed(
        _mount_status(
            timestamp=2,
            yaw=8.0,
            pitch=-2.0,
            roll=1.0,
            verified=False,
            restart_required=True,
        )
    )
    core.end_connection()
    core.begin_connection(
        endpoint=endpoint,
        transport=object(),
        sender=lambda frame: not sent.append(frame),
        handshake_enabled=False,
    )
    competing_owner = object()
    assert core.device_transaction_active
    assert not core.try_acquire_device_transaction(competing_owner)
    core.product_store.feed(_identity(timestamp=3, serial_number="AFD01-002"))
    core.product_store.feed(_hardware_identity(timestamp=3, uid_seed=0x20))
    core.product_store.feed(_navigation_source(timestamp=3, configured=False))
    core.product_store.feed(
        _mount_status(
            timestamp=3,
            yaw=8.0,
            pitch=-2.0,
            roll=1.0,
            verified=False,
            restart_required=False,
        )
    )

    assert controller.pending is None
    assert statuses[-1] is MountConfigurationStatus.SESSION_CHANGED
    assert not core.device_transaction_active


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
                1 << 6,
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


def test_capture_profile_rejects_success_without_capture_applied_bit(
    qapplication_session,
) -> None:
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    controller = CaptureProfileController(core)
    results: list[tuple[bool, bool, str]] = []
    controller.finished.connect(
        lambda target, ok, result: results.append((target, ok, result))
    )
    core.attach_transport(object(), lambda _frame: True)

    assert controller.request(True)
    request_id = controller.pending_request_id
    assert request_id is not None
    core.product_store.feed(
        ServiceControlResponse(
            1,
            request_id,
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

    assert results == [(True, False, "device_error:APPLIED_MASK_MISSING")]
    assert controller.pending_request_id is None
    assert not core.device_transaction_active


@pytest.mark.parametrize("session_change", ("disconnect", "generation"))
def test_capture_profile_releases_transaction_when_session_changes(
    qapplication_session,
    session_change: str,
) -> None:
    endpoint = ("192.168.1.12", 4004)
    core = DeviceSessionCore(endpoint=endpoint)
    controller = CaptureProfileController(core)
    results: list[tuple[bool, bool, str]] = []
    controller.finished.connect(
        lambda target, ok, result: results.append((target, ok, result))
    )
    core.attach_transport(object(), lambda _frame: True)

    assert controller.request(False)
    assert core.device_transaction_active
    if session_change == "disconnect":
        core.end_connection()
    else:
        core.begin_connection(
            endpoint=endpoint,
            transport=object(),
            sender=lambda _frame: True,
            handshake_enabled=False,
        )

    assert controller.pending_request_id is None
    assert results == [(False, False, "device_error:SESSION_CHANGED")]
    assert not core.device_transaction_active


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
    assert controller.retry_timer.isActive()
    assert controller.retry_timer.interval() == SUBSCRIPTION_KEEPALIVE_INTERVAL_MS
    controller.stop()


def test_confirmed_product_subscription_sends_keepalive_and_stops_on_disconnect(
    qapplication_session,
) -> None:
    sent_request_ids: list[int] = []
    core = DeviceSessionCore(endpoint=("192.168.1.12", 4004))
    controller = ProductSubscriptionController(core)

    def send(frame: bytes) -> bool:
        sent_request_ids.append(int.from_bytes(frame[7:11], "little"))
        return True

    core.attach_transport(object(), send)
    controller.start()
    first_request_id = controller.pending_request_id
    assert first_request_id is not None
    controller.feed_response(
        ServiceControlResponse(
            1, first_request_id, ServiceControlOp.SUBSCRIBE, 0, 0,
            0, 0.0, 0.0, 0, 0, False,
        )
    )

    assert controller.confirmed
    assert controller.retry_timer.isActive()
    assert controller.retry_timer.interval() == SUBSCRIPTION_KEEPALIVE_INTERVAL_MS
    controller.retry_timer.timeout.emit()
    assert controller.pending_request_id is not None
    assert controller.pending_request_id != first_request_id
    assert sent_request_ids == [first_request_id, controller.pending_request_id]

    core.end_connection()
    assert not controller.retry_timer.isActive()
    assert not controller.confirmed


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
