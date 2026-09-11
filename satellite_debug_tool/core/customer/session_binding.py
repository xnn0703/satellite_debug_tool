"""Stable customer-view binding over replaceable attachment capabilities."""

from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.comm import DeviceConnectionPhase
from satellite_debug_tool.core.session.command_sender import (
    SessionCommandSender,
    SessionOperationClass,
    SessionOperationGateway,
    SessionRecorderKind,
    SessionRecorderLease,
)

from .device_directory import Endpoint, normalize_customer_endpoint


class CustomerAttachmentCommandSender(SessionCommandSender):
    """Resolve the current attachment at send time; detached means zero send."""

    def __init__(self, binding: "CustomerEndpointSessionBinding") -> None:
        self._binding = binding

    def send(
        self,
        frame: bytes,
        *,
        operation: SessionOperationClass = SessionOperationClass.MUTATING,
    ) -> bool:
        attachment = self._binding.attachment
        if attachment is None:
            return False
        return bool(
            attachment.command_sender.send(bytes(frame), operation=operation)
        )


class CustomerAttachmentOperationGateway(SessionOperationGateway):
    """Give one controller a stable gateway while attachment epochs change."""

    def __init__(self, binding: "CustomerEndpointSessionBinding") -> None:
        self._binding = binding
        self._active_gateway: Optional[SessionOperationGateway] = None

    def operation_available(self, owner: object | None = None) -> bool:
        if self._binding.closed:
            return False
        gateway = self._active_gateway
        if gateway is not None:
            return bool(gateway.operation_available(owner))
        attachment = self._binding.attachment
        if attachment is None:
            return False
        return bool(attachment.new_operation_gateway().operation_available(owner))

    def allows_unconfirmed_mutation(self) -> bool:
        if self._binding.closed:
            return False
        gateway = self._active_gateway
        if gateway is not None:
            return bool(gateway.allows_unconfirmed_mutation())
        attachment = self._binding.attachment
        return bool(
            attachment is not None
            and attachment.new_operation_gateway().allows_unconfirmed_mutation()
        )

    def try_acquire_operation(
        self,
        owner: object,
        *,
        purpose: str,
        production_freeze: bool = False,
        allowed_operations: Iterable[SessionOperationClass] = (),
    ) -> bool:
        if self._binding.closed:
            return False
        if self._active_gateway is not None:
            return bool(
                self._active_gateway.try_acquire_operation(
                    owner,
                    purpose=purpose,
                    production_freeze=production_freeze,
                    allowed_operations=allowed_operations,
                )
            )
        attachment = self._binding.attachment
        if attachment is None:
            return False
        gateway = attachment.new_operation_gateway()
        if not gateway.try_acquire_operation(
            owner,
            purpose=purpose,
            production_freeze=production_freeze,
            allowed_operations=allowed_operations,
        ):
            return False
        self._active_gateway = gateway
        return True

    def release_operation(self, owner: object) -> bool:
        gateway = self._active_gateway
        if gateway is None:
            return False
        if not gateway.release_operation(owner):
            return False
        self._active_gateway = None
        return True

    def begin_terminating(self, owner: object) -> bool:
        if self._binding.closed:
            return False
        gateway = self._active_gateway
        return bool(gateway is not None and gateway.begin_terminating(owner))

    def confirm_terminal(
        self,
        owner: object,
        *,
        request_id: str | int,
        evidence_facts: Iterable[tuple[str, object]],
    ) -> bool:
        gateway = self._active_gateway
        if (
            self._binding.closed
            or gateway is None
            or not gateway.confirm_terminal(
                owner,
                request_id=request_id,
                evidence_facts=evidence_facts,
            )
        ):
            return False
        self._active_gateway = None
        return True

    def update_production_allowlist(
        self,
        owner: object,
        allowed_operations: Iterable[SessionOperationClass],
    ) -> bool:
        gateway = self._active_gateway
        return bool(
            not self._binding.closed
            and gateway is not None
            and gateway.update_production_allowlist(owner, allowed_operations)
        )

    def acquire_recorder(
        self,
        owner: object,
        *,
        kind: SessionRecorderKind,
    ) -> SessionRecorderLease | None:
        if self._binding.closed:
            return None
        attachment = self._binding.attachment
        if attachment is None:
            return None
        return attachment.new_operation_gateway().acquire_recorder(
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
        gateway = self._active_gateway
        return bool(
            not self._binding.closed
            and gateway is not None
            and gateway.update_operation_context(
                owner,
                request_id=request_id,
                target_facts=target_facts,
            )
        )

    def send(
        self,
        frame: bytes,
        *,
        operation: SessionOperationClass = SessionOperationClass.MUTATING,
    ) -> bool:
        if self._binding.closed:
            return False
        gateway = self._active_gateway
        if gateway is not None:
            return bool(gateway.send(bytes(frame), operation=operation))
        attachment = self._binding.attachment
        if attachment is None:
            return False
        return bool(
            attachment.command_sender.send(bytes(frame), operation=operation)
        )


class CustomerEndpointSessionBinding(QObject):
    """One endpoint-fixed UI contract backed by the customer intent directory."""

    connection_state_changed = Signal(bool)
    device_connection_phase_changed = Signal(str)
    attachment_failed = Signal(str)

    def __init__(
        self,
        device_directory,
        endpoint: Endpoint,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.directory = device_directory
        self.endpoint = normalize_customer_endpoint(endpoint)
        runtime = device_directory.runtime(self.endpoint)
        if runtime is None:
            raise ValueError("customer endpoint has no configured runtime")
        self.runtime = runtime
        self.core = runtime.core
        self._closed = False
        self.command_sender = CustomerAttachmentCommandSender(self)
        device_directory.attachment_changed.connect(self._on_attachment_changed)
        failure_signal = getattr(device_directory, "attachment_failed", None)
        self._attachment_failed_signal = failure_signal
        if failure_signal is not None:
            failure_signal.connect(self._on_attachment_failed)
        runtime.presence_changed.connect(self._on_presence_changed)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def attachment(self):
        if self._closed:
            return None
        return self.directory.attachment(self.endpoint)

    @property
    def attached(self) -> bool:
        return self.attachment is not None

    @property
    def connection_phase(self) -> DeviceConnectionPhase:
        if not self.attached:
            return DeviceConnectionPhase.DISCONNECTED
        token = str(getattr(self.runtime.presence_phase, "value", self.runtime.presence_phase))
        if token == "online":
            return DeviceConnectionPhase.ONLINE
        if token == "stale":
            return DeviceConnectionPhase.RECONNECTING
        return DeviceConnectionPhase.WAITING

    @property
    def device_online(self) -> bool:
        return self.connection_phase is DeviceConnectionPhase.ONLINE

    def new_operation_gateway(self) -> CustomerAttachmentOperationGateway:
        return CustomerAttachmentOperationGateway(self)

    def attach(self) -> bool:
        if self._closed:
            return False
        try:
            self.directory.attach(self.endpoint)
        except Exception as exc:
            self.attachment_failed.emit(str(exc))
            return False
        return self.attached

    def detach(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self.directory.detach(self.endpoint))
        except Exception as exc:
            self.attachment_failed.emit(str(exc))
            return False

    def _on_attachment_changed(self, endpoint: object, attached: bool) -> None:
        try:
            normalized = normalize_customer_endpoint(endpoint)
        except ValueError:
            return
        if normalized != self.endpoint:
            return
        self.connection_state_changed.emit(bool(attached))
        self.device_connection_phase_changed.emit(self.connection_phase.value)

    def _on_attachment_failed(self, endpoint: object, reason: str) -> None:
        try:
            normalized = normalize_customer_endpoint(endpoint)
        except ValueError:
            return
        if normalized == self.endpoint:
            self.attachment_failed.emit(str(reason))

    def _on_presence_changed(self, _phase: object) -> None:
        if self.attached:
            self.device_connection_phase_changed.emit(self.connection_phase.value)

    def shutdown(self) -> None:
        """Permanently detach this UI binding from directory/runtime signals."""

        if self._closed:
            return
        self._closed = True
        connections = (
            (
                getattr(self.directory, "attachment_changed", None),
                self._on_attachment_changed,
            ),
            (self._attachment_failed_signal, self._on_attachment_failed),
            (getattr(self.runtime, "presence_changed", None), self._on_presence_changed),
        )
        for signal, slot in connections:
            if signal is None or not hasattr(signal, "disconnect"):
                continue
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass


__all__ = [
    "CustomerAttachmentCommandSender",
    "CustomerAttachmentOperationGateway",
    "CustomerEndpointSessionBinding",
]
