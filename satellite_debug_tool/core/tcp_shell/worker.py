"""Raw TCP shell worker: single socket owner for shell bytes and YMODEM (M37).

The worker is the only read/write owner of the TCP connection. Shell mode
forwards received bytes to the UI; YMODEM mode feeds the sender state machine
directly so binary control bytes never pass through text decoding.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import queue
import socket
import threading
import time
from typing import Optional

from PySide6.QtCore import QThread, Signal

from .recorder import TcpShellSessionRecorder
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
    host: str
    port: int


@dataclass
class _DisconnectCommand:
    reason: str = ""


@dataclass
class _SendTextCommand:
    payload: bytes


@dataclass
class _StartYmodemCommand:
    spec: YmodemFileSpec


class _LinkClosed(RuntimeError):
    pass


class _SocketYmodemTransport(YmodemTransport):
    """Bind the YMODEM sender to the worker-owned socket."""

    def __init__(self, worker: "TcpShellWorker") -> None:
        self._worker = worker

    def send(self, data: bytes) -> None:
        self._worker._send_bytes(data)

    def recv(self, timeout_s: float) -> bytes:
        return self._worker._recv_bytes(timeout_s)

    def cancel_requested(self) -> bool:
        return self._worker._ymodem_cancel.is_set()


class TcpShellWorker(QThread):
    """Own the TCP connection, the YMODEM transfer and the session recorder."""

    connected = Signal(str, int)
    connect_failed = Signal(str)
    disconnected = Signal(str)
    state_changed = Signal(str)
    rx_payload = Signal(bytes)
    ymodem_progress = Signal(object)
    ymodem_finished = Signal(object)
    session_started = Signal(str)

    def __init__(
        self,
        *,
        host: str,
        port: int,
        managed_session: bool,
        operator: str = "",
        recorder: Optional[TcpShellSessionRecorder] = None,
        connect_timeout_s: float = 3.0,
        sender: Optional[YmodemSender] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._host = str(host)
        self._port = int(port)
        self._managed_session = bool(managed_session)
        self._operator = operator
        self._recorder = recorder
        self._connect_timeout_s = float(connect_timeout_s)
        self._sender = sender or YmodemSender()
        self._commands: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._ymodem_cancel = threading.Event()
        self._socket: Optional[socket.socket] = None
        self._state = TcpShellWorkerState.DISCONNECTED
        self._last_ymodem_payload: Optional[dict] = None

    # -- UI-thread API ---------------------------------------------------------

    @property
    def state(self) -> TcpShellWorkerState:
        return self._state

    def submit_connect(self, host: str, port: int) -> None:
        self._commands.put(_ConnectCommand(str(host), int(port)))

    def submit_disconnect(self, reason: str = "") -> None:
        self._commands.put(_DisconnectCommand(reason))

    def submit_send_raw(self, payload: bytes) -> None:
        self._commands.put(_SendTextCommand(bytes(payload)))

    def submit_start_ymodem(self, spec: YmodemFileSpec) -> None:
        self._commands.put(_StartYmodemCommand(spec))

    def cancel_ymodem(self) -> None:
        """Thread-safe cancel; the sender notices within one recv timeout."""
        self._ymodem_cancel.set()

    def stop_and_join(self, timeout_ms: int = 5000) -> None:
        self._stop_event.set()
        self._ymodem_cancel.set()
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
                    if self._socket is not None:
                        continue
                    if self._connect(command.host, command.port):
                        self._shell_loop()
                elif isinstance(command, _DisconnectCommand):
                    break
        finally:
            self._close_socket()
            self._finalize_recorder(reason="worker-stopped")
            self._set_state(TcpShellWorkerState.DISCONNECTED)

    def _connect(self, host: str, port: int) -> bool:
        self._set_state(TcpShellWorkerState.CONNECTING)
        try:
            connection = socket.create_connection(
                (host, port), timeout=self._connect_timeout_s
            )
        except OSError as exc:
            self._set_state(TcpShellWorkerState.DISCONNECTED)
            self.connect_failed.emit(f"{type(exc).__name__}: {exc}")
            return False
        connection.settimeout(0.05)
        self._socket = connection
        if self._recorder is None:
            self._recorder = TcpShellSessionRecorder(
                host=host,
                port=port,
                managed_session=self._managed_session,
                operator=self._operator,
            )
        try:
            session_dir = self._recorder.start()
        except Exception:
            self._close_socket()
            self._set_state(TcpShellWorkerState.DISCONNECTED)
            self.connect_failed.emit("session evidence directory unavailable")
            return False
        self._set_state(TcpShellWorkerState.CONNECTED)
        self.session_started.emit(self._recorder.session_id)
        self.connected.emit(host, port)
        self._record_event("connected", {"host": host, "port": port})
        return True

    def _shell_loop(self) -> None:
        close_reason = "closed"
        while not self._stop_event.is_set():
            try:
                data = self._recv_bytes(0.05)
            except _LinkClosed as exc:
                close_reason = str(exc) or "link-closed"
                break
            if data:
                self.rx_payload.emit(data)
            while True:
                try:
                    command = self._commands.get_nowait()
                except queue.Empty:
                    break
                if isinstance(command, _SendTextCommand):
                    try:
                        self._send_bytes(command.payload)
                    except _LinkClosed as exc:
                        close_reason = str(exc) or "link-closed"
                elif isinstance(command, _StartYmodemCommand):
                    self._run_ymodem(command.spec)
                    if self._socket is None:
                        close_reason = "link-closed-during-ymodem"
                elif isinstance(command, _DisconnectCommand):
                    self._record_event("disconnect_requested", {"reason": command.reason})
                    self._commands.put(command)  # let the outer loop exit too
                    self._close_socket()
                    self._finalize_recorder(reason=command.reason or "operator")
                    self._emit_disconnected(command.reason or "operator")
                    return
                elif isinstance(command, _ConnectCommand):
                    continue
        self._close_socket()
        self._finalize_recorder(reason=close_reason)
        self._emit_disconnected(close_reason)

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
        if self._socket is None:
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

        result = sender.send_batch(spec, _SocketYmodemTransport(self))
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
        if self._socket is not None:
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
                self.rx_payload.emit(data)

    # -- socket helpers (worker thread only) ------------------------------------

    def _recv_bytes(self, timeout_s: float) -> bytes:
        connection = self._socket
        if connection is None:
            raise _LinkClosed("socket-closed")
        connection.settimeout(max(timeout_s, 0.001))
        try:
            data = connection.recv(65536)
        except socket.timeout:
            return b""
        except OSError as exc:
            self._close_socket()
            raise _LinkClosed(f"{type(exc).__name__}: {exc}") from exc
        if not data:
            self._close_socket()
            raise _LinkClosed("peer-closed")
        if self._recorder is not None:
            self._recorder.record_transcript("rx", data)
        return data

    def _send_bytes(self, data: bytes) -> None:
        connection = self._socket
        if connection is None:
            raise _LinkClosed("socket-closed")
        try:
            connection.sendall(data)
        except OSError as exc:
            self._close_socket()
            raise _LinkClosed(f"{type(exc).__name__}: {exc}") from exc
        if self._recorder is not None:
            self._recorder.record_transcript("tx", data)

    def _close_socket(self) -> None:
        connection, self._socket = self._socket, None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

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
