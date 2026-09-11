"""Typed command controllers attached to one DeviceSessionCore."""

from __future__ import annotations

from enum import Enum
import time
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from satellite_debug_tool.core.link_trace import trace_message
from satellite_debug_tool.core.protocol import (
    CommandResponse,
    RespCode,
    build_channel_enable_mask,
    build_debug_enable_v2,
    build_reset_stats,
    build_set_sample_rate,
    build_set_trace_mode,
    build_user_mark,
)

from .device_session import DeviceSessionCore
from .command_sender import (
    SessionCommandSender,
    SessionOperationClass,
    SessionOperationGateway,
    operation_gateway_or_legacy,
)


DEBUG_ACK_TIMEOUT_MS = 3000
DEBUG_LATE_ACK_WINDOW_S = 3.0


class DebugRequestResult(str, Enum):
    ACK = "ack"
    LATE_ACK = "late_ack"
    ALREADY_CONFIRMED = "already_confirmed"
    NOT_CONNECTED = "not_connected"
    BUSY = "busy"
    SEND_FAILED = "send_failed"
    DEVICE_ERROR = "device_error"
    TIMEOUT = "timeout"


def parse_debug_ack_target(message: str) -> bool | None:
    text = (message or "").strip()
    if text == "DEBUG_ENABLE=1":
        return True
    if text == "DEBUG_ENABLE=0":
        return False
    return None


class DebugController(QObject):
    """Apply strict contextual ACK semantics for Debug ON/OFF."""

    state_changed = Signal(bool)
    pending_changed = Signal(object)
    request_finished = Signal(bool, bool, str)

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
        self._connected = False
        self._enabled = False
        self._pending_target: bool | None = None
        self._last_requested_target: bool | None = None
        self._last_request_at = 0.0
        self._ack_timer = QTimer(self)
        self._ack_timer.setSingleShot(True)
        self._ack_timer.timeout.connect(self._on_timeout)
        session.command_response.connect(self.feed_response)
        self.destroyed.connect(self._on_destroyed)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def pending_target(self) -> bool | None:
        return self._pending_target

    def set_connected(self, connected: bool) -> None:
        self._connected = bool(connected)
        if not self._connected:
            self.reset()

    def reset(self) -> None:
        state_changed = self._enabled
        pending_changed = self._pending_target is not None
        self._enabled = False
        self._ack_timer.stop()
        if pending_changed:
            self._pending_target = None
            self._operation_gateway.release_operation(self._lease_token)
            self.pending_changed.emit(None)
        else:
            self._operation_gateway.release_operation(self._lease_token)
        self._last_requested_target = None
        self._last_request_at = 0.0
        if state_changed:
            self.state_changed.emit(False)

    def apply_confirmed_state(self, target: bool) -> None:
        """Apply state proven by another contextual device response."""
        self._set_state(bool(target))

    def request(self, target: bool) -> None:
        requested = bool(target)
        if not self._connected:
            self.request_finished.emit(
                requested,
                False,
                DebugRequestResult.NOT_CONNECTED.value,
            )
            return
        if self._pending_target is not None:
            self.request_finished.emit(requested, False, DebugRequestResult.BUSY.value)
            return
        if self._enabled == requested:
            self.request_finished.emit(
                requested,
                True,
                DebugRequestResult.ALREADY_CONFIRMED.value,
            )
            return
        if not self._operation_gateway.try_acquire_operation(
            self._lease_token,
            purpose="debug-enable",
        ):
            self.request_finished.emit(requested, False, DebugRequestResult.BUSY.value)
            return
        self._pending_target = requested
        self._last_requested_target = requested
        self._last_request_at = time.monotonic()
        if not self._operation_gateway.update_operation_context(
            self._lease_token,
            request_id=f"DEBUG_ENABLE:{int(requested)}",
            target_facts=(("debug_enabled", requested),),
        ):
            self._pending_target = None
            self._last_requested_target = None
            self._last_request_at = 0.0
            self._operation_gateway.release_operation(self._lease_token)
            self.request_finished.emit(
                requested,
                False,
                DebugRequestResult.SEND_FAILED.value,
            )
            return
        self.pending_changed.emit(requested)
        self._ack_timer.start(DEBUG_ACK_TIMEOUT_MS)
        if not self._command_sender.send(
            build_debug_enable_v2(requested),
            operation=SessionOperationClass.MUTATING,
        ):
            self._pending_target = None
            self._ack_timer.stop()
            self._operation_gateway.release_operation(self._lease_token)
            self.pending_changed.emit(None)
            self.request_finished.emit(
                requested,
                False,
                DebugRequestResult.SEND_FAILED.value,
            )
            return
        trace_message("DBG_CTRL", f"send DEBUG_ENABLE target={int(requested)}")

    def set_sample_rate(self, hz: int) -> bool:
        return self._send_one_shot_mutation(
            build_set_sample_rate(int(hz)),
            purpose="debug-set-sample-rate",
        )

    def set_channel_enable_mask(self, mask: int) -> bool:
        return self._send_one_shot_mutation(
            build_channel_enable_mask(int(mask)),
            purpose="debug-set-channel-enable-mask",
        )

    def reset_statistics(self) -> bool:
        return self._send_one_shot_mutation(
            build_reset_stats(),
            purpose="debug-reset-statistics",
        )

    def set_trace_mode(self, mode: int) -> bool:
        return self._send_one_shot_mutation(
            build_set_trace_mode(int(mode)),
            purpose="debug-set-trace-mode",
        )

    def send_user_mark(self, mark_id: int, text: str) -> bool:
        return self._send_one_shot_mutation(
            build_user_mark(int(mark_id), str(text)),
            purpose="debug-user-mark",
        )

    def _send_one_shot_mutation(self, frame: bytes, *, purpose: str) -> bool:
        """Send only on an explicit legacy boundary that accepts sent-only facts."""

        if self._pending_target is not None:
            return False
        if not self._operation_gateway.allows_unconfirmed_mutation():
            return False
        if not self._operation_gateway.try_acquire_operation(
            self._lease_token,
            purpose=purpose,
        ):
            return False
        try:
            return self._command_sender.send(
                frame,
                operation=SessionOperationClass.MUTATING,
            )
        finally:
            self._operation_gateway.release_operation(self._lease_token)

    def send_shutdown_notice(self) -> bool:
        """Best-effort OFF command before the transport is closed."""

        return self._command_sender.send(
            build_debug_enable_v2(False),
            operation=SessionOperationClass.TERMINAL,
        )

    @Slot(object)
    def feed_response(self, response: CommandResponse) -> None:
        target = self._pending_target
        ack_target = parse_debug_ack_target(response.msg or "")
        if target is None:
            if ack_target is None:
                return
            age = time.monotonic() - self._last_request_at
            trace_message(
                "DBG_CTRL",
                f"rx late debug ack target={int(ack_target)} code={response.code} "
                f"msg={response.msg!r} age={age:.3f}s",
            )
            if (
                int(response.code) == int(RespCode.SUCCESS)
                and self._last_requested_target == ack_target
                and 0.0 <= age <= DEBUG_LATE_ACK_WINDOW_S
            ):
                self._set_state(ack_target)
            return

        trace_message(
            "DBG_CTRL",
            f"rx command_response while pending target={int(target)}: "
            f"code={response.code} msg={response.msg!r}",
        )
        if ack_target is None:
            debug_error = (
                "DEBUG_ENABLE" in (response.msg or "").upper()
                and int(response.code) != int(RespCode.SUCCESS)
            )
            if debug_error:
                self._finish(
                    False,
                    f"{DebugRequestResult.DEVICE_ERROR.value}:{response.msg or response.code}",
                )
            return
        if ack_target != target:
            return
        if int(response.code) != int(RespCode.SUCCESS):
            self._finish(
                False,
                f"{DebugRequestResult.DEVICE_ERROR.value}:{response.msg or response.code}",
            )
            return
        self._finish(True, DebugRequestResult.ACK.value)

    @Slot()
    def _on_timeout(self) -> None:
        if self._pending_target is None:
            return
        trace_message("DBG_CTRL", f"ack timeout target={int(self._pending_target)}")
        self._finish_unknown(DebugRequestResult.TIMEOUT.value)

    def _finish_unknown(self, result: str) -> None:
        target = self._pending_target
        if target is None:
            return
        self._ack_timer.stop()
        self._pending_target = None
        self._operation_gateway.release_operation(self._lease_token)
        self.pending_changed.emit(None)
        self.request_finished.emit(target, False, result)

    def _finish(self, ok: bool, result: str) -> None:
        target = self._pending_target
        self._pending_target = None
        self._ack_timer.stop()
        self.pending_changed.emit(None)
        self._operation_gateway.release_operation(self._lease_token)
        if ok and target is not None:
            self._set_state(target)
        if target is not None:
            self.request_finished.emit(target, ok, result)

    def _set_state(self, target: bool) -> None:
        target = bool(target)
        if self._enabled == target:
            return
        self._enabled = target
        trace_message("DBG_CTRL", f"debug state confirmed target={int(target)}")
        self.state_changed.emit(target)

    @Slot(object)
    def _on_destroyed(self, _obj=None) -> None:
        self._pending_target = None
        self._operation_gateway.release_operation(self._lease_token)
