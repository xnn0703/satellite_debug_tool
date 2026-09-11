"""Endpoint-scoped runtime, leases, identity authorization and send gating."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import hashlib
import itertools
import time
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from satellite_debug_tool.core.comm import (
    AdmissionClaimLease,
    BrokerDemandLease,
    Endpoint,
    EndpointDatagram,
    UdpEndpointBroker,
    normalize_endpoint,
)
from satellite_debug_tool.core.product import verified_device_uid, verified_identity_text
from satellite_debug_tool.core.protocol import MetaInfo, ServiceHardwareIdentity, ServiceIdentity
from satellite_debug_tool.core.protocol import RawFrame
from satellite_debug_tool.core.protocol.handshake import Handshake

from .command_sender import (
    SessionCommandSender,
    SessionOperationClass,
    SessionOperationGateway,
    SessionRecorderKind,
    SessionRecorderLease,
)
from .device_session import DeviceSessionCore
from .product_controller import ProductSubscriptionController


class RuntimeLeaseError(RuntimeError):
    """Raised when an ownership transition would violate runtime state."""


class RuntimePresencePhase(str, Enum):
    DORMANT = "dormant"
    WAITING = "waiting"
    ONLINE = "online"
    STALE = "stale"


class RuntimeOperationGateState(str, Enum):
    IDLE = "idle"
    MUTATING = "mutating"
    PRODUCTION_FROZEN = "production_frozen"
    TERMINATING = "terminating"


@dataclass(frozen=True)
class CustomerAttachmentScope:
    endpoint: Endpoint
    epoch: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint", normalize_endpoint(self.endpoint))
        if int(self.epoch) <= 0:
            raise ValueError("customer attachment epoch must be positive")
        object.__setattr__(self, "epoch", int(self.epoch))


@dataclass(frozen=True)
class IdentityAuthorizationScope:
    endpoint: Endpoint
    core_generation: int
    presence_epoch: int
    identity_facts: tuple[tuple[str, str], ...]
    source_cursors: tuple[tuple[str, int], ...]

    @property
    def stable_keys(self) -> tuple[str, ...]:
        return tuple(f"{name}:{value.casefold()}" for name, value in self.identity_facts)


@dataclass(frozen=True)
class EndpointSendCapability:
    token: int
    endpoint: Endpoint
    core_generation: int
    owner: str
    facet: str
    parent_transport_owner: str
    parent_transport_token: int
    wire_send_allowed: bool
    admission_claim_token: int | None
    attachment_scope: CustomerAttachmentScope | None = None
    allowed_operations: frozenset[SessionOperationClass] = frozenset()


@dataclass(frozen=True)
class DatagramSentEvent:
    endpoint: Endpoint
    host_time_ns: int
    monotonic_ns: int
    core_generation: int
    owner: str
    facet: str
    operation_class: SessionOperationClass
    frame: bytes
    fingerprint: str


@dataclass
class _CapabilityState:
    capability: EndpointSendCapability
    promoted_scope: IdentityAuthorizationScope | None = None
    identity_cursor_at_issue: int = 0
    promotion_cursor: int = 0


@dataclass(frozen=True)
class _OperationOwner:
    owner: object
    capability_token: int
    purpose: str
    identity_scope: IdentityAuthorizationScope
    allowed_operations: frozenset[SessionOperationClass]
    request_id: str | None = None
    target_facts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _RecorderOwner:
    owner: object
    capability_token: int
    kind: SessionRecorderKind


class RuntimeOperationGate(QObject):
    """Single CAS owner shared by mutations and Production participant freeze."""

    changed = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._state = RuntimeOperationGateState.IDLE
        self._current: _OperationOwner | None = None

    @property
    def state(self) -> RuntimeOperationGateState:
        return self._state

    @property
    def owner(self) -> object | None:
        return None if self._current is None else self._current.owner

    @property
    def capability_token(self) -> int | None:
        return None if self._current is None else self._current.capability_token

    @property
    def identity_scope(self) -> IdentityAuthorizationScope | None:
        return None if self._current is None else self._current.identity_scope

    @property
    def current_operation(self) -> _OperationOwner | None:
        return self._current

    @property
    def allowed_operations(self) -> frozenset[SessionOperationClass]:
        current = self._current
        return frozenset() if current is None else current.allowed_operations

    def try_acquire(
        self,
        owner: object,
        *,
        capability_token: int,
        purpose: str,
        identity_scope: IdentityAuthorizationScope,
        production_freeze: bool = False,
        allowed_operations: Iterable[SessionOperationClass] = (),
    ) -> bool:
        if owner is None:
            raise ValueError("operation owner is required")
        current = self._current
        if current is not None:
            return bool(
                _owners_equal(current.owner, owner)
                and current.capability_token == int(capability_token)
            )
        self._current = _OperationOwner(
            owner,
            int(capability_token),
            str(purpose).strip() or "operation",
            identity_scope,
            frozenset(SessionOperationClass(value) for value in allowed_operations),
        )
        self._set_state(
            RuntimeOperationGateState.PRODUCTION_FROZEN
            if production_freeze
            else RuntimeOperationGateState.MUTATING
        )
        return True

    def update_production_allowlist(
        self,
        owner: object,
        allowed_operations: Iterable[SessionOperationClass],
    ) -> bool:
        current = self._current
        if (
            current is None
            or self._state is not RuntimeOperationGateState.PRODUCTION_FROZEN
            or not _owners_equal(current.owner, owner)
        ):
            return False
        normalized = frozenset(
            SessionOperationClass(value) for value in allowed_operations
        )
        if normalized == current.allowed_operations:
            return True
        self._current = _OperationOwner(
            current.owner,
            current.capability_token,
            current.purpose,
            current.identity_scope,
            normalized,
            current.request_id,
            current.target_facts,
        )
        self.changed.emit(self._state)
        return True

    def update_context(
        self,
        owner: object,
        *,
        request_id: str | int | None,
        target_facts: Iterable[tuple[str, object]],
    ) -> bool:
        current = self._current
        if current is None or not _owners_equal(current.owner, owner):
            return False
        normalized_facts = _normalize_target_facts(target_facts, current.purpose)
        self._current = _OperationOwner(
            current.owner,
            current.capability_token,
            current.purpose,
            current.identity_scope,
            current.allowed_operations,
            None if request_id is None else str(request_id),
            normalized_facts,
        )
        self.changed.emit(self._state)
        return True

    def operation_allowed(self, operation: SessionOperationClass) -> bool:
        current = self._current
        return bool(
            current is not None
            and SessionOperationClass(operation) in current.allowed_operations
        )

    def release(self, owner: object) -> bool:
        current = self._current
        if current is None or not _owners_equal(current.owner, owner):
            return False
        self._current = None
        self._set_state(RuntimeOperationGateState.IDLE)
        return True

    def begin_terminating(self) -> bool:
        if self._current is None:
            return False
        self._set_state(RuntimeOperationGateState.TERMINATING)
        return True

    def _set_state(self, state: RuntimeOperationGateState) -> None:
        if self._state is state:
            return
        self._state = state
        self.changed.emit(state)


class RuntimeLease:
    """Idempotent lease whose failed release keeps ownership intact."""

    def __init__(
        self,
        runtime: "EndpointSessionRuntime",
        owner: str,
        kind: str,
        token: int,
        release: Callable[[int], bool],
    ) -> None:
        self.runtime = runtime
        self.owner = str(owner)
        self.kind = str(kind)
        self.token = int(token)
        self._release = release
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> bool:
        if self._released:
            return True
        if not self._release(self.token):
            return False
        self._released = True
        return True

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


class SubscriptionDemandLease(RuntimeLease):
    def update(self, fast_rate_hz: int) -> None:
        if self.released:
            raise RuntimeLeaseError("subscription demand has been released")
        self.runtime.update_subscription(self, fast_rate_hz)


class RuntimeRecorderLease(SessionRecorderLease):
    """Capability-bound recorder ownership counted by Runtime and Directory."""

    def __init__(
        self,
        runtime: "EndpointSessionRuntime",
        owner: object,
        kind: SessionRecorderKind,
        capability_token: int,
        token: int,
    ) -> None:
        self.runtime = runtime
        self.owner = owner
        self.kind = SessionRecorderKind(kind)
        self.capability_token = int(capability_token)
        self.token = int(token)
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> bool:
        if self._released:
            return True
        if not self.runtime._release_recorder(self.token):
            return False
        self._released = True
        return True

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


class RuntimeCommandSender(SessionCommandSender):
    """Controller-facing sender bound to one immutable capability."""

    def __init__(
        self,
        runtime: "EndpointSessionRuntime",
        capability: EndpointSendCapability,
        *,
        gate_owner: object | None = None,
    ) -> None:
        self._runtime = runtime
        self.capability = capability
        self.gate_owner = gate_owner

    def send(
        self,
        frame: bytes,
        *,
        operation: SessionOperationClass = SessionOperationClass.MUTATING,
    ) -> bool:
        return self._runtime.send(
            self.capability,
            frame,
            operation=operation,
            gate_owner=self.gate_owner,
        )


class RuntimeOperationGateway(SessionOperationGateway):
    """One controller's owner-scoped mutation gate and sender."""

    def __init__(
        self,
        runtime: "EndpointSessionRuntime",
        capability: EndpointSendCapability,
    ) -> None:
        self._runtime = runtime
        self.capability = capability
        self._gate_owner: object | None = None

    def operation_available(self, owner: object | None = None) -> bool:
        gate = self._runtime.operation_gate
        return bool(
            gate.state is RuntimeOperationGateState.IDLE
            or (
                gate.capability_token == self.capability.token
                and _owners_equal(gate.owner, owner)
            )
        )

    def allows_unconfirmed_mutation(self) -> bool:
        """Shared endpoints require protocol evidence for every mutation."""

        return False

    def try_acquire_operation(
        self,
        owner: object,
        *,
        purpose: str,
        production_freeze: bool = False,
        allowed_operations: Iterable[SessionOperationClass] = (),
    ) -> bool:
        if not self._runtime.acquire_operation(
            self.capability,
            owner,
            purpose=purpose,
            production_freeze=production_freeze,
            allowed_operations=allowed_operations,
        ):
            return False
        self._gate_owner = owner
        return True

    def release_operation(self, owner: object) -> bool:
        if not _owners_equal(self._gate_owner, owner):
            return False
        if (
            self._runtime.operation_gate.state is RuntimeOperationGateState.IDLE
            and self._runtime.operation_gate.current_operation is None
        ):
            # Runtime may have completed an identity/session termination while
            # this controller still owns its local gateway handle.
            self._gate_owner = None
            return True
        if not self._runtime.release_operation(owner):
            return False
        self._gate_owner = None
        return True

    def begin_terminating(self, owner: object) -> bool:
        gate = self._runtime.operation_gate
        if (
            not _owners_equal(self._gate_owner, owner)
            or not _owners_equal(gate.owner, owner)
            or gate.capability_token != self.capability.token
        ):
            return False
        return self._runtime.begin_terminating(
            owner,
            reason="controller requested terminal operation",
        )

    def confirm_terminal(
        self,
        owner: object,
        *,
        request_id: str | int,
        evidence_facts: Iterable[tuple[str, object]],
    ) -> bool:
        if not _owners_equal(self._gate_owner, owner):
            return False
        if not self._runtime.confirm_terminal(
            owner,
            request_id=request_id,
            evidence_facts=evidence_facts,
        ):
            return False
        self._gate_owner = None
        return True

    def update_production_allowlist(
        self,
        owner: object,
        allowed_operations: Iterable[SessionOperationClass],
    ) -> bool:
        if not _owners_equal(self._gate_owner, owner):
            return False
        return self._runtime.update_production_allowlist(
            owner,
            allowed_operations,
        )

    def acquire_recorder(
        self,
        owner: object,
        *,
        kind: SessionRecorderKind,
    ) -> RuntimeRecorderLease | None:
        return self._runtime.acquire_recorder(
            self.capability,
            owner,
            kind=kind,
        )

    def update_operation_context(
        self,
        owner: object,
        *,
        request_id: str | int | None,
        target_facts: Iterable[tuple[str, object]],
    ) -> bool:
        if not _owners_equal(self._gate_owner, owner):
            return False
        return self._runtime.update_operation_context(
            owner,
            request_id=request_id,
            target_facts=target_facts,
        )

    def send(
        self,
        frame: bytes,
        *,
        operation: SessionOperationClass = SessionOperationClass.MUTATING,
    ) -> bool:
        return self._runtime.send(
            self.capability,
            frame,
            operation=operation,
            gate_owner=self._gate_owner,
        )


class _SystemCommandSender(SessionCommandSender):
    def __init__(
        self,
        runtime: "EndpointSessionRuntime",
        owner: str,
        facet: str,
        allowed_operation: SessionOperationClass,
    ) -> None:
        self._runtime = runtime
        self._owner = owner
        self._facet = facet
        self._allowed_operation = allowed_operation

    def send(
        self,
        frame: bytes,
        *,
        operation: SessionOperationClass = SessionOperationClass.MUTATING,
    ) -> bool:
        if operation is not self._allowed_operation:
            return False
        return self._runtime._send_system(
            frame,
            owner=self._owner,
            facet=self._facet,
            operation=operation,
        )


class CustomerAttachmentLease:
    """Atomic Customer ownership bundle in the documented reverse-release order."""

    def __init__(
        self,
        *,
        runtime: "EndpointSessionRuntime",
        owner: str,
        attachment_scope: CustomerAttachmentScope,
        exact_claim: AdmissionClaimLease,
        transport: RuntimeLease,
        observer: RuntimeLease,
        handshake: RuntimeLease,
        subscription: SubscriptionDemandLease,
        capability: EndpointSendCapability,
    ) -> None:
        self.runtime = runtime
        self.owner = owner
        self.attachment_scope = attachment_scope
        self.exact_claim = exact_claim
        self.transport = transport
        self.observer = observer
        self.handshake = handshake
        self.subscription = subscription
        self.capability = capability
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    @property
    def core(self) -> DeviceSessionCore:
        return self.runtime.core

    @property
    def connected(self) -> bool:
        return self.runtime.transport_active

    @property
    def presence_phase(self) -> RuntimePresencePhase:
        return self.runtime.presence_phase

    @property
    def connected_changed(self):
        return self.runtime.transport_changed

    @property
    def presence_changed(self):
        return self.runtime.presence_changed

    @property
    def command_sender(self) -> RuntimeCommandSender:
        return self.runtime.command_sender(self.capability)

    def new_operation_gateway(self) -> RuntimeOperationGateway:
        return self.runtime.operation_gateway(self.capability)

    def release(self) -> bool:
        if self._released:
            return True
        if (
            self.runtime.capability_has_active_operation(self.capability)
            or self.runtime.capability_has_active_recorder(self.capability)
        ):
            return False
        if not self.runtime.release_capability(self.capability):
            return False
        self.subscription.release()
        self.handshake.release()
        self.observer.release()
        if not self.transport.release():
            raise RuntimeLeaseError("cannot release active customer transport")
        self.exact_claim.release()
        self._released = True
        return True

class ProductionAttachmentLease:
    """Atomic Production facet ownership for one admitted endpoint."""

    def __init__(
        self,
        *,
        runtime: "EndpointSessionRuntime",
        owner: str,
        observer: RuntimeLease,
        transport: RuntimeLease,
        subscription: SubscriptionDemandLease,
        capability: EndpointSendCapability,
    ) -> None:
        self.runtime = runtime
        self.owner = owner
        self.observer = observer
        self.transport = transport
        self.subscription = subscription
        self.capability = capability
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    @property
    def core(self) -> DeviceSessionCore:
        return self.runtime.core

    @property
    def connected(self) -> bool:
        return self.runtime.transport_active

    @property
    def presence_phase(self) -> RuntimePresencePhase:
        return self.runtime.presence_phase

    @property
    def connected_changed(self):
        return self.runtime.transport_changed

    @property
    def presence_changed(self):
        return self.runtime.presence_changed

    @property
    def command_sender(self) -> RuntimeCommandSender:
        return self.runtime.command_sender(self.capability)

    def new_operation_gateway(self) -> RuntimeOperationGateway:
        return self.runtime.operation_gateway(self.capability)

    def release(self) -> bool:
        if self._released:
            return True
        if (
            self.runtime.capability_has_active_operation(self.capability)
            or self.runtime.capability_has_active_recorder(self.capability)
        ):
            return False
        if not self.runtime.release_capability(self.capability):
            return False
        self.subscription.release()
        if not self.transport.release():
            raise RuntimeLeaseError("cannot release active Production transport")
        self.observer.release()
        self._released = True
        return True


class EndpointSessionRuntime(QObject):
    """One authoritative Core and lifecycle for one normalized endpoint."""

    datagram_received = Signal(object)
    records_received = Signal(object, object)
    presence_changed = Signal(object)
    identity_authorization_changed = Signal(object)
    transport_changed = Signal(bool)
    datagram_sent = Signal(object)
    lease_changed = Signal()
    identity_conflict = Signal(str)

    _token_counter = itertools.count(1)

    def __init__(
        self,
        endpoint: Endpoint,
        *,
        broker: UdpEndpointBroker,
        core: DeviceSessionCore | None = None,
        presence_timeout_s: float = 3.0,
        terminal_timeout_s: float = 3.0,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.endpoint = normalize_endpoint(endpoint)
        self.broker = broker
        self.core = core or DeviceSessionCore(endpoint=self.endpoint, parent=self)
        self.core.bind_endpoint(self.endpoint)
        self.operation_gate = RuntimeOperationGate(self)
        self._terminal_timeout_ms = max(1, int(float(terminal_timeout_s) * 1000))
        self._terminal_timeout = QTimer(self)
        self._terminal_timeout.setSingleShot(True)
        self._terminal_timeout.timeout.connect(self._on_terminal_timeout)
        self._termination_reason = ""
        self._terminal_sent = False
        self.operation_gate.changed.connect(self._on_gate_changed)
        self.core.bind_managed_transaction_view(
            active=lambda: self.operation_gate.state
            is not RuntimeOperationGateState.IDLE,
            available=self._managed_transaction_available,
        )
        self.operation_gate.changed.connect(
            lambda state: self.core.device_transaction_changed.emit(
                state is not RuntimeOperationGateState.IDLE
            )
        )

        self._presence_timeout_s = float(presence_timeout_s)
        if self._presence_timeout_s <= 0:
            raise ValueError("presence timeout must be positive")
        self._presence_phase = RuntimePresencePhase.DORMANT
        self._presence_epoch = 0
        self._last_valid_record_at: float | None = None
        self._identity_sources: dict[str, str] = {}
        self._identity_source_cursors: dict[str, int] = {}
        self._identity_cursor_counter = 0
        self._identity_authorization_scope: IdentityAuthorizationScope | None = None
        self._identity_conflicted = False
        self._directory_identity_conflicted = False
        self._recent_datagram_signatures: deque[
            tuple[int, int, int, bytes]
        ] = deque()
        self._recent_datagram_signature_set: set[
            tuple[int, int, int, bytes]
        ] = set()

        self._configuration: dict[int, str] = {}
        self._transport: dict[int, tuple[str, BrokerDemandLease]] = {}
        self._non_wire_transport_tokens: set[int] = set()
        self._observers: dict[int, tuple[str, str]] = {}
        self._handshake_demands: dict[int, tuple[str, str]] = {}
        self._subscription_demands: dict[int, tuple[str, str, int]] = {}
        self._capabilities: dict[int, _CapabilityState] = {}
        self._recorders: dict[int, _RecorderOwner] = {}
        self._current_customer_attachment_scope: CustomerAttachmentScope | None = None
        self._last_customer_attachment_epoch = 0

        self._handshake: Handshake | None = None
        self._handshake_timer = QTimer(self)
        self._handshake_timer.setInterval(100)
        self._handshake_timer.timeout.connect(self._tick_handshake)
        self._subscription_controller: ProductSubscriptionController | None = None
        self._presence_timer = QTimer(self)
        self._presence_timer.setInterval(250)
        self._presence_timer.timeout.connect(self.refresh_presence)

    @property
    def presence_phase(self) -> RuntimePresencePhase:
        return self._presence_phase

    @property
    def presence_epoch(self) -> int:
        return self._presence_epoch

    @property
    def last_valid_record_at(self) -> float | None:
        return self._last_valid_record_at

    @property
    def identity_authorization_scope(self) -> IdentityAuthorizationScope | None:
        return self._identity_authorization_scope

    @property
    def identity_conflicted(self) -> bool:
        return self._identity_conflicted or self._directory_identity_conflicted

    @property
    def subscription_controller(self) -> ProductSubscriptionController | None:
        """Return the Runtime-owned subscription state machine for observation."""

        return self._subscription_controller

    @property
    def current_identity_keys(self) -> tuple[str, ...]:
        """Return current-epoch stable keys even while Directory marks a collision."""

        debug_sn = self._identity_sources.get("debug_sn", "")
        product_sn = self._identity_sources.get("product_sn", "")
        product_uid = self._identity_sources.get("product_uid", "")
        if debug_sn and product_sn and debug_sn.casefold() != product_sn.casefold():
            return ()
        keys: list[str] = []
        serial = product_sn or debug_sn
        if serial:
            keys.append(f"sn:{serial.casefold()}")
        if product_uid:
            keys.append(f"uid:{product_uid.casefold()}")
        return tuple(keys)

    @property
    def transport_active(self) -> bool:
        return bool(self._transport)

    @property
    def wire_transport_active(self) -> bool:
        return any(
            token not in self._non_wire_transport_tokens
            for token in self._transport
        )

    @property
    def configuration_owners(self) -> frozenset[str]:
        return frozenset(self._configuration.values())

    @property
    def transport_owners(self) -> frozenset[str]:
        return frozenset(owner for owner, _demand in self._transport.values())

    @property
    def transport_demand_count(self) -> int:
        return len(self._transport)

    @property
    def recorder_count(self) -> int:
        return len(self._recorders)

    @property
    def operation_count(self) -> int:
        return int(self.operation_gate.state is not RuntimeOperationGateState.IDLE)

    @property
    def has_lifecycle_owners(self) -> bool:
        return bool(
            self._configuration
            or self._transport
            or self._observers
            or self._handshake_demands
            or self._subscription_demands
            or self._capabilities
            or self._recorders
            or self.operation_gate.state is not RuntimeOperationGateState.IDLE
        )

    def acquire_configuration(self, owner: str) -> RuntimeLease:
        return self._acquire_simple(self._configuration, owner, "configuration")

    def acquire_customer_attachment(
        self,
        owner: str,
        attachment_scope: CustomerAttachmentScope,
        *,
        subscription_hz: int = 10,
    ) -> CustomerAttachmentLease:
        """Atomically acquire the Customer bundle through the owning Directory."""

        factory = getattr(self, "_customer_attachment_factory", None)
        if factory is None:
            raise RuntimeLeaseError("runtime is not owned by an EndpointSessionDirectory")
        return factory(
            self.endpoint,
            owner,
            attachment_scope,
            subscription_hz=subscription_hz,
        )

    def acquire_observer(self, owner: str, *, facet: str) -> RuntimeLease:
        self._assert_owner_thread()
        token = next(self._token_counter)
        normalized_owner = _required_owner(owner)
        self._observers[token] = (normalized_owner, _required_facet(facet))
        self.lease_changed.emit()
        return RuntimeLease(self, normalized_owner, "observer", token, self._release_observer)

    def acquire_transport(self, owner: str) -> RuntimeLease:
        self._assert_owner_thread()
        normalized_owner = _required_owner(owner)
        token = next(self._token_counter)
        demand = self.broker.acquire_demand(
            f"runtime:{self.endpoint[0]}:{self.endpoint[1]}:{normalized_owner}:{token}"
        )
        return self._install_transport(
            normalized_owner,
            token,
            demand,
            wire=True,
        )

    def acquire_injected_transport(self, owner: str) -> RuntimeLease:
        """Observe explicitly injected host-test datagrams with all wire sends disabled."""

        self._assert_owner_thread()
        normalized_owner = _required_owner(owner)
        token = next(self._token_counter)
        demand = BrokerDemandLease(normalized_owner, lambda: None)
        return self._install_transport(
            normalized_owner,
            token,
            demand,
            wire=False,
        )

    def _install_transport(
        self,
        normalized_owner: str,
        token: int,
        demand: BrokerDemandLease,
        *,
        wire: bool,
    ) -> RuntimeLease:
        first = not self._transport
        had_wire = self.wire_transport_active
        self._transport[token] = (normalized_owner, demand)
        if not wire:
            self._non_wire_transport_tokens.add(token)
        try:
            if first:
                self._clear_datagram_claims()
                self.core.begin_connection(
                    endpoint=self.endpoint,
                    transport=self.broker,
                    sender=lambda _frame: False,
                    handshake_enabled=False,
                )
                self._begin_waiting()
                self._presence_timer.start()
                self._start_handshake_if_needed()
                self._start_subscription_if_needed()
                self.transport_changed.emit(True)
            elif wire and not had_wire:
                self._start_handshake_if_needed()
                self._start_subscription_if_needed()
        except Exception:
            self._transport.pop(token, None)
            self._non_wire_transport_tokens.discard(token)
            demand.release()
            raise
        self.lease_changed.emit()
        return RuntimeLease(self, normalized_owner, "transport", token, self._release_transport)

    def acquire_handshake(self, owner: str, *, facet: str) -> RuntimeLease:
        self._assert_owner_thread()
        token = next(self._token_counter)
        normalized_owner = _required_owner(owner)
        self._handshake_demands[token] = (normalized_owner, _required_facet(facet))
        self._start_handshake_if_needed()
        self.lease_changed.emit()
        return RuntimeLease(self, normalized_owner, "handshake", token, self._release_handshake)

    def acquire_subscription(
        self,
        owner: str,
        fast_rate_hz: int,
        *,
        facet: str,
    ) -> SubscriptionDemandLease:
        self._assert_owner_thread()
        rate = _normalize_subscription_rate(fast_rate_hz)
        token = next(self._token_counter)
        normalized_owner = _required_owner(owner)
        self._subscription_demands[token] = (
            normalized_owner,
            _required_facet(facet),
            rate,
        )
        self._reconcile_subscription()
        self.lease_changed.emit()
        return SubscriptionDemandLease(
            self,
            normalized_owner,
            "subscription",
            token,
            self._release_subscription,
        )

    def update_subscription(
        self,
        lease: SubscriptionDemandLease,
        fast_rate_hz: int,
    ) -> None:
        self._assert_owner_thread()
        current = self._subscription_demands.get(lease.token)
        if current is None:
            raise RuntimeLeaseError("subscription demand is not active")
        self._subscription_demands[lease.token] = (
            current[0],
            current[1],
            _normalize_subscription_rate(fast_rate_hz),
        )
        self._reconcile_subscription()

    def issue_capability(
        self,
        *,
        owner: str,
        facet: str,
        parent_transport_owner: str | None = None,
        parent_transport_lease: RuntimeLease | None = None,
        admission_claim: AdmissionClaimLease | None = None,
        attachment_scope: CustomerAttachmentScope | None = None,
    ) -> EndpointSendCapability:
        self._assert_owner_thread()
        normalized_owner = _required_owner(owner)
        parent_owner = _required_owner(parent_transport_owner or normalized_owner)
        if parent_transport_lease is not None:
            if (
                parent_transport_lease.runtime is not self
                or parent_transport_lease.released
                or parent_transport_lease.kind != "transport"
                or parent_transport_lease.owner != parent_owner
                or parent_transport_lease.token not in self._transport
            ):
                raise RuntimeLeaseError("capability parent TransportDemand is not active")
            parent_transport_token = parent_transport_lease.token
        else:
            candidates = tuple(
                token
                for token, (current_owner, _demand) in self._transport.items()
                if current_owner == parent_owner
            )
            if len(candidates) != 1:
                raise RuntimeLeaseError(
                    "capability requires one exact parent TransportDemand"
                )
            parent_transport_token = candidates[0]
        if attachment_scope is not None and attachment_scope.endpoint != self.endpoint:
            raise RuntimeLeaseError("customer attachment scope endpoint does not match runtime")
        facet_value = _required_facet(facet)
        if attachment_scope is not None:
            current_scope = self._current_customer_attachment_scope
            if current_scope is not None and current_scope != attachment_scope:
                raise RuntimeLeaseError("another Customer attachment scope is active")
            if (
                current_scope is None
                and attachment_scope.epoch <= self._last_customer_attachment_epoch
            ):
                raise RuntimeLeaseError("Customer attachment epoch must increase")
            self._current_customer_attachment_scope = attachment_scope
            self._last_customer_attachment_epoch = max(
                self._last_customer_attachment_epoch,
                attachment_scope.epoch,
            )
        admission_claim_token: int | None = None
        if admission_claim is not None:
            claim_broker = getattr(admission_claim, "_broker", None)
            runtime_broker = getattr(self.broker, "broker", self.broker)
            if (
                admission_claim.released
                or claim_broker is not runtime_broker
                or not runtime_broker.claim_allows(
                    admission_claim.token,
                    self.endpoint,
                    facet=facet_value,
                )
            ):
                raise RuntimeLeaseError(
                    "capability admission claim does not admit this endpoint and facet"
                )
            admission_claim_token = admission_claim.token
        token = next(self._token_counter)
        capability = EndpointSendCapability(
            token=token,
            endpoint=self.endpoint,
            core_generation=int(self.core.generation),
            owner=normalized_owner,
            facet=facet_value,
            parent_transport_owner=parent_owner,
            parent_transport_token=parent_transport_token,
            wire_send_allowed=(
                parent_transport_token not in self._non_wire_transport_tokens
            ),
            admission_claim_token=admission_claim_token,
            attachment_scope=attachment_scope,
            allowed_operations=frozenset(SessionOperationClass),
        )
        state = _CapabilityState(
            capability,
            identity_cursor_at_issue=self._identity_cursor_counter,
        )
        self._capabilities[token] = state
        self.lease_changed.emit()
        return capability

    def release_capability(self, capability: EndpointSendCapability) -> bool:
        self._assert_owner_thread()
        state = self._capabilities.get(capability.token)
        if state is None or state.capability != capability:
            return False
        if (
            self.capability_has_active_operation(capability)
            or self.capability_has_active_recorder(capability)
        ):
            return False
        self._capabilities.pop(capability.token, None)
        attachment_scope = capability.attachment_scope
        if attachment_scope is not None and not any(
            value.capability.attachment_scope == attachment_scope
            for value in self._capabilities.values()
        ):
            self._current_customer_attachment_scope = None
        self.lease_changed.emit()
        return True

    def command_sender(
        self,
        capability: EndpointSendCapability,
        *,
        gate_owner: object | None = None,
    ) -> RuntimeCommandSender:
        return RuntimeCommandSender(self, capability, gate_owner=gate_owner)

    def operation_gateway(
        self,
        capability: EndpointSendCapability,
    ) -> RuntimeOperationGateway:
        if self._valid_capability_state(capability) is None:
            raise RuntimeLeaseError("operation gateway requires an active capability")
        return RuntimeOperationGateway(self, capability)

    def acquire_recorder(
        self,
        capability: EndpointSendCapability,
        owner: object,
        *,
        kind: SessionRecorderKind,
    ) -> RuntimeRecorderLease | None:
        """Acquire authoritative endpoint recorder ownership or fail closed."""

        self._assert_owner_thread()
        if owner is None:
            return None
        state = self._valid_capability_state(capability)
        if state is None:
            return None
        normalized_kind = SessionRecorderKind(kind)
        if (
            normalized_kind is SessionRecorderKind.CUSTOMER_FULL_CAPTURE
            and capability.facet != "customer"
        ):
            return None
        if (
            normalized_kind is SessionRecorderKind.PRODUCTION_EVIDENCE
            and capability.facet != "production"
        ):
            return None
        gate = self.operation_gate
        if normalized_kind is SessionRecorderKind.CUSTOMER_FULL_CAPTURE:
            scope = self._identity_authorization_scope
            if (
                not capability.wire_send_allowed
                or scope is None
                or state.promoted_scope != scope
                or gate.state is not RuntimeOperationGateState.IDLE
            ):
                return None
        elif normalized_kind is SessionRecorderKind.PRODUCTION_EVIDENCE:
            scope = self._identity_authorization_scope
            if (
                scope is None
                or state.promoted_scope != scope
                or (
                    gate.state is not RuntimeOperationGateState.IDLE
                    and not (
                        gate.state is RuntimeOperationGateState.PRODUCTION_FROZEN
                        and gate.capability_token == capability.token
                    )
                )
            ):
                return None
        token = next(self._token_counter)
        self._recorders[token] = _RecorderOwner(
            owner,
            capability.token,
            normalized_kind,
        )
        self.lease_changed.emit()
        return RuntimeRecorderLease(
            self,
            owner,
            normalized_kind,
            capability.token,
            token,
        )

    def acquire_operation(
        self,
        capability: EndpointSendCapability,
        owner: object,
        *,
        purpose: str,
        production_freeze: bool = False,
        allowed_operations: Iterable[SessionOperationClass] = (),
    ) -> bool:
        self._assert_owner_thread()
        state = self._valid_capability_state(capability)
        scope = self._identity_authorization_scope
        if (
            state is None
            or (not capability.wire_send_allowed and not production_freeze)
            or scope is None
            or state.promoted_scope != scope
            or (production_freeze and capability.facet != "production")
        ):
            return False
        if production_freeze and any(
            recorder.kind is SessionRecorderKind.CUSTOMER_FULL_CAPTURE
            or (
                recorder.kind is SessionRecorderKind.PRODUCTION_EVIDENCE
                and recorder.capability_token != capability.token
            )
            for recorder in self._recorders.values()
        ):
            return False
        if not production_freeze and any(
            recorder.kind is SessionRecorderKind.CUSTOMER_FULL_CAPTURE
            and not (
                recorder.capability_token == capability.token
                and _owners_equal(recorder.owner, owner)
            )
            for recorder in self._recorders.values()
        ):
            return False
        return self.operation_gate.try_acquire(
            owner,
            capability_token=capability.token,
            purpose=purpose,
            identity_scope=scope,
            production_freeze=production_freeze,
            allowed_operations=allowed_operations,
        )

    def update_production_allowlist(
        self,
        owner: object,
        allowed_operations: Iterable[SessionOperationClass],
    ) -> bool:
        self._assert_owner_thread()
        return self.operation_gate.update_production_allowlist(
            owner,
            allowed_operations,
        )

    def update_operation_context(
        self,
        owner: object,
        *,
        request_id: str | int | None,
        target_facts: Iterable[tuple[str, object]],
    ) -> bool:
        self._assert_owner_thread()
        if (
            self.operation_gate.state is RuntimeOperationGateState.TERMINATING
            and self._terminal_sent
        ):
            return False
        return self.operation_gate.update_context(
            owner,
            request_id=request_id,
            target_facts=target_facts,
        )

    def begin_terminating(self, owner: object, *, reason: str) -> bool:
        self._assert_owner_thread()
        gate = self.operation_gate
        if not _owners_equal(gate.owner, owner):
            return False
        if gate.state not in {
            RuntimeOperationGateState.MUTATING,
            RuntimeOperationGateState.PRODUCTION_FROZEN,
            RuntimeOperationGateState.TERMINATING,
        }:
            return False
        if gate.state is not RuntimeOperationGateState.TERMINATING:
            gate.begin_terminating()
            self._terminal_sent = False
        self._termination_reason = str(reason).strip() or "terminal operation required"
        self._terminal_timeout.start(self._terminal_timeout_ms)
        return True

    def confirm_terminal(
        self,
        owner: object,
        *,
        request_id: str | int,
        evidence_facts: Iterable[tuple[str, object]],
    ) -> bool:
        """Release TERMINATING only from matching post-send device evidence."""

        self._assert_owner_thread()
        gate = self.operation_gate
        current = gate.current_operation
        raw_evidence = tuple(evidence_facts)
        if (
            current is None
            or gate.state is not RuntimeOperationGateState.TERMINATING
            or not _owners_equal(current.owner, owner)
            or not self._terminal_sent
            or current.request_id is None
            or current.request_id != str(request_id)
            or not raw_evidence
        ):
            return False
        current_identity = self._identity_authorization_scope
        if (
            current_identity is None
            or current_identity.identity_facts != current.identity_scope.identity_facts
        ):
            return False
        try:
            _normalize_target_facts(raw_evidence, "terminal-evidence")
        except (TypeError, ValueError):
            return False
        if not gate.release(owner):
            return False
        self._terminal_timeout.stop()
        self._termination_reason = ""
        self._terminal_sent = False
        return True

    def release_operation(self, owner: object) -> bool:
        self._assert_owner_thread()
        released = self.operation_gate.release(owner)
        if released:
            self._terminal_timeout.stop()
            self._termination_reason = ""
            self._terminal_sent = False
        return released

    @Slot()
    def _on_terminal_timeout(self) -> None:
        current = self.operation_gate.current_operation
        if current is None:
            return
        self.operation_gate.release(current.owner)
        self._termination_reason = ""
        self._terminal_sent = False

    def capability_has_active_operation(
        self,
        capability: EndpointSendCapability,
    ) -> bool:
        return self.operation_gate.capability_token == capability.token

    def capability_has_active_recorder(
        self,
        capability: EndpointSendCapability,
    ) -> bool:
        return any(
            recorder.capability_token == capability.token
            for recorder in self._recorders.values()
        )

    def send(
        self,
        capability: EndpointSendCapability,
        frame: bytes,
        *,
        operation: SessionOperationClass = SessionOperationClass.MUTATING,
        gate_owner: object | None = None,
    ) -> bool:
        self._assert_owner_thread()
        state = self._valid_capability_state(capability)
        if (
            state is None
            or not capability.wire_send_allowed
            or not self.wire_transport_active
        ):
            return False
        operation = SessionOperationClass(operation)
        if operation not in capability.allowed_operations:
            return False
        if operation is SessionOperationClass.TERMINAL:
            if not self._terminal_send_allowed(capability, gate_owner):
                return False
        elif self.operation_gate.state is RuntimeOperationGateState.PRODUCTION_FROZEN:
            if not self._production_capability_send_allowed(
                capability,
                gate_owner,
                operation,
            ):
                return False
        elif operation in {
            SessionOperationClass.IDENTITY,
            SessionOperationClass.HANDSHAKE,
            SessionOperationClass.PRODUCT_SUBSCRIPTION,
            SessionOperationClass.READ_ONLY_QUERY,
        }:
            pass
        elif operation is SessionOperationClass.MUTATING:
            if (
                state.promoted_scope is None
                or state.promoted_scope != self._identity_authorization_scope
                or self.operation_gate.state is not RuntimeOperationGateState.MUTATING
                or not _owners_equal(self.operation_gate.owner, gate_owner)
                or self.operation_gate.capability_token != capability.token
            ):
                return False
        sent = self._send_wire(
            frame,
            owner=capability.owner,
            facet=capability.facet,
            operation=operation,
        )
        if operation is SessionOperationClass.TERMINAL and sent:
            self._terminal_sent = True
        return sent

    def feed_datagram(self, datagram: EndpointDatagram) -> tuple[object, ...]:
        self._assert_owner_thread()
        if datagram.endpoint != self.endpoint:
            raise ValueError("datagram endpoint does not match runtime")
        if not self.transport_active:
            return ()
        if not self._claim_datagram(datagram):
            return ()
        self.datagram_received.emit(datagram)
        records = self.core.feed_bytes(
            datagram.data,
            received_monotonic=datagram.monotonic_ns / 1_000_000_000.0,
        )
        return self._after_records(datagram, records)

    def feed_decoded_datagram(
        self,
        datagram: EndpointDatagram,
        records: tuple[object, ...],
    ) -> tuple[object, ...]:
        """Apply candidate records once after Production admission."""

        self._assert_owner_thread()
        if datagram.endpoint != self.endpoint:
            raise ValueError("datagram endpoint does not match runtime")
        if not self.transport_active:
            raise RuntimeLeaseError("decoded records require an active transport epoch")
        if not self._claim_datagram(datagram):
            return ()
        self.datagram_received.emit(datagram)
        self.core.apply_records(
            records,
            received_monotonic=datagram.monotonic_ns / 1_000_000_000.0,
        )
        return self._after_records(datagram, tuple(records))

    @Slot()
    def refresh_presence(self, now_monotonic: float | None = None) -> None:
        self._assert_owner_thread()
        if self._presence_phase is not RuntimePresencePhase.ONLINE:
            return
        last = self._last_valid_record_at
        if last is None:
            return
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        if now - last <= self._presence_timeout_s:
            return
        self._set_presence(RuntimePresencePhase.STALE)
        self._revoke_identity_authorization()
        self._begin_active_operation_termination("presence became stale")

    def set_directory_identity_conflict(self, conflicted: bool) -> None:
        self._assert_owner_thread()
        value = bool(conflicted)
        if self._directory_identity_conflicted == value:
            return
        self._directory_identity_conflicted = value
        if value:
            self._revoke_identity_authorization()
            self._begin_active_operation_termination(
                "Directory detected an identity conflict"
            )
        else:
            self._evaluate_identity_authorization()

    def _after_records(
        self,
        datagram: EndpointDatagram,
        records: tuple[object, ...],
    ) -> tuple[object, ...]:
        if not records:
            return records
        typed_records = tuple(record for record in records if not isinstance(record, RawFrame))
        if not typed_records:
            # Unknown but CRC-valid envelopes remain observable for diagnostics;
            # they are not registered-domain evidence and cannot refresh presence.
            self.records_received.emit(datagram, records)
            return records
        received = datagram.monotonic_ns / 1_000_000_000.0
        if self._presence_phase in {
            RuntimePresencePhase.WAITING,
            RuntimePresencePhase.STALE,
        }:
            self._begin_presence_epoch()
        self._last_valid_record_at = received
        self._set_presence(RuntimePresencePhase.ONLINE)
        handshake = self._handshake
        if handshake is not None:
            for record in typed_records:
                handshake.feed(record)
        for record in typed_records:
            self._observe_identity_record(record)
        self._evaluate_identity_authorization()
        self.records_received.emit(datagram, records)
        return records

    def _claim_datagram(self, datagram: EndpointDatagram) -> bool:
        signature = (
            int(datagram.wall_time_ns),
            int(datagram.monotonic_ns),
            len(datagram.data),
            hashlib.sha256(datagram.data).digest(),
        )
        if signature in self._recent_datagram_signature_set:
            return False
        self._recent_datagram_signatures.append(signature)
        self._recent_datagram_signature_set.add(signature)
        if len(self._recent_datagram_signatures) > 1024:
            expired = self._recent_datagram_signatures.popleft()
            self._recent_datagram_signature_set.discard(expired)
        return True

    def _clear_datagram_claims(self) -> None:
        self._recent_datagram_signatures.clear()
        self._recent_datagram_signature_set.clear()

    def _observe_identity_record(self, record: object) -> None:
        values: tuple[tuple[str, str], ...] = ()
        if isinstance(record, MetaInfo):
            values = (("debug_sn", verified_identity_text(record.device_sn)),)
        elif isinstance(record, ServiceIdentity):
            serial = (
                verified_identity_text(record.serial_number)
                if record.valid_mask & (1 << 1)
                else ""
            )
            values = (("product_sn", serial),)
        elif isinstance(record, ServiceHardwareIdentity):
            uid = (
                verified_device_uid(record.device_uid)
                if record.valid_mask & (1 << 0)
                else ""
            )
            values = (("product_uid", uid),)
        for source, value in values:
            self._identity_cursor_counter += 1
            self._identity_source_cursors[source] = self._identity_cursor_counter
            previous = self._identity_sources.get(source, "")
            if previous and previous.casefold() != value.casefold():
                self._identity_conflicted = True
                self.identity_conflict.emit(
                    f"endpoint identity source changed at {self.endpoint[0]}:{self.endpoint[1]}"
                )
            self._identity_sources[source] = value

    def _evaluate_identity_authorization(self) -> None:
        if self._presence_phase is not RuntimePresencePhase.ONLINE:
            return
        debug_sn = self._identity_sources.get("debug_sn", "")
        product_sn = self._identity_sources.get("product_sn", "")
        product_uid = self._identity_sources.get("product_uid", "")
        if debug_sn and product_sn and debug_sn.casefold() != product_sn.casefold():
            if not self._identity_conflicted:
                self.identity_conflict.emit(
                    f"Debug/Product serial mismatch at {self.endpoint[0]}:{self.endpoint[1]}"
                )
            self._identity_conflicted = True
        if self.identity_conflicted:
            self._revoke_identity_authorization()
            return
        facts: list[tuple[str, str]] = []
        serial = product_sn or debug_sn
        if serial:
            facts.append(("sn", serial))
        if product_uid:
            facts.append(("uid", product_uid))
        if not facts:
            self._revoke_identity_authorization()
            return
        existing = self._identity_authorization_scope
        normalized_facts = tuple(facts)
        current_cursor = max(self._identity_source_cursors.values(), default=0)
        if (
            existing is not None
            and existing.presence_epoch == self._presence_epoch
            and existing.identity_facts == normalized_facts
        ):
            for state in self._capabilities.values():
                if current_cursor > state.identity_cursor_at_issue:
                    state.promoted_scope = existing
                    state.promotion_cursor = current_cursor
            return
        scope = IdentityAuthorizationScope(
            endpoint=self.endpoint,
            core_generation=int(self.core.generation),
            presence_epoch=self._presence_epoch,
            identity_facts=normalized_facts,
            source_cursors=tuple(sorted(self._identity_source_cursors.items())),
        )
        self._identity_authorization_scope = scope
        for state in self._capabilities.values():
            if current_cursor > state.identity_cursor_at_issue:
                state.promoted_scope = scope
                state.promotion_cursor = current_cursor
        self.identity_authorization_changed.emit(scope)

    def _revoke_identity_authorization(self) -> None:
        if self._identity_authorization_scope is None and all(
            state.promoted_scope is None for state in self._capabilities.values()
        ):
            return
        self._identity_authorization_scope = None
        for state in self._capabilities.values():
            state.promoted_scope = None
        self.identity_authorization_changed.emit(None)
        self._begin_active_operation_termination(
            "identity authorization was revoked"
        )

    def _begin_active_operation_termination(self, reason: str) -> None:
        current = self.operation_gate.current_operation
        if current is None:
            return
        if self.operation_gate.state in {
            RuntimeOperationGateState.MUTATING,
            RuntimeOperationGateState.PRODUCTION_FROZEN,
        }:
            self.operation_gate.begin_terminating()
            self._terminal_sent = False
        if self.operation_gate.state is RuntimeOperationGateState.TERMINATING:
            if not self._termination_reason:
                self._termination_reason = str(reason).strip()
            if not self._terminal_timeout.isActive():
                self._terminal_timeout.start(self._terminal_timeout_ms)

    def _begin_presence_epoch(self) -> None:
        self._presence_epoch += 1
        self._identity_sources.clear()
        self._identity_source_cursors.clear()
        self._identity_conflicted = False
        self._revoke_identity_authorization()

    def _begin_waiting(self) -> None:
        self._last_valid_record_at = None
        self._identity_sources.clear()
        self._identity_source_cursors.clear()
        self._identity_conflicted = False
        self._revoke_identity_authorization()
        self._set_presence(RuntimePresencePhase.WAITING)

    def _set_presence(self, phase: RuntimePresencePhase) -> None:
        if self._presence_phase is phase:
            return
        self._presence_phase = phase
        self.presence_changed.emit(phase)

    def _valid_capability_state(
        self,
        capability: EndpointSendCapability,
    ) -> _CapabilityState | None:
        state = self._capabilities.get(capability.token)
        if state is None or state.capability != capability:
            return None
        if capability.endpoint != self.endpoint:
            return None
        if capability.core_generation != int(self.core.generation):
            return None
        if capability.parent_transport_owner not in self.transport_owners:
            return None
        transport = self._transport.get(capability.parent_transport_token)
        if transport is None or transport[0] != capability.parent_transport_owner:
            return None
        if (
            capability.attachment_scope is not None
            and capability.attachment_scope != self._current_customer_attachment_scope
        ):
            return None
        if capability.admission_claim_token is not None:
            broker = getattr(self.broker, "broker", self.broker)
            if not broker.claim_allows(
                capability.admission_claim_token,
                self.endpoint,
                facet=capability.facet,
            ):
                return None
        return state

    def _terminal_send_allowed(
        self,
        capability: EndpointSendCapability,
        gate_owner: object | None,
    ) -> bool:
        if (
            self.operation_gate.state is not RuntimeOperationGateState.TERMINATING
            or self.operation_gate.capability_token != capability.token
            or not _owners_equal(self.operation_gate.owner, gate_owner)
        ):
            return False
        current = self._identity_authorization_scope
        original = self.operation_gate.identity_scope
        return bool(
            current is not None
            and original is not None
            and current.identity_facts == original.identity_facts
        )

    def _production_capability_send_allowed(
        self,
        capability: EndpointSendCapability,
        gate_owner: object | None,
        operation: SessionOperationClass,
    ) -> bool:
        return bool(
            capability.facet == "production"
            and self.operation_gate.capability_token == capability.token
            and _owners_equal(self.operation_gate.owner, gate_owner)
            and self.operation_gate.operation_allowed(operation)
        )

    def _send_system(
        self,
        frame: bytes,
        *,
        owner: str,
        facet: str,
        operation: SessionOperationClass,
    ) -> bool:
        self._assert_owner_thread()
        operation = SessionOperationClass(operation)
        if operation not in {
            SessionOperationClass.IDENTITY,
            SessionOperationClass.HANDSHAKE,
            SessionOperationClass.PRODUCT_SUBSCRIPTION,
            SessionOperationClass.READ_ONLY_QUERY,
        }:
            return False
        if not self.wire_transport_active:
            return False
        if self.operation_gate.state is RuntimeOperationGateState.PRODUCTION_FROZEN:
            gate_token = self.operation_gate.capability_token
            gate_capability = self._capabilities.get(gate_token or -1)
            if (
                gate_capability is None
                or gate_capability.capability.facet != "production"
                or not self.operation_gate.operation_allowed(operation)
            ):
                return False
            if (
                operation is SessionOperationClass.HANDSHAKE
                and not self._effective_handshake_demands()
            ):
                return False
            if (
                operation is SessionOperationClass.PRODUCT_SUBSCRIPTION
                and not self._effective_subscription_demands()
            ):
                return False
        return self._send_wire(frame, owner=owner, facet=facet, operation=operation)

    def _send_wire(
        self,
        frame: bytes,
        *,
        owner: str,
        facet: str,
        operation: SessionOperationClass,
    ) -> bool:
        payload = bytes(frame)
        if not payload or not self.broker.send_to(self.endpoint, payload):
            return False
        event = DatagramSentEvent(
            endpoint=self.endpoint,
            host_time_ns=time.time_ns(),
            monotonic_ns=time.monotonic_ns(),
            core_generation=int(self.core.generation),
            owner=str(owner),
            facet=str(facet),
            operation_class=operation,
            frame=payload,
            fingerprint=hashlib.sha256(payload).hexdigest(),
        )
        self.datagram_sent.emit(event)
        return True

    def _start_handshake_if_needed(self) -> None:
        if (
            not self.wire_transport_active
            or not self._effective_handshake_demands()
            or self._handshake is not None
        ):
            return
        sender = _SystemCommandSender(
            self,
            "system:handshake",
            "system",
            SessionOperationClass.HANDSHAKE,
        )
        handshake = Handshake(
            self.core.profile_store,
            lambda frame: sender.send(frame, operation=SessionOperationClass.HANDSHAKE),
            parent=self,
        )
        handshake.ready.connect(self.core.profile_ready)
        handshake.link_lost.connect(self.core.link_lost)
        handshake.link_restored.connect(self.core.link_restored)
        self._handshake = handshake
        handshake.start()
        self._handshake_timer.start()

    def _stop_handshake(self) -> None:
        self._handshake_timer.stop()
        handshake = self._handshake
        if handshake is None:
            return
        handshake.stop()
        handshake.deleteLater()
        self._handshake = None

    @Slot()
    def _tick_handshake(self) -> None:
        handshake = self._handshake
        if handshake is not None:
            handshake.tick(self._handshake_timer.interval())

    def _start_subscription_if_needed(self) -> None:
        if not self.wire_transport_active or not self._subscription_demands:
            return
        self._reconcile_subscription()

    def _reconcile_subscription(self) -> None:
        demands = self._effective_subscription_demands()
        if not demands:
            controller = self._subscription_controller
            if controller is not None:
                controller.stop()
            return
        rate = max(value[2] for value in demands)
        controller = self._subscription_controller
        if controller is None:
            sender = _SystemCommandSender(
                self,
                "system:subscription",
                "system",
                SessionOperationClass.PRODUCT_SUBSCRIPTION,
            )
            controller = ProductSubscriptionController(
                self.core,
                fast_rate_hz=rate,
                command_sender=sender,
                parent=self,
            )
            self._subscription_controller = controller
            if self.wire_transport_active:
                controller.start()
            return
        controller.set_fast_rate_hz(rate)
        if self.wire_transport_active and not controller.retry_timer.isActive():
            controller.start()

    def _acquire_simple(
        self,
        target: dict[int, str],
        owner: str,
        kind: str,
    ) -> RuntimeLease:
        self._assert_owner_thread()
        normalized_owner = _required_owner(owner)
        token = next(self._token_counter)
        target[token] = normalized_owner
        self.lease_changed.emit()

        def release(current_token: int) -> bool:
            if current_token not in target:
                return True
            target.pop(current_token, None)
            self.lease_changed.emit()
            return True

        return RuntimeLease(self, normalized_owner, kind, token, release)

    def _release_observer(self, token: int) -> bool:
        self._observers.pop(int(token), None)
        self.lease_changed.emit()
        return True

    def _release_transport(self, token: int) -> bool:
        self._assert_owner_thread()
        current = self._transport.get(int(token))
        if current is None:
            return True
        if len(self._transport) == 1 and (
            self.operation_gate.state is not RuntimeOperationGateState.IDLE
            or self._recorders
        ):
            return False
        had_wire = self.wire_transport_active
        self._transport.pop(int(token), None)
        self._non_wire_transport_tokens.discard(int(token))
        current[1].release()
        if not self._transport:
            self._presence_timer.stop()
            self._stop_handshake()
            controller = self._subscription_controller
            if controller is not None:
                controller.stop()
            self._revoke_identity_authorization()
            self._clear_datagram_claims()
            self.core.end_connection()
            self._set_presence(RuntimePresencePhase.DORMANT)
            self.transport_changed.emit(False)
        elif had_wire and not self.wire_transport_active:
            self._stop_handshake()
            controller = self._subscription_controller
            if controller is not None:
                controller.stop()
        self.lease_changed.emit()
        return True

    def _release_handshake(self, token: int) -> bool:
        self._handshake_demands.pop(int(token), None)
        if not self._handshake_demands:
            self._stop_handshake()
        self.lease_changed.emit()
        return True

    def _release_subscription(self, token: int) -> bool:
        self._subscription_demands.pop(int(token), None)
        self._reconcile_subscription()
        self.lease_changed.emit()
        return True

    def _release_recorder(self, token: int) -> bool:
        self._assert_owner_thread()
        if int(token) not in self._recorders:
            return True
        self._recorders.pop(int(token), None)
        self.lease_changed.emit()
        return True

    @Slot(object)
    def _on_gate_changed(self, _state: RuntimeOperationGateState) -> None:
        self._reconcile_subscription()
        if self._effective_handshake_demands():
            self._start_handshake_if_needed()
        else:
            self._stop_handshake()

    def _effective_handshake_demands(self) -> tuple[tuple[str, str], ...]:
        values = tuple(self._handshake_demands.values())
        if self.operation_gate.state is not RuntimeOperationGateState.PRODUCTION_FROZEN:
            return values
        return tuple(value for value in values if value[1] in {"production", "system"})

    def _effective_subscription_demands(self) -> tuple[tuple[str, str, int], ...]:
        values = tuple(self._subscription_demands.values())
        if self.operation_gate.state is not RuntimeOperationGateState.PRODUCTION_FROZEN:
            return values
        return tuple(value for value in values if value[1] in {"production", "system"})

    def _assert_owner_thread(self) -> None:
        if QThread.currentThread() is not self.thread():
            raise RuntimeError("endpoint runtime must be mutated on its Qt owner thread")

    def _managed_transaction_available(self, owner: object | None) -> bool:
        gate = self.operation_gate
        return bool(
            gate.state is RuntimeOperationGateState.IDLE
            or _owners_equal(gate.owner, owner)
        )

def _normalize_subscription_rate(value: object) -> int:
    rate = int(value)
    if not (1 <= rate <= 20):
        raise ValueError("product-service rate must be 1..20 Hz")
    return rate


def _normalize_target_facts(
    values: Iterable[tuple[str, object]],
    purpose: str,
) -> tuple[tuple[str, str], ...]:
    normalized: dict[str, str] = {}
    for raw_name, raw_value in values:
        name = str(raw_name).strip()
        value = str(raw_value).strip()
        if not name or not value or name in normalized:
            raise ValueError("operation target facts require unique non-empty text")
        normalized[name] = value
    if not normalized:
        normalized["purpose"] = str(purpose).strip() or "operation"
    return tuple(sorted(normalized.items()))


def _required_owner(value: object) -> str:
    owner = str(value).strip()
    if not owner:
        raise ValueError("owner is required")
    return owner


def _required_facet(value: object) -> str:
    facet = str(value).strip().lower()
    if not facet:
        raise ValueError("facet is required")
    return facet


def _owners_equal(first: object | None, second: object | None) -> bool:
    if first is None or second is None:
        return False
    if isinstance(first, str) and isinstance(second, str):
        return first == second
    return first is second


__all__ = [
    "CustomerAttachmentLease",
    "CustomerAttachmentScope",
    "DatagramSentEvent",
    "EndpointSendCapability",
    "EndpointSessionRuntime",
    "IdentityAuthorizationScope",
    "ProductionAttachmentLease",
    "RuntimeCommandSender",
    "RuntimeLease",
    "RuntimeLeaseError",
    "RuntimeOperationGate",
    "RuntimeOperationGateState",
    "RuntimeOperationGateway",
    "RuntimePresencePhase",
    "RuntimeRecorderLease",
    "SubscriptionDemandLease",
]
