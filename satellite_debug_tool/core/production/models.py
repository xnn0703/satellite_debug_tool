"""Stable state and evidence types for the production workspace."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Tuple


class BatchStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"
    INCOMPLETE = "incomplete"


class AttemptStatus(str, Enum):
    PENDING = "pending"
    WAITING_PREREQUISITE = "waiting_prerequisite"
    ARMED = "armed"
    RUNNING = "running"
    ANALYZING = "analyzing"
    PASS = "pass"
    FAIL = "fail"
    INCOMPLETE = "incomplete"
    SKIPPED = "skipped"
    ABORTED = "aborted"


class AttemptPhase(str, Enum):
    PREPARING = "preparing"
    CONVERGING = "converging"
    EFFECTIVE_OBSERVATION = "effective_observation"
    FINISHING = "finishing"


class FixtureActionStatus(str, Enum):
    PREPARED = "prepared"
    COMMAND_SENT_UNCONFIRMED = "command_sent_unconfirmed"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class EvidenceLevel(str, Enum):
    NONE = "none"
    COMMAND_SENT = "command_sent"
    MOTION_OBSERVED = "motion_observed"
    POSE_VERIFIED = "pose_verified"


class ReportStatus(str, Enum):
    NOT_GENERATED = "not_generated"
    GENERATED_V1 = "generated_v1"
    REVIEWED_V2 = "reviewed_v2"
    GENERATION_FAILED = "generation_failed"


EVIDENCE_RANK = {
    EvidenceLevel.NONE: 0,
    EvidenceLevel.COMMAND_SENT: 1,
    EvidenceLevel.MOTION_OBSERVED: 2,
    EvidenceLevel.POSE_VERIFIED: 3,
}


BATCH_TRANSITIONS = {
    BatchStatus.DRAFT: frozenset({BatchStatus.READY, BatchStatus.ABORTED}),
    BatchStatus.READY: frozenset({BatchStatus.RUNNING, BatchStatus.ABORTED}),
    BatchStatus.RUNNING: frozenset(
        {BatchStatus.COMPLETED, BatchStatus.ABORTED, BatchStatus.INCOMPLETE}
    ),
    BatchStatus.COMPLETED: frozenset(),
    BatchStatus.ABORTED: frozenset(),
    BatchStatus.INCOMPLETE: frozenset(),
}


ATTEMPT_TRANSITIONS = {
    AttemptStatus.PENDING: frozenset(
        {
            AttemptStatus.WAITING_PREREQUISITE,
            AttemptStatus.SKIPPED,
            AttemptStatus.ABORTED,
        }
    ),
    AttemptStatus.WAITING_PREREQUISITE: frozenset(
        {
            AttemptStatus.ARMED,
            AttemptStatus.INCOMPLETE,
            AttemptStatus.ABORTED,
        }
    ),
    AttemptStatus.ARMED: frozenset(
        {AttemptStatus.RUNNING, AttemptStatus.INCOMPLETE, AttemptStatus.ABORTED}
    ),
    AttemptStatus.RUNNING: frozenset(
        {
            AttemptStatus.ANALYZING,
            AttemptStatus.INCOMPLETE,
            AttemptStatus.ABORTED,
        }
    ),
    AttemptStatus.ANALYZING: frozenset(
        {
            AttemptStatus.PASS,
            AttemptStatus.FAIL,
            AttemptStatus.INCOMPLETE,
            AttemptStatus.ABORTED,
        }
    ),
    AttemptStatus.PASS: frozenset(),
    AttemptStatus.FAIL: frozenset(),
    AttemptStatus.INCOMPLETE: frozenset(),
    AttemptStatus.SKIPPED: frozenset(),
    AttemptStatus.ABORTED: frozenset(),
}


@dataclass(frozen=True)
class FixtureAction:
    action_id: str
    batch_id: str
    action_type: str
    participant_ids: Tuple[str, ...]
    status: FixtureActionStatus = FixtureActionStatus.PREPARED
    evidence_level: EvidenceLevel = EvidenceLevel.NONE
    command: Mapping[str, Any] = field(default_factory=dict)
    result: Mapping[str, Any] = field(default_factory=dict)
    started_monotonic_ns: Optional[int] = None
    ended_monotonic_ns: Optional[int] = None


def coerce_batch_status(value: BatchStatus | str) -> BatchStatus:
    return value if isinstance(value, BatchStatus) else BatchStatus(str(value))


def coerce_attempt_status(value: AttemptStatus | str) -> AttemptStatus:
    return value if isinstance(value, AttemptStatus) else AttemptStatus(str(value))


def coerce_attempt_phase(value: AttemptPhase | str | None) -> Optional[AttemptPhase]:
    if value is None or isinstance(value, AttemptPhase):
        return value
    return AttemptPhase(str(value))


def coerce_evidence_level(value: EvidenceLevel | str) -> EvidenceLevel:
    return value if isinstance(value, EvidenceLevel) else EvidenceLevel(str(value))
