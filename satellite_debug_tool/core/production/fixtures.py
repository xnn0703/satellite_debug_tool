"""Shared-fixture coordination and evidence-level enforcement."""

from __future__ import annotations

from dataclasses import dataclass, replace
import time
from typing import Any, Mapping, Optional, Protocol, Sequence
import uuid

from .models import (
    EVIDENCE_RANK,
    EvidenceLevel,
    FixtureAction,
    FixtureActionStatus,
    coerce_evidence_level,
)
from .result_store import ProductionResultStore


class FixtureCoordinatorError(RuntimeError):
    pass


class FixtureAdapter(Protocol):
    @property
    def ready(self) -> bool:
        ...

    @property
    def status_text(self) -> str:
        ...


@dataclass(frozen=True)
class ReferenceStatus:
    connected: bool = False
    qualified: bool = False
    qualification_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class FixtureReadiness:
    device_id: str
    recorder_armed: bool
    identity_valid: bool
    recipe_locked: bool
    fixtures_ready: bool
    safety_confirmed: bool

    @property
    def violations(self) -> tuple[str, ...]:
        missing = []
        if not self.recorder_armed:
            missing.append("recorder_not_armed")
        if not self.identity_valid:
            missing.append("identity_not_valid")
        if not self.recipe_locked:
            missing.append("recipe_not_locked")
        if not self.fixtures_ready:
            missing.append("fixtures_not_ready")
        if not self.safety_confirmed:
            missing.append("safety_not_confirmed")
        return tuple(missing)


class FixtureCoordinator:
    def __init__(
        self,
        store: ProductionResultStore,
        *,
        reference_status: Optional[ReferenceStatus] = None,
    ) -> None:
        self._store = store
        self._reference_status = reference_status or ReferenceStatus()
        self._actions: dict[str, FixtureAction] = {}

    @property
    def reference_status(self) -> ReferenceStatus:
        return self._reference_status

    def set_reference_status(self, status: ReferenceStatus) -> None:
        self._reference_status = status

    def prepare_action(
        self,
        batch_id: str,
        action_type: str,
        readiness: Sequence[FixtureReadiness],
        *,
        command: Optional[Mapping[str, Any]] = None,
    ) -> FixtureAction:
        if not 1 <= len(readiness) <= 4:
            raise FixtureCoordinatorError("fixture action requires one to four devices")
        device_ids = tuple(item.device_id for item in readiness)
        if len(set(device_ids)) != len(device_ids):
            raise FixtureCoordinatorError("fixture action contains duplicate devices")
        violations = {
            item.device_id: item.violations for item in readiness if item.violations
        }
        if violations:
            details = ", ".join(
                f"{device_id}={'+'.join(reasons)}"
                for device_id, reasons in violations.items()
            )
            raise FixtureCoordinatorError(f"fixture barrier is not ready: {details}")
        action = FixtureAction(
            action_id=uuid.uuid4().hex,
            batch_id=str(batch_id),
            action_type=str(action_type),
            participant_ids=device_ids,
            command=dict(command or {}),
        )
        self._store.create_fixture_action(action)
        self._actions[action.action_id] = action
        return action

    def mark_command_sent(
        self, action_id: str, *, result: Optional[Mapping[str, Any]] = None
    ) -> FixtureAction:
        return self._update(
            action_id,
            status=FixtureActionStatus.COMMAND_SENT_UNCONFIRMED,
            evidence_level=EvidenceLevel.COMMAND_SENT,
            result=result,
            started_monotonic_ns=time.monotonic_ns(),
        )

    def mark_motion_observed(
        self, action_id: str, *, result: Optional[Mapping[str, Any]] = None
    ) -> FixtureAction:
        return self._update(
            action_id,
            status=FixtureActionStatus.ACTIVE,
            evidence_level=EvidenceLevel.MOTION_OBSERVED,
            result=result,
        )

    def mark_pose_verified(
        self, action_id: str, *, result: Optional[Mapping[str, Any]] = None
    ) -> FixtureAction:
        if not self._reference_status.connected or not self._reference_status.qualified:
            raise FixtureCoordinatorError(
                "POSE_VERIFIED requires a connected, qualified reference sensor"
            )
        payload = dict(result or {})
        payload.setdefault("reference_qualification_id", self._reference_status.qualification_id)
        return self._update(
            action_id,
            status=FixtureActionStatus.ACTIVE,
            evidence_level=EvidenceLevel.POSE_VERIFIED,
            result=payload,
        )

    def complete_action(
        self, action_id: str, *, result: Optional[Mapping[str, Any]] = None
    ) -> FixtureAction:
        current = self._require_action(action_id)
        return self._update(
            action_id,
            status=FixtureActionStatus.COMPLETED,
            evidence_level=current.evidence_level,
            result=result,
            ended_monotonic_ns=time.monotonic_ns(),
        )

    def fail_action(
        self,
        action_id: str,
        reason: str,
        *,
        aborted: bool = False,
    ) -> FixtureAction:
        current = self._require_action(action_id)
        return self._update(
            action_id,
            status=(
                FixtureActionStatus.ABORTED if aborted else FixtureActionStatus.FAILED
            ),
            evidence_level=current.evidence_level,
            result={"reason": str(reason)},
            ended_monotonic_ns=time.monotonic_ns(),
        )

    def action(self, action_id: str) -> FixtureAction:
        return self._require_action(action_id)

    def _update(
        self,
        action_id: str,
        *,
        status: FixtureActionStatus,
        evidence_level: EvidenceLevel | str,
        result: Optional[Mapping[str, Any]] = None,
        started_monotonic_ns: Optional[int] = None,
        ended_monotonic_ns: Optional[int] = None,
    ) -> FixtureAction:
        current = self._require_action(action_id)
        evidence = coerce_evidence_level(evidence_level)
        if EVIDENCE_RANK[evidence] < EVIDENCE_RANK[current.evidence_level]:
            raise FixtureCoordinatorError("fixture evidence cannot be downgraded")
        merged_result = dict(current.result)
        if result:
            merged_result.update(result)
        updated = replace(
            current,
            status=status,
            evidence_level=evidence,
            result=merged_result,
            started_monotonic_ns=(
                current.started_monotonic_ns
                if started_monotonic_ns is None
                else started_monotonic_ns
            ),
            ended_monotonic_ns=(
                current.ended_monotonic_ns
                if ended_monotonic_ns is None
                else ended_monotonic_ns
            ),
        )
        self._store.update_fixture_action(
            action_id,
            status=updated.status.value,
            evidence_level=updated.evidence_level,
            result=updated.result,
            ended_monotonic_ns=updated.ended_monotonic_ns,
        )
        self._actions[action_id] = updated
        return updated

    def _require_action(self, action_id: str) -> FixtureAction:
        try:
            return self._actions[action_id]
        except KeyError as exc:
            raise FixtureCoordinatorError(f"unknown fixture action: {action_id}") from exc
