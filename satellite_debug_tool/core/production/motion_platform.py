"""Lingjing V0.3 A6/A6T motion-platform commands and trajectory timing."""

from __future__ import annotations

from dataclasses import dataclass
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
    max_step_deg: float = 5.0
    minimum_duration_ms: int = 50
    roll_sign: int = 1
    pitch_sign: int = 1
    yaw_sign: int = 1
    calibration_id: str = ""

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
                max_step_deg=float(payload.get("max_step_deg", 5.0)),
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
            socket.inet_aton(self.host)
        except OSError as exc:
            raise MotionPlatformError("motion-platform host must be an IPv4 address") from exc
        if not (1 <= self.port <= 65535):
            raise MotionPlatformError("motion-platform port is out of range")
        for name, value in (
            ("roll_abs_limit_deg", self.roll_abs_limit_deg),
            ("pitch_abs_limit_deg", self.pitch_abs_limit_deg),
            ("yaw_abs_limit_deg", self.yaw_abs_limit_deg),
            ("max_step_deg", self.max_step_deg),
        ):
            if not math.isfinite(value) or value <= 0:
                raise MotionPlatformError(f"{name} must be positive and finite")
        if not math.isfinite(self.z_min_mm) or not math.isfinite(self.z_max_mm):
            raise MotionPlatformError("Z limits must be finite")
        if self.z_min_mm > self.z_max_mm:
            raise MotionPlatformError("z_min_mm cannot exceed z_max_mm")
        if self.minimum_duration_ms <= 0:
            raise MotionPlatformError("minimum_duration_ms must be positive")
        if any(sign not in (-1, 1) for sign in (self.roll_sign, self.pitch_sign, self.yaw_sign)):
            raise MotionPlatformError("axis signs must be -1 or 1")
        _validate_pose(self, self.center_pose)
        _validate_pose(self, self.reset_pose)
        if self.center_pose.z_mm != 100.0:
            raise MotionPlatformError("center pose Z must be 100 mm")
        if self.reset_pose.z_mm != 0.0:
            raise MotionPlatformError("reset pose Z must be 0 mm")


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

    def validate(self) -> None:
        for name, value in (
            ("amplitude_deg", self.amplitude_deg),
            ("frequency_hz", self.frequency_hz),
            ("phase_deg", self.phase_deg),
        ):
            if not math.isfinite(value):
                raise MotionPlatformError(f"trajectory {name} must be finite")
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
        return int(math.floor(self.total_duration_s * 1000.0 / self.sample_period_ms)) + 1

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
        return self._send_pose(pose, duration_ms=duration_ms, enforce_step=True)

    def _send_pose(
        self,
        pose: PlatformPose,
        *,
        duration_ms: int,
        enforce_step: bool,
    ) -> PlatformSendResult:
        if not self.ready:
            raise MotionPlatformError(f"motion platform is not ready: {self.status_text}")
        _validate_pose(self._config, pose)
        if int(duration_ms) < self._config.minimum_duration_ms:
            raise MotionPlatformError("A6T duration is shorter than the configured minimum")
        if enforce_step and self._last_pose is not None:
            delta = max(
                abs(pose.roll_deg - self._last_pose.roll_deg),
                abs(pose.pitch_deg - self._last_pose.pitch_deg),
                abs(pose.yaw_deg - self._last_pose.yaw_deg),
            )
            if delta > self._config.max_step_deg:
                raise MotionPlatformError("A6T angular step exceeds the configured maximum")
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
            enforce_step=False,
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
            enforce_step=False,
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
        stop = stop_event or threading.Event()
        period_ns = profile.sample_period_ms * 1_000_000
        intended = profile.sample_count
        start_ns = self._clock_ns()
        index = 0
        sent = 0
        skipped = 0
        failed = 0
        max_jitter_ns = 0
        max_interval_ns = 0
        previous_send_ns: Optional[int] = None

        while index < intended and not stop.is_set():
            deadline_ns = start_ns + index * period_ns
            now_ns = self._clock_ns()
            if now_ns < deadline_ns:
                self._sleep((deadline_ns - now_ns) / 1_000_000_000.0)
                now_ns = self._clock_ns()
            due_index = min(intended - 1, max(index, (now_ns - start_ns) // period_ns))
            if due_index > index:
                skipped += int(due_index - index)
                index = int(due_index)
                deadline_ns = start_ns + index * period_ns
            jitter_ns = abs(now_ns - deadline_ns)
            max_jitter_ns = max(max_jitter_ns, jitter_ns)
            pose = profile.pose_at(index * profile.sample_period_ms / 1000.0)
            result = self._adapter.send_pose(
                pose,
                duration_ms=profile.sample_period_ms,
            )
            if result.sent:
                sent += 1
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
        )


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
    phase = math.radians(axis.phase_deg)
    return axis.amplitude_deg * envelope * math.sin(
        2.0 * math.pi * axis.frequency_hz * elapsed_s + phase
    )


__all__ = [
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
]
