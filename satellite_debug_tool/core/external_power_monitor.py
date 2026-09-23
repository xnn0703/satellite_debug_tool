"""Process-wide, read-only GW Instek PSW 80-27 monitoring."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import ipaddress
import math
import socket
import time
from typing import Callable, Optional, Protocol

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal, Slot

from satellite_debug_tool.core.production.power_supply import (
    PowerIdentity,
    PowerMeasurement,
    PowerSupplyError,
    ScpiLineCodec,
    parse_bool,
    parse_identity,
    parse_int,
    parse_measurement,
)


EXTERNAL_POWER_HISTORY_SECONDS = 30.0 * 60.0
EXTERNAL_POWER_PORT = 2268
EXTERNAL_POWER_SAMPLE_EVENT = "external_power_sample/v1"
_READ_ONLY_QUERIES = frozenset(
    {
        "*IDN?",
        "OUTP?",
        "MEAS:ALL?",
        "STAT:OPER:COND?",
        "STAT:QUES:COND?",
        "OUTP:PROT:TRIP?",
    }
)


class ExternalPowerPhase(str, Enum):
    UNCONFIGURED = "unconfigured"
    INACTIVE = "inactive"
    CONNECTING = "connecting"
    ONLINE = "online"
    READ_FAILED = "read_failed"


@dataclass(frozen=True)
class ExternalPowerConfig:
    host: str
    port: int = EXTERNAL_POWER_PORT
    connect_timeout_s: float = 2.0
    command_timeout_s: float = 2.0
    poll_interval_s: float = 1.0
    reconnect_interval_s: float = 3.0
    max_line_bytes: int = 1024

    def validate(self) -> None:
        try:
            parsed = ipaddress.ip_address(self.host.strip())
        except ValueError as exc:
            raise PowerSupplyError("external power host must be an IPv4 address") from exc
        if parsed.version != 4:
            raise PowerSupplyError("external power host must be an IPv4 address")
        if int(self.port) != EXTERNAL_POWER_PORT:
            raise PowerSupplyError("external power port must be 2268")
        for name, value in (
            ("connect_timeout_s", self.connect_timeout_s),
            ("command_timeout_s", self.command_timeout_s),
            ("poll_interval_s", self.poll_interval_s),
            ("reconnect_interval_s", self.reconnect_interval_s),
        ):
            if not math.isfinite(float(value)) or float(value) <= 0.0:
                raise PowerSupplyError(f"{name} must be finite and positive")
        if not (64 <= int(self.max_line_bytes) <= 65536):
            raise PowerSupplyError("max_line_bytes must be 64..65536")


@dataclass(frozen=True)
class ExternalPowerSample:
    host_timestamp_ns: int
    monotonic_ns: int
    connection_generation: int
    identity: PowerIdentity
    voltage_v: float
    current_a: float
    power_w: float
    output_enabled: bool
    operation_condition: int
    questionable_condition: int
    protection_tripped: bool

    def metadata_event(self) -> dict[str, object]:
        return {
            "event": EXTERNAL_POWER_SAMPLE_EVENT,
            "host_timestamp_ns": self.host_timestamp_ns,
            "connection_generation": self.connection_generation,
            "identity": {
                "manufacturer": self.identity.manufacturer,
                "model": self.identity.model,
                "serial_number": self.identity.serial_number,
                "firmware": self.identity.firmware,
            },
            "voltage_v": self.voltage_v,
            "current_a": self.current_a,
            "power_w": self.power_w,
            "output_enabled": self.output_enabled,
            "operation_condition": self.operation_condition,
            "questionable_condition": self.questionable_condition,
            "protection_tripped": self.protection_tripped,
        }


@dataclass(frozen=True)
class ExternalPowerSnapshot:
    phase: ExternalPowerPhase
    host: str = ""
    generation: int = 0
    identity: Optional[PowerIdentity] = None
    sample: Optional[ExternalPowerSample] = None
    error: str = ""


class ReadOnlyPowerSession(Protocol):
    @property
    def identity(self) -> Optional[PowerIdentity]: ...

    def connect(self) -> PowerIdentity: ...

    def read_sample(self, generation: int) -> ExternalPowerSample: ...

    def close(self) -> None: ...


class PswReadOnlySession:
    """A strict query-only PSW session with no command-writing surface."""

    def __init__(
        self,
        config: ExternalPowerConfig,
        *,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
        wall_clock_ns: Callable[[], int] = time.time_ns,
        monotonic_clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        config.validate()
        self._config = config
        self._socket_factory = socket_factory
        self._wall_clock_ns = wall_clock_ns
        self._monotonic_clock_ns = monotonic_clock_ns
        self._socket: Optional[socket.socket] = None
        self._codec = ScpiLineCodec(config.max_line_bytes)
        self._identity: Optional[PowerIdentity] = None

    @property
    def identity(self) -> Optional[PowerIdentity]:
        return self._identity

    def connect(self) -> PowerIdentity:
        self.close()
        try:
            sock = self._socket_factory(
                (self._config.host, self._config.port),
                timeout=self._config.connect_timeout_s,
            )
            sock.settimeout(self._config.command_timeout_s)
        except OSError as exc:
            raise PowerSupplyError(f"cannot connect to external power supply: {exc}") from exc
        self._socket = sock
        try:
            identity = parse_identity(self._query("*IDN?"))
            if identity.manufacturer.strip().casefold() != "GW-INSTEK".casefold():
                raise PowerSupplyError(
                    f"unexpected external power manufacturer: {identity.manufacturer}"
                )
            if identity.model.strip().casefold() != "PSW 80-27".casefold():
                raise PowerSupplyError(
                    f"unexpected external power model: {identity.model}"
                )
        except Exception:
            self.close()
            raise
        self._identity = identity
        return identity

    def read_sample(self, generation: int) -> ExternalPowerSample:
        identity = self._identity
        if identity is None:
            raise PowerSupplyError("external power identity has not been verified")
        output = parse_bool(self._query("OUTP?"))
        measurement: PowerMeasurement = parse_measurement(self._query("MEAS:ALL?"))
        operation = parse_int(self._query("STAT:OPER:COND?"))
        questionable = parse_int(self._query("STAT:QUES:COND?"))
        tripped = parse_bool(self._query("OUTP:PROT:TRIP?"))
        voltage = float(measurement.voltage_v)
        current = float(measurement.current_a)
        power = voltage * current
        if not all(math.isfinite(value) for value in (voltage, current, power)):
            raise PowerSupplyError("external power measurement must be finite")
        return ExternalPowerSample(
            host_timestamp_ns=self._wall_clock_ns(),
            monotonic_ns=self._monotonic_clock_ns(),
            connection_generation=int(generation),
            identity=identity,
            voltage_v=voltage,
            current_a=current,
            power_w=power,
            output_enabled=output,
            operation_condition=operation,
            questionable_condition=questionable,
            protection_tripped=tripped,
        )

    def close(self) -> None:
        sock, self._socket = self._socket, None
        self._identity = None
        self._codec.reset()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _query(self, command: str) -> str:
        normalized = str(command).strip().upper()
        if normalized not in _READ_ONLY_QUERIES:
            raise PowerSupplyError(f"external power query is not allowed: {command}")
        sock = self._socket
        if sock is None:
            raise PowerSupplyError("external power supply is not connected")
        try:
            sock.sendall((normalized + "\n").encode("ascii"))
            while True:
                chunk = sock.recv(1024)
                if not chunk:
                    raise PowerSupplyError("external power supply closed the connection")
                lines = self._codec.feed(chunk)
                if not lines:
                    continue
                if len(lines) != 1 or self._codec.buffered_bytes:
                    self._codec.reset()
                    raise PowerSupplyError("unexpected extra external power response data")
                response = lines[0].strip()
                if not response:
                    raise PowerSupplyError("external power query returned an empty response")
                return response
        except (OSError, PowerSupplyError) as exc:
            raise PowerSupplyError(f"external power query failed: {exc}") from exc


class ExternalPowerStore(QObject):
    updated = Signal()
    sample_received = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._snapshot = ExternalPowerSnapshot(ExternalPowerPhase.UNCONFIGURED)
        self._history: deque[ExternalPowerSample] = deque()

    @property
    def snapshot(self) -> ExternalPowerSnapshot:
        return self._snapshot

    @Slot(object)
    def apply_snapshot(self, snapshot: ExternalPowerSnapshot) -> None:
        self._snapshot = snapshot
        self.updated.emit()

    @Slot(object)
    def apply_sample(self, sample: ExternalPowerSample) -> None:
        cutoff_ns = int(sample.monotonic_ns - EXTERNAL_POWER_HISTORY_SECONDS * 1e9)
        self._history.append(sample)
        while self._history and self._history[0].monotonic_ns < cutoff_ns:
            self._history.popleft()
        self._snapshot = ExternalPowerSnapshot(
            phase=ExternalPowerPhase.ONLINE,
            host=self._snapshot.host,
            generation=sample.connection_generation,
            identity=sample.identity,
            sample=sample,
        )
        self.sample_received.emit(sample)
        self.updated.emit()

    def clear(self, *, host: str = "", phase: ExternalPowerPhase = ExternalPowerPhase.UNCONFIGURED) -> None:
        self._history.clear()
        self._snapshot = ExternalPowerSnapshot(phase=phase, host=str(host))
        self.updated.emit()

    def history(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        samples = tuple(self._history)
        return (
            np.asarray([sample.host_timestamp_ns / 1e9 for sample in samples], dtype=float),
            np.asarray([sample.voltage_v for sample in samples], dtype=float),
            np.asarray([sample.current_a for sample in samples], dtype=float),
        )


class ExternalPowerWorker(QThread):
    state_changed = Signal(object)
    sample_ready = Signal(object)

    def __init__(
        self,
        config: ExternalPowerConfig,
        *,
        session_factory: Callable[[ExternalPowerConfig], ReadOnlyPowerSession] = PswReadOnlySession,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        config.validate()
        self._config = config
        self._session_factory = session_factory
        self._initial_generation = 0

    def set_initial_generation(self, generation: int) -> None:
        if self.isRunning():
            raise RuntimeError("cannot change generation while external power worker is running")
        self._initial_generation = max(0, int(generation))

    def run(self) -> None:
        generation = self._initial_generation
        while not self.isInterruptionRequested():
            generation += 1
            self.state_changed.emit(
                ExternalPowerSnapshot(
                    ExternalPowerPhase.CONNECTING,
                    host=self._config.host,
                    generation=generation,
                )
            )
            session = self._session_factory(self._config)
            try:
                identity = session.connect()
                self.state_changed.emit(
                    ExternalPowerSnapshot(
                        ExternalPowerPhase.CONNECTING,
                        host=self._config.host,
                        generation=generation,
                        identity=identity,
                    )
                )
                while not self.isInterruptionRequested():
                    self.sample_ready.emit(session.read_sample(generation))
                    if self._wait_interruptibly(self._config.poll_interval_s):
                        break
            except (OSError, PowerSupplyError, ValueError) as exc:
                self.state_changed.emit(
                    ExternalPowerSnapshot(
                        ExternalPowerPhase.READ_FAILED,
                        host=self._config.host,
                        generation=generation,
                        error=str(exc),
                    )
                )
            finally:
                session.close()
            if self.isInterruptionRequested():
                break
            if self._wait_interruptibly(self._config.reconnect_interval_s):
                break

    def _wait_interruptibly(self, duration_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(duration_s))
        while not self.isInterruptionRequested():
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            self.msleep(max(1, min(50, int(remaining * 1000.0))))
        return True


class ExternalPowerMonitor(QObject):
    """Own worker lifetime and preserve one Store across customer endpoint switches."""

    def __init__(
        self,
        store: ExternalPowerStore,
        *,
        worker_factory: Callable[[ExternalPowerConfig], ExternalPowerWorker] = ExternalPowerWorker,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._worker_factory = worker_factory
        self._host = ""
        self._active = False
        self._worker: Optional[ExternalPowerWorker] = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def active(self) -> bool:
        return self._active

    def configure(self, host: str) -> None:
        normalized = str(host).strip()
        if normalized:
            ExternalPowerConfig(normalized).validate()
        if normalized == self._host:
            return
        self._stop_worker()
        self._host = normalized
        self._store.clear(
            host=normalized,
            phase=(
                ExternalPowerPhase.INACTIVE
                if normalized
                else ExternalPowerPhase.UNCONFIGURED
            ),
        )
        self._sync_worker()

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if active == self._active:
            return
        self._active = active
        if not active:
            self._stop_worker()
            self._store.apply_snapshot(
                ExternalPowerSnapshot(
                    ExternalPowerPhase.INACTIVE,
                    host=self._host,
                    generation=self._store.snapshot.generation,
                )
                if self._host
                else ExternalPowerSnapshot(ExternalPowerPhase.UNCONFIGURED)
            )
            return
        self._sync_worker()

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        self._active = False
        return self._stop_worker(timeout_ms=timeout_ms)

    def _sync_worker(self) -> None:
        if not self._active or not self._host or self._worker is not None:
            return
        worker = self._worker_factory(ExternalPowerConfig(self._host))
        worker.set_initial_generation(self._store.snapshot.generation)
        worker.state_changed.connect(self._store.apply_snapshot)
        worker.sample_ready.connect(self._store.apply_sample)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _stop_worker(self, timeout_ms: int = 5000) -> bool:
        worker = self._worker
        if worker is None:
            return True
        worker.requestInterruption()
        if not worker.wait(max(0, int(timeout_ms))):
            return False
        if self._worker is worker:
            self._worker = None
        worker.deleteLater()
        return True

    @Slot()
    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if worker is self._worker:
            self._worker = None


__all__ = [
    "EXTERNAL_POWER_HISTORY_SECONDS",
    "EXTERNAL_POWER_PORT",
    "EXTERNAL_POWER_SAMPLE_EVENT",
    "ExternalPowerConfig",
    "ExternalPowerMonitor",
    "ExternalPowerPhase",
    "ExternalPowerSample",
    "ExternalPowerSnapshot",
    "ExternalPowerStore",
    "ExternalPowerWorker",
    "PswReadOnlySession",
]
