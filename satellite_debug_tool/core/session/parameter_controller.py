"""Parameter-management state machine for one device session."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from satellite_debug_tool.core.profile import CapabilitySupport
from satellite_debug_tool.core.protocol import (
    CodecError,
    CommandResponse,
    MetaInfo,
    ParaTableReport,
    ParaType,
    RespCode,
    build_para_reset,
    build_para_set,
    build_request_para_table,
)

from .device_session import DeviceSessionCore


PARA_AUTO_FALLBACK_MS = 1500
PARA_SET_TIMEOUT_MS = 10000
PARA_RESET_TIMEOUT_MS = 15000
PARA_READ_TIMEOUT_MS = 10000
PARA_VERIFY_DELAY_MS = 300


class ParameterCapabilityState(str, Enum):
    DISCONNECTED = "disconnected"
    WAITING_PROFILE = "waiting_profile"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class ParameterOperation(str, Enum):
    IDLE = "idle"
    READING = "reading"
    WRITING = "writing"
    VERIFYING = "verifying"
    RESETTING = "resetting"


class ParameterStatus(str, Enum):
    IDLE = "idle"
    WAITING_PROFILE = "waiting_profile"
    WAITING_CAPABILITY = "waiting_capability"
    UNSUPPORTED = "unsupported"
    TRANSACTION_ACTIVE = "transaction_active"
    READING = "reading"
    READ_SEND_FAILED = "read_send_failed"
    READ_TIMEOUT = "read_timeout"
    READ_DEFERRED = "read_deferred"
    WRITE_AWAITING = "write_awaiting"
    WRITE_SEND_FAILED = "write_send_failed"
    WRITE_WAITING_READBACK = "write_waiting_readback"
    WRITE_READBACK_SEND_FAILED = "write_readback_send_failed"
    WRITE_SUCCESS = "write_success"
    WRITE_NOT_READ_BACK = "write_not_read_back"
    WRITE_ERROR = "write_error"
    SESSION_CHANGED = "session_changed"
    RESET_SUCCESS = "reset_success"
    RESET_FAILED = "reset_failed"
    RESET_TIMEOUT = "reset_timeout"


class ParameterController(QObject):
    """Own capability gating, parameter commands, ACK matching, and readback."""

    capability_changed = Signal(object)
    operation_changed = Signal(object)
    table_received = Signal(object)
    status_changed = Signal(object, object)
    parameter_status_changed = Signal(str, object, object)

    def __init__(
        self,
        session: DeviceSessionCore,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._lease_token = object()
        self._connected = session.connected
        self._hardware = session.profile_store.current_hw_type()
        self._capability = ParameterCapabilityState.DISCONNECTED
        self._operation = ParameterOperation.IDLE
        self._read_pending = False
        self._loaded_hardware: Optional[str] = None
        self._auto_read_hardware: Optional[str] = None
        self._pending_name: Optional[str] = None
        self._pending_value: Optional[str] = None
        self._pending_type: Optional[int] = None

        self._response_timer = QTimer(self)
        self._response_timer.setSingleShot(True)
        self._response_timer.timeout.connect(self._on_response_timeout)
        self._read_timer = QTimer(self)
        self._read_timer.setSingleShot(True)
        self._read_timer.timeout.connect(self._on_read_timeout)
        self._verify_timer = QTimer(self)
        self._verify_timer.setSingleShot(True)
        self._verify_timer.timeout.connect(self._request_verify_table)

        session.connection_changed.connect(self.set_connected)
        session.generation_changed.connect(self._on_generation_changed)
        session.record_received.connect(self.feed_record)
        session.profile_store.profile_changed.connect(self._on_profile_changed)
        self.destroyed.connect(
            lambda _obj=None, session=session, token=self._lease_token: session.release_device_transaction(token)
        )
        self._refresh_capability()

    @property
    def capability_state(self) -> ParameterCapabilityState:
        return self._capability

    @property
    def operation(self) -> ParameterOperation:
        return self._operation

    @property
    def supported(self) -> bool:
        return self._capability is ParameterCapabilityState.SUPPORTED

    @property
    def read_pending(self) -> bool:
        return self._read_pending

    @property
    def pending_name(self) -> Optional[str]:
        return self._pending_name

    @property
    def pending_value(self) -> Optional[str]:
        return self._pending_value

    @property
    def pending_type(self) -> Optional[int]:
        return self._pending_type

    @Slot(bool)
    def set_connected(self, connected: bool) -> None:
        connected = bool(connected)
        if self._connected == connected:
            return
        self._connected = connected
        if not connected:
            self._hardware = None
            self._loaded_hardware = None
            self._auto_read_hardware = None
            self._refresh_capability()
            self._finish_session_change()
            return
        self._refresh_capability()

    @Slot(str)
    def _on_profile_changed(self, hardware: str) -> None:
        if hardware:
            self._hardware = hardware
        self._refresh_capability()

    @Slot(int, object)
    def _on_generation_changed(self, _generation: int, _endpoint: object) -> None:
        self._finish_session_change()

    def _finish_session_change(self) -> None:
        """Publish one terminal fact for every operation owned by the old link."""

        if self._operation is ParameterOperation.IDLE and not self._read_pending:
            return
        name = self._pending_name
        if name:
            self.parameter_status_changed.emit(
                name,
                ParameterStatus.SESSION_CHANGED,
                {},
            )
        else:
            self.status_changed.emit(ParameterStatus.SESSION_CHANGED, {})
        self._clear_pending()

    @Slot(object)
    def feed_record(self, record: object) -> None:
        if isinstance(record, MetaInfo):
            self._hardware = record.hw_type or None
            self._refresh_capability()
        elif isinstance(record, ParaTableReport):
            self._apply_table(record)
        elif isinstance(record, CommandResponse):
            self._apply_response(record)

    def request_table(self, *, allow_during_write: bool = False) -> bool:
        if not self.supported:
            self._emit_capability_status()
            return False
        if self._operation in {ParameterOperation.WRITING, ParameterOperation.VERIFYING}:
            if not allow_during_write:
                self.status_changed.emit(ParameterStatus.READ_DEFERRED, {})
                return False
        if self._read_pending:
            self.status_changed.emit(ParameterStatus.READING, {})
            return True
        if not self._session.try_acquire_device_transaction(self._lease_token):
            self.status_changed.emit(ParameterStatus.TRANSACTION_ACTIVE, {})
            return False
        if not self._session.send(build_request_para_table()):
            if self._operation is ParameterOperation.VERIFYING:
                if self._pending_name:
                    self.parameter_status_changed.emit(
                        self._pending_name,
                        ParameterStatus.WRITE_READBACK_SEND_FAILED,
                        {},
                    )
                self._clear_pending()
            else:
                self._session.release_device_transaction(self._lease_token)
                self.status_changed.emit(ParameterStatus.READ_SEND_FAILED, {})
            return False
        self._read_pending = True
        if self._operation is ParameterOperation.IDLE:
            self._set_operation(ParameterOperation.READING)
        self.status_changed.emit(ParameterStatus.READING, {})
        self._read_timer.start(PARA_READ_TIMEOUT_MS)
        return True

    def write(self, name: str, para_type: int, value: str) -> bool:
        if not self.supported:
            self._emit_capability_status()
            return False
        if self._operation is not ParameterOperation.IDLE:
            return False
        try:
            frame = build_para_set(str(name), str(value))
        except CodecError:
            self.parameter_status_changed.emit(
                str(name),
                ParameterStatus.WRITE_SEND_FAILED,
                {},
            )
            return False
        if not self._session.try_acquire_device_transaction(self._lease_token):
            self.status_changed.emit(ParameterStatus.TRANSACTION_ACTIVE, {})
            return False
        self._pending_name = str(name)
        self._pending_value = str(value)
        self._pending_type = int(para_type)
        self._set_operation(ParameterOperation.WRITING)
        self.parameter_status_changed.emit(
            self._pending_name,
            ParameterStatus.WRITE_AWAITING,
            {},
        )
        if not self._session.send(frame):
            self.parameter_status_changed.emit(
                self._pending_name,
                ParameterStatus.WRITE_SEND_FAILED,
                {},
            )
            self._clear_pending()
            return False
        self._response_timer.start(PARA_SET_TIMEOUT_MS)
        return True

    def reset_parameters(self) -> bool:
        if not self.supported:
            self._emit_capability_status()
            return False
        if self._operation is not ParameterOperation.IDLE:
            return False
        if not self._session.try_acquire_device_transaction(self._lease_token):
            self.status_changed.emit(ParameterStatus.TRANSACTION_ACTIVE, {})
            return False
        if not self._session.send(build_para_reset()):
            self._session.release_device_transaction(self._lease_token)
            self.status_changed.emit(ParameterStatus.RESET_FAILED, {"detail": "send_failed"})
            return False
        self._set_operation(ParameterOperation.RESETTING)
        self._response_timer.start(PARA_RESET_TIMEOUT_MS)
        return True

    def reset_runtime(self) -> None:
        self._hardware = None
        self._loaded_hardware = None
        self._auto_read_hardware = None
        self._clear_pending()
        self._refresh_capability()

    def _refresh_capability(self) -> None:
        previous = self._capability
        if not self._connected:
            capability = ParameterCapabilityState.DISCONNECTED
        elif not self._hardware:
            capability = ParameterCapabilityState.WAITING_PROFILE
        else:
            profile = self._session.profile_store
            parameter = profile.capability_status(self._hardware, "parameters")
            context = profile.capability_status(
                self._hardware,
                "command_response_context",
            )
            if all(
                status is CapabilitySupport.SUPPORTED
                for status in (parameter, context)
            ):
                capability = ParameterCapabilityState.SUPPORTED
            elif CapabilitySupport.UNKNOWN in {parameter, context}:
                capability = ParameterCapabilityState.WAITING_PROFILE
            else:
                capability = ParameterCapabilityState.UNSUPPORTED
        self._capability = capability
        if previous is not capability:
            self.capability_changed.emit(capability)
        self._emit_capability_status()
        if capability is ParameterCapabilityState.SUPPORTED:
            self._schedule_auto_read()

    def _emit_capability_status(self) -> None:
        status = {
            ParameterCapabilityState.DISCONNECTED: ParameterStatus.IDLE,
            ParameterCapabilityState.WAITING_PROFILE: (
                ParameterStatus.WAITING_PROFILE
                if not self._hardware
                else ParameterStatus.WAITING_CAPABILITY
            ),
            ParameterCapabilityState.SUPPORTED: ParameterStatus.IDLE,
            ParameterCapabilityState.UNSUPPORTED: ParameterStatus.UNSUPPORTED,
        }[self._capability]
        self.status_changed.emit(status, {})

    def _schedule_auto_read(self) -> None:
        hardware = self._hardware
        if not hardware or self._auto_read_hardware == hardware:
            return
        self._auto_read_hardware = hardware
        QTimer.singleShot(
            PARA_AUTO_FALLBACK_MS,
            lambda expected=hardware: self._run_auto_read(expected),
        )

    def _run_auto_read(self, expected_hardware: str) -> None:
        if (
            self._hardware != expected_hardware
            or not self.supported
            or self._loaded_hardware == expected_hardware
            or self._read_pending
        ):
            return
        self.request_table()

    def _apply_table(self, report: ParaTableReport) -> None:
        self._read_pending = False
        self._read_timer.stop()
        if self._hardware:
            self._loaded_hardware = self._hardware
        self.table_received.emit(report)
        if self._operation in {ParameterOperation.WRITING, ParameterOperation.VERIFYING}:
            self._verify_write(report)
        elif self._operation is ParameterOperation.READING:
            self._set_operation(ParameterOperation.IDLE)
            self._session.release_device_transaction(self._lease_token)
            self.status_changed.emit(ParameterStatus.IDLE, {})

    def _verify_write(self, report: ParaTableReport) -> None:
        name = self._pending_name
        if not name:
            self._clear_pending()
            return
        target = next((item for item in report.params if item.name == name), None)
        if (
            target is not None
            and self._pending_value is not None
            and self._values_match(
                self._pending_type if self._pending_type is not None else target.para_type,
                target.value,
                self._pending_value,
            )
        ):
            self.parameter_status_changed.emit(
                name,
                ParameterStatus.WRITE_SUCCESS,
                {},
            )
            self._clear_pending()
            return
        self.parameter_status_changed.emit(
            name,
            ParameterStatus.WRITE_NOT_READ_BACK,
            {},
        )
        self._clear_pending()

    def _apply_response(self, response: CommandResponse) -> None:
        if self._operation is ParameterOperation.WRITING:
            self._apply_write_response(response)
        elif self._operation is ParameterOperation.RESETTING:
            self._apply_reset_response(response)

    def _apply_write_response(self, response: CommandResponse) -> None:
        name = self._pending_name
        if not name:
            return
        expected = f"PARA_SET={name}"
        if (response.msg or "").strip() != expected:
            return
        if int(response.code) == int(RespCode.SUCCESS):
            self._response_timer.stop()
            self._set_operation(ParameterOperation.VERIFYING)
            self.parameter_status_changed.emit(
                name,
                ParameterStatus.WRITE_WAITING_READBACK,
                {},
            )
            self._verify_timer.start(PARA_VERIFY_DELAY_MS)
            return
        detail = response.msg or str(response.code)
        self._response_timer.stop()
        self.parameter_status_changed.emit(
            name,
            ParameterStatus.WRITE_ERROR,
            {"detail": detail},
        )
        self._clear_pending()

    def _apply_reset_response(self, response: CommandResponse) -> None:
        context = (response.msg or "").strip()
        if not context.startswith("PARA_RESET="):
            return
        if int(response.code) == int(RespCode.SUCCESS):
            if context != "PARA_RESET=OK":
                return
            self._response_timer.stop()
            self._set_operation(ParameterOperation.IDLE)
            self._session.release_device_transaction(self._lease_token)
            self.status_changed.emit(ParameterStatus.RESET_SUCCESS, {})
            QTimer.singleShot(500, self.request_table)
            return
        self._response_timer.stop()
        self._set_operation(ParameterOperation.IDLE)
        self._session.release_device_transaction(self._lease_token)
        self.status_changed.emit(
            ParameterStatus.RESET_FAILED,
            {"detail": response.msg or str(response.code)},
        )

    @Slot()
    def _request_verify_table(self) -> None:
        if self._operation is ParameterOperation.VERIFYING:
            self.request_table(allow_during_write=True)

    @Slot()
    def _on_response_timeout(self) -> None:
        if self._operation in {ParameterOperation.WRITING, ParameterOperation.VERIFYING}:
            if self._pending_name:
                self.parameter_status_changed.emit(
                    self._pending_name,
                    ParameterStatus.WRITE_NOT_READ_BACK,
                    {},
                )
            self._clear_pending()
        elif self._operation is ParameterOperation.RESETTING:
            self._set_operation(ParameterOperation.IDLE)
            self._session.release_device_transaction(self._lease_token)
            self.status_changed.emit(ParameterStatus.RESET_TIMEOUT, {})

    @Slot()
    def _on_read_timeout(self) -> None:
        if not self._read_pending:
            return
        self._read_pending = False
        if self._operation is ParameterOperation.VERIFYING:
            if self._pending_name:
                self.parameter_status_changed.emit(
                    self._pending_name,
                    ParameterStatus.WRITE_NOT_READ_BACK,
                    {},
                )
            self._clear_pending()
            return
        self._set_operation(ParameterOperation.IDLE)
        self._session.release_device_transaction(self._lease_token)
        self.status_changed.emit(ParameterStatus.READ_TIMEOUT, {})

    def _clear_pending(self) -> None:
        self._response_timer.stop()
        self._read_timer.stop()
        self._verify_timer.stop()
        self._read_pending = False
        self._pending_name = None
        self._pending_value = None
        self._pending_type = None
        self._set_operation(ParameterOperation.IDLE)
        self._session.release_device_transaction(self._lease_token)

    def _set_operation(self, operation: ParameterOperation) -> None:
        if self._operation is operation:
            return
        self._operation = operation
        self.operation_changed.emit(operation)

    @staticmethod
    def _values_match(para_type: int, actual: str, expected: str) -> bool:
        if int(para_type) == int(ParaType.FLOAT):
            try:
                return abs(float(actual) - float(expected)) < 1e-4
            except ValueError:
                return actual.strip() == expected.strip()
        if int(para_type) in {
            int(ParaType.INT),
            int(ParaType.UINT8),
            int(ParaType.INT8),
            int(ParaType.UINT16),
            int(ParaType.INT16),
        }:
            try:
                return int(actual, 0) == int(expected, 0)
            except ValueError:
                return actual.strip() == expected.strip()
        return actual.strip() == expected.strip()


__all__ = [
    "PARA_AUTO_FALLBACK_MS",
    "PARA_READ_TIMEOUT_MS",
    "PARA_RESET_TIMEOUT_MS",
    "PARA_SET_TIMEOUT_MS",
    "ParameterCapabilityState",
    "ParameterController",
    "ParameterOperation",
    "ParameterStatus",
]
