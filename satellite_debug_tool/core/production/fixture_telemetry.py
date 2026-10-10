"""Typed fixture-attitude telemetry shared by recorders and fixture UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import time
from typing import Mapping, Optional
from uuid import uuid4

from PySide6.QtCore import QObject, Signal


FIXTURE_ATTITUDE_SCHEMA = "fixture_attitude_sample/v1"


class FixtureAttitudeSource(str, Enum):
    PLATFORM_TARGET = "platform_target_attitude"
    MS6222_RELATIVE = "ms6222_relative_attitude"
    MS6222_TRUE_HEADING = "ms6222_true_heading"


@dataclass(frozen=True)
class FixtureAttitudeSample:
    source: FixtureAttitudeSource
    host_monotonic_ns: int
    host_wall_time_ns: int
    roll_deg: Optional[float] = None
    pitch_deg: Optional[float] = None
    yaw_deg: Optional[float] = None
    valid: bool = True
    invalid_reason: str = ""
    quality: Mapping[str, object] = field(default_factory=dict)
    fixture_profile_id: str = ""
    fixture_profile_sha256: str = ""
    calibration_id: str = ""
    control_session_id: str = ""
    sample_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if int(self.host_monotonic_ns) <= 0 or int(self.host_wall_time_ns) <= 0:
            raise ValueError("fixture attitude timestamps must be positive")
        values = (self.roll_deg, self.pitch_deg, self.yaw_deg)
        if any(value is not None and not math.isfinite(float(value)) for value in values):
            raise ValueError("fixture attitude values must be finite")
        if self.valid and self.source is not FixtureAttitudeSource.MS6222_TRUE_HEADING:
            if any(value is None for value in values):
                raise ValueError("valid relative attitude requires roll, pitch and yaw")
        if self.valid and self.source is FixtureAttitudeSource.MS6222_TRUE_HEADING:
            if self.yaw_deg is None:
                raise ValueError("valid true heading requires yaw")
        if not self.valid and not str(self.invalid_reason).strip():
            raise ValueError("invalid fixture attitude requires a reason")

    def metadata_event(self) -> dict[str, object]:
        return {
            "schema": FIXTURE_ATTITUDE_SCHEMA,
            "sample_id": self.sample_id,
            "source": self.source.value,
            "host_monotonic_ns": int(self.host_monotonic_ns),
            "host_wall_time_ns": int(self.host_wall_time_ns),
            "roll_deg": self.roll_deg,
            "pitch_deg": self.pitch_deg,
            "yaw_deg": self.yaw_deg,
            "valid": bool(self.valid),
            "invalid_reason": str(self.invalid_reason),
            "quality": dict(self.quality),
            "fixture_profile_id": str(self.fixture_profile_id),
            "fixture_profile_sha256": str(self.fixture_profile_sha256),
            "calibration_id": str(self.calibration_id),
            "control_session_id": str(self.control_session_id),
        }


class FixtureTelemetryBus(QObject):
    """Publish immutable fixture facts without giving UI access to recorders."""

    sample_published = Signal(object)

    def __init__(self, parent: Optional[QObject] = None, *, ms_rate_hz: float = 50.0) -> None:
        super().__init__(parent)
        self._minimum_ms_interval_ns = int(1_000_000_000 / max(1.0, ms_rate_hz))
        self._last_ms_ns: dict[FixtureAttitudeSource, int] = {}
        self._last_valid: dict[FixtureAttitudeSource, bool] = {}

    def publish(self, sample: FixtureAttitudeSample) -> bool:
        if sample.source in {
            FixtureAttitudeSource.MS6222_RELATIVE,
            FixtureAttitudeSource.MS6222_TRUE_HEADING,
        }:
            previous_ns = self._last_ms_ns.get(sample.source, 0)
            previous_valid = self._last_valid.get(sample.source)
            if (
                previous_valid == sample.valid
                and sample.host_monotonic_ns - previous_ns < self._minimum_ms_interval_ns
            ):
                return False
            self._last_ms_ns[sample.source] = sample.host_monotonic_ns
            self._last_valid[sample.source] = sample.valid
        self.sample_published.emit(sample)
        return True

    @staticmethod
    def now(
        source: FixtureAttitudeSource,
        **values,
    ) -> FixtureAttitudeSample:
        return FixtureAttitudeSample(
            source=source,
            host_monotonic_ns=time.monotonic_ns(),
            host_wall_time_ns=time.time_ns(),
            **values,
        )


__all__ = [
    "FIXTURE_ATTITUDE_SCHEMA",
    "FixtureAttitudeSample",
    "FixtureAttitudeSource",
    "FixtureTelemetryBus",
]
