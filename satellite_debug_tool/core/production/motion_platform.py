"""Lingjing V0.3 A6/A6T motion-platform commands and trajectory timing."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import math
import socket
import threading
import time
from typing import Callable, Mapping, Optional

from .models import EvidenceLevel


class MotionPlatformError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlatformPose:
    roll_deg: float
    pitch_deg: float
    yaw_deg: float
    x_mm: float = 0.0
    y_mm: float = 0.0
    z_mm: float = 100.0


@dataclass(frozen=True)
class MotionPlatformConfig:
    host: str
    port: int
    center_pose: PlatformPose
    reset_pose: PlatformPose
    roll_abs_limit_deg: float
    pitch_abs_limit_deg: float
    yaw_abs_limit_deg: float
    z_min_mm: float = 0.0
    z_max_mm: float = 100.0
    minimum_duration_ms: int = 50
    roll_sign: int = 1
    pitch_sign: int = 1
    yaw_sign: int = 1
    calibration_id: str = ""
    roll_max_frequency_hz: Optional[float] = None
    pitch_max_frequency_hz: Optional[float] = None
    yaw_max_frequency_hz: Optional[float] = None
    roll_max_velocity_deg_s: Optional[float] = None
    pitch_max_velocity_deg_s: Optional[float] = None
    yaw_max_velocity_deg_s: Optional[float] = None
    roll_max_acceleration_deg_s2: Optional[float] = None
    pitch_max_acceleration_deg_s2: Optional[float] = None
    yaw_max_acceleration_deg_s2: Optional[float] = None
    require_periodic_hard_limits: bool = False

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MotionPlatformConfig":
        try:
            endpoint = payload["endpoint"]
            center = payload["center_pose"]
            reset = payload["reset_pose"]
            limits = payload["limits"]
        except KeyError as exc:
            raise MotionPlatformError(f"missing platform field: {exc.args[0]}") from exc
        if not isinstance(endpoint, Mapping):
            raise MotionPlatformError("motion-platform endpoint must be an object")
        if not isinstance(center, Mapping) or not isinstance(reset, Mapping):
            raise MotionPlatformError("center_pose and reset_pose must be objects")
        if not isinstance(limits, Mapping):
            raise MotionPlatformError("motion-platform limits must be an object")
        signs = payload.get("axis_signs", {})
        if not isinstance(signs, Mapping):
            raise MotionPlatformError("axis_signs must be an object")
        try:
            config = cls(
                host=str(endpoint["host"]),
                port=int(endpoint.get("port", 9800)),
                center_pose=_pose_from_mapping(center),
                reset_pose=_pose_from_mapping(reset),
                roll_abs_limit_deg=float(limits["roll_abs_deg"]),
                pitch_abs_limit_deg=float(limits["pitch_abs_deg"]),
                yaw_abs_limit_deg=float(limits["yaw_abs_deg"]),
                z_min_mm=float(limits.get("z_min_mm", 0.0)),
                z_max_mm=float(limits.get("z_max_mm", 100.0)),
                minimum_duration_ms=int(payload.get("minimum_duration_ms", 50)),
                roll_sign=int(signs.get("roll", 1)),
                pitch_sign=int(signs.get("pitch", 1)),
                yaw_sign=int(signs.get("yaw", 1)),
                calibration_id=str(payload.get("calibration_id", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MotionPlatformError(f"invalid motion-platform configuration: {exc}") from exc
        config.validate()
        return config

    def validate(self) -> None:
        try:
            ipaddress.IPv4Address(self.host)
        except ipaddress.AddressValueError as exc:
            raise MotionPlatformError("motion-platform host must be an IPv4 address") from exc
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise MotionPlatformError("motion-platform port is out of range")
        for name, value in (
            ("roll_abs_limit_deg", self.roll_abs_limit_deg),
            ("pitch_abs_limit_deg", self.pitch_abs_limit_deg),
            ("yaw_abs_limit_deg", self.yaw_abs_limit_deg),
        ):
            if not math.isfinite(value) or value <= 0:
                raise MotionPlatformError(f"{name} must be positive and finite")
        if not math.isfinite(self.z_min_mm) or not math.isfinite(self.z_max_mm):
            raise MotionPlatformError("Z limits must be finite")
        if self.z_min_mm > self.z_max_mm:
            raise MotionPlatformError("z_min_mm cannot exceed z_max_mm")
        if (
            isinstance(self.minimum_duration_ms, bool)
            or not isinstance(self.minimum_duration_ms, int)
            or self.minimum_duration_ms <= 0
        ):
            raise MotionPlatformError("minimum_duration_ms must be positive")
        if any(sign not in (-1, 1) for sign in (self.roll_sign, self.pitch_sign, self.yaw_sign)):
            raise MotionPlatformError("axis signs must be -1 or 1")
        for name, value in self._optional_limits().items():
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise MotionPlatformError(f"{name} must be positive and finite")
        _validate_pose(self, self.center_pose)
        _validate_pose(self, self.reset_pose)
        if self.center_pose.z_mm != 100.0:
            raise MotionPlatformError("center pose Z must be 100 mm")
        if self.reset_pose.z_mm != 0.0:
            raise MotionPlatformError("reset pose Z must be 0 mm")

    @property
    def periodic_limits_complete(self) -> bool:
        return all(
            value is not None
            for name, value in self._optional_limits().items()
            if "frequency" in name or "velocity" in name or "acceleration" in name
        )

    def _optional_limits(self) -> dict[str, Optional[float]]:
        return {
            "roll_max_frequency_hz": self.roll_max_frequency_hz,
            "pitch_max_frequency_hz": self.pitch_max_frequency_hz,
            "yaw_max_frequency_hz": self.yaw_max_frequency_hz,
            "roll_max_velocity_deg_s": self.roll_max_velocity_deg_s,
            "pitch_max_velocity_deg_s": self.pitch_max_velocity_deg_s,
            "yaw_max_velocity_deg_s": self.yaw_max_velocity_deg_s,
            "roll_max_acceleration_deg_s2": self.roll_max_acceleration_deg_s2,
            "pitch_max_acceleration_deg_s2": self.pitch_max_acceleration_deg_s2,
            "yaw_max_acceleration_deg_s2": self.yaw_max_acceleration_deg_s2,
        }


@dataclass(frozen=True)
class PlatformSendResult:
    sent: bool
    raw_command: bytes
    logical_pose: PlatformPose
    command_pose: PlatformPose
    duration_ms: Optional[int]
    monotonic_ns: int
    evidence_level: EvidenceLevel
    error: str = ""


@dataclass(frozen=True)
class SineAxis:
    amplitude_deg: float
    frequency_hz: float
    phase_deg: float = 0.0
    enabled: bool = True

    def validate(self) -> None:
        for name, value in (
            ("amplitude_deg", self.amplitude_deg),
            ("frequency_hz", self.frequency_hz),
            ("phase_deg", self.phase_deg),
        ):
            if not math.isfinite(value):
                raise MotionPlatformError(f"trajectory {name} must be finite")
        if not self.enabled:
            return
        if self.amplitude_deg <= 0:
            raise MotionPlatformError("trajectory amplitude must be positive")
        if self.frequency_hz <= 0:
            raise MotionPlatformError("trajectory frequency must be positive")


@dataclass(frozen=True)
class CombinedSineProfile:
    profile_id: str
    sample_period_ms: int
    ramp_in_s: float
    steady_duration_s: float
    ramp_out_s: float
    roll: SineAxis
    pitch: SineAxis
    yaw: SineAxis
    work_z_mm: float = 100.0

    @property
    def total_duration_s(self) -> float:
        return self.ramp_in_s + self.steady_duration_s + self.ramp_out_s

    @property
    def sample_count(self) -> int:
        total_ms = int(round(self.total_duration_s * 1000.0))
        return int(math.ceil(total_ms / self.sample_period_ms))

    def validate(self) -> None:
        if not self.profile_id.strip():
            raise MotionPlatformError("trajectory profile_id is required")
        if self.sample_period_ms <= 0:
            raise MotionPlatformError("trajectory sample period must be positive")
        if self.ramp_in_s < 0 or self.steady_duration_s <= 0 or self.ramp_out_s < 0:
            raise MotionPlatformError("trajectory durations are invalid")
        if not math.isfinite(self.work_z_mm):
            raise MotionPlatformError("trajectory work Z must be finite")
        self.roll.validate()
        self.pitch.validate()
        self.yaw.validate()
        if not any(axis.enabled for axis in (self.roll, self.pitch, self.yaw)):
            raise MotionPlatformError("trajectory must enable at least one axis")

    def pose_at(self, elapsed_s: float) -> PlatformPose:
        self.validate()
        elapsed = min(max(0.0, float(elapsed_s)), self.total_duration_s)
        envelope = self._envelope(elapsed)
        return PlatformPose(
            roll_deg=_axis_value(self.roll, elapsed, envelope),
            pitch_deg=_axis_value(self.pitch, elapsed, envelope),
            yaw_deg=_axis_value(self.yaw, elapsed, envelope),
            z_mm=self.work_z_mm,
        )

    def _envelope(self, elapsed_s: float) -> float:
        if self.ramp_in_s > 0 and elapsed_s < self.ramp_in_s:
            phase = (math.pi / 2.0) * elapsed_s / self.ramp_in_s
            return math.sin(phase) ** 2
        ramp_out_start = self.ramp_in_s + self.steady_duration_s
        if self.ramp_out_s > 0 and elapsed_s > ramp_out_start:
            remaining = max(0.0, self.total_duration_s - elapsed_s)
            phase = (math.pi / 2.0) * remaining / self.ramp_out_s
            return math.sin(phase) ** 2
        return 1.0


@dataclass(frozen=True)
class TrajectoryRunStatistics:
    intended_points: int
    sent_points: int
    skipped_points: int
    failed_points: int
    max_abs_jitter_ms: float
    max_send_interval_ms: float
    coverage_ratio: float
    stopped: bool
    timing_fault: bool
    timing_fault_details: str


class LingjingPlatformAdapter:
    """Command-only adapter; it deliberately cannot claim platform arrival."""

    def __init__(
        self,
        config: MotionPlatformConfig,
        *,
        sender: Optional[Callable[[bytes, tuple[str, int]], bool]] = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        config.validate()
        self._config = config
        self._sender = sender or self._send_udp
        self._clock_ns = clock_ns
        self._safety_confirmed = False
        self._center_confirmed = False
        self._preflight_calibration_id = ""
        self._last_pose: Optional[PlatformPose] = None
        self._socket: Optional[socket.socket] = None

    @property
    def ready(self) -> bool:
        return (
            self._safety_confirmed
            and self._center_confirmed
            and bool(self._config.calibration_id)
            and self._preflight_calibration_id == self._config.calibration_id
        )

    @property
    def status_text(self) -> str:
        if not self._config.calibration_id:
            return "calibration_missing"
        if self._preflight_calibration_id != self._config.calibration_id:
            return "preflight_required"
        if not self._safety_confirmed:
            return "safety_not_confirmed"
        if not self._center_confirmed:
            return "center_not_confirmed"
        return "safe"

    @property
    def config(self) -> MotionPlatformConfig:
        return self._config

    @property
    def last_pose(self) -> Optional[PlatformPose]:
        return self._last_pose

    def confirm_preflight(
        self,
        calibration_id: str,
        *,
        safety_confirmed: bool,
        center_confirmed: bool,
    ) -> None:
        if str(calibration_id) != self._config.calibration_id:
            raise MotionPlatformError("motion-platform calibration ID does not match")
        self._preflight_calibration_id = str(calibration_id)
        self._safety_confirmed = bool(safety_confirmed)
        self._center_confirmed = bool(center_confirmed)
        self._last_pose = self._config.center_pose if self._center_confirmed else None

    def clear_safety_confirmation(self) -> None:
        self._safety_confirmed = False

    def send_pose(self, pose: PlatformPose, *, duration_ms: int) -> PlatformSendResult:
        return self._send_pose(pose, duration_ms=duration_ms)

    def _send_pose(
        self,
        pose: PlatformPose,
        *,
        duration_ms: int,
    ) -> PlatformSendResult:
        if not self.ready:
            raise MotionPlatformError(f"motion platform is not ready: {self.status_text}")
        _validate_pose(self._config, pose)
        if int(duration_ms) < self._config.minimum_duration_ms:
            raise MotionPlatformError("A6T duration is shorter than the configured minimum")
        command_pose = PlatformPose(
            pose.roll_deg * self._config.roll_sign,
            pose.pitch_deg * self._config.pitch_sign,
            pose.yaw_deg * self._config.yaw_sign,
            pose.x_mm,
            pose.y_mm,
            pose.z_mm,
        )
        command = serialize_a6t(command_pose, int(duration_ms))
        timestamp = self._clock_ns()
        error = ""
        try:
            sent = bool(self._sender(command, (self._config.host, self._config.port)))
        except OSError as exc:
            sent = False
            error = str(exc)
        if sent:
            self._last_pose = pose
        return PlatformSendResult(
            sent=sent,
            raw_command=command,
            logical_pose=pose,
            command_pose=command_pose,
            duration_ms=int(duration_ms),
            monotonic_ns=timestamp,
            evidence_level=(EvidenceLevel.COMMAND_SENT if sent else EvidenceLevel.NONE),
            error=error,
        )

    def send_center(self, *, duration_ms: int) -> PlatformSendResult:
        return self._send_pose(
            self._config.center_pose,
            duration_ms=duration_ms,
        )

    def send_reset(
        self,
        *,
        duration_ms: int,
        explicit_confirmation: bool = False,
    ) -> PlatformSendResult:
        if not explicit_confirmation:
            raise MotionPlatformError("reset to Z=0 requires explicit confirmation")
        return self._send_pose(
            self._config.reset_pose,
            duration_ms=duration_ms,
        )

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _send_udp(self, data: bytes, endpoint: tuple[str, int]) -> bool:
        if self._socket is None:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return self._socket.sendto(data, endpoint) == len(data)


class MotionTrajectoryRunner:
    """Run A6T points against absolute monotonic deadlines without catch-up bursts."""

    def __init__(
        self,
        adapter: LingjingPlatformAdapter,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._adapter = adapter
        self._clock_ns = clock_ns
        self._sleep = sleep

    def run(
        self,
        profile: CombinedSineProfile,
        *,
        stop_event: Optional[threading.Event] = None,
        on_send: Optional[Callable[[PlatformSendResult], None]] = None,
    ) -> TrajectoryRunStatistics:
        profile.validate()
        validate_sine_profile(self._adapter.config, profile)
        stop = stop_event or threading.Event()
        period_ns = profile.sample_period_ms * 1_000_000
        total_duration_ms = int(round(profile.total_duration_s * 1000.0))
        intended = profile.sample_count
        start_ns = self._clock_ns()
        index = 0
        sent = 0
        skipped = 0
        failed = 0
        max_jitter_ns = 0
        max_interval_ns = 0
        previous_send_ns: Optional[int] = None
        last_completion_ns: Optional[int] = None
        timing_fault = False
        timing_fault_details = ""

        while index < intended and not stop.is_set():
            deadline_ns = start_ns + index * period_ns
            now_ns = self._clock_ns()
            if now_ns < deadline_ns:
                self._sleep((deadline_ns - now_ns) / 1_000_000_000.0)
                if stop.is_set():
                    break
                now_ns = self._clock_ns()
            if now_ns >= deadline_ns + period_ns:
                timing_fault = True
                timing_fault_details = (
                    f"trajectory command missed deadline for point {index + 1}"
                )
                skipped = intended - index
                break
            jitter_ns = abs(now_ns - deadline_ns)
            max_jitter_ns = max(max_jitter_ns, jitter_ns)
            target_ms = min((index + 1) * profile.sample_period_ms, total_duration_ms)
            previous_target_ms = index * profile.sample_period_ms
            duration_ms = target_ms - previous_target_ms
            pose = profile.pose_at(target_ms / 1000.0)
            result = self._adapter.send_pose(
                pose,
                duration_ms=duration_ms,
            )
            if result.sent:
                sent += 1
                last_completion_ns = (
                    result.monotonic_ns + duration_ms * 1_000_000
                )
            else:
                failed += 1
            if previous_send_ns is not None:
                max_interval_ns = max(
                    max_interval_ns,
                    result.monotonic_ns - previous_send_ns,
                )
            previous_send_ns = result.monotonic_ns
            if on_send is not None:
                on_send(result)
            index += 1

        self._wait_for_sent_command(last_completion_ns)

        coverage = sent / intended if intended else 0.0
        return TrajectoryRunStatistics(
            intended_points=intended,
            sent_points=sent,
            skipped_points=skipped,
            failed_points=failed,
            max_abs_jitter_ms=max_jitter_ns / 1_000_000.0,
            max_send_interval_ms=max_interval_ns / 1_000_000.0,
            coverage_ratio=coverage,
            stopped=stop.is_set(),
            timing_fault=timing_fault,
            timing_fault_details=timing_fault_details,
        )

    def _wait_for_sent_command(self, completion_ns: Optional[int]) -> None:
        if completion_ns is None:
            return
        remaining_ns = completion_ns - self._clock_ns()
        if remaining_ns > 0:
            self._sleep(remaining_ns / 1_000_000_000.0)


@dataclass(frozen=True)
class AbsoluteMoveStatistics:
    intended_points: int
    sent_points: int
    failed_points: int
    stopped: bool
    final_pose: PlatformPose


class AbsoluteMoveRunner:
    """Execute one logical absolute move as one A6T command."""

    def __init__(
        self,
        adapter: LingjingPlatformAdapter,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._adapter = adapter
        self._clock_ns = clock_ns
        self._sleep = sleep

    def run_axis(
        self,
        axis: str,
        target_deg: float,
        *,
        total_duration_ms: int,
        stop_event: Optional[threading.Event] = None,
        on_send: Optional[Callable[[PlatformSendResult], None]] = None,
    ) -> AbsoluteMoveStatistics:
        if axis not in {"roll", "pitch", "yaw"}:
            raise MotionPlatformError("absolute move axis must be roll, pitch, or yaw")
        current = self._adapter.last_pose
        if current is None:
            raise MotionPlatformError("absolute move requires a confirmed logical pose")
        values = {
            "roll": current.roll_deg,
            "pitch": current.pitch_deg,
            "yaw": current.yaw_deg,
        }
        values[axis] = float(target_deg)
        target = PlatformPose(
            values["roll"],
            values["pitch"],
            values["yaw"],
            current.x_mm,
            current.y_mm,
            current.z_mm,
        )
        return self.run_pose(
            target,
            total_duration_ms=total_duration_ms,
            stop_event=stop_event,
            on_send=on_send,
        )

    def run_pose(
        self,
        target: PlatformPose,
        *,
        total_duration_ms: int,
        stop_event: Optional[threading.Event] = None,
        on_send: Optional[Callable[[PlatformSendResult], None]] = None,
    ) -> AbsoluteMoveStatistics:
        current = self._adapter.last_pose
        if current is None:
            raise MotionPlatformError("absolute move requires a confirmed logical pose")
        _validate_pose(self._adapter.config, target)
        if isinstance(total_duration_ms, bool) or int(total_duration_ms) <= 0:
            raise MotionPlatformError("absolute move duration must be positive")
        if int(total_duration_ms) < self._adapter.config.minimum_duration_ms:
            raise MotionPlatformError(
                "absolute move duration is shorter than the configured minimum"
            )
        stop = stop_event or threading.Event()
        sent = 0
        failed = 0
        final_pose = current
        last_completion_ns: Optional[int] = None
        if not stop.is_set():
            result = self._adapter.send_pose(
                target,
                duration_ms=int(total_duration_ms),
            )
            if result.sent:
                sent = 1
                final_pose = target
                last_completion_ns = (
                    result.monotonic_ns + int(total_duration_ms) * 1_000_000
                )
            else:
                failed = 1
            if on_send is not None:
                on_send(result)
        if last_completion_ns is not None:
            remaining_ns = last_completion_ns - self._clock_ns()
            if remaining_ns > 0:
                self._sleep(remaining_ns / 1_000_000_000.0)
        return AbsoluteMoveStatistics(
            intended_points=1,
            sent_points=sent,
            failed_points=failed,
            stopped=stop.is_set(),
            final_pose=final_pose,
        )


def validate_sine_profile(
    config: MotionPlatformConfig,
    profile: CombinedSineProfile,
) -> None:
    profile.validate()
    config.validate()
    if profile.sample_period_ms < config.minimum_duration_ms:
        raise MotionPlatformError("trajectory sample period is shorter than the A6T minimum")
    total_duration_ms = int(round(profile.total_duration_s * 1000.0))
    final_duration_ms = total_duration_ms % profile.sample_period_ms
    if 0 < final_duration_ms < config.minimum_duration_ms:
        raise MotionPlatformError(
            "trajectory final interval is shorter than the A6T minimum"
        )
    if config.require_periodic_hard_limits and not config.periodic_limits_complete:
        raise MotionPlatformError("periodic motion hard limits are incomplete")
    if not config.periodic_limits_complete:
        return
    axes = {"roll": profile.roll, "pitch": profile.pitch, "yaw": profile.yaw}
    for name, axis in axes.items():
        if not axis.enabled:
            continue
        angle_limit = getattr(config, f"{name}_abs_limit_deg")
        frequency_limit = getattr(config, f"{name}_max_frequency_hz")
        velocity_limit = getattr(config, f"{name}_max_velocity_deg_s")
        acceleration_limit = getattr(config, f"{name}_max_acceleration_deg_s2")
        if axis.amplitude_deg > angle_limit + 1e-12:
            raise MotionPlatformError(f"trajectory {name} amplitude exceeds the hard limit")
        if axis.frequency_hz > float(frequency_limit) + 1e-12:
            raise MotionPlatformError(f"trajectory {name} frequency exceeds the hard limit")
        omega = 2.0 * math.pi * axis.frequency_hz
        envelope_rate = max(
            _envelope_rate_bound(profile.ramp_in_s),
            _envelope_rate_bound(profile.ramp_out_s),
        )
        envelope_acceleration = max(
            _envelope_acceleration_bound(profile.ramp_in_s),
            _envelope_acceleration_bound(profile.ramp_out_s),
        )
        peak_velocity = axis.amplitude_deg * (omega + envelope_rate)
        if peak_velocity > float(velocity_limit) + 1e-12:
            raise MotionPlatformError(f"trajectory {name} peak velocity exceeds the hard limit")
        peak_acceleration = axis.amplitude_deg * (
            omega**2 + 2.0 * omega * envelope_rate + envelope_acceleration
        )
        if peak_acceleration > float(acceleration_limit) + 1e-12:
            raise MotionPlatformError(
                f"trajectory {name} peak acceleration exceeds the hard limit"
            )

def _envelope_rate_bound(ramp_duration_s: float) -> float:
    if ramp_duration_s <= 0.0:
        return 0.0
    return math.pi / (2.0 * ramp_duration_s)


def _envelope_acceleration_bound(ramp_duration_s: float) -> float:
    if ramp_duration_s <= 0.0:
        return 0.0
    return math.pi**2 / (2.0 * ramp_duration_s**2)


def serialize_a6(pose: PlatformPose) -> bytes:
    return _serialize("A6", pose, None)


def serialize_a6t(pose: PlatformPose, duration_ms: int) -> bytes:
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
        raise MotionPlatformError("A6T duration_ms must be a positive integer")
    return _serialize("A6T", pose, duration_ms)


def _serialize(command: str, pose: PlatformPose, duration_ms: Optional[int]) -> bytes:
    values = [
        pose.roll_deg,
        pose.pitch_deg,
        pose.yaw_deg,
        pose.x_mm,
        pose.y_mm,
        pose.z_mm,
    ]
    encoded = ",".join(_format_number(value) for value in values)
    if duration_ms is not None:
        encoded = f"{encoded},{duration_ms}"
    return f"@{command}:{encoded}#".encode("ascii")


def _format_number(value: float) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MotionPlatformError("platform command values must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise MotionPlatformError("platform command values must be finite")
    if abs(number) < 0.0000005:
        number = 0.0
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _pose_from_mapping(payload: Mapping[str, object]) -> PlatformPose:
    try:
        return PlatformPose(
            roll_deg=float(payload["roll_deg"]),
            pitch_deg=float(payload["pitch_deg"]),
            yaw_deg=float(payload["yaw_deg"]),
            x_mm=float(payload["x_mm"]),
            y_mm=float(payload["y_mm"]),
            z_mm=float(payload["z_mm"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MotionPlatformError(f"invalid platform pose: {exc}") from exc


def _validate_pose(config: MotionPlatformConfig, pose: PlatformPose) -> None:
    values = (
        pose.roll_deg,
        pose.pitch_deg,
        pose.yaw_deg,
        pose.x_mm,
        pose.y_mm,
        pose.z_mm,
    )
    if any(not math.isfinite(float(value)) for value in values):
        raise MotionPlatformError("platform pose must contain finite values")
    if abs(pose.roll_deg) > config.roll_abs_limit_deg:
        raise MotionPlatformError("platform roll exceeds the configured limit")
    if abs(pose.pitch_deg) > config.pitch_abs_limit_deg:
        raise MotionPlatformError("platform pitch exceeds the configured limit")
    if abs(pose.yaw_deg) > config.yaw_abs_limit_deg:
        raise MotionPlatformError("platform yaw exceeds the configured limit")
    if pose.x_mm != 0.0 or pose.y_mm != 0.0:
        raise MotionPlatformError("production A6T requires X=0 and Y=0")
    if not (config.z_min_mm <= pose.z_mm <= config.z_max_mm):
        raise MotionPlatformError("platform Z exceeds the configured limit")


def _axis_value(axis: SineAxis, elapsed_s: float, envelope: float) -> float:
    if not axis.enabled:
        return 0.0
    phase = math.radians(axis.phase_deg)
    return axis.amplitude_deg * envelope * math.sin(
        2.0 * math.pi * axis.frequency_hz * elapsed_s + phase
    )


__all__ = [
    "AbsoluteMoveRunner",
    "AbsoluteMoveStatistics",
    "CombinedSineProfile",
    "LingjingPlatformAdapter",
    "MotionPlatformConfig",
    "MotionPlatformError",
    "MotionTrajectoryRunner",
    "PlatformPose",
    "PlatformSendResult",
    "SineAxis",
    "TrajectoryRunStatistics",
    "serialize_a6",
    "serialize_a6t",
    "validate_sine_profile",
]
