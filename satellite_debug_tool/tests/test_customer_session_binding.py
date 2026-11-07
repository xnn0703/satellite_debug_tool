"""Customer attachment binding and dynamic capability isolation tests."""

from __future__ import annotations

from satellite_debug_tool.core.comm import EndpointDatagram, UdpEndpointBroker
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.customer.device_directory import CustomerDeviceDirectory
from satellite_debug_tool.core.customer.session_binding import (
    CustomerEndpointSessionBinding,
)
from satellite_debug_tool.core.external_power_monitor import ExternalPowerSample
from satellite_debug_tool.core.production.power_supply import PowerIdentity
from satellite_debug_tool.core.protocol import CmdType, build_frame
from satellite_debug_tool.core.session import (
    EndpointSessionDirectory,
    SessionOperationClass,
)
from satellite_debug_tool.ui.customer_session_bundle import (
    CustomerEndpointSessionBundleFactory,
)


ENDPOINT = ("192.168.1.13", 4004)


def _meta(serial_number: str) -> bytes:
    payload = bytes([2])
    for value in ("0.0.130", "afd01c", serial_number):
        encoded = value.encode("utf-8")
        payload += bytes([len(encoded)]) + encoded
    return build_frame(CmdType.META_INFO, payload)


def _make(tmp_path, monkeypatch):
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
    broker = UdpEndpointBroker(local_port=0)
    monkeypatch.setattr(broker, "start_broker", lambda: True)
    sent = []
    monkeypatch.setattr(
        broker,
        "send_to",
        lambda endpoint, frame: sent.append((endpoint, bytes(frame))) or True,
    )
    sessions = EndpointSessionDirectory(broker)
    devices = CustomerDeviceDirectory(settings, sessions)
    binding = CustomerEndpointSessionBinding(devices, ENDPOINT)
    return broker, sessions, devices, binding, sent


def test_detached_binding_sends_zero_and_attach_projects_presence(
    tmp_path, monkeypatch, qapplication_session
) -> None:
    broker, sessions, devices, binding, sent = _make(tmp_path, monkeypatch)
    frame = build_frame(CmdType.CONTROL, b"\x00")

    assert not binding.attached
    assert not binding.command_sender.send(
        frame,
        operation=SessionOperationClass.READ_ONLY_QUERY,
    )
    assert sent == []
    assert binding.attach()
    assert binding.connection_phase.value == "waiting"

    runtime = sessions.runtime(ENDPOINT)
    runtime.feed_datagram(
        EndpointDatagram(
            endpoint=ENDPOINT,
            data=_meta("SN-A"),
            wall_time_ns=1,
            monotonic_ns=1_000_000_000,
        )
    )
    assert binding.device_online
    assert binding.connection_phase.value == "online"

    gateway = binding.new_operation_gateway()
    owner = object()
    assert gateway.try_acquire_operation(
        owner,
        purpose="test mutation",
        allowed_operations={SessionOperationClass.MUTATING},
    )
    assert gateway.send(frame, operation=SessionOperationClass.MUTATING)
    assert sent[-1] == (ENDPOINT, frame)
    assert gateway.release_operation(owner)
    assert binding.detach()
    assert not binding.attached
    devices.shutdown()
    broker.stop_broker()


def test_customer_detach_keeps_production_transport_but_binding_is_disconnected(
    tmp_path, monkeypatch, qapplication_session
) -> None:
    broker, sessions, devices, binding, _sent = _make(tmp_path, monkeypatch)
    runtime = sessions.runtime(ENDPOINT)
    production_transport = runtime.acquire_transport("production-test")
    generation = runtime.core.generation

    assert binding.attach()
    assert runtime.core.generation == generation
    assert binding.detach()

    assert runtime.transport_active
    assert runtime.core.connected
    assert not binding.attached
    assert binding.connection_phase.value == "disconnected"
    assert production_transport.release()
    devices.shutdown()
    broker.stop_broker()


def test_delete_then_readd_same_endpoint_replaces_bundle_runtime_and_core(
    tmp_path, monkeypatch, qapplication_session
) -> None:
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
    broker = UdpEndpointBroker(local_port=0)
    monkeypatch.setattr(broker, "start_broker", lambda: True)
    monkeypatch.setattr(broker, "send_to", lambda *_args: True)
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

    first = factory.bundle(ENDPOINT)
    first_runtime = first.binding.runtime
    first_core = first.binding.core

    devices.delete(ENDPOINT)
    qapplication_session.processEvents()
    assert first.binding.closed
    assert factory.existing_bundle(ENDPOINT) is None
    assert sessions.runtime(ENDPOINT) is None

    devices.add(ENDPOINT)
    second = factory.bundle(ENDPOINT)
    assert second is not first
    assert second.binding.runtime is sessions.runtime(ENDPOINT)
    assert second.binding.runtime is not first_runtime
    assert second.binding.core is not first_core

    assert devices.attach(ENDPOINT)
    frame = build_frame(CmdType.CONTROL, b"\x00")
    assert not first.binding.command_sender.send(
        frame,
        operation=SessionOperationClass.READ_ONLY_QUERY,
    )
    assert second.binding.command_sender.send(
        frame,
        operation=SessionOperationClass.READ_ONLY_QUERY,
    )

    assert devices.detach(ENDPOINT)
    factory.shutdown_all()
    devices.shutdown()
    broker.stop_broker()


def _power_sample(*, monotonic_ns: int, output_enabled: bool) -> ExternalPowerSample:
    return ExternalPowerSample(
        host_timestamp_ns=1_800_000_000_000_000_000 + monotonic_ns,
        monotonic_ns=monotonic_ns,
        connection_generation=1,
        identity=PowerIdentity("GW-INSTEK", "PSW 80-27", "PSW1234", "1.70", "raw"),
        voltage_v=24.02,
        current_a=4.42,
        power_w=106.16,
        output_enabled=output_enabled,
        operation_condition=1,
        questionable_condition=0,
        protection_tripped=False,
    )


def _bound_composition(tmp_path, monkeypatch, status_messages):
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
    broker = UdpEndpointBroker(local_port=0)
    monkeypatch.setattr(broker, "start_broker", lambda: True)
    monkeypatch.setattr(broker, "send_to", lambda *_args: True)
    sessions = EndpointSessionDirectory(broker)
    factory = CustomerEndpointSessionBundleFactory(
        settings=settings,
        session_directory=sessions,
        status_sink=lambda message, timeout: status_messages.append((message, timeout)),
    )
    devices = CustomerDeviceDirectory(
        settings,
        sessions,
        supplemental_facts_provider=factory,
    )
    factory.bind_device_directory(devices)
    return broker, sessions, devices, factory


def test_power_output_enabled_alone_keeps_customer_detach_available(
    tmp_path, monkeypatch, qapplication_session
) -> None:
    """电源输出开启是稳态事实，不构成进行中端点操作；断开连接必须可用。"""

    status_messages = []
    broker, sessions, devices, factory = _bound_composition(
        tmp_path, monkeypatch, status_messages
    )
    bundle = factory.bundle(ENDPOINT)
    assert devices.attach(ENDPOINT)
    runtime = sessions.runtime(ENDPOINT)
    assert runtime is not None
    runtime.feed_datagram(
        EndpointDatagram(
            endpoint=ENDPOINT,
            data=_meta("SN-POWER"),
            wall_time_ns=1,
            monotonic_ns=1_000_000_000,
        )
    )
    store = bundle._external_power_store
    assert store is not None
    store.apply_sample(
        _power_sample(monotonic_ns=1_000_000_000, output_enabled=True)
    )

    facts = factory.facts(ENDPOINT, runtime)
    assert facts.operation_busy is False

    assert bundle.binding.detach() is True
    assert not bundle.binding.attached
    assert factory.facts(ENDPOINT, runtime).business_state == ""

    bundle.live.shutdown()
    assert factory.shutdown_all()
    devices.shutdown()
    sessions.shutdown()
    broker.stop_broker()


def test_detach_refusal_reason_reaches_status_sink(
    tmp_path, monkeypatch, qapplication_session
) -> None:
    """目录层拒绝断开时，拒绝原因必须到达状态提示通道。"""

    status_messages = []
    broker, sessions, devices, factory = _bound_composition(
        tmp_path, monkeypatch, status_messages
    )
    bundle = factory.bundle(ENDPOINT)
    assert devices.attach(ENDPOINT)
    runtime = sessions.runtime(ENDPOINT)
    assert runtime is not None
    runtime.feed_datagram(
        EndpointDatagram(
            endpoint=ENDPOINT,
            data=_meta("SN-BUSY"),
            wall_time_ns=1,
            monotonic_ns=1_000_000_000,
        )
    )
    lease = devices.attachment(ENDPOINT)
    gate_owner = object()
    assert runtime.acquire_operation(
        lease.capability,
        gate_owner,
        purpose="rf",
        allowed_operations={SessionOperationClass.MUTATING},
    )

    assert bundle.binding.detach() is False
    assert bundle.binding.attached
    assert status_messages
    reason_message = status_messages[-1][0]
    assert "断开" in reason_message or "disconnect" in reason_message.lower()

    assert runtime.release_operation(gate_owner)
    bundle.live.shutdown()
    assert factory.shutdown_all()
    devices.shutdown()
    sessions.shutdown()
    broker.stop_broker()
