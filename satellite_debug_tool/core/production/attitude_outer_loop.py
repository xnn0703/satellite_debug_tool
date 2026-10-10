"""Bounded low-bandwidth attitude outer-loop decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

from satellite_debug_tool.core.motion_platform import PlatformPose

from .fixture_analysis import TimedAttitude


class OuterLoopStatus(str, Enum):
    CORRECTION_REQUIRED = "correction_required"
    TARGET_VERIFIED = "target_verified"
    ITERATION_LIMIT = "iteration_limit"
    DIVERGING = "diverging"


@dataclass(frozen=True)
class AttitudeOuterLoopConfig:
    gain: float = 0.6
    deadband_deg: float = 0.05
    maximum_step_deg: float = 0.5
    maximum_iterations: int = 3
    divergence_ratio: float = 1.25

    def validate(self) -> None:
        if not 0.0 < self.gain <= 1.0:
            raise ValueError("outer-loop gain must be within (0, 1]")
        if self.deadband_deg <= 0.0 or self.maximum_step_deg <= 0.0:
            raise ValueError("outer-loop deadband and maximum step must be positive")
        if self.maximum_iterations < 1:
            raise ValueError("outer-loop maximum iterations must be positive")
        if self.divergence_ratio <= 1.0:
            raise ValueError("outer-loop divergence ratio must exceed one")


@dataclass(frozen=True)
class OuterLoopDecision:
    status: OuterLoopStatus
    error_deg: tuple[float, float, float]
    error_norm_deg: float
    iteration: int
    command_pose: PlatformPose | None = None


class BoundedAttitudeOuterLoop:
    def __init__(self, config: AttitudeOuterLoopConfig | None = None) -> None:
        self.config = config or AttitudeOuterLoopConfig()
        self.config.validate()
        self._desired: PlatformPose | None = None
        self._command: PlatformPose | None = None
        self._iterations = 0
        self._previous_error_norm: float | None = None

    @property
    def active(self) -> bool:
        return self._desired is not None and self._command is not None

    def begin(self, desired_pose: PlatformPose) -> None:
        self._desired = desired_pose
        self._command = desired_pose
        self._iterations = 0
        self._previous_error_norm = None

    def stop(self) -> None:
        self._desired = None
        self._command = None
        self._iterations = 0
        self._previous_error_norm = None

    def evaluate(self, measured: TimedAttitude) -> OuterLoopDecision:
        if not self.active or self._desired is None or self._command is None:
            raise RuntimeError("attitude outer loop is not active")
        error = (
            _angle_delta(self._desired.roll_deg, measured.roll_deg),
            _angle_delta(self._desired.pitch_deg, measured.pitch_deg),
            _angle_delta(self._desired.yaw_deg, measured.yaw_deg),
        )
        norm = math.sqrt(sum(value * value for value in error))
        if all(abs(value) <= self.config.deadband_deg for value in error):
            decision = OuterLoopDecision(
                OuterLoopStatus.TARGET_VERIFIED, error, norm, self._iterations
            )
            self.stop()
            return decision
        if (
            self._previous_error_norm is not None
            and norm > self._previous_error_norm * self.config.divergence_ratio
        ):
            decision = OuterLoopDecision(
                OuterLoopStatus.DIVERGING, error, norm, self._iterations
            )
            self.stop()
            return decision
        if self._iterations >= self.config.maximum_iterations:
            decision = OuterLoopDecision(
                OuterLoopStatus.ITERATION_LIMIT, error, norm, self._iterations
            )
            self.stop()
            return decision
        correction = tuple(
            max(
                -self.config.maximum_step_deg,
                min(self.config.maximum_step_deg, value * self.config.gain),
            )
            for value in error
        )
        self._command = PlatformPose(
            self._command.roll_deg + correction[0],
            self._command.pitch_deg + correction[1],
            self._command.yaw_deg + correction[2],
            self._command.x_mm,
            self._command.y_mm,
            self._command.z_mm,
        )
        self._iterations += 1
        self._previous_error_norm = norm
        return OuterLoopDecision(
            OuterLoopStatus.CORRECTION_REQUIRED,
            error,
            norm,
            self._iterations,
            self._command,
        )


def _angle_delta(target_deg: float, measured_deg: float) -> float:
    return (float(target_deg) - float(measured_deg) + 180.0) % 360.0 - 180.0


__all__ = [
    "AttitudeOuterLoopConfig",
    "BoundedAttitudeOuterLoop",
    "OuterLoopDecision",
    "OuterLoopStatus",
]
