"""M45 性能与专项回归：持续速率、负载矩阵、洪水交互、满环关停与退化门槛。

注入服务运行在独立线程（不依赖被测 UI 事件循环）；持续速率按注入期间的实际
消费量与注入端实际发送进度双端度量，"停止后归零"只作突发恢复指标。
"""
from __future__ import annotations

from collections import Counter, deque
import gc
import json
import sys
import threading
import time

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
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


class _FrameTimings:
    """只观察现有帧边界；有界聚合后一次输出，保持原调度和预算。"""

    def __init__(self) -> None:
        self.frames = deque(maxlen=2048)
        self.active = False
        self.current = None
        self.last_start = None
        self.last_end = None
        self.current_metric = None
        self.gc_started = {}
        self.gc_generations = {generation: {"count": 0, "total_ms": 0.0, "max_ms": 0.0}
                               for generation in range(3)}

    def begin(self) -> None:
        if self.active:
            return
        self.active = True
        gc.callbacks.append(self._observe_gc)

    def stop(self) -> None:
        self.active = False
        if self._observe_gc in gc.callbacks:
            gc.callbacks.remove(self._observe_gc)
        self.gc_started.clear()

    def _observe_gc(self, phase, info) -> None:
        generation = info["generation"]
        if not self.active or generation not in self.gc_generations:
            return
        if phase == "start":
            self.gc_started[generation] = (time.perf_counter(), self.current, self.current_metric)
        elif phase == "stop":
            started = self.gc_started.pop(generation, None)
            if started is None:
                return
            elapsed_ms = (time.perf_counter() - started[0]) * 1000
            totals = self.gc_generations[generation]
            totals["count"] += 1
            totals["total_ms"] += elapsed_ms
            totals["max_ms"] = max(totals["max_ms"], elapsed_ms)
            frame = started[1]
            if frame is not None:
                frame["gc_ms"] += elapsed_ms
                if started[2] == "screen_ms":
                    frame["screen_gc_ms"] += elapsed_ms

    def observe_frames(self, widget_type, monkeypatch) -> None:
        from satellite_debug_tool.ui import tcp_shell_workspace as shell_ui

        self.budgets = {"interval_ms": shell_ui._FRAME_INTERVAL_MS,
                        "frame_bytes": shell_ui._FRAME_BYTE_BUDGET,
                        "segment_bytes": shell_ui._DRAIN_SEGMENT_BYTES,
                        "parse_ms": shell_ui._PARSE_SOFT_BUDGET_S * 1000}
        flush = widget_type._flush_frame

        def observed_flush(widget):
            if not self.active:
                return flush(widget)
            start = time.perf_counter()
            frame = {"bytes": 0, "decoder_ms": 0.0, "screen_ms": 0.0,
                     "projection_ms": 0.0, "cursor_ms": 0.0, "drain_ms": 0.0,
                     "gc_ms": 0.0, "screen_gc_ms": 0.0,
                     "last_drain_empty": True, "backlog_start": widget._active_ring.backlog_bytes}
            if self.last_start is not None:
                frame["interval_ms"] = (start - self.last_start) * 1000
                frame["idle_gap_ms"] = (start - self.last_end) * 1000
            self.current = frame
            try:
                return flush(widget)
            finally:
                end = time.perf_counter()
                frame["frame_ms"] = (end - start) * 1000
                frame["exit"] = ("empty_ring" if frame["last_drain_empty"] else
                                 "byte_budget" if frame["bytes"] >= self.budgets["frame_bytes"] else "parse_budget")
                self.frames.append(frame)
                self.current = None
                self.last_start, self.last_end = start, end

        monkeypatch.setattr(widget_type, "_flush_frame", observed_flush)

    def observe_stream(self, workspace, monkeypatch) -> None:
        methods = ((workspace._active_ring, "drain", "drain_ms"),
                   (workspace._decoder, "feed", "decoder_ms"),
                   (workspace._screen, "feed", "screen_ms"),
                   (workspace, "_project_updates", "projection_ms"),
                   (workspace, "_sync_terminal_cursor", "cursor_ms"))
        for owner, name, metric in methods:
            self._observe_method(owner, name, metric, monkeypatch)

    def _observe_method(self, owner, name, metric, monkeypatch) -> None:
        method = getattr(owner, name)

        def observed(*args, **kwargs):
            frame = self.current
            if frame is None:
                return method(*args, **kwargs)
            start = time.perf_counter()
            previous_metric = self.current_metric
            self.current_metric = metric
            try:
                result = method(*args, **kwargs)
                if metric == "drain_ms":
                    frame["bytes"] += len(result[0])
                    frame["last_drain_empty"] = len(result[0]) == 0
                return result
            finally:
                self.current_metric = previous_metric
                frame[metric] += (time.perf_counter() - start) * 1000

        monkeypatch.setattr(owner, name, observed)

    def report(self, timer_type: str) -> None:
        self.stop()

        def distribution(key):
            values = sorted(frame[key] for frame in self.frames if key in frame)
            if not values:
                return {}
            return {"mean": round(sum(values) / len(values), 3),
                    "p50": round(values[(len(values) - 1) // 2], 3),
                    "p95": round(values[int((len(values) - 1) * .95)], 3),
                    "max": round(values[-1], 3)}

        summary = {"timer_type": timer_type, "frames": len(self.frames), "budgets": self.budgets,
                   "backlog_start_bytes": distribution("backlog_start"),
                   "gc_by_generation": {generation: {key: round(value, 3) for key, value in totals.items()}
                                        for generation, totals in self.gc_generations.items()},
                   "longest_screen_frames": [
                       {key: round(frame[key], 3) for key in ("bytes", "frame_ms", "screen_ms", "gc_ms", "screen_gc_ms")}
                       for frame in sorted(self.frames, key=lambda frame: frame["screen_ms"], reverse=True)[:5]
                   ],
                   "bytes_histogram": dict(Counter(frame["bytes"] for frame in self.frames)),
                   "exits": dict(Counter(frame["exit"] for frame in self.frames))}
        for key in ("interval_ms", "idle_gap_ms", "frame_ms", "drain_ms", "decoder_ms",
                    "screen_ms", "projection_ms", "cursor_ms", "gc_ms", "screen_gc_ms"):
            summary[key] = distribution(key)
        stream = sys.__stdout__ if sys.__stdout__ is not None else sys.stdout
        print("PERF frame timing: " + json.dumps(summary, sort_keys=True), file=stream, flush=True)


class TestFrameTimingGcObserver:
    def test_gc_duration_is_bound_to_its_generation_frame_and_stage(self, monkeypatch):
        from types import SimpleNamespace
        import satellite_debug_tool.tests.test_shell_throughput_budget as module

        clock = SimpleNamespace(now=0.0)
        monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: clock.now))
        timings = _FrameTimings()
        timings.active = True
        timings.current = {"gc_ms": 0.0, "screen_gc_ms": 0.0}
        timings.current_metric = "screen_ms"
        timings._observe_gc("start", {"generation": 0})
        clock.now = .004
        timings._observe_gc("stop", {"generation": 0})
        timings.current_metric = "projection_ms"
        timings._observe_gc("start", {"generation": 2})
        clock.now = .006
        timings._observe_gc("stop", {"generation": 2})
        assert timings.gc_generations[0]["count"] == 1
        assert timings.gc_generations[0]["total_ms"] == pytest.approx(4.0)
        assert timings.gc_generations[2]["total_ms"] == pytest.approx(2.0)
        assert timings.current["gc_ms"] == pytest.approx(6.0)
        assert timings.current["screen_gc_ms"] == pytest.approx(4.0)
        assert timings.gc_started == {}
        assert len(timings.gc_generations) == 3

    def test_gc_callback_is_removed_when_observation_exits(self):
        baseline = tuple(gc.callbacks)
        timings = _FrameTimings()
        try:
            timings.begin()
            timings.begin()
            assert gc.callbacks.count(timings._observe_gc) == 1
        finally:
            timings.stop()
        timings.stop()
        assert tuple(gc.callbacks) == baseline


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
    @pytest.mark.parametrize("failure_stage", ("connect", "throughput"))
    @pytest.mark.parametrize("close_mode", ("ready", "pending"))
    def test_failed_sustained_check_closes_all_session_owners(
        self, qapplication_session, tmp_path, monkeypatch, failure_stage, close_mode,
    ):
        """连接或吞吐断言失败时，测试拥有的 worker、控件和服务均被收尾。"""
        from contextlib import nullcontext
        from types import SimpleNamespace
        import satellite_debug_tool.tests.test_shell_throughput_budget as module
        import satellite_debug_tool.ui.tcp_shell_workspace as ui

        calls = []
        workspaces = []
        noop = lambda *args, **kwargs: None

        class Server:
            port = 1234
            def close(self): calls.append("server.close")

        class Controller:
            state = TcpShellWorkerState.CONNECTED
            rx_ring = SimpleNamespace(backlog_bytes=0, drain=lambda *_args: (b"", 0))
            def connect_device(self, *_args): pass
            def shutdown(self): calls.append("controller.shutdown")

        class Workspace:
            def __init__(self, *_args, controller):
                workspaces.append(self)
                self.shutdown_complete = close_mode == "ready"
                self._active_ring = controller.rx_ring
                self._output_document = SimpleNamespace(blockCount=lambda: 5000)
                self._decoder = SimpleNamespace(feed=noop)
                self._screen = SimpleNamespace(feed=noop)
                self._frame_timer = SimpleNamespace(timerType=lambda: "test")
            _ingest_segment = _project_updates = _sync_terminal_cursor = _flush_frame = noop
            feed_rx = noop
            def shutdown(self):
                calls.append("workspace.shutdown")
                return self.shutdown_complete
            def close(self): calls.append("workspace.close")
            def deleteLater(self): calls.append("workspace.delete")

        class Injector:
            sent_bytes = _SERIAL_RATE_BPS * 10
            def __init__(self, *_args): pass
            def start(self): pass
            def stop(self): calls.append("injector.stop")

        ticks = iter((0.0, 9.0))

        def wait_until(_app, predicate, **options):
            if "workspace.shutdown" in calls:
                assert options["timeout_s"] == 10.0
                assert "workspace.disposed" not in calls
                assert predicate() is False
                calls.append("workspace.wait")
                workspaces[0].shutdown_complete = True
                return predicate()
            return failure_stage != "connect"

        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks), perf_counter=lambda: 0.0))
        monkeypatch.setattr(module, "_pump", noop)
        monkeypatch.setattr(module, "_wait_until", wait_until)
        monkeypatch.setattr(module, "ScriptServer", Server)
        monkeypatch.setattr(module, "TcpShellController", Controller)
        monkeypatch.setattr(module, "RateInjector", Injector)
        monkeypatch.setattr(ui, "TcpShellWorkspace", Workspace)
        monkeypatch.setattr(module, "QCoreApplication", SimpleNamespace(
            sendPostedEvents=lambda *_args: calls.append("workspace.disposed"),
        ))
        with pytest.raises(AssertionError, match="UI 实际消费速率未达目标" if failure_stage == "throughput" else None):
            self.test_full_scrollback_sustains_serial_rate(
                qapplication_session, tmp_path, monkeypatch, SimpleNamespace(disabled=nullcontext),
            )
        assert calls.count("workspace.shutdown") == 1
        assert calls.count("workspace.disposed") == 1
        assert calls.count("controller.shutdown") == 1
        assert calls.count("server.close") == 1
        assert workspaces[0].shutdown_complete
        assert calls.count("workspace.wait") == (1 if close_mode == "pending" else 0)
        if close_mode == "pending":
            assert calls.index("workspace.wait") < calls.index("workspace.disposed")

    def test_full_scrollback_sustains_serial_rate(self, qapplication_session, tmp_path, monkeypatch, capfd):
        monkeypatch.setenv("HOME", str(tmp_path))
        server = ScriptServer()
        controller = TcpShellController()
        from satellite_debug_tool.ui.tcp_shell_workspace import TcpShellWorkspace

        timings = _FrameTimings()
        timings.observe_frames(TcpShellWorkspace, monkeypatch)
        workspace = None
        injector = None
        try:
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
            timings.observe_stream(workspace, monkeypatch)

            # 预热：先填满 5000 行（确定性路径）
            workspace.feed_rx(_LINE * 5100)
            assert workspace._output_document.blockCount() >= 4900

            injector = RateInjector(server, _LINE, _SERIAL_RATE_BPS)
            warmup_s, window_s = 2.0, 8.0
            injector.start()
            try:
                _pump(qapplication_session, warmup_s)
                consumed_at_warmup = consumed["bytes"]
                timings.begin()
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
                timings.stop()
                try:
                    injector.stop()
                    injector = None
                finally:
                    with capfd.disabled():
                        timings.report(str(workspace._frame_timer.timerType()))

            print(
                f"PERF sustained: consumed {rate:.0f} B/s (target {_SERIAL_RATE_BPS}), "
                f"injected {injected_rate:.0f} B/s, backlog peak {backlog_peak // 1024} KiB, "
                f"blocks {workspace._output_document.blockCount()}"
            )
            assert injected_rate >= _SERIAL_RATE_BPS * 0.95, "注入端未达目标速率（TCP 背压）"
            assert rate >= _SERIAL_RATE_BPS * 0.95, "UI 实际消费速率未达目标"
            assert backlog_peak <= ring.capacity_bytes + 65536, "积压越过环容量合同"
            assert timings.frames, "持续消费窗口需要实际帧耗时证据"
            assert max(frame["frame_ms"] for frame in timings.frames) <= 150.0, "帧回调超出满载退化上限"
            assert workspace._output_document.blockCount() <= _DOC_BLOCK_LIMIT + 1

            # 突发恢复指标（独立于持续速率）：停止注入后 5 s 内归零
            assert _wait_until(qapplication_session, lambda: ring.backlog_bytes == 0, timeout_s=5.0)
        finally:
            timings.stop()
            if injector is not None:
                injector.stop()
            try:
                if workspace is not None:
                    completed = workspace.shutdown()
                    if not completed and not workspace.shutdown_complete:
                        assert _wait_until(
                            qapplication_session, lambda: workspace.shutdown_complete, timeout_s=10.0,
                        ), "test Shell owner must complete its staged drain before disposal"
            finally:
                try:
                    controller.shutdown()
                finally:
                    try:
                        if workspace is not None:
                            workspace.close()
                            workspace.deleteLater()
                            QCoreApplication.sendPostedEvents(workspace, QEvent.Type.DeferredDelete)
                    finally:
                        server.close()


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
        workspace.request_shutdown()
        assert time.perf_counter() - start < 0.5
        assert _wait_until(qapplication_session, lambda: workspace.shutdown_complete, timeout_s=10.0)
        elapsed = time.perf_counter() - start
        print(f"PERF shutdown drain 300 KiB: {elapsed:.2f} s")
        assert elapsed < 10.0
        assert workspace._active_ring.backlog_bytes == 0
        assert workspace._final_drain_pending is False
        assert workspace._log_writer is None
        text = _plain_text(workspace)
        assert text.count("OK") > 1000  # 消费完成而非丢弃
