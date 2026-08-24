"""Contextual Product Service controls with applied-value confirmation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from satellite_debug_tool.core.product import Availability, ControlMode
from satellite_debug_tool.core.link_trace import trace_message
from satellite_debug_tool.core.protocol import (
    ServiceControlOp,
    ServiceControlResponse,
    ServiceResultCode,
    build_service_apply_rf,
    build_service_set_capture_profile,
    build_service_set_control_mode,
    build_service_set_tx_enable,
    build_service_subscribe,
)

from .device_session import DeviceSessionCore


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


@dataclass(frozen=True)
class PendingProductControl:
    request_id: int
    operation: ServiceControlOp
    expected: Any
    response_received: bool = False


_RESULT_STATUS = {
    ServiceResultCode.INVALID_REQUEST: ProductControlStatus.INVALID_REQUEST,
    ServiceResultCode.OUT_OF_RANGE: ProductControlStatus.OUT_OF_RANGE,
    ServiceResultCode.STATE_NOT_ALLOWED: ProductControlStatus.STATE_NOT_ALLOWED,
    ServiceResultCode.NOT_SUPPORTED: ProductControlStatus.NOT_SUPPORTED,
    ServiceResultCode.BUSY: ProductControlStatus.BUSY,
    ServiceResultCode.INTERNAL_ERROR: ProductControlStatus.INTERNAL_ERROR,
}


DISCOVERY_FAST_ATTEMPTS = 10
DISCOVERY_FAST_INTERVAL_MS = 1000
DISCOVERY_SLOW_INTERVAL_MS = 3000


class CaptureProfileResult(str, Enum):
    ACK = "ack"
    SEND_FAILED = "send_failed"
    DEVICE_ERROR = "device_error"
    TIMEOUT = "timeout"


class ProductSubscriptionController(QObject):
    """Own Product Service discovery retries and exact subscription ACKs."""

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
        if self._confirmed or self._session.product_store.telemetry_ready:
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
        if result == ServiceResultCode.SUCCESS:
            trace_message(
                "PRODUCT_SERVICE",
                f"SUBSCRIBE confirmed request_id={request_id}",
            )
            self._confirm()

    @Slot()
    def _on_store_updated(self) -> None:
        if self._session.product_store.telemetry_ready:
            self._confirm()

    def _confirm(self) -> None:
        self._pending_request_id = None
        self._retry_timer.stop()
        self._set_confirmed(True)

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
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self.expire)
        session.product_store.control_response.connect(self.feed_response)

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
        request_id = self._session.next_request_id()
        self._pending_request_id = request_id
        self._pending_target = target
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
        if result == ServiceResultCode.SUCCESS:
            self.finished.emit(target, True, CaptureProfileResult.ACK.value)
            return
        self.finished.emit(
            target,
            False,
            f"{CaptureProfileResult.DEVICE_ERROR.value}:{result.name}",
        )

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
        self._pending: Optional[PendingProductControl] = None
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        session.product_store.control_response.connect(self.feed_response)
        session.product_store.updated.connect(self._check_readback)

    @property
    def pending(self) -> Optional[PendingProductControl]:
        return self._pending

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

    def request_tx_enable(self, enabled: bool) -> bool:
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
            return False
        self._pending = PendingProductControl(request_id, operation, expected)
        self.pending_changed.emit(True)
        self.status_changed.emit(
            ProductControlStatus.WAITING_RESPONSE,
            {"request_id": request_id},
        )
        self._timeout.start(3000)
        if not self._session.send(frame):
            self._timeout.stop()
            self._pending = None
            self.pending_changed.emit(False)
            self.status_changed.emit(ProductControlStatus.SEND_FAILED, {})
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
        if result != ServiceResultCode.SUCCESS:
            self._finish(_RESULT_STATUS.get(result, ProductControlStatus.INTERNAL_ERROR))
            return
        self._pending = PendingProductControl(
            pending.request_id,
            pending.operation,
            pending.expected,
            response_received=True,
        )
        self.status_changed.emit(ProductControlStatus.WAITING_READBACK, {})
        self._timeout.start(5000)
        self._check_readback()

    @Slot()
    def _check_readback(self) -> None:
        pending = self._pending
        if pending is None or not pending.response_received:
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
        self.pending_changed.emit(False)
        self.status_changed.emit(status, {})


__all__ = [
    "CaptureProfileController",
    "CaptureProfileResult",
    "DISCOVERY_FAST_ATTEMPTS",
    "DISCOVERY_FAST_INTERVAL_MS",
    "DISCOVERY_SLOW_INTERVAL_MS",
    "PendingProductControl",
    "ProductControlController",
    "ProductControlStatus",
    "ProductSubscriptionController",
]
