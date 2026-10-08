"""Deterministic motion-platform qualification sequence using MS-6222 evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from .fixture_analysis import (
    CALIBRATION_SEQUENCE,
    CalibrationStage,
    FixtureAnalysisError,
    FixtureCalibration,
    GuidedFixtureCalibration,
    TimedAttitude,
)
from .motion_platform import PlatformPose


class FixtureQualificationState(str, Enum):
    IDLE = "idle"
    MOVING = "moving"
    SETTLING = "settling"
    CAPTURING = "capturing"
    COMPLETE = "complete"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class FixtureQualificationConfig:
    command_angle_deg: float = 3.0
    move_duration_ms: int = 2000
    settle_duration_ms: int = 1000
    capture_duration_ms: int = 2000
    minimum_samples_per_stage: int = 20

    def validate(self) -> None:
        if not math.isfinite(self.command_angle_deg) or self.command_angle_deg <= 0:
            raise FixtureAnalysisError("qualification command angle must be positive")
        for name, value in (
            ("move_duration_ms", self.move_duration_ms),
            ("settle_duration_ms", self.settle_duration_ms),
            ("capture_duration_ms", self.capture_duration_ms),
            ("minimum_samples_per_stage", self.minimum_samples_per_stage),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise FixtureAnalysisError(f"qualification {name} must be positive")


@dataclass(frozen=True)
class FixtureQualificationResult:
    proposed_calibration: FixtureCalibration
    stage_sample_counts: dict[str, int]
    stage_metrics: dict[str, dict[str, object]]
    measurement_basis: str
    result_class: str = "ENGINEERING_ONLY"
    verdict: str = "INCONCLUSIVE"

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "satellite.fixture-qualification",
            "schema_version": 1,
            "result_class": self.result_class,
            "verdict": self.verdict,
            "proposed_calibration": self.proposed_calibration.to_payload(
                include_hash=True
            ),
            "stage_sample_counts": dict(self.stage_sample_counts),
            "stage_metrics": self.stage_metrics,
            "measurement_basis": self.measurement_basis,
        }


class FixtureQualificationController(QObject):
    """Own the qualification state machine while hardware owners perform I/O."""

    move_requested = Signal(str, object, int)
    stage_changed = Signal(str, str, int, int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        profile_id: str,
        profile_sha256: str,
        center_pose: PlatformPose,
        analysis_calibration: Optional[FixtureCalibration] = None,
        config: Optional[FixtureQualificationConfig] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.config = config or FixtureQualificationConfig()
        self.config.validate()
        self._center_pose = center_pose
        self._analysis_calibration = (
            analysis_calibration
            if analysis_calibration is not None
            and analysis_calibration.coordinate_valid
            else None
        )
        self._guided = GuidedFixtureCalibration(
            profile_id,
            profile_sha256,
            command_angle_deg=self.config.command_angle_deg,
            minimum_samples_per_stage=self.config.minimum_samples_per_stage,
        )
        self._state = FixtureQualificationState.IDLE
        self._index = -1
        self._capture_samples: list[TimedAttitude] = []
        self._response_samples: dict[CalibrationStage, list[TimedAttitude]] = {}
        self._stage_samples: dict[CalibrationStage, tuple[TimedAttitude, ...]] = {}
        self._command_ns: dict[CalibrationStage, int] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timer)

    @property
    def state(self) -> FixtureQualificationState:
        return self._state

    @property
    def active(self) -> bool:
        return self._state in {
            FixtureQualificationState.MOVING,
            FixtureQualificationState.SETTLING,
            FixtureQualificationState.CAPTURING,
        }

    @property
    def current_stage(self) -> Optional[CalibrationStage]:
        if 0 <= self._index < len(CALIBRATION_SEQUENCE):
            return CALIBRATION_SEQUENCE[self._index]
        return None

    def start(self) -> None:
        if self._state != FixtureQualificationState.IDLE:
            raise FixtureAnalysisError("fixture qualification is already started")
        self._index = 0
        self._request_current_move()

    def record_command_sent(self, monotonic_ns: int) -> None:
        stage = self.current_stage
        if self._state == FixtureQualificationState.MOVING and stage is not None:
            self._command_ns[stage] = int(monotonic_ns)

    def add_sample(self, sample: TimedAttitude) -> None:
        if not self.active or not sample.valid:
            return
        stage = self.current_stage
        if stage is None:
            return
        self._response_samples.setdefault(stage, []).append(sample)
        if self._state == FixtureQualificationState.CAPTURING:
            self._capture_samples.append(sample)

    def motion_completed(self) -> None:
        if self._state != FixtureQualificationState.MOVING:
            return
        self._state = FixtureQualificationState.SETTLING
        self._emit_stage()
        self._timer.start(self.config.settle_duration_ms)

    def motion_failed(self, details: str) -> None:
        self._fail(f"qualification motion failed: {details}")

    def stop(self) -> None:
        if not self.active:
            return
        self._timer.stop()
        self._state = FixtureQualificationState.STOPPED
        self.failed.emit("fixture qualification stopped by operator")

    def _request_current_move(self) -> None:
        stage = self.current_stage
        if stage is None:
            self._complete()
            return
        self._state = FixtureQualificationState.MOVING
        self._capture_samples = []
        self._response_samples[stage] = []
        self._emit_stage()
        self.move_requested.emit(
            stage.value,
            _stage_pose(stage, self._center_pose, self.config.command_angle_deg),
            self.config.move_duration_ms,
        )

    def _on_timer(self) -> None:
        if self._state == FixtureQualificationState.SETTLING:
            self._capture_samples = []
            self._state = FixtureQualificationState.CAPTURING
            self._emit_stage()
            self._timer.start(self.config.capture_duration_ms)
            return
        if self._state != FixtureQualificationState.CAPTURING:
            return
        stage = self.current_stage
        if stage is None:
            self._fail("qualification stage disappeared")
            return
        samples = tuple(self._capture_samples)
        try:
            self._guided.record_stage(stage, samples)
        except FixtureAnalysisError as exc:
            self._fail(str(exc))
            return
        self._stage_samples[stage] = samples
        self._index += 1
        self._request_current_move()

    def _complete(self) -> None:
        try:
            calibration = self._guided.evaluate(confirmed=False)
            metric_calibration = self._analysis_calibration or calibration
            metrics = _stage_metrics(
                metric_calibration,
                self._stage_samples,
                self._response_samples,
                self._command_ns,
                self.config.command_angle_deg,
            )
        except FixtureAnalysisError as exc:
            self._fail(str(exc))
            return
        self._state = FixtureQualificationState.COMPLETE
        self.completed.emit(
            FixtureQualificationResult(
                proposed_calibration=calibration,
                stage_sample_counts={
                    stage.value: len(samples)
                    for stage, samples in self._stage_samples.items()
                },
                stage_metrics=metrics,
                measurement_basis=(
                    "CONFIRMED_CALIBRATION"
                    if self._analysis_calibration is not None
                    else "SELF_DERIVED_ENGINEERING_MAPPING"
                ),
            )
        )

    def _fail(self, details: str) -> None:
        if self._state in {
            FixtureQualificationState.COMPLETE,
            FixtureQualificationState.FAILED,
            FixtureQualificationState.STOPPED,
        }:
            return
        self._timer.stop()
        self._state = FixtureQualificationState.FAILED
        self.failed.emit(str(details))

    def _emit_stage(self) -> None:
        stage = self.current_stage
        if stage is None:
            return
        self.stage_changed.emit(
            stage.value,
            self._state.value,
            self._index + 1,
            len(CALIBRATION_SEQUENCE),
        )


def _stage_pose(
    stage: CalibrationStage,
    center: PlatformPose,
    angle_deg: float,
) -> PlatformPose:
    values = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
    if stage not in {CalibrationStage.CENTER_START, CalibrationStage.CENTER_END}:
        axis, direction = stage.value.rsplit("_", 1)
        values[axis] = angle_deg if direction == "plus" else -angle_deg
    return PlatformPose(
        values["roll"],
        values["pitch"],
        values["yaw"],
        center.x_mm,
        center.y_mm,
        center.z_mm,
    )


def _stage_metrics(
    calibration: FixtureCalibration,
    stable_samples: dict[CalibrationStage, tuple[TimedAttitude, ...]],
    response_samples: dict[CalibrationStage, list[TimedAttitude]],
    command_ns: dict[CalibrationStage, int],
    command_angle_deg: float,
) -> dict[str, dict[str, object]]:
    metrics: dict[str, dict[str, object]] = {}
    for stage in CALIBRATION_SEQUENCE:
        stable = [calibration.apply(sample) for sample in stable_samples.get(stage, ())]
        response = [calibration.apply(sample) for sample in response_samples.get(stage, ())]
        target = _stage_pose(stage, PlatformPose(0, 0, 0), command_angle_deg)
        values: dict[str, object] = {
            "target": asdict(target),
            "stable_sample_count": len(stable),
            "response_sample_count": len(response),
        }
        for axis in ("roll", "pitch", "yaw"):
            series = np.asarray(
                [getattr(sample, f"{axis}_deg") for sample in stable], dtype=float
            )
            target_value = getattr(target, f"{axis}_deg")
            values[axis] = {
                "mean_deg": float(np.mean(series)) if len(series) else None,
                "std_deg": float(np.std(series)) if len(series) else None,
                "steady_error_deg": (
                    float(np.mean(series) - target_value) if len(series) else None
                ),
            }
        if stage not in {CalibrationStage.CENTER_START, CalibrationStage.CENTER_END}:
            axis = stage.value.rsplit("_", 1)[0]
            target_value = getattr(target, f"{axis}_deg")
            response_values = np.asarray(
                [getattr(sample, f"{axis}_deg") for sample in response], dtype=float
            )
            response_times = np.asarray(
                [sample.monotonic_ns for sample in response], dtype=np.int64
            )
            started_ns = command_ns.get(stage)
            values["response"] = _response_metrics(
                response_values,
                response_times,
                started_ns,
                target_value,
            )
        metrics[stage.value] = values
    return metrics


def _response_metrics(
    values: np.ndarray,
    times_ns: np.ndarray,
    command_ns: Optional[int],
    target_deg: float,
) -> dict[str, object]:
    if command_ns is None or len(values) < 2 or abs(target_deg) <= 1e-12:
        return {"valid": False}
    baseline = float(np.median(values[: min(10, len(values))]))
    delta = target_deg - baseline
    direction = 1.0 if delta >= 0 else -1.0
    progress = direction * (values - baseline)
    amplitude = abs(delta)

    def crossing(fraction: float) -> Optional[float]:
        indices = np.flatnonzero(progress >= amplitude * fraction)
        if not len(indices):
            return None
        return max(0.0, (int(times_ns[int(indices[0])]) - command_ns) / 1e9)

    onset = crossing(0.1)
    ninety = crossing(0.9)
    return {
        "valid": onset is not None,
        "onset_delay_s": onset,
        "rise_time_10_90_s": (
            ninety - onset if onset is not None and ninety is not None else None
        ),
        "baseline_deg": baseline,
        "peak_deg": float(np.max(values) if direction > 0 else np.min(values)),
    }


__all__ = [
    "FixtureQualificationConfig",
    "FixtureQualificationController",
    "FixtureQualificationResult",
    "FixtureQualificationState",
]
