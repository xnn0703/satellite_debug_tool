"""Contextual Product Service controls with applied-value confirmation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from satellite_debug_tool.core.product import Availability, ControlMode, NavigationSource
from satellite_debug_tool.core.product.service_store import ProductTelemetryCursor
from satellite_debug_tool.core.link_trace import trace_message
from satellite_debug_tool.core.protocol import (
    ServiceControlOp,
    ServiceControlResponse,
    ServiceResultCode,
    SERVICE_PERSISTED_DEVICE_MOUNT,
    MOUNT_CONTRACT_FRD1,
    MOUNT_STATUS_VALID_CONTRACT,
    MOUNT_STATUS_VALID_ANGLES,
    MOUNT_STATUS_VALID_RBV_VERIFIED,
    MOUNT_STATUS_VALID_RESTART_REQUIRED,
    build_device_reboot,
    build_service_apply_rf,
    build_service_set_capture_profile,
    build_service_set_control_mode,
    build_service_set_device_mount,
    build_service_set_tx_enable,
    build_service_subscribe,
)

from .device_session import DeviceSessionCore, DeviceSessionScope
from .command_sender import (
    SessionCommandSender,
    SessionOperationClass,
    SessionOperationGateway,
    SessionRecorderKind,
    SessionRecorderLease,
    command_sender_or_legacy,
    operation_gateway_or_legacy,
)


class ProductControlStatus(str, Enum):
    IDLE = "idle"
    SEND_FAILED = "send_failed"
    WAITING_RESPONSE = "waiting_response"
    WAITING_READBACK = "waiting_readback"
    APPLIED = "applied"
    INVALID_REQUEST = "invalid_request"
    OUT_OF_RANGE = "out_of_range"
    STATE_NOT_ALLOWED = "state_not_allowed"
    NOT_SUPPORTED = "not_supported"
    BUSY = "busy"
    INTERNAL_ERROR = "internal_error"
    RESPONSE_TIMEOUT = "response_timeout"
    READBACK_TIMEOUT = "readback_timeout"
    SESSION_CHANGED = "session_changed"


class MountConfigurationStatus(str, Enum):
    """Observable stages and terminal results of one atomic mount change."""

    IDLE = "idle"
    SEND_FAILED = "send_failed"
    WAITING_RESPONSE = "waiting_response"
    WAITING_READBACK = "waiting_readback"
    WAITING_RESTART = "waiting_restart"
    APPLIED = "applied"
    INVALID_REQUEST = "invalid_request"
    OUT_OF_RANGE = "out_of_range"
    STATE_NOT_ALLOWED = "state_not_allowed"
    NOT_SUPPORTED = "not_supported"
    BUSY = "busy"
    INTERNAL_ERROR = "internal_error"
    RESPONSE_TIMEOUT = "response_timeout"
    READBACK_TIMEOUT = "readback_timeout"
    RESTART_SEND_FAILED = "restart_send_failed"
    RESTART_TIMEOUT = "restart_timeout"
    SESSION_CHANGED = "session_changed"
    IDENTITY_UNAVAILABLE = "identity_unavailable"


class _MountConfigurationStage(str, Enum):
    RESPONSE = "response"
    READBACK = "readback"
    RESTART = "restart"


@dataclass(frozen=True)
class PendingMountConfiguration:
    request_id: int
    target_deg: tuple[float, float, float]
    stage: _MountConfigurationStage
    readback_cursor: ProductTelemetryCursor
    endpoint: object
    device_uid: str
    serial_number: str
    identity_generation: int
    hardware_identity_generation: int
    navigation_source_generation: int


@dataclass(frozen=True)
class PendingProductControl:
    request_id: int
    operation: ServiceControlOp
    expected: Any
    response_received: bool = False
    readback_cursor: Optional[ProductTelemetryCursor] = None


_RESULT_STATUS = {
    ServiceResultCode.INVALID_REQUEST: ProductControlStatus.INVALID_REQUEST,
    ServiceResultCode.OUT_OF_RANGE: ProductControlStatus.OUT_OF_RANGE,
    ServiceResultCode.STATE_NOT_ALLOWED: ProductControlStatus.STATE_NOT_ALLOWED,
    ServiceResultCode.NOT_SUPPORTED: ProductControlStatus.NOT_SUPPORTED,
    ServiceResultCode.BUSY: ProductControlStatus.BUSY,
    ServiceResultCode.INTERNAL_ERROR: ProductControlStatus.INTERNAL_ERROR,
}

_MOUNT_RESULT_STATUS = {
    ServiceResultCode.INVALID_REQUEST: MountConfigurationStatus.INVALID_REQUEST,
    ServiceResultCode.OUT_OF_RANGE: MountConfigurationStatus.OUT_OF_RANGE,
    ServiceResultCode.STATE_NOT_ALLOWED: MountConfigurationStatus.STATE_NOT_ALLOWED,
    ServiceResultCode.NOT_SUPPORTED: MountConfigurationStatus.NOT_SUPPORTED,
    ServiceResultCode.BUSY: MountConfigurationStatus.BUSY,
    ServiceResultCode.INTERNAL_ERROR: MountConfigurationStatus.INTERNAL_ERROR,
}

_PRODUCT_CONTROL_APPLIED_MASKS = {
    ServiceControlOp.SET_CONTROL_MODE: 1 << 0,
    ServiceControlOp.APPLY_RF: (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4),
    ServiceControlOp.SET_TX_ENABLE: 1 << 5,
}
_CAPTURE_PROFILE_APPLIED_MASK = 1 << 6


def _cursor_proves_later_measurement(
    baseline: ProductTelemetryCursor,
    current: ProductTelemetryCursor,
) -> bool:
    """Require one later arrival and a strictly later u32 device time."""

    if current.epoch != baseline.epoch:
        return False
    if current.generation <= baseline.generation:
        return False
    if current.timestamp_ms is None:
        return False
    if baseline.timestamp_ms is None:
        return True
    delta = (int(current.timestamp_ms) - int(baseline.timestamp_ms)) & 0xFFFFFFFF
    return 0 < delta < 0x80000000


DISCOVERY_FAST_ATTEMPTS = 10
DISCOVERY_FAST_INTERVAL_MS = 1000
DISCOVERY_SLOW_INTERVAL_MS = 3000
SUBSCRIPTION_KEEPALIVE_INTERVAL_MS = 1000


class CaptureProfileResult(str, Enum):
    ACK = "ack"
    SEND_FAILED = "send_failed"
    DEVICE_ERROR = "device_error"
    TIMEOUT = "timeout"


class ProductSubscriptionController(QObject):
    """Own Product Service discovery retries and idempotent keepalive sends."""

    confirmed_changed = Signal(bool)

    def __init__(
        self,
        session: DeviceSessionCore,
        *,
        fast_rate_hz: int = 10,
        command_sender: SessionCommandSender | None = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._command_sender = command_sender_or_legacy(session, command_sender)
        self._fast_rate_hz = max(1, int(fast_rate_hz))
        self._attempts = 0
        self._pending_request_id: int | None = None
        self._confirmed = False
        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(DISCOVERY_FAST_INTERVAL_MS)
        self._retry_timer.timeout.connect(self.retry)
        session.product_store.control_response.connect(self.feed_response)
        session.product_store.updated.connect(self._on_store_updated)
        session.connection_changed.connect(self._on_connection_changed)

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def pending_request_id(self) -> int | None:
        return self._pending_request_id

    @property
    def confirmed(self) -> bool:
        return self._confirmed

    @property
    def retry_timer(self) -> QTimer:
        return self._retry_timer

    def start(self) -> None:
        self._retry_timer.stop()
        self._attempts = 0
        self._pending_request_id = None
        self._set_confirmed(False)
        self._retry_timer.setInterval(DISCOVERY_FAST_INTERVAL_MS)
        self.send_now()
        if self._session.connected and not self._confirmed:
            self._retry_timer.start()

    def stop(self) -> None:
        self._retry_timer.stop()
        self._attempts = 0
        self._pending_request_id = None
        self._set_confirmed(False)

    def send_now(self) -> bool:
        self._attempts += 1
        request_id = self._session.next_request_id()
        self._pending_request_id = request_id
        sent = self._command_sender.send(
            build_service_subscribe(request_id, self._fast_rate_hz),
            operation=SessionOperationClass.PRODUCT_SUBSCRIPTION,
        )
        if not sent:
            self._pending_request_id = None
        trace_message(
            "PRODUCT_SERVICE",
            f"TX SUBSCRIBE request_id={request_id} "
            f"fast_rate_hz={self._fast_rate_hz} attempt={self._attempts} "
            f"result={int(sent)}",
        )
        return sent

    def set_fast_rate_hz(self, fast_rate_hz: int) -> bool:
        """Apply one validated aggregate rate without creating another state machine."""

        rate = int(fast_rate_hz)
        if not (1 <= rate <= 20):
            raise ValueError("product-service rate must be 1..20 Hz")
        if rate == self._fast_rate_hz:
            return True
        self._fast_rate_hz = rate
        if not self._session.connected:
            return True
        self._pending_request_id = None
        self._set_confirmed(False)
        return self.send_now()

    @Slot()
    def retry(self) -> None:
        if not self._session.connected:
            self._retry_timer.stop()
            return
        if self._confirmed:
            self.send_now()
            return
        if self._session.product_store.telemetry_ready:
            self._confirm()
            return
        self.send_now()
        if self._attempts >= DISCOVERY_FAST_ATTEMPTS:
            self._retry_timer.setInterval(DISCOVERY_SLOW_INTERVAL_MS)

    @Slot(object)
    def feed_response(self, response: ServiceControlResponse) -> None:
        if (
            self._pending_request_id is None
            or response.request_id != self._pending_request_id
            or response.operation != int(ServiceControlOp.SUBSCRIBE)
        ):
            return
        request_id = self._pending_request_id
        self._pending_request_id = None
        try:
            result = ServiceResultCode(response.result_code)
        except ValueError:
            self._finish_unknown(
                ProductControlStatus.INTERNAL_ERROR,
                "Product control returned an unknown result code",
            )
            return
        if result == ServiceResultCode.ACCEPTED:
            trace_message(
                "PRODUCT_SERVICE",
                f"SUBSCRIBE confirmed request_id={request_id}",
            )
            self._confirm()

    @Slot()
    def _on_store_updated(self) -> None:
        if not self._confirmed and self._session.product_store.telemetry_ready:
            self._confirm()

    def _confirm(self) -> None:
        self._pending_request_id = None
        if not self._confirmed:
            self._retry_timer.setInterval(SUBSCRIPTION_KEEPALIVE_INTERVAL_MS)
            if self._session.connected:
                self._retry_timer.start()
        self._set_confirmed(True)

    @Slot(bool)
    def _on_connection_changed(self, connected: bool) -> None:
        if not connected:
            self.stop()

    def _set_confirmed(self, confirmed: bool) -> None:
        value = bool(confirmed)
        if self._confirmed == value:
            return
        self._confirmed = value
        self.confirmed_changed.emit(value)


class CaptureProfileController(QObject):
    """Own full-capture profile requests, exact ACK context, and timeout."""

    pending_changed = Signal(object)
    finished = Signal(bool, bool, str)
    resync_required_changed = Signal(bool)

    def __init__(
        self,
        session: DeviceSessionCore,
        parent: Optional[QObject] = None,
        *,
        command_sender: SessionCommandSender | None = None,
        operation_gateway: SessionOperationGateway | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._operation_gateway = operation_gateway_or_legacy(
            session,
            operation_gateway,
            command_sender,
        )
        self._command_sender = self._operation_gateway
        self._pending_request_id: int | None = None
        self._pending_target: bool | None = None
        self._pending_epoch: int | None = None
        self._resync_required = False
        self._lease_token = object()
        self._recorder_lease: SessionRecorderLease | None = None
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self.expire)
        session.product_store.control_response.connect(self.feed_response)
        session.product_store.updated.connect(self._on_store_updated)
        session.connection_changed.connect(self._on_connection_changed)
        session.generation_changed.connect(self._on_generation_changed)
        self.destroyed.connect(self._on_destroyed)

    @property
    def pending_request_id(self) -> int | None:
        return self._pending_request_id

    @property
    def pending_target(self) -> bool | None:
        return self._pending_target

    @property
    def timeout_timer(self) -> QTimer:
        return self._timeout

    @property
    def recording_lease_active(self) -> bool:
        lease = self._recorder_lease
        return lease is not None and not lease.released

    @property
    def resync_required(self) -> bool:
        return self._resync_required

    def request(self, support_full: bool) -> bool:
        if self._pending_request_id is not None:
            return False
        target = bool(support_full)
        acquired_recorder = False
        if target and not self.recording_lease_active:
            acquire_recorder = getattr(
                self._operation_gateway,
                "acquire_recorder",
                None,
            )
            if acquire_recorder is None:
                self.finished.emit(
                    target,
                    False,
                    f"{CaptureProfileResult.DEVICE_ERROR.value}:BUSY",
                )
                return False
            self._recorder_lease = acquire_recorder(
                self._lease_token,
                kind=SessionRecorderKind.CUSTOMER_FULL_CAPTURE,
            )
            if self._recorder_lease is None:
                self.finished.emit(
                    target,
                    False,
                    f"{CaptureProfileResult.DEVICE_ERROR.value}:BUSY",
                )
                return False
            acquired_recorder = True
        if not self._operation_gateway.try_acquire_operation(
            self._lease_token,
            purpose="capture-profile",
        ):
            if acquired_recorder:
                self._release_recorder()
            self.finished.emit(
                target,
                False,
                f"{CaptureProfileResult.DEVICE_ERROR.value}:BUSY",
            )
            return False
        request_id = self._session.next_request_id()
        self._pending_request_id = request_id
        self._pending_target = target
        self._pending_epoch = self._session.product_store.telemetry_epoch
        update_context = getattr(
            self._operation_gateway,
            "update_operation_context",
            None,
        )
        if update_context is not None:
            update_context(
                self._lease_token,
                request_id=request_id,
                target_facts=(("support_full", int(target)),),
            )
        self.pending_changed.emit(target)
        self._timeout.start(3000)
        if self._command_sender.send(
            build_service_set_capture_profile(request_id, target),
            operation=SessionOperationClass.MUTATING,
        ):
            return True
        self._finish_unconfirmed()
        self.finished.emit(target, False, CaptureProfileResult.SEND_FAILED.value)
        return False

    def reset(self) -> None:
        if self._pending_request_id is not None or self.recording_lease_active:
            self._finish_unconfirmed()
            return
        self._clear_pending()

    @Slot(object)
    def feed_response(self, response: ServiceControlResponse) -> None:
        if (
            self._pending_request_id is None
            or response.request_id != self._pending_request_id
            or response.operation != int(ServiceControlOp.SET_CAPTURE_PROFILE)
        ):
            return
        target = bool(self._pending_target)
        try:
            result = ServiceResultCode(response.result_code)
        except ValueError:
            self._finish_unconfirmed()
            self.finished.emit(
                target,
                False,
                f"{CaptureProfileResult.DEVICE_ERROR.value}:UNKNOWN_RESULT",
            )
            return
        if result == ServiceResultCode.ACCEPTED:
            if response.applied_mask & _CAPTURE_PROFILE_APPLIED_MASK:
                self._set_resync_required(False)
                self._clear_pending(
                    release_operation=not target,
                    release_recorder=not target,
                )
                self.finished.emit(target, True, CaptureProfileResult.ACK.value)
                return
            detail = "APPLIED_MASK_MISSING"
        else:
            detail = result.name
        uncertain = (not target) or result == ServiceResultCode.ACCEPTED
        if uncertain:
            self._finish_unconfirmed()
        else:
            self._clear_pending(
                release_operation=True,
                release_recorder=target,
            )
        self.finished.emit(
            target,
            False,
            f"{CaptureProfileResult.DEVICE_ERROR.value}:{detail}",
        )

    @Slot(bool)
    def _on_connection_changed(self, connected: bool) -> None:
        if not connected:
            self._cancel_for_session_change()

    @Slot(int, object)
    def _on_generation_changed(self, _generation: int, _endpoint: object) -> None:
        self._cancel_for_session_change()

    def _cancel_for_session_change(self) -> None:
        if self._pending_request_id is None:
            return
        target = bool(self._pending_target)
        self._finish_unconfirmed()
        self.finished.emit(
            target,
            False,
            f"{CaptureProfileResult.DEVICE_ERROR.value}:SESSION_CHANGED",
        )

    @Slot()
    def _on_store_updated(self) -> None:
        if (
            self._pending_epoch is not None
            and self._session.product_store.telemetry_epoch != self._pending_epoch
        ):
            self._cancel_for_session_change()

    @Slot()
    def expire(self) -> None:
        if self._pending_request_id is None:
            return
        target = bool(self._pending_target)
        self._finish_unconfirmed()
        self.finished.emit(target, False, CaptureProfileResult.TIMEOUT.value)

    def _finish_unconfirmed(self) -> None:
        """Release local ownership while preserving the endpoint-local unknown fact."""

        self._set_resync_required(True)
        self._clear_pending(release_operation=True, release_recorder=True)

    def _set_resync_required(self, required: bool) -> None:
        required = bool(required)
        if self._resync_required == required:
            return
        self._resync_required = required
        self.resync_required_changed.emit(required)

    def _clear_pending(
        self,
        *,
        release_operation: bool = True,
        release_recorder: bool = False,
    ) -> None:
        had_pending = self._pending_request_id is not None
        self._timeout.stop()
        self._pending_request_id = None
        self._pending_target = None
        self._pending_epoch = None
        if release_operation:
            self._operation_gateway.release_operation(self._lease_token)
        if release_recorder:
            self._release_recorder()
        if had_pending:
            self.pending_changed.emit(None)

    def _release_recorder(self) -> bool:
        lease, self._recorder_lease = self._recorder_lease, None
        if lease is None:
            return True
        if lease.release():
            return True
        self._recorder_lease = lease
        return False

    @Slot(object)
    def _on_destroyed(self, _obj=None) -> None:
        if self.recording_lease_active:
            self._finish_unconfirmed()
            return
        self._operation_gateway.release_operation(self._lease_token)


class ProductControlController(QObject):
    """Own request IDs, response context, timeout, and readback matching."""

    pending_changed = Signal(bool)
    status_changed = Signal(object, object)

    def __init__(
        self,
        session: DeviceSessionCore,
        parent: Optional[QObject] = None,
        *,
        command_sender: SessionCommandSender | None = None,
        operation_gateway: SessionOperationGateway | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._operation_gateway = operation_gateway_or_legacy(
            session,
            operation_gateway,
            command_sender,
        )
        self._command_sender = self._operation_gateway
        self._lease_token = object()
        self._pending: Optional[PendingProductControl] = None
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        session.product_store.control_response.connect(self.feed_response)
        session.product_store.updated.connect(self._check_readback)
        session.connection_changed.connect(self._on_connection_changed)
        session.generation_changed.connect(self._on_generation_changed)
        self.destroyed.connect(self._on_destroyed)

    @property
    def pending(self) -> Optional[PendingProductControl]:
        return self._pending

    @property
    def transaction_available(self) -> bool:
        """Whether this controller can claim the shared device transaction now."""

        return self._operation_gateway.operation_available(self._lease_token)

    def request_control_mode(self, target: ControlMode) -> bool:
        request_id = self._session.next_request_id()
        frame = build_service_set_control_mode(
            request_id,
            1 if target == ControlMode.MANUAL else 0,
        )
        return self._begin(
            request_id,
            ServiceControlOp.SET_CONTROL_MODE,
            target,
            frame,
        )

    def request_rf(
        self,
        rx_frequency_mhz: float,
        tx_frequency_mhz: float,
        rx_polarization: int,
        tx_polarization: int,
    ) -> bool:
        expected = (
            float(rx_frequency_mhz),
            float(tx_frequency_mhz),
            int(rx_polarization),
            int(tx_polarization),
        )
        request_id = self._session.next_request_id()
        return self._begin(
            request_id,
            ServiceControlOp.APPLY_RF,
            expected,
            build_service_apply_rf(request_id, *expected),
        )

    def request_tx_enable(
        self,
        enabled: bool,
        *,
        confirmed_scope: DeviceSessionScope,
    ) -> bool:
        if not self._session.operation_scope_matches(confirmed_scope):
            self.status_changed.emit(ProductControlStatus.SESSION_CHANGED, {})
            return False
        request_id = self._session.next_request_id()
        expected = bool(enabled)
        return self._begin(
            request_id,
            ServiceControlOp.SET_TX_ENABLE,
            expected,
            build_service_set_tx_enable(request_id, expected),
        )

    def _begin(
        self,
        request_id: int,
        operation: ServiceControlOp,
        expected: Any,
        frame: bytes,
    ) -> bool:
        if self._pending is not None:
            self.status_changed.emit(ProductControlStatus.BUSY, {})
            return False
        if not self._operation_gateway.try_acquire_operation(
            self._lease_token,
            purpose=f"product-control:{operation.name.lower()}",
        ):
            self.status_changed.emit(ProductControlStatus.BUSY, {})
            return False
        self._pending = PendingProductControl(
            request_id,
            operation,
            expected,
            readback_cursor=self._telemetry_cursor(operation),
        )
        if not self._operation_gateway.update_operation_context(
            self._lease_token,
            request_id=request_id,
            target_facts=(
                ("operation", operation.name),
                ("expected", repr(expected)),
            ),
        ):
            self._finish(ProductControlStatus.SEND_FAILED)
            return False
        self.pending_changed.emit(True)
        self.status_changed.emit(
            ProductControlStatus.WAITING_RESPONSE,
            {"request_id": request_id},
        )
        self._timeout.start(3000)
        if not self._command_sender.send(
            frame,
            operation=SessionOperationClass.MUTATING,
        ):
            self._finish(ProductControlStatus.SEND_FAILED)
            return False
        return True

    @Slot(object)
    def feed_response(self, response: ServiceControlResponse) -> None:
        pending = self._pending
        if (
            pending is None
            or response.request_id != pending.request_id
            or response.operation != int(pending.operation)
        ):
            return
        try:
            result = ServiceResultCode(response.result_code)
        except ValueError:
            result = ServiceResultCode.INTERNAL_ERROR
        if result != ServiceResultCode.ACCEPTED:
            self._finish(_RESULT_STATUS.get(result, ProductControlStatus.INTERNAL_ERROR))
            return
        request_cursor = pending.readback_cursor
        current_cursor = self._telemetry_cursor(pending.operation)
        if request_cursor is None or current_cursor.epoch != request_cursor.epoch:
            self._finish_unknown(
                ProductControlStatus.SESSION_CHANGED,
                "Product control telemetry epoch changed before confirmation",
            )
            return
        required_mask = _PRODUCT_CONTROL_APPLIED_MASKS.get(pending.operation)
        if (
            required_mask is None
            or (response.applied_mask & required_mask) != required_mask
        ):
            self._finish_unknown(
                ProductControlStatus.INTERNAL_ERROR,
                "Product control was accepted without complete applied evidence",
            )
            return
        self._pending = PendingProductControl(
            pending.request_id,
            pending.operation,
            pending.expected,
            response_received=True,
            readback_cursor=current_cursor,
        )
        self.status_changed.emit(ProductControlStatus.WAITING_READBACK, {})
        self._timeout.start(5000)
        self._check_readback()

    @Slot()
    def _check_readback(self) -> None:
        pending = self._pending
        if pending is None or not pending.response_received:
            return
        baseline = pending.readback_cursor
        if baseline is None:
            return
        current = self._telemetry_cursor(pending.operation)
        if current.epoch != baseline.epoch:
            self._finish_unknown(
                ProductControlStatus.SESSION_CHANGED,
                "Product control telemetry epoch changed before applied readback",
            )
            return
        if baseline.timestamp_ms is None:
            if (
                current.generation > baseline.generation
                and current.timestamp_ms is not None
            ):
                # Without a device-time waterline at ACK, the first datagram
                # can still be queued from before the request.  Establish the
                # waterline and require another strictly later measurement.
                self._pending = replace(pending, readback_cursor=current)
            return
        if not _cursor_proves_later_measurement(baseline, current):
            return
        operation = self._session.product_store.snapshot().operation
        matched = False
        if pending.operation == ServiceControlOp.SET_CONTROL_MODE:
            matched = (
                operation.control_mode.availability == Availability.VALID
                and operation.control_mode.value == pending.expected
            )
        elif pending.operation == ServiceControlOp.APPLY_RF:
            rx, tx, rx_polarization, tx_polarization = pending.expected
            matched = (
                operation.rx_frequency_mhz.availability == Availability.VALID
                and operation.tx_frequency_mhz.availability == Availability.VALID
                and operation.rx_polarization.availability == Availability.VALID
                and operation.tx_polarization.availability == Availability.VALID
                and abs(float(operation.rx_frequency_mhz.value) - rx) <= 0.001
                and abs(float(operation.tx_frequency_mhz.value) - tx) <= 0.001
                and int(operation.rx_polarization.value) == rx_polarization
                and int(operation.tx_polarization.value) == tx_polarization
            )
        elif pending.operation == ServiceControlOp.SET_TX_ENABLE:
            matched = (
                operation.tx_enabled.availability == Availability.VALID
                and bool(operation.tx_enabled.value) == pending.expected
            )
        if matched:
            self._finish(ProductControlStatus.APPLIED)

    def _telemetry_cursor(
        self,
        operation: ServiceControlOp,
    ) -> ProductTelemetryCursor:
        """Return the Product telemetry stream that proves this operation applied."""
        store = self._session.product_store
        if operation == ServiceControlOp.APPLY_RF:
            return store.slow_telemetry_cursor
        return store.fast_telemetry_cursor

    @Slot(bool)
    def _on_connection_changed(self, connected: bool) -> None:
        if not connected and self._pending is not None:
            self._finish_unknown(
                ProductControlStatus.SESSION_CHANGED,
                "Product control connection changed before confirmation",
            )

    @Slot(int, object)
    def _on_generation_changed(self, _generation: int, _endpoint: object) -> None:
        if self._pending is not None:
            self._finish_unknown(
                ProductControlStatus.SESSION_CHANGED,
                "Product control generation changed before confirmation",
            )

    @Slot()
    def _on_timeout(self) -> None:
        pending = self._pending
        if pending is None:
            return
        status = (
            ProductControlStatus.READBACK_TIMEOUT
            if pending.response_received
            else ProductControlStatus.RESPONSE_TIMEOUT
        )
        self._finish_unknown(status, "Product control timed out before applied evidence")

    def _finish_unknown(
        self,
        status: ProductControlStatus,
        reason: str,
    ) -> None:
        """Publish an unconfirmed result and release this endpoint operation."""

        del reason
        self._timeout.stop()
        self._pending = None
        self._operation_gateway.release_operation(self._lease_token)
        self.pending_changed.emit(False)
        self.status_changed.emit(status, {})

    def _finish(self, status: ProductControlStatus) -> None:
        self._timeout.stop()
        self._pending = None
        self._operation_gateway.release_operation(self._lease_token)
        self.pending_changed.emit(False)
        self.status_changed.emit(status, {})

    @Slot(object)
    def _on_destroyed(self, _obj=None) -> None:
        self._pending = None
        self._operation_gateway.release_operation(self._lease_token)


class MountConfigurationController(QObject):
    """Own mount write, correlated ACK, reboot, and authoritative readback."""

    pending_changed = Signal(bool)
    status_changed = Signal(object, object)

    def __init__(
        self,
        session: DeviceSessionCore,
        parent: Optional[QObject] = None,
        *,
        command_sender: SessionCommandSender | None = None,
        operation_gateway: SessionOperationGateway | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._operation_gateway = operation_gateway_or_legacy(
            session,
            operation_gateway,
            command_sender,
        )
        self._command_sender = self._operation_gateway
        self._lease_token = object()
        self._pending: Optional[PendingMountConfiguration] = None
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        session.product_store.control_response.connect(self.feed_response)
        session.product_store.updated.connect(self._check_readback)
        session.connection_changed.connect(self._on_connection_changed)
        session.generation_changed.connect(self._on_generation_changed)
        self.destroyed.connect(self._on_destroyed)

    @property
    def pending(self) -> Optional[PendingMountConfiguration]:
        return self._pending

    @property
    def transaction_available(self) -> bool:
        """Whether this controller can claim the shared device transaction now."""

        return self._operation_gateway.operation_available(self._lease_token)

    def request_mount(
        self,
        mount_yaw_deg: float,
        mount_pitch_deg: float,
        mount_roll_deg: float,
    ) -> bool:
        """Begin one three-axis atomic write when no other mount write is active."""

        if self._pending is not None:
            self.status_changed.emit(MountConfigurationStatus.BUSY, {})
            return False
        target = (
            float(mount_yaw_deg),
            float(mount_pitch_deg),
            float(mount_roll_deg),
        )
        request_id = self._session.next_request_id()
        try:
            frame = build_service_set_device_mount(request_id, *target)
        except ValueError:
            self.status_changed.emit(MountConfigurationStatus.OUT_OF_RANGE, {})
            return False
        if not self._operation_gateway.try_acquire_operation(
            self._lease_token,
            purpose="mount-configuration",
        ):
            self.status_changed.emit(MountConfigurationStatus.BUSY, {})
            return False
        store = self._session.product_store
        device_uid, serial_number = self._device_identity()
        if not device_uid and not serial_number:
            self._operation_gateway.release_operation(self._lease_token)
            self.status_changed.emit(
                MountConfigurationStatus.IDENTITY_UNAVAILABLE,
                {},
            )
            return False
        self._pending = PendingMountConfiguration(
            request_id=request_id,
            target_deg=target,
            stage=_MountConfigurationStage.RESPONSE,
            readback_cursor=store.mount_status_cursor,
            endpoint=self._session.endpoint,
            device_uid=device_uid,
            serial_number=serial_number,
            identity_generation=store.identity_generation,
            hardware_identity_generation=store.hardware_identity_generation,
            navigation_source_generation=store.navigation_source_generation,
        )
        if not self._operation_gateway.update_operation_context(
            self._lease_token,
            request_id=request_id,
            target_facts=(
                ("mount_yaw_deg", target[0]),
                ("mount_pitch_deg", target[1]),
                ("mount_roll_deg", target[2]),
                ("stage", "write"),
            ),
        ):
            self._finish(MountConfigurationStatus.SEND_FAILED)
            return False
        self.pending_changed.emit(True)
        self.status_changed.emit(
            MountConfigurationStatus.WAITING_RESPONSE,
            {"request_id": request_id},
        )
        self._timeout.start(3000)
        if self._command_sender.send(
            frame,
            operation=SessionOperationClass.MUTATING,
        ):
            return True
        if self._pending is not None:
            self._finish(MountConfigurationStatus.SEND_FAILED)
        return False

    @Slot(object)
    def feed_response(self, response: ServiceControlResponse) -> None:
        pending = self._pending
        if (
            pending is None
            or pending.stage is not _MountConfigurationStage.RESPONSE
            or response.request_id != pending.request_id
            or response.operation != int(ServiceControlOp.SET_DEVICE_MOUNT)
        ):
            return
        try:
            result = ServiceResultCode(response.result_code)
        except ValueError:
            result = ServiceResultCode.INTERNAL_ERROR
        if result != ServiceResultCode.ACCEPTED:
            self._finish(
                _MOUNT_RESULT_STATUS.get(
                    result,
                    MountConfigurationStatus.INTERNAL_ERROR,
                )
            )
            return
        if (
            self._session.product_store.mount_status_cursor.epoch
            != pending.readback_cursor.epoch
        ):
            self._finish_unknown(
                MountConfigurationStatus.SESSION_CHANGED,
                "Mount telemetry epoch changed after the write was accepted",
            )
            return
        if not (response.applied_mask & SERVICE_PERSISTED_DEVICE_MOUNT):
            self._finish_unknown(
                MountConfigurationStatus.INTERNAL_ERROR,
                "Mount write was accepted without persisted-mount evidence",
            )
            return
        self._pending = replace(
            pending,
            stage=_MountConfigurationStage.READBACK,
            readback_cursor=self._session.product_store.mount_status_cursor,
        )
        self.status_changed.emit(MountConfigurationStatus.WAITING_READBACK, {})
        self._timeout.start(5000)
        self._check_readback()

    @Slot()
    def _check_readback(self) -> None:
        pending = self._pending
        if pending is None or pending.stage is _MountConfigurationStage.RESPONSE:
            return
        store = self._session.product_store
        current_cursor = store.mount_status_cursor
        if (
            pending.stage is _MountConfigurationStage.READBACK
            and current_cursor.epoch != pending.readback_cursor.epoch
        ):
            self._finish_unknown(
                MountConfigurationStatus.SESSION_CHANGED,
                "Mount telemetry epoch changed before matching readback",
            )
            return
        if current_cursor.generation <= pending.readback_cursor.generation:
            return
        if pending.stage is _MountConfigurationStage.READBACK:
            if not _cursor_proves_later_measurement(
                pending.readback_cursor,
                current_cursor,
            ):
                return
        status = store.mount_status_record
        if status is None or not self._angles_match(status, pending.target_deg):
            return
        identity_match = self._identity_matches(pending, require_new=False)
        if identity_match is None:
            return
        if not identity_match:
            self._finish_unknown(
                MountConfigurationStatus.SESSION_CHANGED,
                "Mount readback identity differed from the frozen device identity",
            )
            return
        if not (status.valid_mask & MOUNT_STATUS_VALID_RESTART_REQUIRED):
            return

        if pending.stage is _MountConfigurationStage.READBACK:
            if status.restart_required:
                self._send_reboot(pending)
            return

        if status.restart_required:
            return
        identity_match = self._identity_matches(pending, require_new=True)
        if identity_match is None:
            return
        if not identity_match:
            self._finish_unknown(
                MountConfigurationStatus.SESSION_CHANGED,
                "Mount post-restart identity differed from the frozen device identity",
            )
            return
        if store.navigation_source_generation <= pending.navigation_source_generation:
            return
        require_rbv = self._bynav_attitude_requirement(
            store.snapshot().navigation_sources
        )
        if require_rbv is None:
            return
        if self._rbv_ready(status, require_rbv):
            self._finish(MountConfigurationStatus.APPLIED)

    def _send_reboot(self, pending: PendingMountConfiguration) -> None:
        self._pending = replace(
            pending,
            stage=_MountConfigurationStage.RESTART,
            readback_cursor=self._session.product_store.mount_status_cursor,
            identity_generation=self._session.product_store.identity_generation,
            hardware_identity_generation=self._session.product_store.hardware_identity_generation,
            navigation_source_generation=self._session.product_store.navigation_source_generation,
        )
        self.status_changed.emit(MountConfigurationStatus.WAITING_RESTART, {})
        self._timeout.start(120000)
        if not self._operation_gateway.update_operation_context(
            self._lease_token,
            request_id=f"{pending.request_id}:restart",
            target_facts=(
                ("mount_yaw_deg", pending.target_deg[0]),
                ("mount_pitch_deg", pending.target_deg[1]),
                ("mount_roll_deg", pending.target_deg[2]),
                ("stage", "restart"),
            ),
        ):
            self._finish_unknown(
                MountConfigurationStatus.RESTART_SEND_FAILED,
                "Mount restart context could not be frozen after persisted write",
            )
            return
        if self._command_sender.send(
            build_device_reboot(),
            operation=SessionOperationClass.MUTATING,
        ):
            return
        if self._pending is not None:
            self._finish_unknown(
                MountConfigurationStatus.RESTART_SEND_FAILED,
                "Mount restart command could not be written after persisted write",
            )

    @staticmethod
    def _angles_match(status, target: tuple[float, float, float]) -> bool:
        if (
            not (status.valid_mask & MOUNT_STATUS_VALID_CONTRACT)
            or status.mount_contract_id != MOUNT_CONTRACT_FRD1
            or not (status.valid_mask & MOUNT_STATUS_VALID_ANGLES)
        ):
            return False
        actual = (
            float(status.mount_yaw_deg),
            float(status.mount_pitch_deg),
            float(status.mount_roll_deg),
        )
        return all(abs(value - expected) <= 0.051 for value, expected in zip(actual, target))

    @staticmethod
    def _rbv_ready(status, required: bool) -> bool:
        if not required:
            return True
        return bool(
            status.valid_mask & MOUNT_STATUS_VALID_RBV_VERIFIED
            and status.rbv_verified
        )

    @staticmethod
    def _bynav_attitude_requirement(navigation) -> Optional[bool]:
        """Resolve whether final confirmation requires Bynav RBV verification."""

        configured = navigation.external_ins_configured
        if configured.availability != Availability.VALID:
            return None
        if not configured.value:
            return False
        source = navigation.external_ins_source
        if source.availability != Availability.VALID:
            return None
        if source.value != NavigationSource.BYNAV:
            return False
        role_mask = navigation.external_role_mask
        if role_mask.availability != Availability.VALID:
            return None
        return bool(int(role_mask.value or 0) & (1 << 2))

    def _device_identity(self) -> tuple[str, str]:
        """Return the available immutable UID and production serial facts."""

        facts = dict(self._session.device_scope().product_identity_facts)
        return (
            facts.get("product_device_uid", ""),
            facts.get("product_serial_number", ""),
        )

    def _identity_matches(
        self,
        pending: PendingMountConfiguration,
        *,
        require_new: bool,
    ) -> Optional[bool]:
        """Match all identity facts captured before reboot; None means wait."""

        store = self._session.product_store
        if require_new:
            if (
                pending.serial_number
                and store.identity_generation <= pending.identity_generation
            ):
                return None
            if (
                pending.device_uid
                and store.hardware_identity_generation
                <= pending.hardware_identity_generation
            ):
                return None
        uid, serial = self._device_identity()
        for expected, actual in (
            (pending.device_uid, uid),
            (pending.serial_number, serial),
        ):
            if not expected:
                continue
            if not actual:
                return None
            if actual != expected:
                return False
        return True

    @Slot(bool)
    def _on_connection_changed(self, connected: bool) -> None:
        pending = self._pending
        if pending is None or connected:
            return
        if pending.stage is not _MountConfigurationStage.RESTART:
            self._finish_unknown(
                MountConfigurationStatus.SESSION_CHANGED,
                "Mount connection changed before matching applied evidence",
            )

    @Slot(int, object)
    def _on_generation_changed(self, _generation: int, endpoint: object) -> None:
        pending = self._pending
        if pending is None:
            return
        if endpoint != pending.endpoint:
            self._finish_unknown(
                MountConfigurationStatus.SESSION_CHANGED,
                "Mount operation returned on a different endpoint",
            )
            return
        if pending.stage is not _MountConfigurationStage.RESTART:
            self._finish_unknown(
                MountConfigurationStatus.SESSION_CHANGED,
                "Mount session generation changed before the requested restart",
            )
            return
        if not self._operation_gateway.try_acquire_operation(
            self._lease_token,
            purpose="mount-return-verification",
        ):
            self._finish_unknown(
                MountConfigurationStatus.SESSION_CHANGED,
                "Mount return verification could not retain its operation owner",
            )

    @Slot()
    def _on_timeout(self) -> None:
        pending = self._pending
        if pending is None:
            return
        if pending.stage is _MountConfigurationStage.RESPONSE:
            status = MountConfigurationStatus.RESPONSE_TIMEOUT
        elif pending.stage is _MountConfigurationStage.READBACK:
            status = MountConfigurationStatus.READBACK_TIMEOUT
        else:
            status = MountConfigurationStatus.RESTART_TIMEOUT
        self._finish_unknown(
            status,
            "Mount operation timed out before matching persisted and post-restart evidence",
        )

    def _finish_unknown(
        self,
        status: MountConfigurationStatus,
        reason: str,
    ) -> None:
        del reason
        self._timeout.stop()
        self._pending = None
        self._operation_gateway.release_operation(self._lease_token)
        self.pending_changed.emit(False)
        self.status_changed.emit(status, {})

    def _finish(self, status: MountConfigurationStatus) -> None:
        self._timeout.stop()
        self._pending = None
        self._operation_gateway.release_operation(self._lease_token)
        self.pending_changed.emit(False)
        self.status_changed.emit(status, {})

    @Slot(object)
    def _on_destroyed(self, _obj=None) -> None:
        self._pending = None
        self._operation_gateway.release_operation(self._lease_token)


__all__ = [
    "CaptureProfileController",
    "CaptureProfileResult",
    "DISCOVERY_FAST_ATTEMPTS",
    "DISCOVERY_FAST_INTERVAL_MS",
    "DISCOVERY_SLOW_INTERVAL_MS",
    "SUBSCRIPTION_KEEPALIVE_INTERVAL_MS",
    "PendingProductControl",
    "PendingMountConfiguration",
    "MountConfigurationController",
    "MountConfigurationStatus",
    "ProductControlController",
    "ProductControlStatus",
    "ProductSubscriptionController",
]
