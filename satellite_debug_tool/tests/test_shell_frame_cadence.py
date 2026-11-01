"""帧起点周期合同回归：假时钟/单次 timer，独立于真实平台计时精度。"""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
import shiboken6

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.tcp_shell.terminal import TerminalUpdate
from satellite_debug_tool.ui import tcp_shell_workspace as module


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def advance(self, seconds):
        self.now += seconds


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self):
        for callback in tuple(self.callbacks):
            callback()


class FakeTimer:
    def __init__(self, clock, parent=None):
        self.clock = clock
        self.timeout = FakeSignal()
        self.interval_ms = 0
        self.timer_type = Qt.TimerType.CoarseTimer
        self.single_shot = False
        self.active = False
        self.due = None
        self.starts = []

    def setSingleShot(self, enabled):
        self.single_shot = enabled

    def setInterval(self, milliseconds):
        self.interval_ms = milliseconds

    def setTimerType(self, timer_type):
        self.timer_type = timer_type

    def start(self, milliseconds=None):
        if milliseconds is not None:
            self.interval_ms = milliseconds
        self.active = True
        self.due = self.clock.now + self.interval_ms / 1000
        self.starts.append((self.clock.now, self.interval_ms))

    def stop(self):
        self.active = False

    def isActive(self):
        return self.active

    def fire(self, *, early=False):
        assert self.active
        if not early:
            self.clock.now = max(self.clock.now, self.due)
        if self.single_shot:
            self.active = False
        self.timeout.emit()


@pytest.fixture
def frame_harness(qapplication_session, tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    monkeypatch.setattr(module, "QTimer", lambda parent=None: FakeTimer(clock, parent))
    workspace = module.TcpShellWorkspace(Settings(config_directory=tmp_path))
    work = SimpleNamespace(seconds=0.020, calls=0)

    def project(*args, **kwargs):
        work.calls += 1
        clock.advance(work.seconds)

    monkeypatch.setattr(workspace, "_ingest_segment", lambda *_args: TerminalUpdate(0, 0))
    monkeypatch.setattr(workspace, "_project_updates", project)
    monkeypatch.setattr(workspace, "_sync_terminal_cursor", lambda: None)
    monkeypatch.setattr(workspace, "_refresh_backlog_indicator", lambda *_args: None)
    monkeypatch.setattr(workspace, "_append_event", lambda *_args: None)
    monkeypatch.setattr(workspace, "_sync_controls", lambda: None)
    yield SimpleNamespace(workspace=workspace, clock=clock, work=work, timer=workspace._frame_timer)
    workspace._frame_timer.stop()
    workspace._controller.shutdown()
    shiboken6.delete(workspace)


def test_frame_work_is_inside_start_period(frame_harness):
    h = frame_harness
    h.workspace._active_ring.push(b"x" * 32768)
    h.workspace._on_rx_pending()
    h.timer.fire()
    # 首帧起点33ms，工作20ms结束于53ms；下一帧仍应在66ms附近，而不是86ms。
    assert h.work.calls == 1
    assert 0.066 <= h.timer.due <= 0.0671


def test_overrun_enqueues_one_frame_without_inline_catchup(frame_harness):
    h = frame_harness
    h.work.seconds = 0.040
    h.workspace._active_ring.push(b"x" * 32768)
    h.workspace._on_rx_pending()
    h.timer.fire()
    assert h.work.calls == 1
    assert h.timer.active
    assert h.timer.starts[-1][1] == 0
    assert h.timer.due == h.clock.now
    assert len(h.timer.starts) == 2


def test_early_timeout_preserves_start_lower_bound(frame_harness):
    h = frame_harness
    h.workspace._active_ring.push(b"x")
    h.workspace._on_rx_pending()
    assert h.timer.timer_type == Qt.TimerType.PreciseTimer
    h.clock.advance(0.032)
    h.timer.fire(early=True)
    assert h.work.calls == 0
    assert h.timer.active
    assert h.timer.due >= 0.033


def test_synchronous_feed_does_not_arm_realtime_schedule(frame_harness):
    h = frame_harness
    h.workspace.feed_rx(b"x" * 20480)
    assert h.workspace._active_ring.backlog_bytes == 0
    assert h.timer.active is False
    assert h.timer.starts == []


@pytest.mark.parametrize("entry", ("pending", "disconnect", "shutdown"))
def test_repeated_realtime_intent_preserves_armed_deadline(frame_harness, entry):
    h = frame_harness
    h.workspace._active_ring.push(b"x" * 32768)

    def request():
        if entry == "pending":
            h.workspace._on_rx_pending()
        elif entry == "disconnect":
            h.workspace._on_disconnected("operator")
        else:
            h.workspace._on_shutdown_transport_finished()

    request()
    deadline = h.timer.due
    h.clock.advance(0.005)
    request()
    assert h.timer.due == deadline
    assert len(h.timer.starts) == 1


def test_final_drain_finishes_once_and_releases_schedule(frame_harness, monkeypatch):
    h = frame_harness
    finished = []
    monkeypatch.setattr(h.workspace._decoder, "finish", lambda: finished.append(True) or [])
    h.workspace._on_disconnected("operator")
    h.timer.fire()
    assert finished == [True]
    assert h.workspace._final_drain_pending is False
    assert h.timer.active is False
    assert len(h.timer.starts) == 1
