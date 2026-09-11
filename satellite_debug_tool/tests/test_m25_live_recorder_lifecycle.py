"""M25 Live recorder ownership and simplified shutdown regressions."""

from __future__ import annotations

from satellite_debug_tool.core.comm import EndpointDatagram, UdpEndpointBroker
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.customer.device_directory import CustomerDeviceDirectory
from satellite_debug_tool.core.protocol import (
    CmdType,
    ServiceControlOp,
    ServiceControlResponse,
    build_frame,
)
from satellite_debug_tool.core.session import (
    EndpointSessionDirectory,
    RuntimeOperationGateState,
    SessionOperationClass,
)
from satellite_debug_tool.io.recording_path_registry import (
    RecordingPathRegistry,
    canonical_recording_key,
)
from satellite_debug_tool.ui import live_view as live_view_module
from satellite_debug_tool.ui.customer_session_bundle import (
    CustomerEndpointSessionBundleFactory,
)
from satellite_debug_tool.ui.live_view import CustomerRecordingState


ENDPOINT = ("192.168.1.13", 4004)


def _meta(serial_number: str, *, monotonic_s: float) -> EndpointDatagram:
    payload = bytes([2])
    for value in ("0.0.130", "afd01c", serial_number):
        encoded = value.encode("utf-8")
        payload += bytes([len(encoded)]) + encoded
    return EndpointDatagram(
        endpoint=ENDPOINT,
        data=build_frame(CmdType.META_INFO, payload),
        wall_time_ns=int(monotonic_s * 1_000_000_000),
        monotonic_ns=int(monotonic_s * 1_000_000_000),
    )


def _capture_response(request_id: int) -> ServiceControlResponse:
    return ServiceControlResponse(
        1,
        request_id,
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


def _settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    settings.set(
        "customer.devices",
        [{"ip": ENDPOINT[0], "port": ENDPOINT[1]}],
    )
    settings.set(
        "customer.active_endpoint",
        {"ip": ENDPOINT[0], "port": ENDPOINT[1]},
    )
    settings.save()
    return settings


def _composition(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path, monkeypatch)
    broker = UdpEndpointBroker(local_port=0)
    monkeypatch.setattr(broker, "start_broker", lambda: True)
    sent: list[tuple[tuple[str, int], bytes]] = []
    monkeypatch.setattr(
        broker,
        "send_to",
        lambda endpoint, frame: not sent.append((endpoint, bytes(frame))),
    )
    sessions = EndpointSessionDirectory(broker)
    factory = CustomerEndpointSessionBundleFactory(
        settings=settings,
        session_directory=sessions,
    )
    devices = CustomerDeviceDirectory(
        settings,
        sessions,
        supplemental_facts_provider=factory,
    )
    factory.bind_device_directory(devices)
    return broker, sessions, devices, factory, sent


def test_managed_live_passive_recorder_is_runtime_owned_and_allows_batch_freeze(
    tmp_path,
    monkeypatch,
    qapplication_session,
) -> None:
    broker, sessions, devices, factory, _sent = _composition(tmp_path, monkeypatch)
    bundle = factory.bundle(ENDPOINT)
    assert devices.attach(ENDPOINT)
    runtime = sessions.runtime(ENDPOINT)
    assert runtime is not None
    runtime.feed_datagram(_meta("AFD01-PASSIVE", monotonic_s=1.0))

    target = tmp_path / "engineering-passive.sdb"
    assert bundle.live._start_recording(
        format_version=2,
        customer=False,
        filepath=target,
    )
    assert runtime.recorder_count == 1
    assert devices.devices()[0].recording_active

    production = sessions.acquire_production_attachment(
        ENDPOINT,
        "production-passive",
    )
    runtime.feed_datagram(_meta("AFD01-PASSIVE", monotonic_s=2.0))
    production_gateway = production.new_operation_gateway()
    production_owner = object()
    assert production_gateway.try_acquire_operation(
        production_owner,
        purpose="production-batch",
        production_freeze=True,
        allowed_operations={SessionOperationClass.MUTATING},
    )
    assert production_gateway.release_operation(production_owner)

    assert bundle.live.shutdown()
    assert runtime.recorder_count == 0
    assert not devices.devices()[0].recording_active
    assert production.release()
    assert devices.detach(ENDPOINT)
    assert factory.shutdown_all()
    devices.shutdown()
    assert sessions.shutdown()
    broker.stop_broker()


def test_managed_live_stop_failure_retains_runtime_recorder_and_blocks_shutdown(
    tmp_path,
    monkeypatch,
    qapplication_session,
) -> None:
    broker, sessions, devices, factory, _sent = _composition(tmp_path, monkeypatch)
    bundle = factory.bundle(ENDPOINT)
    assert devices.attach(ENDPOINT)
    runtime = sessions.runtime(ENDPOINT)
    assert runtime is not None
    runtime.feed_datagram(_meta("AFD01-STOP", monotonic_s=1.0))
    assert bundle.live._start_recording(
        format_version=2,
        customer=False,
        filepath=tmp_path / "engineering-stop.sdb",
    )
    recorder = bundle.live._recorder
    assert recorder is not None
    real_stop = recorder.stop
    monkeypatch.setattr(recorder, "stop", lambda: False)

    assert not bundle.live.shutdown()
    assert bundle.live.is_recording()
    assert runtime.recorder_count == 1
    assert devices.attachment(ENDPOINT) is not None

    monkeypatch.setattr(recorder, "stop", real_stop)
    assert bundle.live.shutdown()
    assert runtime.recorder_count == 0
    assert devices.detach(ENDPOINT)
    assert factory.shutdown_all()
    devices.shutdown()
    assert sessions.shutdown()
    broker.stop_broker()


def test_managed_live_constructor_failure_releases_path_and_runtime_recorder(
    tmp_path,
    monkeypatch,
    qapplication_session,
) -> None:
    broker, sessions, devices, factory, _sent = _composition(tmp_path, monkeypatch)
    bundle = factory.bundle(ENDPOINT)
    assert devices.attach(ENDPOINT)
    runtime = sessions.runtime(ENDPOINT)
    assert runtime is not None
    runtime.feed_datagram(_meta("AFD01-START-FAIL", monotonic_s=1.0))

    def fail_constructor(*_args, **_kwargs):
        raise ValueError("invalid recorder metadata")

    monkeypatch.setattr(live_view_module, "DataRecorder", fail_constructor)
    target = tmp_path / "engineering-start-fail.sdb"
    assert not bundle.live._start_recording(
        format_version=2,
        customer=False,
        filepath=target,
    )
    assert runtime.recorder_count == 0
    assert (
        canonical_recording_key(target)
        not in RecordingPathRegistry.default().active_keys
    )
    assert not target.exists()

    assert devices.detach(ENDPOINT)
    assert factory.shutdown_all()
    devices.shutdown()
    assert sessions.shutdown()
    broker.stop_broker()


def test_bundle_shutdown_releases_capture_owner_without_persistent_recovery(
    tmp_path,
    monkeypatch,
    qapplication_session,
) -> None:
    broker, sessions, devices, factory, _sent = _composition(tmp_path, monkeypatch)
    bundle = factory.bundle(ENDPOINT)
    assert devices.attach(ENDPOINT)
    runtime = sessions.runtime(ENDPOINT)
    assert runtime is not None
    runtime.feed_datagram(_meta("AFD01-CAPTURE", monotonic_s=1.0))

    live = bundle.live
    live._customer_recording_path = tmp_path / "customer-capture.sdb"
    live._set_customer_recording_state(CustomerRecordingState.PREPARING)
    assert live._capture_profile_controller.request(True)
    request_id = live._capture_profile_controller.pending_request_id
    assert request_id is not None
    live._capture_profile_controller.feed_response(_capture_response(request_id))
    assert live.is_recording()
    assert live.customer_recording_state() is CustomerRecordingState.ACTIVE
    assert runtime.recorder_count == 1
    assert runtime.operation_gate.state is RuntimeOperationGateState.MUTATING

    # 退出时只收口本 endpoint 的 writer/owner，不创建持久化恢复记录。
    assert factory.shutdown_all()
    assert factory.existing_bundle(ENDPOINT) is None
    assert bundle.binding.closed
    assert runtime.recorder_count == 0
    assert runtime.operation_gate.state is RuntimeOperationGateState.IDLE
    assert not (tmp_path / "session_recovery.json").exists()
    devices.shutdown()
    assert devices.attachment(ENDPOINT) is None
    assert sessions.shutdown()
    broker.stop_broker()


def test_restore_timeout_releases_runtime_owner_and_marks_local_resync(
    tmp_path,
    monkeypatch,
    qapplication_session,
) -> None:
    broker, sessions, devices, factory, _sent = _composition(tmp_path, monkeypatch)
    bundle = factory.bundle(ENDPOINT)
    assert devices.attach(ENDPOINT)
    runtime = sessions.runtime(ENDPOINT)
    assert runtime is not None
    runtime.feed_datagram(_meta("AFD01-RESTORE", monotonic_s=1.0))

    live = bundle.live
    live._customer_recording_path = tmp_path / "customer-restore.sdb"
    live._set_customer_recording_state(CustomerRecordingState.PREPARING)
    assert live._capture_profile_controller.request(True)
    request_id = live._capture_profile_controller.pending_request_id
    assert request_id is not None
    live._capture_profile_controller.feed_response(_capture_response(request_id))
    assert live.is_recording()
    assert live._stop_recording(restore_customer_profile=True)
    assert live.customer_recording_state() is CustomerRecordingState.RESTORING

    restore_id = live._capture_profile_controller.pending_request_id
    assert restore_id is not None
    live._capture_profile_controller.expire()
    assert runtime.operation_gate.state is RuntimeOperationGateState.IDLE
    assert not live._capture_profile_controller.recording_lease_active
    assert live._capture_profile_controller.resync_required
    assert live.customer_recording_state() is CustomerRecordingState.IDLE

    # 本地未知状态不阻塞关闭，也不影响其他 endpoint 的会话。
    assert factory.shutdown_all()
    assert bundle.binding.closed
    assert runtime.recorder_count == 0
    assert runtime.operation_gate.state is RuntimeOperationGateState.IDLE
    assert not (tmp_path / "session_recovery.json").exists()

    devices.shutdown()
    assert devices.attachment(ENDPOINT) is None
    assert sessions.shutdown()
    broker.stop_broker()
