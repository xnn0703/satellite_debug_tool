"""Authoritative attempt progression for the first automatic production flow."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Sequence

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


class ProductionRunCoordinator(QObject):
    """Advance attempts only after explicit prerequisite, start, and result facts.

    Hardware owners consume ``step_requested`` and report the three facts through
    ``confirm_prerequisites``, ``confirm_started`` and ``complete_step``.  This
    class does not infer hardware success from elapsed time.
    """

    step_requested = Signal(object)
    state_changed = Signal(object)

    FIRST_FLOW_TEST_IDS = (
        "firmware_verification",
        "parameter_verification",
        "static_acquisition",
        "locked_rocking",
        "power_on_rocking",
    )

    def __init__(
        self,
        store: ProductionResultStore,
        batch_id: str,
        *,
        test_ids: Sequence[str] = FIRST_FLOW_TEST_IDS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._batch_id = str(batch_id)
        requested = tuple(str(value) for value in test_ids)
        unknown = tuple(value for value in requested if value not in self.FIRST_FLOW_TEST_IDS)
        if unknown:
            raise ValueError("unsupported automatic test: " + ", ".join(unknown))
        if len(set(requested)) != len(requested):
            raise ValueError("automatic test IDs must be unique")
        self._test_ids = requested
        self._state = ProductionRunState.IDLE
        self._step_index = -1
        self._participants: tuple[str, ...] = ()

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
        if set(results) != set(self._participants):
            raise ValueError("step results must contain every frozen participant exactly once")
        attempts = self._current_attempts(AttemptStatus.RUNNING)
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
            payload.setdefault("verdict", "pass" if outcome.passed else "fail")
            self._store.transition_attempt(
                int(attempt["attempt_id"]),
                AttemptStatus.PASS if outcome.passed else AttemptStatus.FAIL,
                result=payload,
            )
        if self._step_index + 1 >= len(self._test_ids):
            self._set_state(ProductionRunState.COMPLETE)
            return None
        return self._request_next_step()

    def fail_shared_step(self, reason: str, *, evidence: Mapping[str, object]) -> None:
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
            payload.update(
                {"reason": str(reason), "shared_evidence": dict(evidence)}
            )
            self._store.transition_attempt(
                int(attempt["attempt_id"]), AttemptStatus.INCOMPLETE, result=payload
            )
        self._set_state(ProductionRunState.INCOMPLETE)

    def abort(self, reason: str) -> None:
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
                result={"reason": str(reason)},
            )
        self._set_state(ProductionRunState.ABORTED)

    def _request_next_step(self) -> ProductionStepRequest:
        self._step_index += 1
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
        self.state_changed.emit(state)


__all__ = [
    "DeviceStepResult",
    "ProductionRunCoordinator",
    "ProductionRunState",
    "ProductionStepRequest",
]
