"""MS-6222 passive serial worker lifecycle tests."""

from __future__ import annotations

from collections import deque
import os
import time

from satellite_debug_tool.tests.test_ms6222_protocol import _ins_frame


class _FakeSerial:
    def __init__(self, **_kwargs) -> None:
        frame = _ins_frame()
        self._chunks = deque((frame[:19], frame[19:83], frame[83:]))
        self.closed = False

    @property
    def in_waiting(self) -> int:
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, _size: int) -> bytes:
        if self._chunks:
            return self._chunks.popleft()
        time.sleep(0.002)
        return b""

    def cancel_read(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _wait_until(qapp, predicate, *, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return bool(predicate())


def test_worker_passively_parses_fragmented_serial_stream() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from satellite_debug_tool.core.production import (
        Ms6222InsRecord,
        Ms6222SerialWorker,
    )

    qapp = QApplication.instance() or QApplication([])
    frames = []
    connection_states = []
    worker = Ms6222SerialWorker("FAKE-RS422", serial_factory=_FakeSerial)
    worker.frame_received.connect(frames.append)
    worker.connection_changed.connect(
        lambda connected, port: connection_states.append((connected, port))
    )

    worker.start()
    assert _wait_until(qapp, lambda: len(frames) == 1)
    statistics = worker.statistics()
    worker.stop()
    assert worker.wait(2000)
    qapp.processEvents()

    assert isinstance(frames[0].record, Ms6222InsRecord)
    assert frames[0].record.yaw_deg == 359.5
    assert statistics.parser.ins_frames == 1
    assert statistics.parser.valid_frames == 1
    assert connection_states[0] == (True, "FAKE-RS422")
    assert connection_states[-1] == (False, "FAKE-RS422")
