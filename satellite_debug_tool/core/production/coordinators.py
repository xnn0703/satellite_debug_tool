"""Authoritative coordinators for production batches and fixture sessions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Sequence

from PySide6.QtCore import QObject, Signal

from .fixture_profile import (
    FixtureControlLease,
    FixtureLeaseError,
    FixtureLeaseHandle,
    WorkstationFixtureProfile,
)
from .fixture_session import (
    FixtureSessionRecorder,
    FixtureSessionResult,
)
from .models import AttemptStatus, BatchStatus
from .recipe import ProductionRecipe
from .result_store import ProductionResultStore, ResultStoreError


class FixtureSessionState(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class FixtureSessionContext:
    recorder: FixtureSessionRecorder
    session_dir: Path


class BatchCoordinator(QObject):
    """Own batch state transitions, result-store lifetime, and fixture lease."""

    changed = Signal(object)

    def __init__(
        self,
        lease: FixtureControlLease,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._lease = lease
        self._lease_handle: Optional[FixtureLeaseHandle] = None
        self._store: Optional[ProductionResultStore] = None
        self._batch: Optional[dict] = None
        self._output_dir: Optional[Path] = None

    @property
    def store(self) -> Optional[ProductionResultStore]:
        return self._store

    @property
    def batch(self) -> Optional[dict]:
        return None if self._batch is None else dict(self._batch)

    @property
    def output_dir(self) -> Optional[Path]:
        return self._output_dir

    @property
    def lease_active(self) -> bool:
        return self._lease.held_by(self._lease_handle)

    def reserve_start(self) -> None:
        """Reserve the batch fixture owner before asynchronous fixture preparation."""
        _store, batch = self._require_batch(BatchStatus.READY)
        if self.lease_active:
            return
        try:
            self._lease_handle = self._lease.acquire(f"batch:{batch['batch_id']}")
        except FixtureLeaseError as exc:
            raise ResultStoreError(str(exc)) from exc

    def cancel_start_reservation(self) -> None:
        if self._batch is None or self._batch.get("status") != BatchStatus.READY.value:
            return
        self._release_lease()

    def reset_terminal_batch(self) -> None:
        """Release the terminal batch so one workspace can own the next batch."""

        if self._batch is None:
            return
        if self._batch.get("status") not in {
            BatchStatus.COMPLETED.value,
            BatchStatus.INCOMPLETE.value,
            BatchStatus.ABORTED.value,
        }:
            raise ResultStoreError("only a terminal batch can be reset")
        if self.lease_active:
            raise ResultStoreError("batch fixture lease is still active")
        store = self._store
        self._store = None
        self._batch = None
        self._output_dir = None
        if store is not None:
            store.close()

    def create(
        self,
        *,
        batch_id: str,
        recipe: ProductionRecipe,
        operator: str,
        output_dir: Path,
        notes: str = "",
        weather: str = "",
        ambient_temperature_c: float | None = None,
        ambient_humidity_percent: float | None = None,
    ) -> dict:
        if self._batch is not None:
            raise ResultStoreError("a batch is already active in this workspace")
        output = Path(output_dir)
        store = ProductionResultStore(output / "batch.sqlite3")
        try:
            batch = store.create_batch(
                batch_id,
                recipe,
                operator=operator,
                output_dir=output,
                notes=notes,
                weather=weather,
                ambient_temperature_c=ambient_temperature_c,
                ambient_humidity_percent=ambient_humidity_percent,
            )
        except Exception:
            store.close()
            raise
        self._store = store
        self._batch = batch
        self._output_dir = output
        self.changed.emit(dict(batch))
        return dict(batch)

    def start(
        self,
        participant_serials: Sequence[str],
        test_ids: Sequence[str],
        *,
        freeze_participants: Callable[[], None],
    ) -> dict:
        store, batch = self._require_batch(BatchStatus.READY)
        handle = self._lease_handle
        if not self._lease.held_by(handle):
            try:
                handle = self._lease.acquire(f"batch:{batch['batch_id']}")
            except FixtureLeaseError as exc:
                raise ResultStoreError(str(exc)) from exc
        try:
            updated = store.start_batch(
                batch["batch_id"],
                participant_serials,
                test_ids,
            )
            freeze_participants()
        except Exception:
            self._lease.release(handle)
            self._lease_handle = None
            current = store.get_batch(batch["batch_id"])
            if current.get("status") == BatchStatus.RUNNING.value:
                current = store.transition_batch(
                    batch["batch_id"],
                    BatchStatus.INCOMPLETE,
                )
                self._batch = current
                self.changed.emit(dict(current))
            raise
        self._lease_handle = handle
        self._batch = updated
        self.changed.emit(dict(updated))
        return dict(updated)

    def abort(self) -> dict:
        store, batch = self._require_batch()
        if batch["status"] not in {BatchStatus.READY.value, BatchStatus.RUNNING.value}:
            raise ResultStoreError("abort requires a ready or running batch")
        for attempt in store.list_attempts(batch["batch_id"]):
            if attempt["status"] in {
                AttemptStatus.PENDING.value,
                AttemptStatus.WAITING_PREREQUISITE.value,
                AttemptStatus.ARMED.value,
                AttemptStatus.RUNNING.value,
                AttemptStatus.ANALYZING.value,
            }:
                store.transition_attempt(
                    int(attempt["attempt_id"]),
                    AttemptStatus.ABORTED,
                )
        updated = store.transition_batch(batch["batch_id"], BatchStatus.ABORTED)
        self._batch = updated
        self._release_lease()
        self.changed.emit(dict(updated))
        return dict(updated)

    _VEHICLE_TEST_IDS = frozenset({"locked_drive", "power_on_drive"})

    def suspend(self) -> dict:
        """台架段完成且车辆项待操作员门禁时挂起批次；报告归档被终态门禁拒绝。"""

        store, batch = self._require_batch(BatchStatus.RUNNING)
        pending = [
            attempt
            for attempt in store.list_attempts(batch["batch_id"])
            if attempt["status"] == AttemptStatus.PENDING.value
        ]
        non_vehicle = [
            attempt for attempt in pending
            if attempt["test_id"] not in self._VEHICLE_TEST_IDS
        ]
        if non_vehicle:
            raise ResultStoreError(
                "only vehicle items may stay pending at suspension: "
                + ", ".join(sorted({item["test_id"] for item in non_vehicle}))
            )
        if not pending:
            return self.complete()
        updated = store.transition_batch(batch["batch_id"], BatchStatus.SUSPENDED)
        self._batch = updated
        self.changed.emit(dict(updated))
        return dict(updated)

    def complete(self) -> dict:
        """Commit a completed batch after every attempt reached a final result.

        待测项全部为车辆工况时批次转为挂起，等待操作员门禁的车载段。
        """

        store, batch = self._require_batch(BatchStatus.RUNNING)
        attempts = store.list_attempts(batch["batch_id"])
        if not attempts:
            raise ResultStoreError("a completed batch requires test attempts")
        pending = [
            attempt
            for attempt in attempts
            if attempt["status"] == AttemptStatus.PENDING.value
        ]
        if pending and all(
            attempt["test_id"] in self._VEHICLE_TEST_IDS for attempt in pending
        ):
            return self.suspend()
        active = [
            attempt
            for attempt in attempts
            if attempt["status"]
            not in {
                AttemptStatus.PASS.value,
                AttemptStatus.FAIL.value,
                AttemptStatus.SKIPPED.value,
                AttemptStatus.INCOMPLETE.value,
                AttemptStatus.ABORTED.value,
            }
        ]
        if active:
            raise ResultStoreError("all attempts must reach a final result")
        incomplete = [
            attempt
            for attempt in attempts
            if attempt["status"]
            in {AttemptStatus.INCOMPLETE.value, AttemptStatus.ABORTED.value}
        ]
        target = BatchStatus.INCOMPLETE if incomplete else BatchStatus.COMPLETED
        updated = store.transition_batch(batch["batch_id"], target)
        self._batch = updated
        self._release_lease()
        self.changed.emit(dict(updated))
        return dict(updated)

    def mark_incomplete(self) -> Optional[dict]:
        if self._store is None or self._batch is None:
            return None
        if self._batch.get("status") != BatchStatus.RUNNING.value:
            return dict(self._batch)
        for attempt in self._store.list_attempts(self._batch["batch_id"]):
            status = attempt["status"]
            if status in {
                AttemptStatus.WAITING_PREREQUISITE.value,
                AttemptStatus.ARMED.value,
                AttemptStatus.RUNNING.value,
                AttemptStatus.ANALYZING.value,
            }:
                self._store.transition_attempt(
                    int(attempt["attempt_id"]),
                    AttemptStatus.INCOMPLETE,
                )
            elif status == AttemptStatus.PENDING.value:
                self._store.transition_attempt(
                    int(attempt["attempt_id"]),
                    AttemptStatus.ABORTED,
                )
        self._batch = self._store.transition_batch(
            self._batch["batch_id"],
            BatchStatus.INCOMPLETE,
        )
        self._release_lease()
        self.changed.emit(dict(self._batch))
        return dict(self._batch)

    def record_event(self, event_type: str, payload: dict) -> None:
        if self._store is None or self._batch is None:
            return
        self._store.record_event(self._batch["batch_id"], event_type, payload)

    def skip_attempt(self, attempt_id: int, *, result: dict) -> dict:
        """Record a coordinator-approved not-applicable production attempt."""

        store, _batch = self._require_batch(BatchStatus.RUNNING)
        updated = store.transition_attempt(
            int(attempt_id),
            AttemptStatus.SKIPPED,
            result=dict(result),
        )
        self.changed.emit(self.batch)
        return updated

    def adopt_suspended(self, *, store, batch_id: str, output_dir: Path) -> dict:
        """接续一个挂起批次：重绑存储与批次事实，状态保持 SUSPENDED。"""

        if self._batch is not None:
            raise ResultStoreError("a batch is already active in this workspace")
        batch = store.get_batch(str(batch_id))
        if batch["status"] != BatchStatus.SUSPENDED.value:
            raise ResultStoreError("resume requires a suspended batch")
        self._store = store
        self._batch = batch
        self._output_dir = Path(output_dir)
        self.changed.emit(dict(batch))
        return dict(batch)

    def resume_retest(self) -> dict:
        """核验补测：INCOMPLETE 批次回到运行态，等待新建的补测 attempt。"""

        store, batch = self._require_batch(BatchStatus.INCOMPLETE)
        updated = store.transition_batch(batch["batch_id"], BatchStatus.RUNNING)
        self._batch = updated
        self.changed.emit(dict(updated))
        return dict(updated)

    def resume_vehicle_phase(self) -> dict:
        """操作员门禁开始车载段：挂起批次回到运行态。"""

        store, batch = self._require_batch(BatchStatus.SUSPENDED)
        updated = store.transition_batch(batch["batch_id"], BatchStatus.RUNNING)
        self._batch = updated
        self.changed.emit(dict(updated))
        return dict(updated)

    def close(self) -> None:
        self.mark_incomplete()
        self._release_lease()
        if self._store is not None:
            self._store.close()
        self._store = None

    def _require_batch(
        self,
        status: Optional[BatchStatus] = None,
    ) -> tuple[ProductionResultStore, dict]:
        if self._store is None or self._batch is None:
            raise ResultStoreError("no batch is ready")
        if status is not None and self._batch.get("status") != status.value:
            raise ResultStoreError(
                f"batch must be {status.value}"
            )
        return self._store, self._batch

    def _release_lease(self) -> None:
        handle = self._lease_handle
        self._lease_handle = None
        if handle is not None:
            self._lease.release(handle)


class FixtureSessionCoordinator(QObject):
    """Own fixture-session recorder lifetime and exclusive control lease."""

    state_changed = Signal(object)

    def __init__(
        self,
        lease: FixtureControlLease,
        *,
        session_root: Optional[Path],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._lease = lease
        self._session_root = None if session_root is None else Path(session_root)
        self._lease_handle: Optional[FixtureLeaseHandle] = None
        self._recorder: Optional[FixtureSessionRecorder] = None
        self._state = FixtureSessionState.IDLE

    @property
    def state(self) -> FixtureSessionState:
        return self._state

    @property
    def recorder(self) -> Optional[FixtureSessionRecorder]:
        return self._recorder

    @property
    def active(self) -> bool:
        return (
            self._recorder is not None
            and self._lease.held_by(self._lease_handle)
            and self._state in {
                FixtureSessionState.RECORDING,
                FixtureSessionState.FINALIZING,
            }
        )

    def begin(
        self,
        profile: WorkstationFixtureProfile,
        *,
        operator: str,
    ) -> FixtureSessionContext:
        if self.active or self._recorder is not None:
            raise RuntimeError("fixture session is already active")
        recorder = FixtureSessionRecorder(
            profile,
            operator=operator,
            root=self._session_root,
        )
        handle = self._lease.acquire(f"fixture-debug:{recorder.session_id}")
        try:
            session_dir = recorder.start()
        except Exception:
            self._lease.release(handle)
            raise
        self._recorder = recorder
        self._lease_handle = handle
        self._set_state(FixtureSessionState.RECORDING)
        return FixtureSessionContext(recorder, session_dir)

    def begin_finalization(self) -> FixtureSessionRecorder:
        if self._recorder is None or self._state != FixtureSessionState.RECORDING:
            raise RuntimeError("fixture session is not recording")
        self._set_state(FixtureSessionState.FINALIZING)
        return self._recorder

    def complete(self, result: FixtureSessionResult) -> None:
        state = (
            FixtureSessionState.COMPLETE
            if result.status == "complete"
            else FixtureSessionState.INCOMPLETE
        )
        self._release(state)

    def finalization_failed(self) -> None:
        self._release(FixtureSessionState.INCOMPLETE)

    def abort(self, *, reason: str, summary: Optional[dict] = None) -> None:
        recorder = self._recorder
        if recorder is not None and self._state != FixtureSessionState.FINALIZING:
            recorder.abort(reason=reason, summary=summary)
        self._release(FixtureSessionState.INCOMPLETE)

    def _release(self, state: FixtureSessionState) -> None:
        handle = self._lease_handle
        self._lease_handle = None
        self._recorder = None
        if handle is not None:
            self._lease.release(handle)
        self._set_state(state)

    def reset(self) -> None:
        if self.active:
            raise RuntimeError("active fixture session must be finalized")
        self._set_state(FixtureSessionState.IDLE)

    def _set_state(self, state: FixtureSessionState) -> None:
        if self._state == state:
            return
        self._state = state
        self.state_changed.emit(state)


__all__ = [
    "BatchCoordinator",
    "FixtureSessionContext",
    "FixtureSessionCoordinator",
    "FixtureSessionState",
]
