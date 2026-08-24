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
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._connected = False
        self._enabled = False
        self._pending_target: bool | None = None
        self._last_requested_target: bool | None = None
        self._last_request_at = 0.0
        self._ack_timer = QTimer(self)
        self._ack_timer.setSingleShot(True)
        self._ack_timer.timeout.connect(self._on_timeout)
        session.command_response.connect(self.feed_response)

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
        self._pending_target = None
        self._last_requested_target = None
        self._last_request_at = 0.0
        self._ack_timer.stop()
        if pending_changed:
            self.pending_changed.emit(None)
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
        self._pending_target = requested
        self._last_requested_target = requested
        self._last_request_at = time.monotonic()
        self.pending_changed.emit(requested)
        self._ack_timer.start(DEBUG_ACK_TIMEOUT_MS)
        if not self._session.send(build_debug_enable_v2(requested)):
            self._pending_target = None
            self._ack_timer.stop()
            self.pending_changed.emit(None)
            self.request_finished.emit(
                requested,
                False,
                DebugRequestResult.SEND_FAILED.value,
            )
            return
        trace_message("DBG_CTRL", f"send DEBUG_ENABLE target={int(requested)}")

    def set_sample_rate(self, hz: int) -> bool:
        return self._session.send(build_set_sample_rate(int(hz)))

    def set_channel_enable_mask(self, mask: int) -> bool:
        return self._session.send(build_channel_enable_mask(int(mask)))

    def reset_statistics(self) -> bool:
        return self._session.send(build_reset_stats())

    def set_trace_mode(self, mode: int) -> bool:
        return self._session.send(build_set_trace_mode(int(mode)))

    def send_user_mark(self, mark_id: int, text: str) -> bool:
        return self._session.send(build_user_mark(int(mark_id), str(text)))

    def send_shutdown_notice(self) -> bool:
        """Best-effort OFF command before the transport is closed."""

        return self._session.send(build_debug_enable_v2(False))

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
        self._finish(False, DebugRequestResult.TIMEOUT.value)

    def _finish(self, ok: bool, result: str) -> None:
        target = self._pending_target
        self._pending_target = None
        self._ack_timer.stop()
        self.pending_changed.emit(None)
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
