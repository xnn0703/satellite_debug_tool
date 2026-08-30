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
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
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
        sent = self._session.send(
            build_service_subscribe(request_id, self._fast_rate_hz)
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
            result = ServiceResultCode.INTERNAL_ERROR
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

    def __init__(
        self,
        session: DeviceSessionCore,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._pending_request_id: int | None = None
        self._pending_target: bool | None = None
        self._pending_epoch: int | None = None
        self._lease_token = object()
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self.expire)
        session.product_store.control_response.connect(self.feed_response)
        session.product_store.updated.connect(self._on_store_updated)
        session.connection_changed.connect(self._on_connection_changed)
        session.generation_changed.connect(self._on_generation_changed)
        self.destroyed.connect(
            lambda _obj=None, session=session, token=self._lease_token: session.release_device_transaction(token)
        )

    @property
    def pending_request_id(self) -> int | None:
        return self._pending_request_id

    @property
    def pending_target(self) -> bool | None:
        return self._pending_target

    @property
    def timeout_timer(self) -> QTimer:
        return self._timeout

    def request(self, support_full: bool) -> bool:
        if self._pending_request_id is not None:
            return False
        target = bool(support_full)
        if not self._session.try_acquire_device_transaction(self._lease_token):
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
        self.pending_changed.emit(target)
        self._timeout.start(3000)
        if self._session.send(
            build_service_set_capture_profile(request_id, target)
        ):
            return True
        self._clear_pending()
        self.finished.emit(target, False, CaptureProfileResult.SEND_FAILED.value)
        return False

    def reset(self) -> None:
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
        self._clear_pending()
        try:
            result = ServiceResultCode(response.result_code)
        except ValueError:
            result = ServiceResultCode.INTERNAL_ERROR
        if result == ServiceResultCode.ACCEPTED:
            if response.applied_mask & _CAPTURE_PROFILE_APPLIED_MASK:
                self.finished.emit(target, True, CaptureProfileResult.ACK.value)
                return
            detail = "APPLIED_MASK_MISSING"
        else:
            detail = result.name
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
        self._clear_pending()
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
        self._clear_pending()
        self.finished.emit(target, False, CaptureProfileResult.TIMEOUT.value)

    def _clear_pending(self) -> None:
        had_pending = self._pending_request_id is not None
        self._timeout.stop()
        self._pending_request_id = None
        self._pending_target = None
        self._pending_epoch = None
        self._session.release_device_transaction(self._lease_token)
        if had_pending:
            self.pending_changed.emit(None)


class ProductControlController(QObject):
    """Own request IDs, response context, timeout, and readback matching."""

    pending_changed = Signal(bool)
    status_changed = Signal(object, object)

    def __init__(
        self,
        session: DeviceSessionCore,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._lease_token = object()
        self._pending: Optional[PendingProductControl] = None
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        session.product_store.control_response.connect(self.feed_response)
        session.product_store.updated.connect(self._check_readback)
        session.connection_changed.connect(self._on_connection_changed)
        session.generation_changed.connect(self._on_generation_changed)
        self.destroyed.connect(
            lambda _obj=None, session=session, token=self._lease_token: session.release_device_transaction(token)
        )

    @property
    def pending(self) -> Optional[PendingProductControl]:
        return self._pending

    @property
    def transaction_available(self) -> bool:
        """Whether this controller can claim the shared device transaction now."""

        return self._session.device_transaction_available(self._lease_token)

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
        if not self._session.try_acquire_device_transaction(self._lease_token):
            self.status_changed.emit(ProductControlStatus.BUSY, {})
            return False
        self._pending = PendingProductControl(
            request_id,
            operation,
            expected,
            readback_cursor=self._telemetry_cursor(operation),
        )
        self.pending_changed.emit(True)
        self.status_changed.emit(
            ProductControlStatus.WAITING_RESPONSE,
            {"request_id": request_id},
        )
        self._timeout.start(3000)
        if not self._session.send(frame):
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
            self._finish(ProductControlStatus.SESSION_CHANGED)
            return
        required_mask = _PRODUCT_CONTROL_APPLIED_MASKS.get(pending.operation)
        if (
            required_mask is None
            or (response.applied_mask & required_mask) != required_mask
        ):
            self._finish(ProductControlStatus.INTERNAL_ERROR)
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
            self._finish(ProductControlStatus.SESSION_CHANGED)
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
            self._finish(ProductControlStatus.SESSION_CHANGED)

    @Slot(int, object)
    def _on_generation_changed(self, _generation: int, _endpoint: object) -> None:
        if self._pending is not None:
            self._finish(ProductControlStatus.SESSION_CHANGED)

    @Slot()
    def _on_timeout(self) -> None:
        pending = self._pending
        if pending is None:
            return
        self._finish(
            ProductControlStatus.READBACK_TIMEOUT
            if pending.response_received
            else ProductControlStatus.RESPONSE_TIMEOUT
        )

    def _finish(self, status: ProductControlStatus) -> None:
        self._timeout.stop()
        self._pending = None
        self._session.release_device_transaction(self._lease_token)
        self.pending_changed.emit(False)
        self.status_changed.emit(status, {})


class MountConfigurationController(QObject):
    """Own mount write, correlated ACK, reboot, and authoritative readback."""

    pending_changed = Signal(bool)
    status_changed = Signal(object, object)

    def __init__(
        self,
        session: DeviceSessionCore,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._lease_token = object()
        self._pending: Optional[PendingMountConfiguration] = None
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        session.product_store.control_response.connect(self.feed_response)
        session.product_store.updated.connect(self._check_readback)
        session.connection_changed.connect(self._on_connection_changed)
        session.generation_changed.connect(self._on_generation_changed)
        self.destroyed.connect(
            lambda _obj=None, session=session, token=self._lease_token: session.release_device_transaction(token)
        )

    @property
    def pending(self) -> Optional[PendingMountConfiguration]:
        return self._pending

    @property
    def transaction_available(self) -> bool:
        """Whether this controller can claim the shared device transaction now."""

        return self._session.device_transaction_available(self._lease_token)

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
        if not self._session.try_acquire_device_transaction(self._lease_token):
            self.status_changed.emit(MountConfigurationStatus.BUSY, {})
            return False
        store = self._session.product_store
        device_uid, serial_number = self._device_identity()
        if not device_uid and not serial_number:
            self._session.release_device_transaction(self._lease_token)
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
        self.pending_changed.emit(True)
        self.status_changed.emit(
            MountConfigurationStatus.WAITING_RESPONSE,
            {"request_id": request_id},
        )
        self._timeout.start(3000)
        if self._session.send(frame):
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
            self._finish(MountConfigurationStatus.SESSION_CHANGED)
            return
        if not (response.applied_mask & SERVICE_PERSISTED_DEVICE_MOUNT):
            self._finish(MountConfigurationStatus.INTERNAL_ERROR)
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
            self._finish(MountConfigurationStatus.SESSION_CHANGED)
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
            self._finish(MountConfigurationStatus.SESSION_CHANGED)
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
            self._finish(MountConfigurationStatus.SESSION_CHANGED)
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
        if self._session.send(build_device_reboot()):
            return
        if self._pending is not None:
            self._finish(MountConfigurationStatus.RESTART_SEND_FAILED)

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
            self._finish(MountConfigurationStatus.SESSION_CHANGED)

    @Slot(int, object)
    def _on_generation_changed(self, _generation: int, endpoint: object) -> None:
        pending = self._pending
        if pending is None:
            return
        if endpoint != pending.endpoint:
            self._finish(MountConfigurationStatus.SESSION_CHANGED)
            return
        if pending.stage is not _MountConfigurationStage.RESTART:
            self._finish(MountConfigurationStatus.SESSION_CHANGED)
            return
        if not self._session.try_acquire_device_transaction(self._lease_token):
            self._finish(MountConfigurationStatus.SESSION_CHANGED)

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
        self._finish(status)

    def _finish(self, status: MountConfigurationStatus) -> None:
        self._timeout.stop()
        self._pending = None
        self._session.release_device_transaction(self._lease_token)
        self.pending_changed.emit(False)
        self.status_changed.emit(status, {})


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
