"""运动平台轨迹预检和绝对单调时钟调度。"""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Callable

from .controller import DirectMotionPlatformController
from .lingjing_protocol import solve_actuator_targets
from .models import MotionActionResult, MotionPlatformError, PlatformPose


@dataclass(frozen=True)
class SineAxis:
    amplitude_deg: float
    frequency_hz: float
    phase_deg: float = 0.0
    enabled: bool = True

    def validate(self) -> None:
        if not all(math.isfinite(value) for value in (self.amplitude_deg, self.frequency_hz, self.phase_deg)):
            raise MotionPlatformError("sine axis values must be finite")
        if self.enabled and (self.amplitude_deg <= 0 or self.frequency_hz <= 0):
            raise MotionPlatformError("enabled sine axis requires positive amplitude and frequency")


@dataclass(frozen=True)
class SineTrajectory:
    sample_period_ms: int
    ramp_in_s: float
    steady_duration_s: float
    ramp_out_s: float
    roll: SineAxis
    pitch: SineAxis
    yaw: SineAxis
    work_z_mm: float = 100.0
    profile_id: str = "trajectory"

    @property
    def total_duration_s(self) -> float:
        return self.ramp_in_s + self.steady_duration_s + self.ramp_out_s

    @property
    def sample_count(self) -> int:
        return int(math.ceil(self.total_duration_s * 1000.0 / self.sample_period_ms))

    def validate(self) -> None:
        if self.sample_period_ms <= 0:
            raise MotionPlatformError("trajectory sample period must be positive")
        if self.ramp_in_s < 0 or self.steady_duration_s <= 0 or self.ramp_out_s < 0:
            raise MotionPlatformError("trajectory durations are invalid")
        for axis in (self.roll, self.pitch, self.yaw):
            axis.validate()
        if not any(axis.enabled for axis in (self.roll, self.pitch, self.yaw)):
            raise MotionPlatformError("trajectory requires an enabled axis")

    def pose_at(self, elapsed_s: float) -> PlatformPose:
        elapsed = min(max(float(elapsed_s), 0.0), self.total_duration_s)
        envelope = 1.0
        if self.ramp_in_s > 0 and elapsed < self.ramp_in_s:
            envelope = math.sin(math.pi * elapsed / (2.0 * self.ramp_in_s)) ** 2
        ramp_out_start = self.ramp_in_s + self.steady_duration_s
        if self.ramp_out_s > 0 and elapsed > ramp_out_start:
            remaining = max(0.0, self.total_duration_s - elapsed)
            envelope = math.sin(math.pi * remaining / (2.0 * self.ramp_out_s)) ** 2
        return PlatformPose(
            _axis_value(self.roll, elapsed, envelope),
            _axis_value(self.pitch, elapsed, envelope),
            _axis_value(self.yaw, elapsed, envelope),
            z_mm=self.work_z_mm,
        )


@dataclass(frozen=True)
class TrajectoryResult:
    intended_points: int
    sent_points: int
    skipped_points: int
    maximum_lateness_ms: float
    stopped: bool
    arrival: MotionActionResult | None


class MotionTrajectoryRunner:
    def __init__(
        self,
        controller: DirectMotionPlatformController,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._controller = controller
        self._clock_ns = clock_ns
        self._sleep = sleep

    def preflight(self, trajectory: SineTrajectory) -> tuple[PlatformPose, ...]:
        trajectory.validate()
        validate_sine_trajectory(self._controller.config, trajectory)
        if trajectory.sample_period_ms < self._controller.config.minimum_duration_ms:
            raise MotionPlatformError("trajectory sample period is shorter than controller minimum")
        count = trajectory.sample_count
        points = tuple(
            trajectory.pose_at(min((index + 1) * trajectory.sample_period_ms / 1000.0, trajectory.total_duration_s))
            for index in range(count)
        )
        for point in points:
            solve_actuator_targets(self._controller.config, point)
        return points

    def run(self, trajectory: SineTrajectory, *, stop_event: threading.Event | None = None) -> TrajectoryResult:
        points = self.preflight(trajectory)
        stop = stop_event or threading.Event()
        period_ns = trajectory.sample_period_ms * 1_000_000
        start_ns = self._clock_ns()
        index = 0
        sent = 0
        skipped = 0
        maximum_lateness_ns = 0
        self._controller.mark_trajectory_active()
        try:
            while index < len(points) and not stop.is_set():
                deadline_ns = start_ns + index * period_ns
                now = self._clock_ns()
                if now < deadline_ns:
                    self._sleep((deadline_ns - now) / 1_000_000_000.0)
                    now = self._clock_ns()
                if stop.is_set():
                    break
                if now >= deadline_ns + period_ns:
                    new_index = min(len(points), int((now - start_ns) // period_ns))
                    skipped += max(1, new_index - index)
                    index = new_index
                    if index >= len(points):
                        break
                    deadline_ns = start_ns + index * period_ns
                maximum_lateness_ns = max(maximum_lateness_ns, max(0, now - deadline_ns))
                self._controller.send_trajectory_point(points[index], duration_ms=trajectory.sample_period_ms)
                sent += 1
                index += 1
        finally:
            self._controller.mark_trajectory_stopped()
        arrival = None
        if not stop.is_set() and sent and index == len(points):
            arrival = self._controller.wait_for_target(points[-1], action="trajectory")
        return TrajectoryResult(
            intended_points=len(points),
            sent_points=sent,
            skipped_points=skipped,
            maximum_lateness_ms=maximum_lateness_ns / 1_000_000.0,
            stopped=stop.is_set(),
            arrival=arrival,
        )


def _axis_value(axis: SineAxis, elapsed_s: float, envelope: float) -> float:
    if not axis.enabled:
        return 0.0
    phase = 2.0 * math.pi * axis.frequency_hz * elapsed_s + math.radians(axis.phase_deg)
    return axis.amplitude_deg * envelope * math.sin(phase)


def validate_sine_trajectory(config, trajectory: SineTrajectory) -> None:
    trajectory.validate()
    for name, axis in (("roll", trajectory.roll), ("pitch", trajectory.pitch), ("yaw", trajectory.yaw)):
        if not axis.enabled:
            continue
        angle_limit = getattr(config, f"{name}_abs_limit_deg")
        if axis.amplitude_deg > angle_limit:
            raise MotionPlatformError(f"{name} trajectory amplitude exceeds configured limit")
        frequency_limit = getattr(config, f"{name}_max_frequency_hz")
        velocity_limit = getattr(config, f"{name}_max_velocity_deg_s")
        acceleration_limit = getattr(config, f"{name}_max_acceleration_deg_s2")
        if None in (frequency_limit, velocity_limit, acceleration_limit):
            raise MotionPlatformError(f"{name} trajectory operating limits are incomplete")
        velocity = 2.0 * math.pi * axis.frequency_hz * axis.amplitude_deg
        acceleration = (2.0 * math.pi * axis.frequency_hz) ** 2 * axis.amplitude_deg
        if axis.frequency_hz > frequency_limit:
            raise MotionPlatformError(f"{name} trajectory frequency exceeds configured limit")
        if velocity > velocity_limit:
            raise MotionPlatformError(f"{name} trajectory velocity exceeds configured limit")
        if acceleration > acceleration_limit:
            raise MotionPlatformError(f"{name} trajectory acceleration exceeds configured limit")


__all__ = ["MotionTrajectoryRunner", "SineAxis", "SineTrajectory", "TrajectoryResult", "validate_sine_trajectory"]
