"""Typed command-sender boundary for device-session controllers."""

from __future__ import annotations

from enum import Enum
from typing import Callable, Iterable, Protocol, runtime_checkable


class SessionOperationClass(str, Enum):
    IDENTITY = "identity"
    HANDSHAKE = "handshake"
    PRODUCT_SUBSCRIPTION = "product_subscription"
    READ_ONLY_QUERY = "read_only_query"
    MUTATING = "mutating"
    TERMINAL = "terminal"


class SessionRecorderKind(str, Enum):
    """Recorder ownership classes with explicit Production-freeze semantics."""

    CUSTOMER_FULL_CAPTURE = "customer_full_capture"
    ENGINEERING_PASSIVE = "engineering_passive"
    PRODUCTION_EVIDENCE = "production_evidence"


@runtime_checkable
class SessionRecorderLease(Protocol):
    """Idempotent proof that one endpoint recorder remains active."""

    @property
    def released(self) -> bool:
        ...

    def release(self) -> bool:
        ...


@runtime_checkable
class SessionCommandSender(Protocol):
    """Send one frame with an explicit, fail-closed operation class."""

    def send(
        self,
        frame: bytes,
        *,
        operation: SessionOperationClass = SessionOperationClass.MUTATING,
    ) -> bool:
        ...


@runtime_checkable
class SessionOperationGateway(SessionCommandSender, Protocol):
    """Controller boundary for the one shared mutation CAS and typed sends."""

    def operation_available(self, owner: object | None = None) -> bool:
        ...

    def allows_unconfirmed_mutation(self) -> bool:
        """Whether this explicit transport boundary permits sent-only mutation."""

        ...

    def try_acquire_operation(
        self,
        owner: object,
        *,
        purpose: str,
        production_freeze: bool = False,
        allowed_operations: Iterable[SessionOperationClass] = (),
    ) -> bool:
        ...

    def release_operation(self, owner: object) -> bool:
        ...

    def begin_terminating(self, owner: object) -> bool:
        ...

    def confirm_terminal(
        self,
        owner: object,
        *,
        request_id: str | int,
        evidence_facts: Iterable[tuple[str, object]],
    ) -> bool:
        """Commit a terminal operation only from matching device evidence."""

        ...

    def update_production_allowlist(
        self,
        owner: object,
        allowed_operations: Iterable[SessionOperationClass],
    ) -> bool:
        ...

    def acquire_recorder(
        self,
        owner: object,
        *,
        kind: SessionRecorderKind,
    ) -> SessionRecorderLease | None:
        ...

    def update_operation_context(
        self,
        owner: object,
        *,
        request_id: str | int | None,
        target_facts: Iterable[tuple[str, object]],
    ) -> bool:
        ...

class CallableSessionCommandSender:
    """Adapter for legacy transports that expose only ``Callable[[bytes], bool]``."""

    def __init__(self, sender: Callable[[bytes], object]) -> None:
        self._sender = sender

    def send(
        self,
        frame: bytes,
        *,
        operation: SessionOperationClass = SessionOperationClass.MUTATING,
    ) -> bool:
        del operation
        return bool(self._sender(bytes(frame)))


class LegacySessionOperationGateway:
    """Preserve the serial/Core transaction contract behind the new boundary."""

    def __init__(
        self,
        session,
        sender: SessionCommandSender | None = None,
    ) -> None:
        self._session = session
        self._sender = command_sender_or_legacy(session, sender)

    def operation_available(self, owner: object | None = None) -> bool:
        return bool(self._session.device_transaction_available(owner))

    def allows_unconfirmed_mutation(self) -> bool:
        # Preserve the standalone serial boundary's historical sent-only
        # diagnostic controls explicitly.
        return True

    def try_acquire_operation(
        self,
        owner: object,
        *,
        purpose: str,
        production_freeze: bool = False,
        allowed_operations: Iterable[SessionOperationClass] = (),
    ) -> bool:
        del purpose, production_freeze, allowed_operations
        return bool(self._session.try_acquire_device_transaction(owner))

    def release_operation(self, owner: object) -> bool:
        return bool(self._session.release_device_transaction(owner))

    def begin_terminating(self, owner: object) -> bool:
        return bool(self._session.device_transaction_available(owner))

    def confirm_terminal(
        self,
        owner: object,
        *,
        request_id: str | int,
        evidence_facts: Iterable[tuple[str, object]],
    ) -> bool:
        del request_id
        if not tuple(evidence_facts):
            return False
        return bool(self._session.release_device_transaction(owner))

    def update_production_allowlist(
        self,
        owner: object,
        allowed_operations: Iterable[SessionOperationClass],
    ) -> bool:
        del owner, allowed_operations
        return False

    def acquire_recorder(
        self,
        owner: object,
        *,
        kind: SessionRecorderKind,
    ) -> SessionRecorderLease | None:
        del kind
        return _LegacyRecorderLease(owner)

    def update_operation_context(
        self,
        owner: object,
        *,
        request_id: str | int | None,
        target_facts: Iterable[tuple[str, object]],
    ) -> bool:
        del owner, request_id, target_facts
        return True

    def send(
        self,
        frame: bytes,
        *,
        operation: SessionOperationClass = SessionOperationClass.MUTATING,
    ) -> bool:
        return self._sender.send(frame, operation=operation)


class _LegacyRecorderLease:
    """Local recorder ownership used only by the explicit legacy boundary."""

    def __init__(self, owner: object) -> None:
        self.owner = owner
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> bool:
        self._released = True
        return True


def command_sender_or_legacy(
    session,
    sender: SessionCommandSender | None,
) -> SessionCommandSender:
    """Keep serial/legacy tests working while shared UDP injects a scoped sender."""

    if sender is not None:
        return sender
    return CallableSessionCommandSender(session.send)


def operation_gateway_or_legacy(
    session,
    gateway: SessionOperationGateway | None,
    sender: SessionCommandSender | None = None,
) -> SessionOperationGateway:
    if gateway is not None:
        return gateway
    return LegacySessionOperationGateway(session, sender)


__all__ = [
    "CallableSessionCommandSender",
    "LegacySessionOperationGateway",
    "SessionCommandSender",
    "SessionOperationGateway",
    "SessionOperationClass",
    "SessionRecorderKind",
    "SessionRecorderLease",
    "command_sender_or_legacy",
    "operation_gateway_or_legacy",
]
