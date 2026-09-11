"""UI controllers must use endpoint-bound, independently-created gateways."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.session import (
    DeviceSessionCore,
    SessionOperationClass,
)
from satellite_debug_tool.core.protocol import (
    MetaInfo,
    ProfileSemanticStateEntry,
    ProfileSemanticsReport,
    StateDefEntry,
    StateReport,
    StateSample,
)
from satellite_debug_tool.ui.customer_maintenance_view import (
    CustomerMaintenanceView,
)
from satellite_debug_tool.ui.customer_rf_control_view import CustomerRfControlView
from satellite_debug_tool.ui.device_view import DeviceView
from satellite_debug_tool.ui.tracking_simulator_view import TrackingSimulatorView


class _Gateway:
    def __init__(self) -> None:
        self.acquisitions: list[tuple[object, str, frozenset[SessionOperationClass]]] = []
        self.releases: list[object] = []
        self.sent: list[tuple[bytes, SessionOperationClass]] = []
        self.contexts: list[tuple[object, object, tuple[tuple[str, object], ...]]] = []
        self.terminations: list[object] = []
        self.terminal_confirmations: list[
            tuple[object, object, tuple[tuple[str, object], ...]]
        ] = []

    def operation_available(self, _owner=None) -> bool:
        return True

    def allows_unconfirmed_mutation(self) -> bool:
        return True

    def try_acquire_operation(
        self,
        owner,
        *,
        purpose: str,
        production_freeze: bool = False,
        allowed_operations=(),
    ) -> bool:
        del production_freeze
        self.acquisitions.append(
            (owner, str(purpose), frozenset(allowed_operations))
        )
        return True

    def release_operation(self, owner) -> bool:
        self.releases.append(owner)
        return True

    def begin_terminating(self, _owner) -> bool:
        self.terminations.append(_owner)
        return True

    def update_operation_context(
        self,
        owner,
        *,
        request_id,
        target_facts,
    ) -> bool:
        self.contexts.append((owner, request_id, tuple(target_facts)))
        return True

    def confirm_terminal(
        self,
        owner,
        *,
        request_id,
        evidence_facts,
    ) -> bool:
        self.terminal_confirmations.append(
            (owner, request_id, tuple(evidence_facts))
        )
        return True

    def update_production_allowlist(self, _owner, _allowed_operations) -> bool:
        return True

    def send(
        self,
        frame: bytes,
        *,
        operation: SessionOperationClass = SessionOperationClass.MUTATING,
    ) -> bool:
        self.sent.append((bytes(frame), SessionOperationClass(operation)))
        return True


class _GatewayFactory:
    def __init__(self) -> None:
        self.created: list[_Gateway] = []

    def new_operation_gateway(self) -> _Gateway:
        gateway = _Gateway()
        self.created.append(gateway)
        return gateway


class _LiveDouble(QObject):
    connection_state_changed = Signal(bool)
    device_connection_phase_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._session = DeviceSessionCore()

    def session_core(self) -> DeviceSessionCore:
        return self._session

    def product_store(self):
        return self._session.product_store

    def profile_store(self):
        return self._session.profile_store

    def customer_service_state(self):
        return self._session.customer_service_state()

    def is_connected(self) -> bool:
        return self._session.connected


class _DeviceDouble(QObject):
    ota_status_changed = Signal(str, object, int, bool)
    ota_artifact_changed = Signal(object)

    def customer_ota_available(self, _artifact=None) -> bool:
        return False

    def abort_customer_ota(self) -> None:
        pass


class _SettingsDouble:
    def get(self, _key: str, default=None):
        return default


def test_device_view_requests_distinct_gateways_for_parameter_and_ota(
    qapplication_session,
) -> None:
    factory = _GatewayFactory()
    view = DeviceView(
        session_core=DeviceSessionCore(),
        operation_gateway_factory=factory.new_operation_gateway,
    )

    assert len(factory.created) == 2
    assert factory.created[0] is not factory.created[1]
    assert view._parameter_controller._operation_gateway is factory.created[0]
    assert view._ota_controller._operation_gateway is factory.created[1]

    view.deleteLater()
    qapplication_session.processEvents()


def test_device_view_rejects_a_factory_that_reuses_one_gateway(
    qapplication_session,
) -> None:
    gateway = _Gateway()

    try:
        DeviceView(
            session_core=DeviceSessionCore(),
            operation_gateway_factory=lambda: gateway,
        )
    except ValueError as exc:
        assert "independent gateways" in str(exc)
    else:
        raise AssertionError("DeviceView accepted one gateway for two controllers")
    qapplication_session.processEvents()


def test_customer_rf_and_maintenance_request_separate_gateways(
    qapplication_session,
) -> None:
    live = _LiveDouble()
    device = _DeviceDouble()
    factory = _GatewayFactory()
    rf = CustomerRfControlView(
        live,
        operation_gateway_factory=factory.new_operation_gateway,
    )
    maintenance = CustomerMaintenanceView(
        live,
        device,
        _SettingsDouble(),
        trusted_keys={},
        operation_gateway_factory=factory.new_operation_gateway,
    )

    assert len(factory.created) == 2
    assert factory.created[0] is not factory.created[1]
    assert rf._controller._operation_gateway is factory.created[0]
    assert maintenance._mount_controller._operation_gateway is factory.created[1]

    rf.deleteLater()
    maintenance.deleteLater()
    qapplication_session.processEvents()


def test_tracking_simulator_routes_transaction_and_frames_through_gateway(
    qapplication_session,
) -> None:
    direct_session_sends: list[bytes] = []
    session = DeviceSessionCore()
    session.attach_transport(
        object(),
        lambda frame: direct_session_sends.append(bytes(frame)) or True,
    )
    session.profile_store.apply_meta(MetaInfo(2, "0.0.140", "afd01", "SIM-1"))
    session.profile_store.apply_profile_semantics(
        "afd01",
        ProfileSemanticsReport(
            table_ver=1,
            states=[ProfileSemanticStateEntry(9, "tracking_sim_active")],
        ),
    )
    session.profile_store.apply_state_define(
        "afd01",
        1,
        [StateDefEntry(9, 0, 0, "TRACKING_SIM_ACTIVE")],
    )
    session.state_store.update("afd01", StateReport(1, [StateSample(9, 1)]))
    factory = _GatewayFactory()
    view = TrackingSimulatorView(
        session,
        operation_gateway_factory=factory.new_operation_gateway,
    )

    view.start()
    gateway = factory.created[0]
    assert len(gateway.acquisitions) == 1
    assert gateway.acquisitions[0][1] == "tracking-simulator"
    assert gateway.acquisitions[0][2] == {
        SessionOperationClass.MUTATING,
        SessionOperationClass.TERMINAL,
    }
    assert gateway.contexts[0][1] == f"tracking-start:{view._session_id}"
    assert len(gateway.sent) == 1
    assert gateway.sent[0][1] is SessionOperationClass.MUTATING
    assert direct_session_sends == []

    view.stop()
    assert len(gateway.sent) == 2
    assert gateway.sent[-1][1] is SessionOperationClass.TERMINAL
    assert gateway.terminations == [view._lease_token]
    assert gateway.releases == []
    assert gateway.terminal_confirmations == []
    view._tick()
    assert gateway.terminal_confirmations == []
    session.state_store.update("afd01", StateReport(2, [StateSample(9, 0)]))
    view._tick()
    assert len(gateway.terminal_confirmations) == 1
    assert gateway.terminal_confirmations[0][1].startswith("tracking-stop:")
    assert view._phase == "stopped"
    view._timer.stop()
    view.deleteLater()
    qapplication_session.processEvents()


def test_tracking_simulator_keeps_legacy_session_sender_without_factory(
    qapplication_session,
) -> None:
    direct_session_sends: list[bytes] = []
    session = DeviceSessionCore()
    session.attach_transport(
        object(),
        lambda frame: direct_session_sends.append(bytes(frame)) or True,
    )
    view = TrackingSimulatorView(session)

    view.start()
    assert len(direct_session_sends) == 1
    assert session.device_transaction_active

    view.stop()
    assert len(direct_session_sends) == 2
    assert session.device_transaction_active
    view._deadline = 0.0
    view._tick()
    assert not session.device_transaction_active
    view._timer.stop()
    view.deleteLater()
    qapplication_session.processEvents()
