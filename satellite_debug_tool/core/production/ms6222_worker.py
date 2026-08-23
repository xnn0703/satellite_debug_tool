"""Dedicated passive serial worker for the MS-6222 reference unit."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot
import serial

from .ms6222_protocol import (
    Ms6222FrameEnvelope,
    Ms6222FrameType,
    Ms6222ParserStatistics,
    Ms6222StreamParser,
)


@dataclass(frozen=True)
class Ms6222WorkerStatistics:
    parser: Ms6222ParserStatistics
    connected: bool
    elapsed_s: float
    ins_rate_hz: float
    gnss_rate_hz: float
    rawimu_rate_hz: float
    valid_ratio: float


class Ms6222SerialWorker(QThread):
    frame_received = Signal(object)
    invalid_frame_received = Signal(object)
    statistics_changed = Signal(object)
    connection_changed = Signal(bool, str)
    error_occurred = Signal(str)

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 460800,
        serial_factory=serial.Serial,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._port = str(port)
        self._baudrate = int(baudrate)
        self._serial_factory = serial_factory
        self._stop_event = threading.Event()
        self._serial: Optional[serial.Serial] = None
        self._parser = Ms6222StreamParser()
        self._connected = False
        self._start_monotonic_ns = 0

    @property
    def port(self) -> str:
        return self._port

    @property
    def parser_statistics(self) -> Ms6222ParserStatistics:
        return self._parser.statistics

    def run(self) -> None:
        self._stop_event.clear()
        self._start_monotonic_ns = time.monotonic_ns()
        last_statistics_ns = self._start_monotonic_ns
        try:
            self._serial = self._serial_factory(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
                write_timeout=0.1,
            )
            self._connected = True
            self.connection_changed.emit(True, self._port)
            while not self._stop_event.is_set():
                device = self._serial
                if device is None:
                    break
                waiting = int(getattr(device, "in_waiting", 0) or 0)
                chunk = device.read(max(1, min(waiting, 4096)))
                if chunk:
                    monotonic_ns = time.monotonic_ns()
                    wall_ns = time.time_ns()
                    for envelope in self._parser.feed(
                        chunk,
                        host_monotonic_ns=monotonic_ns,
                        host_wall_time_ns=wall_ns,
                    ):
                        if envelope.valid:
                            self.frame_received.emit(envelope)
                        else:
                            self.invalid_frame_received.emit(envelope)
                now_ns = time.monotonic_ns()
                if now_ns - last_statistics_ns >= 1_000_000_000:
                    self.statistics_changed.emit(self.statistics(now_ns=now_ns))
                    last_statistics_ns = now_ns
        except (OSError, serial.SerialException, ValueError) as exc:
            if not self._stop_event.is_set():
                self.error_occurred.emit(str(exc))
        finally:
            device = self._serial
            self._serial = None
            if device is not None:
                try:
                    device.close()
                except (OSError, serial.SerialException):
                    pass
            was_connected = self._connected
            self._connected = False
            if was_connected:
                self.connection_changed.emit(False, self._port)
            self.statistics_changed.emit(self.statistics())

    def statistics(self, *, now_ns: Optional[int] = None) -> Ms6222WorkerStatistics:
        current_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
        elapsed_s = max(
            0.0,
            (current_ns - self._start_monotonic_ns) / 1_000_000_000.0,
        ) if self._start_monotonic_ns else 0.0
        stats = self._parser.statistics
        total = stats.valid_frames + stats.invalid_frames
        denominator = max(elapsed_s, 1e-9)
        return Ms6222WorkerStatistics(
            parser=stats,
            connected=self._connected,
            elapsed_s=elapsed_s,
            ins_rate_hz=stats.ins_frames / denominator,
            gnss_rate_hz=stats.gnss_frames / denominator,
            rawimu_rate_hz=stats.rawimu_frames / denominator,
            valid_ratio=stats.valid_frames / total if total else 0.0,
        )

    @Slot()
    def stop(self) -> None:
        self._stop_event.set()
        device = self._serial
        if device is not None:
            try:
                device.cancel_read()
            except (AttributeError, OSError, serial.SerialException):
                pass


__all__ = ["Ms6222SerialWorker", "Ms6222WorkerStatistics"]
