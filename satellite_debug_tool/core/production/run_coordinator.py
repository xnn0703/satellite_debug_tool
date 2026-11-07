"""Authoritative attempt progression for the first automatic production flow."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import time
from typing import Callable, Mapping, Optional, Sequence

from PySide6.QtCore import QObject, Signal

from .models import AttemptPhase, AttemptStatus, BatchStatus
from .result_store import ProductionResultStore, ResultStoreError


class ProductionRunState(str, Enum):
    IDLE = "idle"
    WAITING_PREREQUISITE = "waiting_prerequisite"
    ARMED = "armed"
    RUNNING = "running"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    ABORTED = "aborted"


@dataclass(frozen=True)
class ProductionStepRequest:
    batch_id: str
    test_id: str
    participant_serials: tuple[str, ...]
    ordinal: int


@dataclass(frozen=True)
class DeviceStepResult:
    passed: bool
    result: Mapping[str, object]
    incomplete: bool = False
    skipped: bool = False


@dataclass(frozen=True)
class ProductionProgress:
    test_id: str
    state: ProductionRunState
    phase: AttemptPhase
    ordinal: int
    total: int
    elapsed_s: float
    effective_s: Mapping[str, float]
    remaining_s: Optional[float]
    reason: str


class ProductionRunCoordinator(QObject):
    """Advance attempts only after explicit prerequisite, start, and result facts.

    Hardware owners consume ``step_requested`` and report the three facts through
    ``confirm_prerequisites``, ``confirm_started`` and ``complete_step``.  This
    class does not infer hardware success from elapsed time.
    """

    step_requested = Signal(object)
    state_changed = Signal(object)
    progress_changed = Signal(object)

    FIRST_FLOW_TEST_IDS = (
        "firmware_verification",
        "parameter_verification",
        "static_acquisition",
        "locked_rocking",
        "power_on_rocking",
    )

    VEHICLE_TEST_IDS = (
        "locked_drive",
        "power_on_drive",
    )

    def __init__(
        self,
        store: ProductionResultStore,
        batch_id: str,
        *,
        test_ids: Sequence[str] = FIRST_FLOW_TEST_IDS,
        parent: Optional[QObject] = None,
        clock: Callable[[], float] = time.monotonic,
        observation_s: Optional[float] = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._batch_id = str(batch_id)
        requested = tuple(str(value) for value in test_ids)
        unknown = tuple(
            value for value in requested
            if value not in (*self.FIRST_FLOW_TEST_IDS, *self.VEHICLE_TEST_IDS)
        )
        if unknown:
            raise ValueError("unsupported automatic test: " + ", ".join(unknown))
        if len(set(requested)) != len(requested):
            raise ValueError("automatic test IDs must be unique")
        self._test_ids = requested
        self._state = ProductionRunState.IDLE
        self._step_index = -1
        self._participants: tuple[str, ...] = ()
        self._clock = clock
        if observation_s is not None and (not math.isfinite(observation_s) or observation_s <= 0):
            raise ValueError("observation duration must be finite and positive")
        self._observation_s = observation_s
        self._step_started_at = self._clock()
        self._step_finished_at: Optional[float] = None
        self._phase = AttemptPhase.PREPARING
        self._reason = ""
        self._effective: dict[str, float] = {}
        self._observation_started_at: Optional[float] = None
        self._last_observation_at: Optional[float] = None
        self._previous_valid: frozenset[str] = frozenset()

    @property
    def observation_started_at(self) -> float | None:
        return self._observation_started_at

    @property
    def observation_deadline(self) -> float | None:
        if self._observation_started_at is None or self._observation_s is None:
            return None
        return self._observation_started_at + self._observation_s

    @property
    def progress(self) -> ProductionProgress:
        now = self._clock() if self._step_finished_at is None else self._step_finished_at
        timed = self.current_test_id in {
            "static_acquisition", "locked_rocking", "power_on_rocking",
            "locked_drive", "power_on_drive",
        }
        remaining = None
        if timed and self._observation_s is not None:
            deadline = self.observation_deadline
            remaining = self._observation_s if deadline is None else max(0.0, deadline - now)
        return ProductionProgress(
            self.current_test_id, self._state, self._phase,
            self._step_index + 1, len(self._test_ids),
            max(0.0, now - self._step_started_at),
            dict(self._effective), remaining, self._reason,
        )

    def explain_wait(self, reason: str) -> None:
        self._reason = str(reason)
        self.progress_changed.emit(self.progress)

    def set_observation_duration(self, duration_s: float) -> None:
        """当前步骤准备时冻结本项工况时长。"""
        self._require_state(ProductionRunState.WAITING_PREREQUISITE)
        if not math.isfinite(duration_s) or duration_s <= 0:
            raise ValueError("observation duration must be finite and positive")
        self._observation_s = float(duration_s)

    def observe(self, valid_serials: Sequence[str], *, acquired_serials: Sequence[str], maximum_gap_s: float) -> bool:
        """固定工况窗口持续计时；独立保留每台设备的正 SNR 证据时长。"""
        self._require_state(ProductionRunState.RUNNING)
        if self._observation_s is None:
            raise RuntimeError("observation duration requires a recipe")
        if not math.isfinite(maximum_gap_s) or maximum_gap_s <= 0:
            raise ValueError("observation gap must be finite and positive")
        current = frozenset(valid_serials)
        acquired = frozenset(acquired_serials)
        if not acquired.issubset(self._participants) or not current.issubset(acquired):
            raise ValueError("observation requires known acquired participants")
        now = self._clock()
        elapsed = 0.0 if self._last_observation_at is None else now - self._last_observation_at
        if 0 <= elapsed <= maximum_gap_s:
            for serial in current & self._previous_valid:
                self._effective[serial] = min(self._observation_s, self._effective[serial] + elapsed)
        self._last_observation_at = now
        self._previous_valid = current
        self._phase = (AttemptPhase.EFFECTIVE_OBSERVATION
                       if acquired == frozenset(self._participants) else AttemptPhase.CONVERGING)
        self.progress_changed.emit(self.progress)
        return self.progress.remaining_s == 0

    @property
    def state(self) -> ProductionRunState:
        return self._state

    @property
    def current_test_id(self) -> str:
        if not 0 <= self._step_index < len(self._test_ids):
            return ""
        return self._test_ids[self._step_index]

    def start(self) -> ProductionStepRequest | None:
        if self._state != ProductionRunState.IDLE:
            raise RuntimeError("production run has already started")
        batch = self._store.get_batch(self._batch_id)
        if batch["status"] != BatchStatus.RUNNING.value:
            raise ResultStoreError("automatic run requires a running batch")
        self._participants = tuple(
            device["serial_number"]
            for device in self._store.list_devices(self._batch_id)
            if device["status"] == "participant"
        )
        if not self._participants:
            raise ResultStoreError("automatic run requires frozen participants")
        if not self._test_ids:
            self._set_state(ProductionRunState.COMPLETE)
            return None
        return self._request_next_step()

    def confirm_prerequisites(self, *, evidence: Mapping[str, object]) -> None:
        self._require_state(ProductionRunState.WAITING_PREREQUISITE)
        self._transition_current_attempts(
            AttemptStatus.ARMED,
            phase=AttemptPhase.PREPARING,
            result={"prerequisite_evidence": dict(evidence)},
        )
        self._set_state(ProductionRunState.ARMED)

    def confirm_started(self, *, evidence: Mapping[str, object]) -> None:
        self._require_state(ProductionRunState.ARMED)
        self._phase = AttemptPhase.CONVERGING
        self._observation_started_at = self._clock()
        self._transition_current_attempts(
            AttemptStatus.RUNNING,
            phase=AttemptPhase.CONVERGING,
            result={"start_evidence": dict(evidence)},
        )
        self._set_state(ProductionRunState.RUNNING)

    def complete_step(
        self,
        results: Mapping[str, DeviceStepResult],
    ) -> ProductionStepRequest | None:
        self._require_state(ProductionRunState.RUNNING)
        return self._complete_results(results, AttemptStatus.RUNNING)

    def complete_preparation(self, results: Mapping[str, DeviceStepResult]) -> ProductionStepRequest | None:
        """前置条件未满足时保存判定，不生成硬件已启动证据。"""
        if self._state not in {ProductionRunState.WAITING_PREREQUISITE, ProductionRunState.ARMED}:
            raise RuntimeError("preparation result requires a preparing step")
        if any(result.passed or result.skipped for result in results.values()):
            raise ValueError("preparation failure requires FAIL or INCOMPLETE results")
        status = (AttemptStatus.ARMED if self._state is ProductionRunState.ARMED
                  else AttemptStatus.WAITING_PREREQUISITE)
        return self._complete_results(results, status)

    def _complete_results(self, results: Mapping[str, DeviceStepResult],
                          status: AttemptStatus) -> ProductionStepRequest | None:
        if set(results) != set(self._participants):
            raise ValueError("step results must contain every frozen participant exactly once")
        attempts = self._current_attempts(status)
        self._phase = AttemptPhase.FINISHING
        for serial in self._participants:
            attempt = attempts[serial]
            self._store.transition_attempt(
                int(attempt["attempt_id"]),
                AttemptStatus.ANALYZING,
                phase=AttemptPhase.FINISHING,
            )
            outcome = results[serial]
            payload = dict(attempt.get("result_json") or {})
            payload.update(outcome.result)
            payload["effective_observation_s"] = self._effective[serial]
            payload["step_elapsed_s"] = self.progress.elapsed_s
            verdict = "skipped" if outcome.skipped else "incomplete" if outcome.incomplete else "pass" if outcome.passed else "fail"
            payload.setdefault("verdict", verdict)
            self._store.transition_attempt(
                int(attempt["attempt_id"]),
                AttemptStatus.SKIPPED if outcome.skipped else AttemptStatus.INCOMPLETE if outcome.incomplete else AttemptStatus.PASS if outcome.passed else AttemptStatus.FAIL,
                result=payload,
            )
        if self._step_index + 1 >= len(self._test_ids):
            self._set_state(ProductionRunState.COMPLETE)
            return None
        return self._request_next_step()

    def fail_shared_step(self, reason: str, *, evidence: Mapping[str, object],
                         device_results: Mapping[str, Mapping[str, object]] | None = None) -> None:
        if self._state not in {
            ProductionRunState.WAITING_PREREQUISITE,
            ProductionRunState.ARMED,
            ProductionRunState.RUNNING,
        }:
            raise RuntimeError("no active production step")
        status = {
            ProductionRunState.WAITING_PREREQUISITE: AttemptStatus.WAITING_PREREQUISITE,
            ProductionRunState.ARMED: AttemptStatus.ARMED,
            ProductionRunState.RUNNING: AttemptStatus.RUNNING,
        }[self._state]
        for attempt in self._current_attempts(status).values():
            payload = dict(attempt.get("result_json") or {})
            payload.update((device_results or {}).get(attempt["serial_number"], {}))
            payload.update(
                {"reason": str(reason), "shared_evidence": dict(evidence)}
            )
            self._store.transition_attempt(
                int(attempt["attempt_id"]), AttemptStatus.INCOMPLETE, result=payload
            )
        self._set_state(ProductionRunState.INCOMPLETE)

    def abort(self, reason: str, *, device_results: Mapping[str, Mapping[str, object]] | None = None) -> None:
        if self._state in {
            ProductionRunState.COMPLETE,
            ProductionRunState.INCOMPLETE,
            ProductionRunState.ABORTED,
        }:
            return
        active = {
            AttemptStatus.WAITING_PREREQUISITE.value,
            AttemptStatus.ARMED.value,
            AttemptStatus.RUNNING.value,
        }
        for attempt in self._store.list_attempts(self._batch_id):
            if attempt["test_id"] != self.current_test_id or attempt["status"] not in active:
                continue
            self._store.transition_attempt(
                int(attempt["attempt_id"]),
                AttemptStatus.ABORTED,
                result={**dict(attempt.get("result_json") or {}),
                        **dict((device_results or {}).get(attempt["serial_number"], {})), "reason": str(reason)},
            )
        self._set_state(ProductionRunState.ABORTED)

    def _request_next_step(self) -> ProductionStepRequest:
        self._step_index += 1
        self._step_started_at = self._clock()
        self._step_finished_at = None
        self._phase = AttemptPhase.PREPARING
        self._reason = ""
        self._effective = {serial: 0.0 for serial in self._participants}
        self._observation_started_at = None
        self._last_observation_at = None
        self._previous_valid = frozenset()
        test_id = self.current_test_id
        attempts = self._attempts_for_test(test_id)
        for serial in self._participants:
            attempt = attempts.get(serial)
            if attempt is None or attempt["status"] != AttemptStatus.PENDING.value:
                raise ResultStoreError(
                    f"automatic attempt is not pending: {test_id}/{serial}"
                )
            self._store.transition_attempt(
                int(attempt["attempt_id"]),
                AttemptStatus.WAITING_PREREQUISITE,
                phase=AttemptPhase.PREPARING,
            )
        request = ProductionStepRequest(
            self._batch_id, test_id, self._participants, self._step_index + 1
        )
        self._set_state(ProductionRunState.WAITING_PREREQUISITE)
        self.step_requested.emit(request)
        return request

    def _attempts_for_test(self, test_id: str) -> dict[str, dict]:
        return {
            attempt["serial_number"]: attempt
            for attempt in self._store.list_attempts(self._batch_id)
            if attempt["test_id"] == test_id
            and attempt["serial_number"] in self._participants
        }

    def _current_attempts(self, status: AttemptStatus) -> dict[str, dict]:
        attempts = self._attempts_for_test(self.current_test_id)
        invalid = {
            serial: attempt["status"]
            for serial, attempt in attempts.items()
            if attempt["status"] != status.value
        }
        if set(attempts) != set(self._participants) or invalid:
            raise ResultStoreError("current automatic attempt state is inconsistent")
        return attempts

    def _transition_current_attempts(
        self,
        status: AttemptStatus,
        *,
        phase: AttemptPhase,
        result: Mapping[str, object],
    ) -> None:
        expected = {
            AttemptStatus.ARMED: AttemptStatus.WAITING_PREREQUISITE,
            AttemptStatus.RUNNING: AttemptStatus.ARMED,
        }[status]
        for attempt in self._current_attempts(expected).values():
            merged = dict(attempt.get("result_json") or {})
            merged.update(result)
            self._store.transition_attempt(
                int(attempt["attempt_id"]), status, phase=phase, result=merged
            )

    def _require_state(self, state: ProductionRunState) -> None:
        if self._state != state:
            raise RuntimeError(
                f"production run must be {state.value}; current state is {self._state.value}"
            )

    def _set_state(self, state: ProductionRunState) -> None:
        self._state = state
        if state in {ProductionRunState.COMPLETE, ProductionRunState.INCOMPLETE, ProductionRunState.ABORTED}:
            self._step_finished_at = self._clock()
        self.state_changed.emit(state)
        self.progress_changed.emit(self.progress)


__all__ = [
    "DeviceStepResult",
    "ProductionRunCoordinator",
    "ProductionRunState",
    "ProductionStepRequest",
]
