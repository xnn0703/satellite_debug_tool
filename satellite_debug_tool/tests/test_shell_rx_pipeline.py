"""M45 接收管线合同回归：转发链、有界环端到端停止、清屏代际、帧合并坐标与串口读取上限。"""
from __future__ import annotations

import json
from pathlib import Path
import socket
import threading

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.tcp_shell.controller import TcpShellController
from satellite_debug_tool.core.tcp_shell.recorder import TcpShellSessionRecorder
from satellite_debug_tool.core.tcp_shell.rx_bridge import RxRing
from satellite_debug_tool.core.tcp_shell.transport import (
    MAX_READ_CHUNK,
    SerialShellLink,
    ShellLink,
)
from satellite_debug_tool.core.tcp_shell.worker import TcpShellWorker, TcpShellWorkerState
from satellite_debug_tool.tests.test_tcp_shell_ui import (
    _strip_stamp,
    _wait_until,
    make_workspace,
)


class ScriptServer:
    """独立线程的注入服务：按需发送字节并记录发送进度，不依赖被测事件循环。"""

    def __init__(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self._conn: socket.socket | None = None
        self._lock = threading.Lock()
        self.sent = bytearray()
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._server.accept()
            conn.settimeout(0.2)
            with self._lock:
                self._conn = conn
            while self._running:
                try:
                    if not conn.recv(4096):
                        break
                except socket.timeout:
                    continue
                except OSError:
                    break
        except OSError:
            pass

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._conn is not None

    def send(self, data: bytes) -> None:
        with self._lock:
            conn = self._conn
            self.sent += data
            if conn is not None:
                conn.sendall(data)

    def close_client(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._running = False
        self.close_client()
        try:
            self._server.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


def _transcript_rx_bytes(session_dir: Path) -> bytes:
    payload = bytearray()
    for line in (session_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["direction"] == "rx":
            payload += bytes.fromhex(record["hex"])
    return bytes(payload)


def _drain_all(ring: RxRing) -> bytes:
    chunks = []
    while True:
        data, _ = ring.drain(1 << 20)
        if not data:
            return b"".join(chunks)
        chunks.append(data)


def _plain_text(workspace) -> str:
    return "\n".join(
        _strip_stamp(line) for line in workspace._terminal.toPlainText().split("\n")
    )


class TestControllerRelay:
    def test_natural_link_close_reaches_ui_subscribers(self, qapplication_session, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        controller = TcpShellController()
        received: list[str] = []
        controller.disconnected.connect(received.append)
        server = ScriptServer()
        try:
            controller.connect_device(
                {"transport": "tcp", "host": "127.0.0.1", "port": server.port}
            )
            assert _wait_until(
                qapplication_session, lambda: controller.state is TcpShellWorkerState.CONNECTED
            )
            server.close_client()
            assert _wait_until(qapplication_session, lambda: bool(received))
            assert received[0] in ("peer-closed", "link-closed")
        finally:
            controller.shutdown()
            server.close()

    def test_operator_disconnect_and_reconnect_generation(self, qapplication_session, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        controller = TcpShellController()
        received: list[str] = []
        controller.disconnected.connect(received.append)
        first = ScriptServer()
        try:
            controller.connect_device(
                {"transport": "tcp", "host": "127.0.0.1", "port": first.port}
            )
            assert _wait_until(
                qapplication_session, lambda: controller.state is TcpShellWorkerState.CONNECTED
            )
            generation = controller.session_generation
            assert controller.rx_ring is not None
            controller.disconnect_device("operator")
            assert _wait_until(qapplication_session, lambda: received == ["operator"])
            assert controller.rx_ring is None
        finally:
            controller.shutdown()
            first.close()
        second = ScriptServer()
        try:
            controller.connect_device(
                {"transport": "tcp", "host": "127.0.0.1", "port": second.port}
            )
            assert _wait_until(
                qapplication_session, lambda: controller.state is TcpShellWorkerState.CONNECTED
            )
            assert controller.session_generation == generation + 1
            assert controller.rx_ring is not None
            assert controller.rx_ring.backlog_bytes == 0
        finally:
            controller.shutdown()
            second.close()

    def test_ymodem_finished_relayed_on_cancel_path(self, qapplication_session, tmp_path, monkeypatch):
        from satellite_debug_tool.tests.test_tcp_shell_worker import FakeAfd01Device

        monkeypatch.setenv("HOME", str(tmp_path))
        device = FakeAfd01Device(stall_after_block=1)
        device.start()
        controller = TcpShellController()
        finished: list[dict] = []
        controller.ymodem_finished.connect(finished.append)
        try:
            controller.connect_device(
                {"transport": "tcp", "host": "127.0.0.1", "port": device.port}
            )
            assert _wait_until(
                qapplication_session, lambda: controller.state is TcpShellWorkerState.CONNECTED
            )
            firmware = tmp_path / "f.bin"
            firmware.write_bytes(bytes(3000))
            assert controller.start_ymodem(firmware).allowed
            assert _wait_until(
                qapplication_session, lambda: controller.state is TcpShellWorkerState.YMODEM
            )
            controller.cancel_ymodem()
            assert _wait_until(qapplication_session, lambda: bool(finished))
            assert finished[0]["outcome"] == "cancelled"
        finally:
            controller.shutdown()
            device.close()


class TestWorkerRingPipeline:
    def _start_worker(self, tmp_path, server, ring) -> TcpShellWorker:
        target = {"transport": "tcp", "host": "127.0.0.1", "port": server.port}
        worker = TcpShellWorker(
            target=target,
            managed_session=False,
            recorder=TcpShellSessionRecorder(
                target=target, managed_session=False, root=tmp_path / "sessions"
            ),
            rx_ring=ring,
        )
        worker.start()
        worker.submit_connect()
        return worker

    def test_stop_with_full_ring_preserves_read_bytes_in_order(
        self, qapplication_session, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        server = ScriptServer()
        worker = self._start_worker(
            tmp_path, server, RxRing(capacity_bytes=4096, wait_segment_s=0.002)
        )
        pending_events: list[int] = []
        worker.rx_pending.connect(lambda: pending_events.append(1))
        try:
            assert _wait_until(
                qapplication_session, lambda: worker.state is TcpShellWorkerState.CONNECTED
            )
            ring = worker.rx_ring  # _connect 按注入参数重建，取会话实际 ring
            payload = bytes(range(256)) * 384  # 96 KiB 洪水
            server.send(payload)
            assert _wait_until(
                qapplication_session, lambda: ring.backlog_bytes >= 4096
            ), "ring 未填满，洪水未形成背压"
            worker.stop_and_join(timeout_ms=8000)
            drained = _drain_all(ring)
            assert drained == payload[: len(drained)]  # 已读字节保序
            assert len(drained) >= 4096  # 停止时环满且有部分入环数据
            assert _transcript_rx_bytes(self._session_dir(worker, tmp_path)) == drained
            assert pending_events  # 边沿通知真实触发过
        finally:
            worker.stop_and_join(timeout_ms=8000)
            server.close()

    def _session_dir(self, worker, tmp_path) -> Path:
        sessions = list((tmp_path / "sessions").iterdir())
        assert len(sessions) == 1
        return sessions[0]

    def test_clear_generation_boundary_end_to_end(self, qapplication_session, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        server = ScriptServer()
        controller = TcpShellController()
        workspace = None
        try:
            from satellite_debug_tool.ui.tcp_shell_workspace import TcpShellWorkspace

            workspace = TcpShellWorkspace(Settings(), controller=controller)
            controller.connect_device(
                {"transport": "tcp", "host": "127.0.0.1", "port": server.port}
            )
            assert _wait_until(
                qapplication_session, lambda: controller.state is TcpShellWorkerState.CONNECTED
            )
            assert _wait_until(
                qapplication_session, lambda: workspace._active_ring is controller.rx_ring
            )
            server.send(b"old\xe5")
            assert _wait_until(
                qapplication_session,
                lambda: "old" in workspace._terminal.toPlainText(),
            )
            workspace._on_clear_terminal()
            assert _wait_until(
                qapplication_session, lambda: workspace._rx_epoch == 1
            ), "清屏代际未被帧观察到"
            server.send(b"\x9cnew")
            assert _wait_until(
                qapplication_session, lambda: _plain_text(workspace).endswith("new")
            )
            assert "old" not in _plain_text(workspace)  # 在途旧字节不进入新屏幕
        finally:
            if workspace is not None:
                workspace.shutdown()
            controller.shutdown()
            server.close()


class TestFrameMergeCoordinates:
    def test_multiple_updates_with_eviction_merge_to_correct_rows(
        self, qapplication_session, tmp_path, monkeypatch
    ):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._screen.max_rows = 5
        workspace.feed_rx(b"a\r\nb\r\nc\r\nd\r\ne")
        workspace._active_ring.push(b"\r\nf")
        workspace._active_ring.push(b"\r\ng")
        workspace._flush_frame()
        assert _plain_text(workspace) == "c\nd\ne\nf\ng"

        sequential = make_workspace(qapplication_session, tmp_path, monkeypatch)
        sequential._screen.max_rows = 5
        sequential.feed_rx(b"a\r\nb\r\nc\r\nd\r\ne\r\nf\r\ng")
        assert _plain_text(sequential) == "c\nd\ne\nf\ng"

    def test_single_update_evicting_more_than_document(
        self, qapplication_session, tmp_path, monkeypatch
    ):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._screen.max_rows = 3
        workspace.feed_rx(b"a\r\nb\r\nc")
        workspace._active_ring.push(b"\r\n1\r\n2\r\n3\r\n4\r\n5")
        workspace._flush_frame()
        assert _plain_text(workspace) == "3\n4\n5"

    def test_empty_and_style_only_segments_are_inert(
        self, qapplication_session, tmp_path, monkeypatch
    ):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace.feed_rx(b"")
        workspace.feed_rx(b"\x1b[32m")
        assert _plain_text(workspace) == ""


class TestSerialReadCap:
    def test_serial_recv_chunks_at_max_read_chunk(self, monkeypatch):
        import serial

        payload = bytes(range(256)) * 480  # 120 KiB 积压

        class FakeSerial:
            def __init__(self, *args, **kwargs):
                self._pending = payload
                self.timeout = 0.0

            @property
            def in_waiting(self) -> int:
                return len(self._pending)

            def read(self, count: int) -> bytes:
                taken, self._pending = self._pending[:count], self._pending[count:]
                return taken

            def write(self, data):  # noqa: ANN001 - stub
                return len(data)

            def flush(self) -> None:
                return None

            def close(self) -> None:
                return None

        monkeypatch.setattr(serial, "Serial", FakeSerial)
        link = SerialShellLink("COMX", 115200)
        link.open()
        drained = bytearray()
        while True:
            data = link.recv(0.01)
            assert len(data) <= MAX_READ_CHUNK
            if not data:
                break
            drained += data
        assert bytes(drained) == payload

    def test_serial_stub_stop_hands_over_residual_by_chunk(
        self, qapplication_session, tmp_path, monkeypatch
    ):
        import satellite_debug_tool.core.tcp_shell.worker as worker_module

        monkeypatch.setenv("HOME", str(tmp_path))
        payload = bytes(range(256)) * 480  # 120 KiB，超过单次读取上限
        link_state = {"pending": payload}

        class StubSerialLink(ShellLink):
            label = "stub-serial@115200"

            def open(self) -> None:
                return None

            def recv(self, timeout_s: float) -> bytes:
                pending = link_state["pending"]
                taken, link_state["pending"] = pending[:MAX_READ_CHUNK], pending[MAX_READ_CHUNK:]
                return taken

            def send(self, data: bytes) -> None:
                return None

            def close(self) -> None:
                return None

        monkeypatch.setattr(
            worker_module,
            "create_link",
            lambda target, connect_timeout_s=3.0: StubSerialLink(),
        )
        target = {"transport": "serial", "port": "COMX", "baudrate": 115200}
        recorder = TcpShellSessionRecorder(
            target=target, managed_session=False, root=tmp_path / "sessions"
        )
        ring = RxRing(capacity_bytes=4096, wait_segment_s=0.002)
        worker = TcpShellWorker(
            target=target, managed_session=False, recorder=recorder, rx_ring=ring
        )
        worker.start()
        worker.submit_connect()
        try:
            assert _wait_until(
                qapplication_session, lambda: worker.state is TcpShellWorkerState.CONNECTED
            )
            ring = worker.rx_ring  # _connect 按注入参数重建，取会话实际 ring
            assert _wait_until(
                qapplication_session, lambda: ring.backlog_bytes >= 4096
            ), "分批读取未把环填满"
            worker.stop_and_join(timeout_ms=8000)
            drained = _drain_all(ring)
            assert drained == payload[: len(drained)]
            assert len(drained) >= 4096
            assert _transcript_rx_bytes(recorder.session_dir) == drained
        finally:
            worker.stop_and_join(timeout_ms=8000)
