"""M37 TCP shell worker 端到端回归：真 socket + 假 AFD01 设备。

假设备镜像固件 ymodem.c 的接收行为：rb 回显 → C 握手 → ACK+C → 逐块 ACK →
EOT 两阶段 → C → 空名批结束 ACK；abort 发单个 CAN。
"""

from __future__ import annotations

import json
from pathlib import Path
import socket
import threading
import time

import pytest

from satellite_debug_tool.core.tcp_shell.recorder import TcpShellSessionRecorder
from satellite_debug_tool.core.tcp_shell.worker import TcpShellWorker, TcpShellWorkerState
from satellite_debug_tool.core.tcp_shell.ymodem_sender import (
    YmodemFileSpec,
    YmodemSender,
)


def _fast_sender() -> YmodemSender:
    return YmodemSender(c_timeout_s=6.0, ack_timeout_s=2.0, final_ack_timeout_s=4.0)

ACK = b"\x06"
NAK = b"\x15"
CAN = b"\x18"
C = b"\x43"
EOT = b"\x04"
STX = 0x02


def _wait_until(qapplication_session, predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapplication_session.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    qapplication_session.processEvents()
    return bool(predicate())


class FakeAfd01Device(threading.Thread):
    """Mirror the firmware TCP shell + YMODEM receiver on a local socket."""

    def __init__(self, *, fragment_controls: bool = False, stall_after_block: int = -1):
        super().__init__(daemon=True)
        self.fragment_controls = fragment_controls
        self.stall_after_block = stall_after_block
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.received_text = b""
        self.frames: list[bytes] = []
        self.raw_after_rb = bytearray()
        self.cancel_received = False
        self.error = ""

    def run(self) -> None:
        try:
            conn, _ = self.server.accept()
            conn.settimeout(10.0)
            self._shell_phase(conn)
            conn.close()
        except Exception as exc:  # noqa: BLE001 - record and let the test fail
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                self.server.close()
            except OSError:
                pass

    # -- shell ------------------------------------------------------------------

    def _shell_phase(self, conn: socket.socket) -> None:
        conn.sendall(b"letter shell 1.0\r\n")  # welcome banner
        buffer = b""
        while b"rb\r" not in buffer:
            chunk = conn.recv(4096)
            if not chunk:
                return
            self.received_text += chunk
            buffer += chunk
        conn.sendall(b"rb\r\n")  # echo before the firmware silences output
        # 固件合同：C 在进入 WAITING_START 后的下一次 receive 超时（~2 s）才发出，
        # 晚于上位机的 echo drain 窗口；假设备用压缩但仍晚于 drain 的延迟模拟。
        time.sleep(0.55)
        self._ymodem_phase(conn)

    # -- ymodem receiver (firmware mirror) ----------------------------------------

    def _recv_exact(self, conn: socket.socket, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = conn.recv(count - len(data))
            if not chunk:
                raise ConnectionError("sender closed during frame")
            data += chunk
        return data

    def _send_control(self, conn: socket.socket, data: bytes) -> None:
        if self.fragment_controls and len(data) > 1:
            for byte in data:
                conn.sendall(bytes([byte]))
                time.sleep(0.002)
        else:
            conn.sendall(data)

    def _ymodem_phase(self, conn: socket.socket) -> None:
        self._send_control(conn, C)
        header = self._recv_frame(conn)
        if header is None or header[1] != 0 or header[3] == 0:
            conn.sendall(CAN)
            return
        self._send_control(conn, ACK)
        self._send_control(conn, C)
        block_index = 0
        while True:
            first = self._recv_exact(conn, 1)
            if first == EOT:
                if not self._handle_eot(conn):
                    return
                continue
            if first == CAN:
                self.cancel_received = True
                return
            if first[0] != STX:
                conn.sendall(CAN)
                return
            rest = self._recv_exact(conn, 1028)
            frame = first + rest
            sequence = frame[1]
            if sequence == 0 and frame[3] == 0:
                # batch-end header (empty filename)
                self._send_control(conn, ACK)
                return
            block_index += 1
            self.frames.append(frame)
            if self.stall_after_block == block_index:
                # 静默等待 sender 取消；收到 CAN 后保持连接（镜像固件 abort 后
                # 回到 shell 的行为），直到 client 断开。
                while True:
                    byte = self._recv_exact(conn, 1)
                    if byte == CAN:
                        self.cancel_received = True
                        try:
                            conn.settimeout(5.0)
                            while conn.recv(4096):
                                pass
                        except OSError:
                            pass
                        return
            self._send_control(conn, ACK)

    def _handle_eot(self, conn: socket.socket) -> bool:
        # firmware: first EOT -> NAK, second EOT -> ACK (after commit), then C
        self._send_control(conn, NAK)
        second = self._recv_exact(conn, 1)
        if second != EOT:
            conn.sendall(CAN)
            return False
        time.sleep(0.02)  # device commits candidate to flash
        self._send_control(conn, ACK)
        self._send_control(conn, C)
        return True

    def _recv_frame(self, conn: socket.socket):
        first = self._recv_exact(conn, 1)
        if first[0] != STX:
            return None
        rest = self._recv_exact(conn, 1028)
        return first + rest

    def payload_bytes(self) -> bytes:
        return b"".join(frame[3:-2] for frame in self.frames)

    def close(self) -> None:
        try:
            self.server.close()
        except OSError:
            pass


class WorkerHarness:
    def __init__(self, qapplication_session, tmp_path, device: FakeAfd01Device):
        self.qapp = qapplication_session
        self.events: list[tuple[str, object]] = []
        self.session_dir_hint = tmp_path
        self.device = device
        self.worker: TcpShellWorker | None = None

    def start_worker(self, **kwargs) -> TcpShellWorker:
        worker = TcpShellWorker(
            host="127.0.0.1",
            port=self.device.port,
            managed_session=False,
            recorder=TcpShellSessionRecorder(
                host="127.0.0.1",
                port=self.device.port,
                managed_session=False,
                root=self.session_dir_hint / "sessions",
            ),
            **kwargs,
        )
        for name in (
            "connected",
            "connect_failed",
            "disconnected",
            "state_changed",
            "rx_payload",
            "ymodem_progress",
            "ymodem_finished",
            "session_started",
        ):
            signal = getattr(worker, name)

            def record(value, _name=name):
                self.events.append((_name, value))

            signal.connect(record)
        self.worker = worker
        worker.start()
        return worker

    def wait_event(self, name: str, timeout_s: float = 5.0):
        _wait_until(self.qapp, lambda: any(n == name for n, _v in self.events), timeout_s)
        for event_name, value in self.events:
            if event_name == name:
                return value
        return None

    def wait_state(self, state: TcpShellWorkerState, timeout_s: float = 5.0) -> bool:
        return _wait_until(self.qapp, lambda: self.worker.state is state, timeout_s)

    def finish(self) -> None:
        worker = self.worker
        if worker is not None:
            worker.stop_and_join(timeout_ms=8000)
            self.worker = None
        self.device.close()


@pytest.fixture()
def device_server():
    server = FakeAfd01Device()
    server.start()
    yield server
    server.close()


class TestShellSession:
    def test_connect_send_raw_and_disconnect(
        self, qapplication_session, tmp_path, device_server
    ):
        harness = WorkerHarness(qapplication_session, tmp_path, device_server)
        worker = harness.start_worker()
        worker.submit_connect("127.0.0.1", device_server.port)
        assert harness.wait_state(TcpShellWorkerState.CONNECTED)
        worker.submit_send_raw(b"help\r")
        assert _wait_until(
            qapplication_session, lambda: b"help\r" in device_server.received_text
        )
        worker.submit_disconnect("operator")
        worker.stop_and_join()
        assert harness.wait_event("disconnected") in ("operator",)

    def test_connect_failure_emits_signal(self, qapplication_session, tmp_path):
        server = FakeAfd01Device()
        server.start()
        server.close()  # 端口已关闭，连接必然失败
        harness = WorkerHarness(qapplication_session, tmp_path, server)
        worker = harness.start_worker()
        worker.submit_connect("127.0.0.1", server.port)
        assert harness.wait_event("connect_failed") is not None
        worker.stop_and_join()


class TestYmodemEndToEnd:
    def test_full_transfer_with_fragmented_controls(
        self, qapplication_session, tmp_path
    ):
        # 不注入 sender：走生产路径，验证进度与逐块证据回调真实触发。
        device = FakeAfd01Device(fragment_controls=True)
        device.start()
        try:
            harness = WorkerHarness(qapplication_session, tmp_path, device)
            worker = harness.start_worker()
            worker.submit_connect("127.0.0.1", device.port)
            assert harness.wait_state(TcpShellWorkerState.CONNECTED)
            payload = bytes((index * 13 + 5) & 0xFF for index in range(2500))
            spec = YmodemFileSpec("afd01c_app_0.0.131_beta.bin", payload)
            worker.submit_start_ymodem(spec)
            finished = None

            def _finished():
                nonlocal finished
                finished = harness.wait_event("ymodem_finished", timeout_s=0.2)
                return finished is not None

            assert _wait_until(qapplication_session, _finished, timeout_s=8.0), device.error
            assert finished["outcome"] == "completed"
            assert finished["bytes_confirmed"] == 2500
            assert finished["filename"] == spec.filename
            # 生产 sender 的进度与逐块证据回调必须真实触发（进度恒 0 的回归）。
            progress_events = [v for n, v in harness.events if n == "ymodem_progress"]
            assert progress_events, "ymodem_progress never emitted"
            assert progress_events[-1]["bytes_confirmed"] == 2500
            assert progress_events[-1]["total_bytes"] == 2500
            assert device.payload_bytes().startswith(payload)
            assert device.payload_bytes()[:2500] == payload
            worker.submit_disconnect("operator")
            harness.finish()
        finally:
            device.close()

    def test_cancel_sends_can_can(self, qapplication_session, tmp_path):
        device = FakeAfd01Device(stall_after_block=1)
        device.start()
        try:
            harness = WorkerHarness(qapplication_session, tmp_path, device)
            worker = harness.start_worker(sender=_fast_sender())
            worker.submit_connect("127.0.0.1", device.port)
            assert harness.wait_state(TcpShellWorkerState.CONNECTED)
            spec = YmodemFileSpec("f.bin", bytes(3000))
            worker.submit_start_ymodem(spec)
            assert harness.wait_state(TcpShellWorkerState.YMODEM)
            worker.cancel_ymodem()
            finished = None

            def _finished():
                nonlocal finished
                finished = harness.wait_event("ymodem_finished", timeout_s=0.2)
                return finished is not None

            assert _wait_until(qapplication_session, _finished, timeout_s=8.0)
            assert finished["outcome"] == "cancelled"
            assert _wait_until(qapplication_session, lambda: device.cancel_received)
            assert worker.state is TcpShellWorkerState.CONNECTED
            harness.finish()
        finally:
            device.close()


class TestRecorderEvidence:
    def test_session_files_and_incomplete_finalization(
        self, qapplication_session, tmp_path, device_server
    ):
        harness = WorkerHarness(qapplication_session, tmp_path, device_server)
        worker = harness.start_worker()
        worker.submit_connect("127.0.0.1", device_server.port)
        assert harness.wait_state(TcpShellWorkerState.CONNECTED)
        worker.submit_send_raw(b"version\r")
        assert _wait_until(
            qapplication_session, lambda: b"version\r" in device_server.received_text
        )
        session_id = harness.wait_event("session_started")
        assert session_id
        worker.stop_and_join(timeout_ms=8000)

        session_dir = tmp_path / "sessions" / session_id
        config = json.loads((session_dir / "config.json").read_text(encoding="utf-8"))
        assert config["result_class"] == "ENGINEERING_ONLY"
        assert config["managed_session"] is False
        manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["schema"] == "satellite-debug-tool/tcp-shell-session"
        assert manifest["status"] == "incomplete"  # worker-stop 非操作员收尾
        summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
        assert summary["status"] == "incomplete"
        transcript_lines = (
            (session_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
        )
        directions = {json.loads(line)["direction"] for line in transcript_lines}
        assert directions == {"tx", "rx"}
        events = [json.loads(line) for line in
                  (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert events[0]["event_type"] == "session_started"
        assert events[-1]["event_type"] == "session_finished"
