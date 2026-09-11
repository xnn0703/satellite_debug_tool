"""M25 shared UDP broker and endpoint-runtime safety contracts."""

from __future__ import annotations

import struct

import pytest

from satellite_debug_tool.core.comm import EndpointDatagram, UdpEndpointBroker
from satellite_debug_tool.core.product import ControlMode
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    CmdType,
    CommandResponse,
    MetaInfo,
    ParaTableReport,
    ProfileSemanticCapabilityEntry,
    ProfileSemanticsReport,
    RawFrame,
    RespCode,
    ServiceControlOp,
    ServiceControlResponse,
    build_frame,
)
from satellite_debug_tool.core.production import DeviceSession, FleetController, FleetDatagram
from satellite_debug_tool.core.session import (
    CustomerAttachmentScope,
    CaptureProfileController,
    DebugController,
    DeviceSessionCore,
    EndpointSessionDirectory,
    MountConfigurationController,
    OtaController,
    OtaState,
    OtaStatus,
    ParameterController,
    ProductControlController,
    RuntimeOperationGateState,
    RuntimeLeaseError,
    RuntimePresencePhase,
    SessionOperationClass,
)


def _meta_info(serial_number: str) -> bytes:
    payload = bytes([2])
    for value in ("0.0.130", "afd01", serial_number):
        encoded = value.encode("utf-8")
        payload += bytes([len(encoded)]) + encoded
    return build_frame(CmdType.META_INFO, payload)


def _datagram(
    endpoint: tuple[str, int],
    data: bytes,
    *,
    monotonic_s: float,
) -> EndpointDatagram:
    return EndpointDatagram(
        endpoint=endpoint,
        data=data,
        wall_time_ns=1,
        monotonic_ns=int(monotonic_s * 1_000_000_000),
    )


@pytest.fixture
def directory(monkeypatch, qapplication_session):
    broker = UdpEndpointBroker(local_port=0)
    monkeypatch.setattr(broker, "start_broker", lambda: True)
    monkeypatch.setattr(broker, "send_to", lambda _endpoint, _frame: True)
    value = EndpointSessionDirectory(broker)
    yield value
    broker.stop_broker()


def _runtime_for_controller(monkeypatch, *, suffix: int):
    broker = UdpEndpointBroker(local_port=0)
    sent: list[bytes] = []
    monkeypatch.setattr(broker, "start_broker", lambda: True)
    monkeypatch.setattr(
        broker,
        "send_to",
        lambda _endpoint, frame: not sent.append(bytes(frame)),
    )
    directory = EndpointSessionDirectory(broker)
    endpoint = (f"192.168.2.{suffix}", 4004)
    runtime = directory.get_or_create_runtime(endpoint)
    owner = f"controller-{suffix}"
    claim = broker.register_exact_claim(endpoint, owner=owner, facet="customer")
    transport = runtime.acquire_transport(owner)
    capability = runtime.issue_capability(
        owner=owner,
        facet="customer",
        parent_transport_lease=transport,
        admission_claim=claim,
    )
    runtime.feed_datagram(
        _datagram(endpoint, _meta_info(f"AFD01-{suffix}"), monotonic_s=1.0)
    )
    return runtime, capability, sent


def test_unknown_registered_envelope_does_not_establish_or_refresh_presence(
    directory: EndpointSessionDirectory,
) -> None:
    endpoint = ("192.168.1.13", 4004)
    runtime = directory.get_or_create_runtime(endpoint)
    claim = directory.broker.register_exact_claim(
        endpoint,
        owner="customer-a",
        facet="customer",
    )
    transport = runtime.acquire_transport("customer-a")

    records = runtime.feed_datagram(
        _datagram(endpoint, build_frame(0x12, b"unknown"), monotonic_s=1.0)
    )

    assert len(records) == 1
    assert isinstance(records[0], RawFrame)
    assert runtime.presence_phase is RuntimePresencePhase.WAITING
    assert runtime.presence_epoch == 0
    assert runtime.last_valid_record_at is None

    runtime.feed_datagram(_datagram(endpoint, _meta_info("AFD01-A"), monotonic_s=2.0))
    assert runtime.presence_phase is RuntimePresencePhase.ONLINE
    assert runtime.presence_epoch == 1
    assert runtime.last_valid_record_at == pytest.approx(2.0)

    runtime.feed_datagram(
        _datagram(endpoint, build_frame(0x12, b"still-unknown"), monotonic_s=4.0)
    )
    assert runtime.last_valid_record_at == pytest.approx(2.0)

    runtime.refresh_presence(5.01)
    assert runtime.presence_phase is RuntimePresencePhase.STALE

    assert transport.release()
    claim.release()


def test_runtime_emits_each_accepted_raw_datagram_once_before_decode_observation(
    directory: EndpointSessionDirectory,
) -> None:
    endpoint = ("192.168.1.18", 4004)
    runtime = directory.get_or_create_runtime(endpoint)
    claim = directory.broker.register_exact_claim(
        endpoint,
        owner="customer-raw",
        facet="customer",
    )
    transport = runtime.acquire_transport("customer-raw")
    raw_events: list[EndpointDatagram] = []
    decoded_events: list[tuple[EndpointDatagram, tuple[object, ...]]] = []
    runtime.datagram_received.connect(raw_events.append)
    runtime.records_received.connect(
        lambda datagram, records: decoded_events.append((datagram, tuple(records)))
    )

    frame = _meta_info("AFD01-RAW")
    leading = _datagram(endpoint, frame[:8], monotonic_s=1.0)
    trailing = _datagram(endpoint, frame[8:], monotonic_s=1.1)
    assert runtime.feed_datagram(leading) == ()
    assert raw_events == [leading]
    assert decoded_events == []

    assert runtime.feed_datagram(trailing)
    assert raw_events == [leading, trailing]
    assert len(decoded_events) == 1

    predecoded = _datagram(endpoint, b"already-decoded", monotonic_s=1.2)
    records = (MetaInfo(2, "0.0.130", "afd01", "AFD01-RAW"),)
    assert runtime.feed_decoded_datagram(predecoded, records) == records
    assert raw_events == [leading, trailing, predecoded]
    assert len(decoded_events) == 2

    # Directory/Fleet double delivery of the same immutable datagram is ignored.
    assert runtime.feed_decoded_datagram(predecoded, records) == ()
    assert raw_events == [leading, trailing, predecoded]
    assert len(decoded_events) == 2

    # A second ingress path may deliver the old object after another datagram.
    assert runtime.feed_datagram(leading) == ()
    assert raw_events == [leading, trailing, predecoded]
    assert len(decoded_events) == 2

    assert transport.release()
    claim.release()


def test_production_session_observes_runtime_raw_event_without_second_feed(
    directory: EndpointSessionDirectory,
    monkeypatch,
) -> None:
    endpoint = ("192.168.1.19", 4004)
    claim = directory.broker.register_exact_claim(
        endpoint,
        owner="production-raw",
        facet="production",
    )
    attachment = directory.acquire_production_attachment(
        endpoint,
        "production-raw",
        admission_claim=claim,
    )
    session = DeviceSession(
        endpoint,
        1,
        runtime=attachment.runtime,
        attachment=attachment,
        session_owner="production-raw",
    )
    recorded: list[tuple[EndpointDatagram, bool]] = []
    monkeypatch.setattr(
        session,
        "_record_datagram",
        lambda datagram, *, record: recorded.append((datagram, bool(record))),
    )

    frame = _meta_info("AFD01-PRODUCTION-RAW")
    leading = _datagram(endpoint, frame[:8], monotonic_s=1.0)
    trailing = _datagram(endpoint, frame[8:], monotonic_s=1.1)
    assert session.feed_datagram(leading) == ()
    assert session.feed_datagram(trailing)
    assert recorded == [(leading, True), (trailing, True)]

    # An already-routed duplicate never becomes a second recorder event.
    assert attachment.runtime.feed_datagram(trailing) == ()
    assert recorded == [(leading, True), (trailing, True)]

    predecoded = _datagram(endpoint, b"not-recorded", monotonic_s=1.2)
    records = (MetaInfo(2, "0.0.130", "afd01", "AFD01-PRODUCTION-RAW"),)
    assert session.feed_decoded_datagram(predecoded, records, record=False) == records
    assert recorded[-1] == (predecoded, False)

    assert session.release_attachment()
    claim.release()


def test_broker_and_directory_shutdown_are_bounded_and_lease_guarded(
    qapplication_session,
) -> None:
    broker = UdpEndpointBroker(local_port=0)
    endpoint = ("127.0.0.1", 4004)
    claim = broker.register_exact_claim(
        endpoint,
        owner="shutdown-test",
        facet="customer",
    )
    demand = broker.acquire_demand("shutdown-test")

    assert broker.has_active_demand
    assert broker.active_local_port is not None
    assert broker.active_local_port > 0
    assert not broker.shutdown(timeout_ms=100)

    demand.release()
    assert not broker.has_active_demand
    assert broker.active_local_port is None
    assert not broker.shutdown(timeout_ms=100)
    claim.release()
    assert broker.shutdown(timeout_ms=1000)

    directory = EndpointSessionDirectory(UdpEndpointBroker(local_port=0))
    configuration = directory.acquire_configuration(endpoint, "configured-device")
    assert not directory.shutdown(timeout_ms=100)
    assert configuration.release()
    assert directory.shutdown(timeout_ms=1000)
    assert directory.closed


def test_capability_claim_must_admit_the_same_endpoint_and_facet(
    directory: EndpointSessionDirectory,
) -> None:
    endpoint = ("192.168.1.20", 4004)
    other_endpoint = ("192.168.1.21", 4004)
    runtime = directory.get_or_create_runtime(endpoint)
    transport = runtime.acquire_transport("customer-claim")
    wrong_endpoint = directory.broker.register_exact_claim(
        other_endpoint,
        owner="customer-claim",
        facet="customer",
    )
    wrong_facet = directory.broker.register_exact_claim(
        endpoint,
        owner="production-claim",
        facet="production",
    )

    with pytest.raises(RuntimeLeaseError):
        runtime.issue_capability(
            owner="customer-claim",
            facet="customer",
            parent_transport_lease=transport,
            admission_claim=wrong_endpoint,
        )
    with pytest.raises(RuntimeLeaseError):
        runtime.issue_capability(
            owner="customer-claim",
            facet="customer",
            parent_transport_lease=transport,
            admission_claim=wrong_facet,
        )

    wrong_facet.release()
    wrong_endpoint.release()
    assert transport.release()


def _capture_response(request_id: int, *, applied: bool = True) -> ServiceControlResponse:
    return ServiceControlResponse(
        1,
        request_id,
        ServiceControlOp.SET_CAPTURE_PROFILE,
        0,
        (1 << 6) if applied else 0,
        0,
        0.0,
        0.0,
        0,
        0,
        False,
    )


def test_customer_capture_controller_holds_recorder_and_gate_until_restore(
    directory: EndpointSessionDirectory,
) -> None:
    endpoint = ("192.168.1.27", 4004)
    customer = directory.acquire_customer_attachment(
        endpoint,
        "customer-recording",
        CustomerAttachmentScope(endpoint, 1),
    )
    runtime = customer.runtime
    runtime.feed_datagram(
        _datagram(endpoint, _meta_info("AFD01-RECORDING"), monotonic_s=1.0)
    )
    capture = CaptureProfileController(
        runtime.core,
        operation_gateway=customer.new_operation_gateway(),
    )

    assert capture.request(True)
    prepare_id = capture.pending_request_id
    assert prepare_id is not None
    capture.feed_response(_capture_response(prepare_id))
    assert capture.recording_lease_active
    assert runtime.recorder_count == 1
    assert runtime.operation_gate.state is RuntimeOperationGateState.MUTATING

    production = directory.acquire_production_attachment(
        endpoint,
        "production-recording",
    )
    runtime.feed_datagram(
        _datagram(endpoint, _meta_info("AFD01-RECORDING"), monotonic_s=2.0)
    )
    production_gateway = production.new_operation_gateway()
    production_owner = object()
    assert not production_gateway.try_acquire_operation(
        production_owner,
        purpose="production-batch",
        production_freeze=True,
        allowed_operations={SessionOperationClass.MUTATING},
    )

    assert capture.request(False)
    restore_id = capture.pending_request_id
    assert restore_id is not None
    capture.feed_response(_capture_response(restore_id))
    assert not capture.recording_lease_active
    assert runtime.recorder_count == 0
    assert runtime.operation_gate.state is RuntimeOperationGateState.IDLE
    assert production_gateway.try_acquire_operation(
        production_owner,
        purpose="production-batch",
        production_freeze=True,
        allowed_operations={SessionOperationClass.MUTATING},
    )
    assert production_gateway.release_operation(production_owner)

    assert production.release()
    assert customer.release()


def test_production_freeze_system_traffic_requires_explicit_batch_allowlist(
    directory: EndpointSessionDirectory,
) -> None:
    endpoint = ("192.168.1.12", 4004)
    runtime = directory.get_or_create_runtime(endpoint)
    claim = directory.broker.register_exact_claim(
        endpoint,
        owner="production",
        facet="production",
    )
    transport = runtime.acquire_transport("production")
    capability = runtime.issue_capability(
        owner="batch-1",
        facet="production",
        parent_transport_owner="production",
    )
    runtime.feed_datagram(_datagram(endpoint, _meta_info("AFD01-P"), monotonic_s=1.0))
    gate_owner = object()

    assert runtime.acquire_operation(
        capability,
        gate_owner,
        purpose="batch",
        production_freeze=True,
        allowed_operations={SessionOperationClass.READ_ONLY_QUERY},
    )
    assert runtime.operation_gate.state is RuntimeOperationGateState.PRODUCTION_FROZEN

    # Customer/system demands do not become an implicit batch allowlist.
    customer_handshake = runtime.acquire_handshake("customer", facet="customer")
    assert not runtime._send_system(
        build_frame(CmdType.CONTROL, b"\x04"),
        owner="system:handshake",
        facet="system",
        operation=SessionOperationClass.HANDSHAKE,
    )

    production_handshake = runtime.acquire_handshake("batch-1", facet="production")
    assert not runtime._send_system(
        build_frame(CmdType.CONTROL, b"\x04"),
        owner="system:handshake",
        facet="system",
        operation=SessionOperationClass.HANDSHAKE,
    )

    # The same production demand is usable only after the batch explicitly
    # includes HANDSHAKE in its phase allowlist.
    assert runtime.update_production_allowlist(
        gate_owner,
        {
            SessionOperationClass.READ_ONLY_QUERY,
            SessionOperationClass.HANDSHAKE,
        },
    )
    assert runtime._send_system(
        build_frame(CmdType.CONTROL, b"\x04"),
        owner="system:handshake",
        facet="system",
        operation=SessionOperationClass.HANDSHAKE,
    )

    assert runtime.release_operation(gate_owner)
    assert runtime.release_capability(capability)
    production_handshake.release()
    customer_handshake.release()
    assert transport.release()
    claim.release()


def test_runtime_mutation_uses_capability_gate_and_emits_one_sent_event(
    directory: EndpointSessionDirectory,
) -> None:
    endpoint = ("192.168.1.14", 4004)
    runtime = directory.get_or_create_runtime(endpoint)
    claim = directory.broker.register_exact_claim(
        endpoint,
        owner="customer-c",
        facet="customer",
    )
    transport = runtime.acquire_transport("customer-c")
    capability = runtime.issue_capability(
        owner="customer-c",
        facet="customer",
        parent_transport_owner="customer-c",
    )
    owner = object()
    frame = build_frame(CmdType.CONTROL, b"\x01")
    events: list[object] = []
    runtime.datagram_sent.connect(events.append)

    assert not runtime.acquire_operation(capability, owner, purpose="rf")
    assert not runtime.command_sender(capability, gate_owner=owner).send(frame)
    assert events == []

    runtime.feed_datagram(_datagram(endpoint, _meta_info("AFD01-C"), monotonic_s=1.0))
    assert runtime.acquire_operation(capability, owner, purpose="rf")
    assert runtime.command_sender(capability, gate_owner=owner).send(frame)
    assert len(events) == 1
    assert events[0].endpoint == endpoint
    assert events[0].operation_class is SessionOperationClass.MUTATING

    assert runtime.release_operation(owner)
    assert runtime.release_capability(capability)
    assert transport.release()
    claim.release()


def test_real_controllers_share_runtime_gateway_and_production_cas(
    directory: EndpointSessionDirectory,
    monkeypatch,
) -> None:
    endpoint = ("192.168.1.15", 4004)
    sent: list[tuple[tuple[str, int], bytes]] = []
    monkeypatch.setattr(
        directory.broker,
        "send_to",
        lambda target, frame: not sent.append((target, bytes(frame))),
    )
    runtime = directory.get_or_create_runtime(endpoint)
    claim = directory.broker.register_exact_claim(
        endpoint,
        owner="customer-d",
        facet="customer",
    )
    transport = runtime.acquire_transport("customer-d")
    capability = runtime.issue_capability(
        owner="customer-d",
        facet="customer",
        parent_transport_owner="customer-d",
    )
    runtime.feed_datagram(_datagram(endpoint, _meta_info("AFD01-D"), monotonic_s=1.0))
    core = runtime.core
    core.profile_store.apply_meta(MetaInfo(2, "0.0.140", "afd01", "AFD01-D"))
    core.profile_store.apply_profile_semantics(
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

    product = ProductControlController(
        core,
        operation_gateway=runtime.operation_gateway(capability),
    )
    parameters = ParameterController(
        core,
        operation_gateway=runtime.operation_gateway(capability),
    )
    ota = OtaController(
        core,
        operation_gateway=runtime.operation_gateway(capability),
    )
    ota._on_profile_changed("afd01")
    debug = DebugController(
        core,
        operation_gateway=runtime.operation_gateway(capability),
    )
    debug.set_connected(True)

    sent.clear()
    assert product.request_control_mode(ControlMode.MANUAL)
    assert len(sent) == 1
    assert runtime.operation_gate.state is RuntimeOperationGateState.MUTATING
    assert core.device_transaction_active
    assert not core.try_acquire_device_transaction(object())

    production_transport = runtime.acquire_transport("production")
    production_capability = runtime.issue_capability(
        owner="batch-2",
        facet="production",
        parent_transport_owner="production",
    )
    runtime.feed_datagram(_datagram(endpoint, _meta_info("AFD01-D"), monotonic_s=2.0))
    production_owner = object()
    assert not runtime.acquire_operation(
        production_capability,
        production_owner,
        purpose="batch",
        production_freeze=True,
        allowed_operations={SessionOperationClass.MUTATING},
    )
    pending = product.pending
    assert pending is not None
    product.feed_response(
        ServiceControlResponse(
            1,
            pending.request_id,
            int(pending.operation),
            5,
            0,
            0,
            0.0,
            0.0,
            0,
            0,
            False,
        )
    )
    assert not core.device_transaction_active

    assert parameters.request_table()
    assert len(sent) == 2
    core.apply_records((ParaTableReport(table_ver=1, params=[]),))
    assert runtime.operation_gate.state is RuntimeOperationGateState.IDLE

    token = ota.configure_engineering_file(b"A" * 32, "afd01.bin")
    assert token is not None
    assert ota.start(pause_debug=False)
    assert len(sent) == 3
    ota.feed_record(CommandResponse(RespCode.INTERNAL_ERROR, "OTA_BEGIN=REJECTED"))
    assert runtime.operation_gate.state is RuntimeOperationGateState.IDLE

    one_shot_mutations = (
        lambda: debug.set_sample_rate(10),
        lambda: debug.set_channel_enable_mask(1),
        debug.reset_statistics,
        lambda: debug.set_trace_mode(1),
        lambda: debug.send_user_mark(1, "runtime-gateway"),
    )
    for mutation in one_shot_mutations:
        previous_count = len(sent)
        assert not mutation()
        assert len(sent) == previous_count
        assert runtime.operation_gate.state is RuntimeOperationGateState.IDLE

    debug.request(True)
    assert debug.pending_target is True
    assert runtime.operation_gate.state is RuntimeOperationGateState.MUTATING
    pending_count = len(sent)
    assert not debug.reset_statistics()
    assert len(sent) == pending_count
    debug.feed_response(CommandResponse(RespCode.SUCCESS, "DEBUG_ENABLE=1"))
    assert runtime.operation_gate.state is RuntimeOperationGateState.IDLE

    assert runtime.acquire_operation(
        production_capability,
        production_owner,
        purpose="batch",
        production_freeze=True,
        allowed_operations={SessionOperationClass.MUTATING},
    )
    frozen_count = len(sent)
    assert not product.request_control_mode(ControlMode.MANUAL)
    assert len(sent) == frozen_count
    assert runtime.release_operation(production_owner)

    assert runtime.release_capability(production_capability)
    assert production_transport.release()
    assert runtime.release_capability(capability)
    assert transport.release()
    claim.release()


def test_runtime_gateway_drops_local_owner_after_trusted_external_clear(
    directory: EndpointSessionDirectory,
) -> None:
    endpoint = ("192.168.2.50", 4004)
    runtime = directory.get_or_create_runtime(endpoint)
    claim = directory.broker.register_exact_claim(
        endpoint,
        owner="external-clear",
        facet="customer",
    )
    transport = runtime.acquire_transport("external-clear")
    capability = runtime.issue_capability(
        owner="external-clear",
        facet="customer",
        parent_transport_lease=transport,
        admission_claim=claim,
    )
    runtime.feed_datagram(
        _datagram(endpoint, _meta_info("AFD01-EXTERNAL-CLEAR"), monotonic_s=1.0)
    )
    gateway = runtime.operation_gateway(capability)
    owner = object()
    assert gateway.try_acquire_operation(owner, purpose="external-clear-test")
    assert runtime.operation_gate.release(owner)
    assert gateway.release_operation(owner)
    assert not gateway.release_operation(object())


@pytest.mark.parametrize("rejected_stage", ("data", "end"))
def test_ota_data_or_end_rejection_releases_local_owner_after_abort_timeout(
    monkeypatch,
    qapplication_session,
    rejected_stage: str,
) -> None:
    suffix = 45 if rejected_stage == "data" else 46
    runtime, capability, sent = _runtime_for_controller(monkeypatch, suffix=suffix)
    core = runtime.core
    core.profile_store.apply_profile_semantics(
        "afd01",
        ProfileSemanticsReport(
            table_ver=1,
            capabilities=[
                ProfileSemanticCapabilityEntry("ota", True),
                ProfileSemanticCapabilityEntry("command_response_context", True),
            ],
        ),
    )
    ota = OtaController(
        core,
        operation_gateway=runtime.operation_gateway(capability),
    )
    ota._on_profile_changed("afd01")
    statuses: list[OtaStatus] = []
    ota.status_changed.connect(lambda status, _values: statuses.append(status))
    assert ota.configure_engineering_file(b"A", "one-byte.bin") is not None
    assert ota.start(pause_debug=False)
    ota.feed_record(CommandResponse(RespCode.SUCCESS, "OTA_BEGIN=READY"))
    qapplication_session.processEvents()
    if rejected_stage == "data":
        ota.feed_record(CommandResponse(RespCode.INTERNAL_ERROR, "OTA_DATA=0"))
        assert OtaStatus.CHUNK_REJECTED in statuses
    else:
        ota.feed_record(CommandResponse(RespCode.SUCCESS, "OTA_DATA=0"))
        qapplication_session.processEvents()
        ota.feed_record(CommandResponse(RespCode.INTERNAL_ERROR, "OTA_END=FAILED"))
        assert OtaStatus.END_REJECTED in statuses

    assert ota.state is OtaState.TERMINATING
    assert OtaStatus.ABORT_PENDING in statuses
    assert runtime.operation_gate.state is RuntimeOperationGateState.TERMINATING
    ota._on_response_timeout()
    assert runtime.operation_gate.state is RuntimeOperationGateState.IDLE
    assert ota.state is OtaState.IDLE
    assert statuses[-1] is OtaStatus.ABORT_PENDING


def test_new_customer_attachment_requires_identity_seen_after_its_epoch(
    directory: EndpointSessionDirectory,
) -> None:
    endpoint = ("192.168.1.17", 4004)
    runtime = directory.get_or_create_runtime(endpoint)
    production_transport = runtime.acquire_transport("production-keepalive")

    first = directory.acquire_customer_attachment(
        endpoint,
        "customer-first",
        CustomerAttachmentScope(endpoint, 1),
    )
    runtime.feed_datagram(_datagram(endpoint, _meta_info("AFD01-E"), monotonic_s=1.0))
    first_owner = object()
    assert runtime.acquire_operation(
        first.capability,
        first_owner,
        purpose="first",
    )
    assert runtime.release_operation(first_owner)
    assert first.release()

    generation = runtime.core.generation
    second = directory.acquire_customer_attachment(
        endpoint,
        "customer-second",
        CustomerAttachmentScope(endpoint, 2),
    )
    second_owner = object()
    assert runtime.core.generation == generation
    assert not runtime.acquire_operation(
        second.capability,
        second_owner,
        purpose="second",
    )

    # A non-identity record cannot promote the new attachment.
    runtime.feed_datagram(
        _datagram(
            endpoint,
            build_frame(
                CmdType.HEARTBEAT,
                struct.pack("<IBIHHI", 1, 1, 1024, 1, 1, 0),
            ),
            monotonic_s=1.5,
        )
    )
    assert not runtime.acquire_operation(
        second.capability,
        second_owner,
        purpose="second",
    )

    runtime.feed_datagram(_datagram(endpoint, _meta_info("AFD01-E"), monotonic_s=2.0))
    assert runtime.acquire_operation(
        second.capability,
        second_owner,
        purpose="second",
    )
    assert runtime.release_operation(second_owner)
    assert second.release()
    assert production_transport.release()


def test_production_reuses_injected_broker_directory_and_candidate_parser(
    monkeypatch,
    qapplication_session,
) -> None:
    broker = UdpEndpointBroker(local_port=0)
    monkeypatch.setattr(broker, "start_broker", lambda: True)
    monkeypatch.setattr(broker, "send_to", lambda _endpoint, _frame: True)
    directory = EndpointSessionDirectory(broker)
    controller = FleetController(
        discovery_cidr="192.168.1.0/24",
        local_port=0,
        broker=broker,
        session_directory=directory,
    )
    endpoint = ("192.168.1.16", 4004)
    identity = _meta_product_identity("AFD01-FLEET")

    controller._on_datagram(
        FleetDatagram(endpoint, identity[:8], 1, 1_000_000_000)
    )
    assert controller.sessions() == ()
    assert controller.candidate_count == 1

    controller._on_datagram(
        FleetDatagram(endpoint, identity[8:], 2, 1_100_000_000)
    )
    session = controller.sessions()[0]
    runtime = directory.runtime(endpoint)
    assert runtime is not None
    assert controller.session_directory is directory
    assert controller.hub.broker is broker
    assert session.runtime is runtime
    assert session.core is runtime.core
    assert session.receiver.frames_ok == 0
    assert session.serial_number == "AFD01-FLEET"

    controller.stop()
    broker.stop_broker()


def test_production_admission_observes_active_runtime_without_candidate_redecode(
    directory: EndpointSessionDirectory,
) -> None:
    endpoint = ("192.168.1.23", 4004)
    claim = directory.broker.register_exact_claim(
        endpoint,
        owner="customer-active",
        facet="customer",
    )
    customer_transport = directory.acquire_transport(endpoint, "customer-active")
    runtime = directory.runtime(endpoint)
    assert runtime is not None
    controller = FleetController(
        discovery_cidr="192.168.1.0/24",
        local_port=0,
        broker=directory.broker,
        session_directory=directory,
    )
    controller._admission_enabled = True
    datagram = FleetDatagram(
        endpoint,
        _meta_product_identity("AFD01-SHARED-FEED"),
        1,
        1_000_000_000,
    )

    assert runtime.feed_datagram(datagram)
    session = controller.sessions()[0]
    assert session.runtime is runtime
    assert session.core is runtime.core
    assert session.serial_number == "AFD01-SHARED-FEED"
    assert controller.candidate_count == 0
    assert runtime.core.receiver.frames_ok == 1

    # The Fleet ingress path sees the immutable datagram again but cannot parse it twice.
    controller._on_broker_datagram(datagram)
    assert runtime.core.receiver.frames_ok == 1
    assert controller.candidate_count == 0

    controller.stop()
    assert customer_transport.release()
    claim.release()


def test_production_host_injection_fallback_cannot_send_wire(
    monkeypatch,
    qapplication_session,
) -> None:
    broker = UdpEndpointBroker(local_port=45678)
    wire_frames: list[bytes] = []
    monkeypatch.setattr(broker, "start_broker", lambda: False)
    monkeypatch.setattr(
        broker,
        "send_to",
        lambda _endpoint, frame: not wire_frames.append(bytes(frame)),
    )
    directory = EndpointSessionDirectory(broker)
    controller = FleetController(
        discovery_cidr="127.0.0.1/32",
        local_port=45678,
        broker=broker,
        session_directory=directory,
    )
    endpoint = ("127.0.0.1", 4004)

    controller.inject_datagram(
        FleetDatagram(
            endpoint,
            _meta_product_identity("AFD01-INJECTED"),
            1,
            1_000_000_000,
        )
    )

    session = controller.sessions()[0]
    attachment = session.attachment
    assert attachment is not None
    assert not attachment.capability.wire_send_allowed
    assert not attachment.runtime.wire_transport_active
    assert broker.demand_count == 0
    assert wire_frames == []

    gateway = attachment.new_operation_gateway()
    owner = object()
    assert gateway.try_acquire_operation(
        owner,
        purpose="host-test-freeze",
        production_freeze=True,
        allowed_operations={SessionOperationClass.MUTATING},
    )
    assert not gateway.send(
        build_frame(CmdType.CONTROL, b"\x01"),
        operation=SessionOperationClass.MUTATING,
    )
    assert wire_frames == []
    assert gateway.release_operation(owner)

    controller.stop()
    assert directory.shutdown()


def test_production_candidate_65th_source_is_rejected_without_eviction(
    monkeypatch,
    qapplication_session,
) -> None:
    broker = UdpEndpointBroker(local_port=0)
    monkeypatch.setattr(broker, "start_broker", lambda: True)
    controller = FleetController(
        discovery_cidr="192.168.0.0/22",
        local_port=0,
        broker=broker,
        session_directory=EndpointSessionDirectory(broker),
    )
    rejected: list[str] = []
    controller.endpoint_rejected.connect(rejected.append)
    partial = _meta_product_identity("AFD01-CANDIDATE")[:8]
    endpoints = [
        (f"192.168.{index // 254}.{index % 254 + 1}", 4004)
        for index in range(65)
    ]
    for index, endpoint in enumerate(endpoints):
        controller._on_datagram(
            FleetDatagram(endpoint, partial, index + 1, 1_000_000_000 + index)
        )

    assert controller.candidate_count == 64
    assert controller.candidate_rejections == 1
    assert rejected == [f"{endpoints[-1][0]}:{endpoints[-1][1]}"]
    controller.stop()
    broker.stop_broker()


def _meta_product_identity(serial_number: str) -> bytes:
    payload = struct.pack("<BII", 1, 1, 0x17)
    for value in ("AFD01", serial_number, "0.0.130", "0.0.3"):
        encoded = value.encode("utf-8")
        payload += bytes([len(encoded)]) + encoded
    payload += bytes([2])
    return build_frame(CmdType.SERVICE_IDENTITY, payload)
