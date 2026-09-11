"""Customer attachment binding and dynamic capability isolation tests."""

from __future__ import annotations

from satellite_debug_tool.core.comm import EndpointDatagram, UdpEndpointBroker
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.customer.device_directory import CustomerDeviceDirectory
from satellite_debug_tool.core.customer.session_binding import (
    CustomerEndpointSessionBinding,
)
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
