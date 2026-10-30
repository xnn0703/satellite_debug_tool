"""M45 性能与专项回归：持续速率、负载矩阵、洪水交互、满环关停与退化门槛。

注入服务运行在独立线程（不依赖被测 UI 事件循环）；持续速率按注入期间的实际
消费量与注入端实际发送进度双端度量，"停止后归零"只作突发恢复指标。
"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.tcp_shell.controller import TcpShellController
from satellite_debug_tool.core.tcp_shell.worker import TcpShellWorkerState
from satellite_debug_tool.tests.test_shell_rx_pipeline import (
    ScriptServer,
    _plain_text,
)
from satellite_debug_tool.tests.test_tcp_shell_ui import (
    _wait_until,
    make_workspace,
)

_LINE = b"[10:24:56.123] I system: temp 36.5C gain 12.5dB snr 8.2dB agc 3\x1b[32m OK\x1b[0m\r\n"
_LINE_LEN = len(_LINE)
_SERIAL_RATE_BPS = 92160  # 921600 8N1 有效载荷
_DOC_BLOCK_LIMIT = 5000


def _pump(qapplication_session, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapplication_session.processEvents()
        time.sleep(0.002)


class RateInjector:
    """独立线程按目标速率注入；记录实际发送进度。"""

    def __init__(self, server: ScriptServer, payload: bytes, rate_bps: int) -> None:
        self._server = server
        self._payload = payload
        self._chunk = max(1, rate_bps // 10)
        self._period = 0.1
        self.sent_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            offset = 0
            chunk = self._payload * ((self._chunk // len(self._payload)) + 1)
            while offset < self._chunk:
                piece = chunk[: self._chunk - offset]
                self._server.send(piece)
                self.sent_bytes += len(piece)
                offset += len(piece)
            next_tick += self._period
            delay = next_tick - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_tick = time.monotonic()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)


class TestSustainedRate:
    def test_full_scrollback_sustains_serial_rate(self, qapplication_session, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        server = ScriptServer()
        controller = TcpShellController()
        from satellite_debug_tool.ui.tcp_shell_workspace import TcpShellWorkspace

        workspace = TcpShellWorkspace(Settings(), controller=controller)
        consumed = {"bytes": 0}
        original_ingest = workspace._ingest_segment

        def counting_ingest(data, log_buffer):
            consumed["bytes"] += len(data)
            return original_ingest(data, log_buffer)

        workspace._ingest_segment = counting_ingest
        controller.connect_device(
            {"transport": "tcp", "host": "127.0.0.1", "port": server.port}
        )
        assert _wait_until(
            qapplication_session, lambda: controller.state is TcpShellWorkerState.CONNECTED
        )
        ring = controller.rx_ring

        # 预热：先填满 5000 行（确定性路径）
        workspace.feed_rx(_LINE * 5100)
        assert workspace._output_document.blockCount() >= 4900

        injector = RateInjector(server, _LINE, _SERIAL_RATE_BPS)
        warmup_s, window_s = 2.0, 8.0
        injector.start()
        try:
            _pump(qapplication_session, warmup_s)
            consumed_at_warmup = consumed["bytes"]
            backlog_peak = 0
            samples = []
            deadline = time.monotonic() + window_s
            while time.monotonic() < deadline:
                qapplication_session.processEvents()
                backlog_peak = max(backlog_peak, ring.backlog_bytes)
                samples.append(ring.backlog_bytes)
                time.sleep(0.002)
            elapsed = window_s
            consumed_in_window = consumed["bytes"] - consumed_at_warmup
            rate = consumed_in_window / elapsed
            injected_rate = injector.sent_bytes / (warmup_s + window_s)
        finally:
            injector.stop()
            server.close()

        print(
            f"PERF sustained: consumed {rate:.0f} B/s (target {_SERIAL_RATE_BPS}), "
            f"injected {injected_rate:.0f} B/s, backlog peak {backlog_peak // 1024} KiB, "
            f"blocks {workspace._output_document.blockCount()}"
        )
        assert injected_rate >= _SERIAL_RATE_BPS * 0.95, "注入端未达目标速率（TCP 背压）"
        assert rate >= _SERIAL_RATE_BPS * 0.95, "UI 实际消费速率未达目标"
        assert backlog_peak <= ring.capacity_bytes + 65536, "积压越过环容量合同"
        assert workspace._output_document.blockCount() <= _DOC_BLOCK_LIMIT + 1

        # 突发恢复指标（独立于持续速率）：停止注入后 5 s 内归零
        assert _wait_until(qapplication_session, lambda: ring.backlog_bytes == 0, timeout_s=5.0)
        workspace.shutdown()
        controller.shutdown()


class TestLoadMatrix:
    def test_burst_64kb_chunks_drain(self, qapplication_session, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        server = ScriptServer()
        controller = TcpShellController()
        from satellite_debug_tool.ui.tcp_shell_workspace import TcpShellWorkspace

        workspace = TcpShellWorkspace(Settings(), controller=controller)
        controller.connect_device(
            {"transport": "tcp", "host": "127.0.0.1", "port": server.port}
        )
        assert _wait_until(
            qapplication_session, lambda: controller.state is TcpShellWorkerState.CONNECTED
        )
        ring = controller.rx_ring
        try:
            payload = _LINE * (65536 // _LINE_LEN + 1)
            for _ in range(4):
                server.send(payload[:65536])
                _pump(qapplication_session, 0.05)
            assert _wait_until(
                qapplication_session, lambda: ring.backlog_bytes == 0, timeout_s=5.0
            )
            assert workspace._output_document.blockCount() <= _DOC_BLOCK_LIMIT + 1
        finally:
            workspace.shutdown()
            controller.shutdown()
            server.close()

    def test_small_packets_50hz_preserve_stream(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        packet = b"[ms] tick\r\n"
        count = 150
        for index in range(count):
            workspace.feed_rx(packet)
            if index % 10 == 0:
                qapplication_session.processEvents()
        _pump(qapplication_session, 0.2)
        text = _plain_text(workspace)
        assert text.count("tick") == count

    def test_ansi_and_long_line_and_hex_and_log(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        # 文本日志开（UI 线程每帧缓冲写一次）；预置目录避免离屏弹文件对话框
        workspace._settings.set("tcp_shell.log_dir", str(tmp_path / "shell-logs"))
        workspace._on_log_toggled(True)
        assert workspace._log_writer is not None
        # HEX 开（双文档路径）
        workspace._on_hex_toggled(True)
        long_line = b"x" * 4096 + b"\r\n"
        payload = _LINE * 50 + b"\x1b[1;31mRED\x1b[0m\r\n" + long_line * 20
        start = time.perf_counter()
        workspace.feed_rx(payload)
        elapsed = time.perf_counter() - start
        print(f"PERF matrix ansi/longline/hex/log {len(payload)//1024} KiB: {elapsed:.2f} s")
        workspace._on_hex_toggled(False)  # 断言针对正文文档（HEX 视图下终端显示 hex 文档）
        assert "RED" in _plain_text(workspace)
        assert workspace._hex_document.blockCount() > 0
        log_text = workspace._log_writer.path.read_text(encoding="utf-8")
        assert "RED" in log_text
        assert workspace._output_document.isUndoAvailable() is False
        assert workspace._hex_document.isUndoAvailable() is False
        workspace.shutdown()
        assert workspace._log_writer is None  # 日志最后关闭

    def test_wide_degradation_gate_512kib(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        payload = _LINE * (512 * 1024 // _LINE_LEN + 1)
        start = time.perf_counter()
        workspace.feed_rx(payload[: 512 * 1024])
        elapsed = time.perf_counter() - start
        print(f"PERF wide gate 512 KiB full path: {elapsed:.2f} s")
        assert elapsed < 15.0


class TestFloodInteraction:
    def test_key_to_send_p99_under_flood(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        terminal = workspace._terminal
        terminal.set_input_enabled(True)
        # 预置 512 KiB 积压并启动真实帧调度，洪水期间测键事件到命令入队的延迟。
        # 等待期间泵事件（不能阻塞主线程，否则键事件本身无法送达）。
        payload = _LINE * (512 * 1024 // _LINE_LEN + 1)
        workspace._active_ring.push(payload[: 512 * 1024])
        workspace._on_rx_pending()
        latencies = []
        original_send = workspace._controller.send_raw
        flood = payload[: 16 * 1024]
        try:
            for _ in range(40):
                # 持续补注保持洪水：键事件必须与排空帧竞争事件循环
                workspace._active_ring.push(flood)
                sent = threading.Event()

                def counting_send(data):
                    result = original_send(data)
                    sent.set()
                    return result

                workspace._controller.send_raw = counting_send
                event = QKeyEvent(
                    QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a"
                )
                posted = time.perf_counter()
                qapplication_session.postEvent(terminal, event)
                deadline = time.monotonic() + 2.0
                while not sent.is_set() and time.monotonic() < deadline:
                    qapplication_session.processEvents()
                    time.sleep(0.001)
                assert sent.is_set(), "键事件在洪水期间未被消费"
                latencies.append((time.perf_counter() - posted) * 1000)
                workspace._controller.send_raw = original_send
                qapplication_session.processEvents()
            latencies.sort()
            p99 = latencies[int(len(latencies) * 0.99) - 1]
            print(
                f"PERF key->send under flood: p99 {p99:.1f} ms, "
                f"max {latencies[-1]:.1f} ms, median {latencies[len(latencies)//2]:.1f} ms"
            )
            assert p99 <= 150.0
        finally:
            workspace._controller.send_raw = original_send
            workspace.shutdown()

    def test_disconnect_responsiveness_under_flood(self, qapplication_session, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        server = ScriptServer()
        controller = TcpShellController()
        controller.connect_device(
            {"transport": "tcp", "host": "127.0.0.1", "port": server.port}
        )
        assert _wait_until(
            qapplication_session, lambda: controller.state is TcpShellWorkerState.CONNECTED
        )
        ring = controller.rx_ring
        try:
            server.send(_LINE * (200 * 1024 // _LINE_LEN + 1))
            assert _wait_until(
                qapplication_session, lambda: ring.backlog_bytes > 64 * 1024, timeout_s=3.0
            )
            start = time.perf_counter()
            controller.disconnect_device("operator")
            assert _wait_until(
                qapplication_session,
                lambda: controller.state is TcpShellWorkerState.DISCONNECTED,
                timeout_s=2.0,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            print(f"PERF disconnect under flood: {elapsed_ms:.0f} ms")
            assert elapsed_ms <= 500.0
            assert controller.rx_ring is None
        finally:
            controller.shutdown()
            server.close()


class TestShutdownWithBacklog:
    def test_shutdown_with_full_ring_completes_staged_drain(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._settings.set("tcp_shell.log_dir", str(tmp_path / "shell-logs"))
        workspace._on_log_toggled(True)
        # 300 KiB 积压：直接入环（不触发通知），关停必须分帧排空且不冻结
        payload = _LINE * (300 * 1024 // _LINE_LEN + 1)
        workspace._active_ring.push(payload[: 300 * 1024])
        start = time.perf_counter()
        workspace.shutdown()
        elapsed = time.perf_counter() - start
        print(f"PERF shutdown drain 300 KiB: {elapsed:.2f} s")
        assert elapsed < 10.0
        assert workspace._active_ring.backlog_bytes == 0
        assert workspace._final_drain_pending is False
        assert workspace._log_writer is None
        text = _plain_text(workspace)
        assert text.count("OK") > 1000  # 消费完成而非丢弃
