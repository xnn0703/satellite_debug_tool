"""Serialized GW Instek PSW80-27 monitoring and closed-loop control."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import ipaddress
import math
import queue
import time
from typing import Callable, Optional

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal, Slot

from satellite_debug_tool.core.production.power_supply import (
    PowerIdentity,
    PowerActionResult,
    PowerSupplyConfig,
    PowerSupplyError,
    GwInstekPswAdapter,
    psw80_27_validation_policy,
)


EXTERNAL_POWER_HISTORY_SECONDS = 30.0 * 60.0
EXTERNAL_POWER_PORT = 2268
EXTERNAL_POWER_SAMPLE_EVENT = "external_power_sample/v1"

EXTERNAL_POWER_ACTION_EVENT = "external_power_action/v1"


class ExternalPowerPhase(str, Enum):
    UNCONFIGURED = "unconfigured"
    INACTIVE = "inactive"
    CONNECTING = "connecting"
    ONLINE = "online"
    READ_FAILED = "read_failed"


@dataclass(frozen=True)
class ExternalPowerConfig:
    host: str
    voltage_set_v: float = 12.0
    current_set_a: float = 12.0
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
        psw80_27_validation_policy(float(self.voltage_set_v), float(self.current_set_a))
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

    def supply_config(self) -> PowerSupplyConfig:
        policy = psw80_27_validation_policy(
            float(self.voltage_set_v), float(self.current_set_a)
        )
        return PowerSupplyConfig(
            host=self.host,
            port=self.port,
            voltage_set_v=self.voltage_set_v,
            current_set_a=self.current_set_a,
            voltage_setpoint_tolerance_v=policy.voltage_setpoint_tolerance_v,
            current_setpoint_tolerance_a=policy.current_setpoint_tolerance_a,
            output_voltage_min_v=policy.output_voltage_min_v,
            output_voltage_max_v=policy.output_voltage_max_v,
            off_voltage_max_v=policy.off_voltage_max_v,
            command_timeout_s=self.command_timeout_s,
            connect_timeout_s=self.connect_timeout_s,
            output_settle_timeout_s=3.0,
            max_line_bytes=self.max_line_bytes,
        )


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


class ExternalPowerAction(str, Enum):
    PREPARE = "prepare"
    ENABLE = "enable"
    DISABLE = "disable"
    READ = "read"


@dataclass(frozen=True)
class ExternalPowerActionOutcome:
    action: ExternalPowerAction
    action_id: str
    succeeded: bool
    sample: Optional[ExternalPowerSample] = None
    error: str = ""

    def metadata_event(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "event": EXTERNAL_POWER_ACTION_EVENT,
            "action": self.action.value,
            "action_id": self.action_id,
            "succeeded": self.succeeded,
            "error": self.error,
        }
        if self.sample is not None:
            sample_payload = self.sample.metadata_event()
            sample_payload.pop("event", None)
            sample_payload.pop("host_timestamp_ns", None)
            payload.update(sample_payload)
        return payload


class PswControlledSession:
    """One adapter instance owns both polling and explicit control actions."""

    def __init__(self, config: ExternalPowerConfig) -> None:
        config.validate()
        self._adapter = GwInstekPswAdapter(config.supply_config())

    @property
    def identity(self) -> Optional[PowerIdentity]:
        return self._adapter.identity

    def connect(self) -> PowerIdentity:
        return self._adapter.connect()

    def read_sample(self, generation: int) -> ExternalPowerSample:
        return self._sample(self._adapter.inspect(), generation)

    def execute(
        self,
        action: ExternalPowerAction,
        action_id: str,
        generation: int,
    ) -> ExternalPowerSample:
        if action is ExternalPowerAction.PREPARE:
            result = self._adapter.prepare_output_off(fixture_action_id=action_id)
        elif action is ExternalPowerAction.ENABLE:
            result = self._adapter.enable_output(fixture_action_id=action_id)
        elif action is ExternalPowerAction.DISABLE:
            result = self._adapter.disable_output(fixture_action_id=action_id)
        elif action is ExternalPowerAction.READ:
            result = self._adapter.inspect(fixture_action_id=action_id)
        else:  # pragma: no cover - exhaustive enum guard
            raise PowerSupplyError(f"unsupported external power action: {action}")
        return self._sample(result, generation)

    def close(self) -> None:
        self._adapter.close()

    def _sample(
        self,
        result: PowerActionResult,
        generation: int,
    ) -> ExternalPowerSample:
        identity = self._adapter.identity
        if identity is None:
            raise PowerSupplyError("external power identity has not been verified")
        voltage = float(result.measurement.voltage_v)
        current = float(result.measurement.current_a)
        return ExternalPowerSample(
            host_timestamp_ns=time.time_ns(),
            monotonic_ns=time.monotonic_ns(),
            connection_generation=int(generation),
            identity=identity,
            voltage_v=voltage,
            current_a=current,
            power_w=voltage * current,
            output_enabled=result.output_enabled,
            operation_condition=result.operation_condition,
            questionable_condition=result.questionable_condition,
            protection_tripped=result.protection_tripped,
        )


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
    action_finished = Signal(object)

    def __init__(
        self,
        config: ExternalPowerConfig,
        *,
        session_factory: Callable[[ExternalPowerConfig], object] = PswControlledSession,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        config.validate()
        self._config = config
        self._session_factory = session_factory
        self._initial_generation = 0
        self._actions: queue.Queue[tuple[ExternalPowerAction, str]] = queue.Queue()

    def request_action(self, action: ExternalPowerAction, action_id: str) -> None:
        self._actions.put((ExternalPowerAction(action), str(action_id).strip()))

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
                    try:
                        action, action_id = self._actions.get_nowait()
                    except queue.Empty:
                        action = None
                    if action is None:
                        self.sample_ready.emit(session.read_sample(generation))
                    else:
                        try:
                            sample = session.execute(action, action_id, generation)
                        except (OSError, PowerSupplyError, ValueError) as exc:
                            self.action_finished.emit(
                                ExternalPowerActionOutcome(
                                    action=action,
                                    action_id=action_id,
                                    succeeded=False,
                                    error=str(exc),
                                )
                            )
                        else:
                            self.sample_ready.emit(sample)
                            self.action_finished.emit(
                                ExternalPowerActionOutcome(
                                    action=action,
                                    action_id=action_id,
                                    succeeded=True,
                                    sample=sample,
                                )
                            )
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

    action_finished = Signal(object)

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
        self._voltage_set_v = 12.0
        self._current_set_a = 12.0
        self._active = False
        self._worker: Optional[ExternalPowerWorker] = None
        self._pending_actions = 0

    @property
    def host(self) -> str:
        return self._host

    @property
    def active(self) -> bool:
        return self._active

    @property
    def action_pending(self) -> bool:
        return self._pending_actions > 0

    def configure(
        self,
        host: str,
        voltage_set_v: float = 12.0,
        current_set_a: float = 12.0,
    ) -> None:
        normalized = str(host).strip()
        if normalized:
            ExternalPowerConfig(
                normalized,
                voltage_set_v=float(voltage_set_v),
                current_set_a=float(current_set_a),
            ).validate()
        if (
            normalized == self._host
            and float(voltage_set_v) == self._voltage_set_v
            and float(current_set_a) == self._current_set_a
        ):
            return
        self._stop_worker()
        self._host = normalized
        self._voltage_set_v = float(voltage_set_v)
        self._current_set_a = float(current_set_a)
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

    def request_action(self, action: ExternalPowerAction, action_id: str = "") -> None:
        worker = self._worker
        if worker is None or not worker.isRunning():
            raise PowerSupplyError("external power supply is not connected")
        token = str(action_id).strip() or f"customer-{time.time_ns()}"
        self._pending_actions += 1
        worker.request_action(action, token)

    def _sync_worker(self) -> None:
        if not self._active or not self._host or self._worker is not None:
            return
        worker = self._worker_factory(
            ExternalPowerConfig(
                self._host,
                voltage_set_v=self._voltage_set_v,
                current_set_a=self._current_set_a,
            )
        )
        worker.set_initial_generation(self._store.snapshot.generation)
        worker.state_changed.connect(self._store.apply_snapshot)
        worker.sample_ready.connect(self._store.apply_sample)
        worker.action_finished.connect(self._on_action_finished)
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
        self._pending_actions = 0
        worker.deleteLater()
        return True

    @Slot()
    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if worker is self._worker:
            self._worker = None
        self._pending_actions = 0

    @Slot(object)
    def _on_action_finished(self, outcome: ExternalPowerActionOutcome) -> None:
        self._pending_actions = max(0, self._pending_actions - 1)
        self.action_finished.emit(outcome)


__all__ = [
    "EXTERNAL_POWER_HISTORY_SECONDS",
    "EXTERNAL_POWER_PORT",
    "EXTERNAL_POWER_ACTION_EVENT",
    "EXTERNAL_POWER_SAMPLE_EVENT",
    "ExternalPowerConfig",
    "ExternalPowerAction",
    "ExternalPowerActionOutcome",
    "ExternalPowerMonitor",
    "ExternalPowerPhase",
    "ExternalPowerSample",
    "ExternalPowerSnapshot",
    "ExternalPowerStore",
    "ExternalPowerWorker",
    "PswControlledSession",
]
