"""Settings-backed customer Directory adapter regression tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from satellite_debug_tool.core.comm import EndpointDatagram, UdpEndpointBroker
from satellite_debug_tool.core.config import Settings, SettingsSaveError
from satellite_debug_tool.core.customer import (
    CustomerDeviceBusyError,
    CustomerDeviceCapacityError,
    CustomerDeviceConfigurationBlocked,
    CustomerDeviceDirectory,
    CustomerDevicePersistenceError,
    CustomerDeviceSupplementalFacts,
)
from satellite_debug_tool.core.protocol import CmdType, build_frame
from satellite_debug_tool.core.session import (
    EndpointSessionDirectory,
    SessionOperationClass,
)


AFD_ENDPOINT = ("192.168.1.13", 4004)
ESA_ENDPOINT = ("192.168.1.12", 4004)
THIRD_ENDPOINT = ("192.168.1.14", 4004)
FOURTH_ENDPOINT = ("192.168.1.15", 4004)
FIFTH_ENDPOINT = ("192.168.1.16", 4004)


def _entry(endpoint):
    return {"ip": endpoint[0], "port": endpoint[1]}


def _meta_info(serial_number: str, hardware: str = "afd01") -> bytes:
    payload = bytes([2])
    for value in ("0.0.130", hardware, serial_number):
        encoded = value.encode("utf-8")
        payload += bytes([len(encoded)]) + encoded
    return build_frame(CmdType.META_INFO, payload)


@pytest.fixture
def customer_settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setenv("HOME", str(tmp_path))
    return Settings()


@pytest.fixture
def session_directory(monkeypatch, qapplication_session):
    broker = UdpEndpointBroker(local_port=0)
    monkeypatch.setattr(broker, "start_broker", lambda: True)
    monkeypatch.setattr(broker, "send_to", lambda _endpoint, _frame: True)
    directory = EndpointSessionDirectory(broker)
    yield directory
    broker.stop_broker()


def _seed(settings: Settings, endpoints, active) -> None:
    settings.set("customer.devices", [_entry(endpoint) for endpoint in endpoints])
    settings.set(
        "customer.active_endpoint",
        _entry(active) if active is not None else None,
    )
    settings.save()


def test_initial_projection_owns_one_configuration_lease_without_transport(
    customer_settings,
    session_directory,
) -> None:
    _seed(customer_settings, (AFD_ENDPOINT, ESA_ENDPOINT), ESA_ENDPOINT)
    adapter = CustomerDeviceDirectory(customer_settings, session_directory)

    assert adapter.endpoints() == (AFD_ENDPOINT, ESA_ENDPOINT)
    assert adapter.active_endpoint() == ESA_ENDPOINT
    assert tuple(snapshot.endpoint for snapshot in adapter.devices()) == (
        AFD_ENDPOINT,
        ESA_ENDPOINT,
    )
    for endpoint in adapter.endpoints():
        runtime = session_directory.runtime(endpoint)
        assert runtime is not None
        assert runtime.transport_active is False
        assert runtime.configuration_owners == {
            f"customer-config:{endpoint[0]}:{endpoint[1]}"
        }
    assert [snapshot.connection_phase for snapshot in adapter.devices()] == [
        "DISCONNECTED",
        "DISCONNECTED",
    ]

    adapter.shutdown()
    assert session_directory.endpoints() == ()


def test_add_duplicate_and_capacity_keep_stable_order_and_explicit_result(
    customer_settings,
    session_directory,
) -> None:
    adapter = CustomerDeviceDirectory(customer_settings, session_directory)
    devices_events = []
    active_events = []
    adapter.devices_changed.connect(lambda: devices_events.append(adapter.endpoints()))
    adapter.active_endpoint_changed.connect(active_events.append)

    adapter.add(AFD_ENDPOINT)
    assert adapter.attachment(AFD_ENDPOINT) is None
    assert session_directory.runtime(AFD_ENDPOINT).transport_active is False
    adapter.add(ESA_ENDPOINT)
    adapter.add(THIRD_ENDPOINT)
    adapter.add(FOURTH_ENDPOINT)
    assert adapter.endpoints() == (
        AFD_ENDPOINT,
        ESA_ENDPOINT,
        THIRD_ENDPOINT,
        FOURTH_ENDPOINT,
    )
    assert adapter.active_endpoint() == FOURTH_ENDPOINT
    assert len(devices_events) == 4

    duplicate = adapter.add(AFD_ENDPOINT)
    assert duplicate.duplicate_selected is True
    assert duplicate.changed is True
    assert adapter.active_endpoint() == AFD_ENDPOINT
    assert len(devices_events) == 4
    assert active_events[-1] == AFD_ENDPOINT

    with pytest.raises(CustomerDeviceCapacityError):
        adapter.add(FIFTH_ENDPOINT)
    assert adapter.endpoints() == (
        AFD_ENDPOINT,
        ESA_ENDPOINT,
        THIRD_ENDPOINT,
        FOURTH_ENDPOINT,
    )
    assert session_directory.endpoints() == tuple(sorted(adapter.endpoints()))
    assert customer_settings.get("customer.devices") == [
        _entry(endpoint) for endpoint in adapter.endpoints()
    ]
    adapter.shutdown()


def test_edit_and_delete_preserve_order_and_apply_deterministic_active_fallback(
    customer_settings,
    session_directory,
) -> None:
    _seed(
        customer_settings,
        (AFD_ENDPOINT, ESA_ENDPOINT, THIRD_ENDPOINT),
        ESA_ENDPOINT,
    )
    adapter = CustomerDeviceDirectory(customer_settings, session_directory)

    result = adapter.edit(ESA_ENDPOINT, FOURTH_ENDPOINT)
    assert result.changed is True
    assert adapter.endpoints() == (AFD_ENDPOINT, FOURTH_ENDPOINT, THIRD_ENDPOINT)
    assert adapter.active_endpoint() == FOURTH_ENDPOINT
    assert session_directory.runtime(ESA_ENDPOINT) is None
    assert session_directory.runtime(FOURTH_ENDPOINT) is not None

    adapter.delete(FOURTH_ENDPOINT)
    assert adapter.endpoints() == (AFD_ENDPOINT, THIRD_ENDPOINT)
    assert adapter.active_endpoint() == THIRD_ENDPOINT
    adapter.delete(THIRD_ENDPOINT)
    assert adapter.active_endpoint() == AFD_ENDPOINT
    adapter.delete(AFD_ENDPOINT)
    assert adapter.endpoints() == ()
    assert adapter.active_endpoint() is None
    assert customer_settings.get("customer.devices") == []
    assert customer_settings.get("customer.active_endpoint") is None
    adapter.shutdown()


def test_edit_to_existing_selects_target_without_mutating_source(
    customer_settings,
    session_directory,
) -> None:
    _seed(customer_settings, (AFD_ENDPOINT, ESA_ENDPOINT), AFD_ENDPOINT)
    adapter = CustomerDeviceDirectory(customer_settings, session_directory)

    result = adapter.edit(AFD_ENDPOINT, ESA_ENDPOINT)

    assert result.duplicate_selected is True
    assert adapter.endpoints() == (AFD_ENDPOINT, ESA_ENDPOINT)
    assert adapter.active_endpoint() == ESA_ENDPOINT
    assert customer_settings.get("customer.devices") == [
        _entry(AFD_ENDPOINT),
        _entry(ESA_ENDPOINT),
    ]
    adapter.shutdown()


@pytest.mark.parametrize("operation", ["select", "add", "edit", "delete"])
def test_settings_failure_rolls_back_memory_leases_and_signals(
    operation,
    customer_settings,
    session_directory,
    monkeypatch,
) -> None:
    _seed(customer_settings, (AFD_ENDPOINT,), AFD_ENDPOINT)
    adapter = CustomerDeviceDirectory(customer_settings, session_directory)
    before_devices = copy.deepcopy(customer_settings.get("customer.devices"))
    before_active = copy.deepcopy(customer_settings.get("customer.active_endpoint"))
    before_file = customer_settings._config_file.read_bytes()
    devices_events = []
    active_events = []
    adapter.devices_changed.connect(lambda: devices_events.append(True))
    adapter.active_endpoint_changed.connect(active_events.append)

    def fail_save() -> None:
        raise SettingsSaveError("injected customer save failure")

    monkeypatch.setattr(customer_settings, "save", fail_save)
    with pytest.raises(CustomerDevicePersistenceError):
        if operation == "select":
            adapter.select_endpoint(None)
        elif operation == "add":
            adapter.add(ESA_ENDPOINT)
        elif operation == "edit":
            adapter.edit(AFD_ENDPOINT, ESA_ENDPOINT)
        else:
            adapter.delete(AFD_ENDPOINT)

    assert adapter.endpoints() == (AFD_ENDPOINT,)
    assert adapter.active_endpoint() == AFD_ENDPOINT
    assert session_directory.endpoints() == (AFD_ENDPOINT,)
    assert customer_settings.get("customer.devices") == before_devices
    assert customer_settings.get("customer.active_endpoint") == before_active
    assert customer_settings._config_file.read_bytes() == before_file
    assert devices_events == []
    assert active_events == []
    adapter.shutdown()


def test_uncertain_settings_commit_blocks_followup_and_releases_provisional_lease(
    customer_settings,
    session_directory,
    monkeypatch,
) -> None:
    _seed(customer_settings, (AFD_ENDPOINT,), AFD_ENDPOINT)
    adapter = CustomerDeviceDirectory(customer_settings, session_directory)

    def fail_after_replace() -> None:
        customer_settings._read_only_recovery = True
        customer_settings._device_configuration_blocked = True
        customer_settings._device_configuration_error = "durability unconfirmed"
        raise SettingsSaveError("injected post-replace uncertainty")

    monkeypatch.setattr(customer_settings, "save", fail_after_replace)
    with pytest.raises(CustomerDevicePersistenceError):
        adapter.add(ESA_ENDPOINT)

    assert adapter.configuration_blocked
    assert adapter.endpoints() == (AFD_ENDPOINT,)
    assert session_directory.endpoints() == (AFD_ENDPOINT,)
    with pytest.raises(CustomerDeviceConfigurationBlocked):
        adapter.select_endpoint(None)
    adapter.shutdown()


class _SupplementalFacts(QObject):
    device_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.recording = False

    def __call__(self, _endpoint, _runtime):
        return CustomerDeviceSupplementalFacts(
            business_state="RECORDING" if self.recording else "",
            recording_active=self.recording,
        )


def test_snapshot_uses_current_presence_identity_gate_and_supplemental_facts(
    customer_settings,
    session_directory,
) -> None:
    _seed(customer_settings, (AFD_ENDPOINT,), AFD_ENDPOINT)
    supplemental = _SupplementalFacts()
    adapter = CustomerDeviceDirectory(
        customer_settings,
        session_directory,
        supplemental_facts_provider=supplemental,
    )
    attachment = adapter.attach(AFD_ENDPOINT)
    assert attachment.attachment_scope.epoch == 1
    runtime = attachment.runtime
    changed = []
    adapter.device_changed.connect(changed.append)

    waiting = adapter.devices()[0]
    assert waiting.connection_phase == "WAITING"
    assert waiting.identity_pending is True
    assert waiting.display_identity == ""

    runtime.feed_datagram(
        EndpointDatagram(
            endpoint=AFD_ENDPOINT,
            data=_meta_info("SN-A"),
            wall_time_ns=1,
            monotonic_ns=1_000_000_000,
        )
    )
    online = adapter.devices()[0]
    assert online.connection_phase == "ONLINE"
    assert online.identity_pending is False
    assert online.display_identity == "AFD01 SN-A"

    gate_owner = object()
    assert runtime.acquire_operation(
        attachment.capability,
        gate_owner,
        purpose="rf",
        allowed_operations={SessionOperationClass.MUTATING},
    )
    busy = adapter.devices()[0]
    assert busy.operation_busy is True
    assert busy.business_state == "OPERATION_BUSY"
    assert adapter.detach(AFD_ENDPOINT) is False
    assert adapter.attachment(AFD_ENDPOINT) is attachment
    assert runtime.release_operation(gate_owner)
    supplemental.recording = True
    supplemental.device_changed.emit(AFD_ENDPOINT)
    recording = adapter.devices()[0]
    assert recording.recording_active is True
    assert recording.business_state == "RECORDING"
    with pytest.raises(CustomerDeviceBusyError):
        adapter.delete(AFD_ENDPOINT)

    supplemental.recording = False
    other_configuration = session_directory.acquire_configuration(
        ESA_ENDPOINT,
        "identity-conflict-test",
    )
    other_attachment = session_directory.acquire_production_attachment(
        ESA_ENDPOINT,
        "identity-conflict-test",
    )
    other_attachment.runtime.feed_datagram(
        EndpointDatagram(
            endpoint=ESA_ENDPOINT,
            data=_meta_info("SN-A"),
            wall_time_ns=2,
            monotonic_ns=2_000_000_000,
        )
    )
    conflict = adapter.devices()[0]
    assert conflict.connection_phase == "ONLINE"
    assert conflict.identity_conflict is True
    assert conflict.identity_pending is False
    assert conflict.display_identity == ""
    assert other_attachment.release()
    assert other_configuration.release()
    assert adapter.detach(AFD_ENDPOINT) is True
    assert adapter.attachment(AFD_ENDPOINT) is None
    reattached = adapter.attach(AFD_ENDPOINT)
    assert reattached.attachment_scope.epoch == 2
    assert adapter.detach(AFD_ENDPOINT) is True
    assert AFD_ENDPOINT in changed
    adapter.shutdown()


def test_edit_delete_require_customer_attachment_to_be_detached(
    customer_settings,
    session_directory,
) -> None:
    _seed(customer_settings, (AFD_ENDPOINT,), AFD_ENDPOINT)
    adapter = CustomerDeviceDirectory(customer_settings, session_directory)
    adapter.attach(AFD_ENDPOINT)

    with pytest.raises(CustomerDeviceBusyError):
        adapter.edit(AFD_ENDPOINT, ESA_ENDPOINT)
    with pytest.raises(CustomerDeviceBusyError):
        adapter.delete(AFD_ENDPOINT)
    assert adapter.endpoints() == (AFD_ENDPOINT,)

    assert adapter.detach(AFD_ENDPOINT) is True
    adapter.delete(AFD_ENDPOINT)
    adapter.shutdown()


def test_production_transport_does_not_make_detached_customer_connected(
    customer_settings,
    session_directory,
) -> None:
    _seed(customer_settings, (AFD_ENDPOINT,), AFD_ENDPOINT)
    adapter = CustomerDeviceDirectory(customer_settings, session_directory)
    production = session_directory.acquire_production_attachment(
        AFD_ENDPOINT,
        "production-test",
    )
    runtime = production.runtime

    runtime.feed_datagram(
        EndpointDatagram(
            endpoint=AFD_ENDPOINT,
            data=_meta_info("SN-PRODUCTION"),
            wall_time_ns=1,
            monotonic_ns=1_000_000_000,
        )
    )
    assert runtime.transport_active is True
    assert adapter.attachment(AFD_ENDPOINT) is None
    detached = adapter.devices()[0]
    assert detached.connection_phase == "DISCONNECTED"
    assert detached.identity_pending is False

    assert production.release()
    adapter.shutdown()


def test_blocked_settings_expose_empty_fail_closed_directory(
    customer_settings,
    session_directory,
) -> None:
    customer_settings.set("customer.devices", [_entry(AFD_ENDPOINT)])
    customer_settings.set("customer.active_endpoint", _entry(AFD_ENDPOINT))
    customer_settings._device_configuration_blocked = True
    customer_settings._device_configuration_error = "migration unresolved"

    adapter = CustomerDeviceDirectory(customer_settings, session_directory)

    assert adapter.configuration_blocked
    assert adapter.configuration_error == "migration unresolved"
    assert adapter.devices() == ()
    assert session_directory.endpoints() == ()
    with pytest.raises(CustomerDeviceConfigurationBlocked):
        adapter.add(AFD_ENDPOINT)
    adapter.shutdown()


def test_adapter_matches_customer_workspace_duck_contract(
    customer_settings,
    session_directory,
    qapplication_session,
) -> None:
    from satellite_debug_tool.ui.customer_workspace import CustomerWorkspace

    adapter = CustomerDeviceDirectory(customer_settings, session_directory)
    adapter.add(AFD_ENDPOINT)
    factory_calls = []

    class _Bundle:
        def __init__(self, endpoint) -> None:
            self.endpoint = endpoint

        def page(self, _page_id: str) -> QWidget:
            return QWidget()

    def bundle_factory(endpoint):
        factory_calls.append(endpoint)
        return _Bundle(endpoint)

    workspace = CustomerWorkspace(
        object(),
        customer_settings,
        lambda: object(),
        device_directory=adapter,
        page_bundle_factory=bundle_factory,
    )
    workspace.activate_view()

    assert workspace.active_endpoint == AFD_ENDPOINT
    assert workspace.device_list.rows[0].endpoint == AFD_ENDPOINT
    assert factory_calls == [AFD_ENDPOINT]
    adapter.add(ESA_ENDPOINT)
    assert workspace.active_endpoint == ESA_ENDPOINT
    assert tuple(row.endpoint for row in workspace.device_list.rows) == (
        AFD_ENDPOINT,
        ESA_ENDPOINT,
    )

    workspace.shutdown()
    workspace.deleteLater()
    qapplication_session.processEvents()
    adapter.shutdown()
