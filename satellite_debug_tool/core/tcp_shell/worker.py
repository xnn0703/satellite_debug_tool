"""Raw shell session worker: single link owner for shell bytes and YMODEM (M37/M45).

The worker is the only read/write owner of the byte-stream link (TCP socket or
serial port). Shell bytes enter the session's bounded :class:`RxRing`; the UI
consumes them frame by frame and the worker emits one ``rx_pending`` per
notification edge. YMODEM mode feeds the sender state machine directly so
binary control bytes never pass through the ring.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import queue
import threading
import time
from typing import Optional

from PySide6.QtCore import QThread, Signal

from .recorder import TcpShellSessionRecorder
from .rx_bridge import DEFAULT_CAPACITY_BYTES, DEFAULT_WAIT_SEGMENT_S, PushStatus, RxRing
from .transport import ShellLink, ShellLinkError, create_link
from .ymodem_sender import (
    YmodemFileSpec,
    YmodemSendOutcome,
    YmodemSender,
    YmodemTransport,
)

_RB_COMMAND = b"rb\r"
_ECHO_DRAIN_S = 0.4


class TcpShellWorkerState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    YMODEM = "ymodem"


@dataclass
class _ConnectCommand:
    pass


@dataclass
class _DisconnectCommand:
    reason: str = ""


@dataclass
class _SendTextCommand:
    payload: bytes


@dataclass
class _ClearRxCommand:
    pass


@dataclass
class _StartYmodemCommand:
    spec: YmodemFileSpec


class _LinkClosed(RuntimeError):
    pass


class _PumpDisposition(str, Enum):
    """命令泵在入环等待片段间遇到的中断类命令。"""

    DISCONNECT = "disconnect"
    CLEAR_RX = "clear-rx"


class _LinkYmodemTransport(YmodemTransport):
    """Bind the YMODEM sender to the worker-owned link."""

    def __init__(self, worker: "TcpShellWorker") -> None:
        self._worker = worker

    def send(self, data: bytes) -> None:
        self._worker._send_bytes(data)

    def recv(self, timeout_s: float) -> bytes:
        return self._worker._recv_bytes(timeout_s)

    def cancel_requested(self) -> bool:
        return self._worker._ymodem_cancel.is_set()


class TcpShellWorker(QThread):
    """Own the shell link, the YMODEM transfer and the session recorder."""

    connected = Signal(str)
    connect_failed = Signal(str)
    disconnected = Signal(str)
    state_changed = Signal(str)
    rx_pending = Signal()
    ymodem_progress = Signal(object)
    ymodem_finished = Signal(object)
    session_started = Signal(str)

    def __init__(
        self,
        *,
        target: dict,
        managed_session: bool,
        operator: str = "",
        recorder: Optional[TcpShellSessionRecorder] = None,
        session_root: Optional[Path] = None,
        connect_timeout_s: float = 3.0,
        sender: Optional[YmodemSender] = None,
        rx_ring: Optional[RxRing] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._target = dict(target)
        self._managed_session = bool(managed_session)
        self._operator = operator
        self._recorder = recorder
        self._session_root = session_root
        self._connect_timeout_s = float(connect_timeout_s)
        # None 时在 _run_ymodem 里构造带进度/证据回调的 sender；
        # 此处不能预建默认实例，否则会吞掉传输期回调（进度恒为 0 的根因）。
        self._sender = sender
        self._commands: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._ymodem_cancel = threading.Event()
        self._link: Optional[ShellLink] = None
        self._state = TcpShellWorkerState.DISCONNECTED
        self._last_ymodem_payload: Optional[dict] = None
        # 每条连接一个 ring；_connect 成功时按同一参数重建，禁止跨连接复用。
        self._rx_capacity = rx_ring.capacity_bytes if rx_ring else DEFAULT_CAPACITY_BYTES
        self._rx_wait_segment = rx_ring.wait_segment_s if rx_ring else DEFAULT_WAIT_SEGMENT_S
        self.rx_ring = rx_ring or RxRing(
            capacity_bytes=self._rx_capacity, wait_segment_s=self._rx_wait_segment
        )

    # -- UI-thread API ---------------------------------------------------------

    @property
    def state(self) -> TcpShellWorkerState:
        return self._state

    def submit_connect(self) -> None:
        self._commands.put(_ConnectCommand())

    def submit_disconnect(self, reason: str = "") -> None:
        self._commands.put(_DisconnectCommand(reason))

    def submit_send_raw(self, payload: bytes) -> None:
        self._commands.put(_SendTextCommand(bytes(payload)))

    def submit_clear_rx(self) -> None:
        self._commands.put(_ClearRxCommand())

    def submit_start_ymodem(self, spec: YmodemFileSpec) -> None:
        self._commands.put(_StartYmodemCommand(spec))

    def cancel_ymodem(self) -> None:
        """Thread-safe cancel; the sender notices within one recv timeout."""
        self._ymodem_cancel.set()

    def stop_and_join(self, timeout_ms: int = 5000) -> None:
        self._stop_event.set()
        self._ymodem_cancel.set()
        self.rx_ring.wake_stop()
        self.submit_disconnect("worker-stop")
        self.wait(timeout_ms)

    # -- worker-thread loop ----------------------------------------------------

    def run(self) -> None:  # noqa: C901 - single owner loop with clear stages
        try:
            while not self._stop_event.is_set():
                try:
                    command = self._commands.get(timeout=0.1)
                except queue.Empty:
                    continue
                if isinstance(command, _ConnectCommand):
                    if self._link is not None:
                        continue
                    if self._connect():
                        self._shell_loop()
                elif isinstance(command, _DisconnectCommand):
                    break
        finally:
            self._close_link()
            self._finalize_recorder(reason="worker-stopped")
            self._set_state(TcpShellWorkerState.DISCONNECTED)

    def _connect(self) -> bool:
        self._set_state(TcpShellWorkerState.CONNECTING)
        try:
            link = create_link(self._target, connect_timeout_s=self._connect_timeout_s)
            link.open()
        except ShellLinkError as exc:
            self._set_state(TcpShellWorkerState.DISCONNECTED)
            self.connect_failed.emit(str(exc))
            return False
        self._link = link
        self.rx_ring = RxRing(
            capacity_bytes=self._rx_capacity, wait_segment_s=self._rx_wait_segment
        )
        if self._recorder is None:
            self._recorder = TcpShellSessionRecorder(
                target=self._target,
                managed_session=self._managed_session,
                operator=self._operator,
                root=self._session_root,
            )
        try:
            session_dir = self._recorder.start()
        except Exception:
            self._close_link()
            self._set_state(TcpShellWorkerState.DISCONNECTED)
            self.connect_failed.emit("session evidence directory unavailable")
            return False
        self._set_state(TcpShellWorkerState.CONNECTED)
        self.session_started.emit(self._recorder.session_id)
        self.connected.emit(link.label)
        self._record_event("connected", {"target": dict(self._target), "label": link.label})
        return True

    def _shell_loop(self) -> None:
        close_reason = "closed"
        residual = b""
        pump_disconnect_reason: Optional[str] = None
        while not self._stop_event.is_set():
            try:
                data = self._recv_bytes(0.05)
            except _LinkClosed as exc:
                close_reason = str(exc) or "link-closed"
                break
            abort, remaining, reason = self._feed_ring(data)
            if abort == "disconnect":
                residual = remaining
                pump_disconnect_reason = reason
                break
            if abort == "stop":
                residual = remaining
                break
            # abort == "clear-rx"：后缀已按清屏代际丢弃，继续常规命令处理
            exit_hint = self._handle_commands()
            if exit_hint == "disconnect":
                return  # 命令式断开已在 _handle_commands 内完成收尾
            if exit_hint is not None:
                close_reason = exit_hint
                break
        self._hand_over_residual(residual)
        if pump_disconnect_reason is not None:
            self._disconnect_now(pump_disconnect_reason)
            return
        self._close_link()
        self._finalize_recorder(reason=close_reason)
        self._emit_disconnected(close_reason)

    def _handle_commands(self) -> Optional[str]:
        """处理命令队列；返回退出原因（disconnect 直接完成收尾并返回）。"""

        close_hint: Optional[str] = None
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                break
            if isinstance(command, _SendTextCommand):
                try:
                    self._send_bytes(command.payload)
                except _LinkClosed:
                    pass
            elif isinstance(command, _StartYmodemCommand):
                self._run_ymodem(command.spec)
                if self._link is None:
                    close_hint = "link-closed-during-ymodem"
            elif isinstance(command, _ClearRxCommand):
                self._clear_rx_ring()
            elif isinstance(command, _DisconnectCommand):
                self._record_event("disconnect_requested", {"reason": command.reason})
                self._commands.put(command)  # let the outer loop exit too
                self._close_link()
                self._finalize_recorder(reason=command.reason or "operator")
                self._emit_disconnected(command.reason or "operator")
                return "disconnect"
            elif isinstance(command, _ConnectCommand):
                continue
        return close_hint

    # -- rx ring feeding (worker thread) ------------------------------------------

    def _feed_ring(self, data: bytes) -> tuple[Optional[str], bytes, str]:
        """把已读字节入环；容量等待片段间泵命令。

        返回 (中断原因, 未入环后缀, 断开原因)：stop/disconnect 需移交后缀；
        clear-rx 的后缀已按清屏代际丢弃，返回空后缀。
        """

        remaining = data
        while remaining:
            result = self.rx_ring.push(remaining)
            if result.notify:
                self.rx_pending.emit()
            remaining = remaining[result.accepted:]
            if result.status is PushStatus.STOPPED:
                return "stop", remaining, ""
            if result.status is PushStatus.RETRY:
                disposition, reason = self._pump_commands_in_push()
                if disposition is _PumpDisposition.DISCONNECT:
                    return "disconnect", remaining, reason
                if disposition is _PumpDisposition.CLEAR_RX:
                    self._clear_rx_ring()
                    return "clear-rx", b"", ""
        return None, b"", ""

    def _pump_commands_in_push(self) -> tuple[Optional[_PumpDisposition], str]:
        """入环等待片段间的命令泵：发送立即执行，YMODEM 推迟，断开/清屏中断。"""

        stashed: list = []
        disposition: Optional[_PumpDisposition] = None
        reason = ""
        while disposition is None:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                break
            if isinstance(command, _SendTextCommand):
                try:
                    self._send_bytes(command.payload)
                except _LinkClosed:
                    pass
            elif isinstance(command, _StartYmodemCommand):
                stashed.append(command)  # 不在入环等待栈内执行；回队由外层调度
            elif isinstance(command, _ClearRxCommand):
                disposition = _PumpDisposition.CLEAR_RX
            elif isinstance(command, _DisconnectCommand):
                self._commands.put(command)  # let the outer loop exit too
                reason = command.reason or "operator"
                disposition = _PumpDisposition.DISCONNECT
            elif isinstance(command, _ConnectCommand):
                continue
        for command in stashed:
            self._commands.put(command)
        return disposition, reason

    def _clear_rx_ring(self) -> None:
        """清屏代际：丢弃已入环字节并推进代际；在途后缀由调用方丢弃。"""

        if self.rx_ring.clear():
            self.rx_pending.emit()

    def _hand_over_residual(self, suffix: bytes) -> None:
        """停止/退出路径的残余移交：已读未入环后缀非阻塞入环，生产就此结束。"""

        if self.rx_ring.push_final(suffix):
            self.rx_pending.emit()

    def _disconnect_now(self, reason: str) -> None:
        self._record_event("disconnect_requested", {"reason": reason})
        self._close_link()
        self._finalize_recorder(reason=reason)
        self._emit_disconnected(reason)

    def _run_ymodem(self, spec: YmodemFileSpec) -> None:
        recorder = self._recorder
        self._ymodem_cancel.clear()
        self._set_state(TcpShellWorkerState.YMODEM)
        self._record_event(
            "ymodem_started",
            {"filename": spec.filename, "size": spec.size, "sha256": spec.sha256},
        )
        if recorder is not None:
            recorder.record_ymodem(
                {"stage": "start", "filename": spec.filename, "size": spec.size}
            )
        try:
            self._send_bytes(_RB_COMMAND)
        except _LinkClosed as exc:
            self._finish_ymodem(
                spec,
                YmodemSendOutcome.ERROR,
                0,
                0,
                0.0,
                error=str(exc) or "link-closed",
            )
            return
        self._drain_echo(_ECHO_DRAIN_S)
        if self._link is None:
            self._finish_ymodem(
                spec, YmodemSendOutcome.ERROR, 0, 0, 0.0, error="link-closed"
            )
            return
        sender = self._sender or YmodemSender(
            on_event=lambda kind, details: self._on_ymodem_event(kind, details),
            on_progress=lambda confirmed, total, retransmits: self.ymodem_progress.emit(
                {
                    "bytes_confirmed": confirmed,
                    "total_bytes": total,
                    "retransmits": retransmits,
                    "filename": spec.filename,
                }
            ),
        )

        result = sender.send_batch(spec, _LinkYmodemTransport(self))
        self._finish_ymodem(
            spec,
            result.outcome,
            result.blocks_sent,
            result.bytes_confirmed,
            result.duration_s,
            error=result.error,
            retransmits=result.retransmits,
        )

    def _on_ymodem_event(self, kind: str, details: dict) -> None:
        if self._recorder is not None:
            self._recorder.record_ymodem({"stage": kind, **details})

    def _finish_ymodem(
        self,
        spec: YmodemFileSpec,
        outcome: YmodemSendOutcome,
        blocks: int,
        bytes_confirmed: int,
        duration_s: float,
        *,
        error: str = "",
        retransmits: int = 0,
    ) -> None:
        payload = {
            "outcome": outcome.value,
            "error": error,
            "blocks": blocks,
            "bytes_confirmed": bytes_confirmed,
            "retransmits": retransmits,
            "duration_s": duration_s,
            "filename": spec.filename,
            "size": spec.size,
            "sha256": spec.sha256,
        }
        if self._recorder is not None:
            self._recorder.record_ymodem({"stage": "finished", **payload})
        self._record_event("ymodem_finished", payload)
        self._last_ymodem_payload = payload
        self.ymodem_finished.emit(payload)
        if self._link is not None:
            self._set_state(TcpShellWorkerState.CONNECTED)
        else:
            self._set_state(TcpShellWorkerState.DISCONNECTED)

    def _drain_echo(self, duration_s: float) -> None:
        """Read the shell echo of ``rb`` before the receiver silences output."""

        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            try:
                data = self._recv_bytes(0.05)
            except _LinkClosed:
                return
            if data:
                self._feed_ring(data)

    # -- link helpers (worker thread only) ------------------------------------

    def _recv_bytes(self, timeout_s: float) -> bytes:
        link = self._link
        if link is None:
            raise _LinkClosed("link-closed")
        try:
            data = link.recv(timeout_s)
        except ShellLinkError as exc:
            raise _LinkClosed(str(exc)) from exc
        if self._recorder is not None and data:
            self._recorder.record_transcript("rx", data)
        return data

    def _send_bytes(self, data: bytes) -> None:
        link = self._link
        if link is None:
            raise _LinkClosed("link-closed")
        try:
            link.send(data)
        except ShellLinkError as exc:
            raise _LinkClosed(str(exc)) from exc
        if self._recorder is not None:
            self._recorder.record_transcript("tx", data)

    def _close_link(self) -> None:
        link, self._link = self._link, None
        if link is not None:
            link.close()

    def _finalize_recorder(self, *, reason: str) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        ymodem_summary = None
        if getattr(self, "_last_ymodem_payload", None):
            ymodem_summary = dict(self._last_ymodem_payload)
        recorder.finalize(
            status="complete" if reason in ("operator", "closed") else "incomplete",
            final_state="disconnected",
            incomplete_reason="" if reason in ("operator", "closed") else reason,
            ymodem=ymodem_summary,
        )
        self._recorder = None

    def _record_event(self, event_type: str, details: dict) -> None:
        if self._recorder is not None:
            try:
                self._recorder.record_event(event_type, details)
            except Exception:  # noqa: BLE001 - evidence write must not kill the link
                pass

    def _emit_disconnected(self, reason: str) -> None:
        self._set_state(TcpShellWorkerState.DISCONNECTED)
        self.disconnected.emit(reason)

    def _set_state(self, state: TcpShellWorkerState) -> None:
        if state is not self._state:
            self._state = state
            self.state_changed.emit(state.value)


__all__ = ["TcpShellWorker", "TcpShellWorkerState"]
