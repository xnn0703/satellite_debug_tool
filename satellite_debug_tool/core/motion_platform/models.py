"""运动平台组件的公共领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import ipaddress
import math
from typing import Optional


class MotionPlatformError(RuntimeError):
    """运动平台配置、状态或动作不满足控制合同。"""


class MotionPlatformState(str, Enum):
    OFFLINE = "offline"
    STARTING = "starting"
    READY = "ready"
    HOMING = "homing"
    MOVING = "moving"
    TRAJECTORY = "trajectory"
    RECOVERY_REQUIRED = "recovery_required"
    FAULT = "fault"


class MotionActionOutcome(str, Enum):
    SETTLED = "settled"
    SETTLED_OUTSIDE_TARGET = "settled_outside_target"
    HOME_CONFIRMED = "home_confirmed"


@dataclass(frozen=True)
class PlatformPose:
    roll_deg: float
    pitch_deg: float
    yaw_deg: float
    x_mm: float = 0.0
    y_mm: float = 0.0
    z_mm: float = 100.0

    def validate_finite(self) -> None:
        if not all(math.isfinite(value) for value in self.as_tuple()):
            raise MotionPlatformError("platform pose values must be finite")

    def as_tuple(self) -> tuple[float, float, float, float, float, float]:
        return (
            self.roll_deg,
            self.pitch_deg,
            self.yaw_deg,
            self.x_mm,
            self.y_mm,
            self.z_mm,
        )


@dataclass(frozen=True)
class PlatformGeometry:
    top_diameter_mm: float = 430.0
    top_short_edge_mm: float = 50.0
    bottom_diameter_mm: float = 600.0
    bottom_short_edge_mm: float = 50.0
    fixed_leg_length_mm: float = 425.0
    maximum_extension_mm: float = 200.0
    pulses_per_extension_mm: float = 1000.0
    electronic_gear_ratio: float = 39.5


@dataclass(frozen=True)
class MotionPlatformConfig:
    model_id: str
    local_ipv4: str
    feedback_port: int
    controller_ipv4: str
    command_port: int
    center_pose: PlatformPose
    roll_abs_limit_deg: float
    pitch_abs_limit_deg: float
    yaw_abs_limit_deg: float
    z_min_mm: float
    z_max_mm: float
    minimum_duration_ms: int = 20
    stationary_window_ms: int = 300
    stationary_span_pulses: int = 100
    target_guard_mm: float = 0.1
    feedback_fresh_ms: int = 150
    action_timeout_ms: int = 30000
    home_timeout_ms: int = 90000
    roll_sign: int = 1
    pitch_sign: int = 1
    yaw_sign: int = 1
    calibration_id: str = ""
    geometry: PlatformGeometry = PlatformGeometry()
    roll_max_frequency_hz: Optional[float] = None
    pitch_max_frequency_hz: Optional[float] = None
    yaw_max_frequency_hz: Optional[float] = None
    roll_max_velocity_deg_s: Optional[float] = None
    pitch_max_velocity_deg_s: Optional[float] = None
    yaw_max_velocity_deg_s: Optional[float] = None
    roll_max_acceleration_deg_s2: Optional[float] = None
    pitch_max_acceleration_deg_s2: Optional[float] = None
    yaw_max_acceleration_deg_s2: Optional[float] = None

    def validate(self) -> None:
        if not self.model_id.strip():
            raise MotionPlatformError("motion-platform model_id is required")
        for name, address in (
            ("local_ipv4", self.local_ipv4),
            ("controller_ipv4", self.controller_ipv4),
        ):
            try:
                ipaddress.IPv4Address(address)
            except ipaddress.AddressValueError as exc:
                raise MotionPlatformError(f"{name} must be a strict IPv4 address") from exc
        for name, port in (
            ("feedback_port", self.feedback_port),
            ("command_port", self.command_port),
        ):
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise MotionPlatformError(f"{name} must be within 1..65535")
        for name, value in (
            ("roll_abs_limit_deg", self.roll_abs_limit_deg),
            ("pitch_abs_limit_deg", self.pitch_abs_limit_deg),
            ("yaw_abs_limit_deg", self.yaw_abs_limit_deg),
        ):
            if not math.isfinite(value) or value <= 0:
                raise MotionPlatformError(f"{name} must be positive and finite")
        if not math.isfinite(self.z_min_mm) or not math.isfinite(self.z_max_mm):
            raise MotionPlatformError("platform Z limits must be finite")
        if self.z_min_mm > self.z_max_mm:
            raise MotionPlatformError("platform z_min_mm cannot exceed z_max_mm")
        self.center_pose.validate_finite()
        if not self.z_min_mm <= self.center_pose.z_mm <= self.z_max_mm:
            raise MotionPlatformError("platform center pose is outside Z limits")
        for name, value in (
            ("minimum_duration_ms", self.minimum_duration_ms),
            ("stationary_window_ms", self.stationary_window_ms),
            ("stationary_span_pulses", self.stationary_span_pulses),
            ("feedback_fresh_ms", self.feedback_fresh_ms),
            ("action_timeout_ms", self.action_timeout_ms),
            ("home_timeout_ms", self.home_timeout_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise MotionPlatformError(f"{name} must be a positive integer")
        if not math.isfinite(self.target_guard_mm) or self.target_guard_mm <= 0:
            raise MotionPlatformError("target_guard_mm must be positive and finite")
        if any(sign not in (-1, 1) for sign in (self.roll_sign, self.pitch_sign, self.yaw_sign)):
            raise MotionPlatformError("axis signs must be -1 or 1")
        for value in self.geometry.__dict__.values():
            if not math.isfinite(value) or value <= 0:
                raise MotionPlatformError("platform geometry values must be positive and finite")
        for name in (
            "roll_max_frequency_hz", "pitch_max_frequency_hz", "yaw_max_frequency_hz",
            "roll_max_velocity_deg_s", "pitch_max_velocity_deg_s", "yaw_max_velocity_deg_s",
            "roll_max_acceleration_deg_s2", "pitch_max_acceleration_deg_s2", "yaw_max_acceleration_deg_s2",
        ):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise MotionPlatformError(f"{name} must be positive and finite")

    def command_pose(self, pose: PlatformPose) -> PlatformPose:
        return PlatformPose(
            pose.roll_deg * self.roll_sign,
            pose.pitch_deg * self.pitch_sign,
            pose.yaw_deg * self.yaw_sign,
            pose.x_mm,
            pose.y_mm,
            pose.z_mm,
        )


@dataclass(frozen=True)
class MotionPlatformSnapshot:
    state: MotionPlatformState
    generation: int
    feedback_monotonic_ns: int = 0
    feedback_age_ms: Optional[float] = None
    target_pulses: tuple[int, ...] = ()
    actual_pulses: tuple[int, ...] = ()
    errors_pulses: tuple[int, ...] = ()
    servo_error_codes: tuple[int, ...] = ()
    system_state: Optional[int] = None
    received_command: Optional[int] = None
    received_command_result: Optional[int] = None
    details: str = ""

    @property
    def ready(self) -> bool:
        return self.state == MotionPlatformState.READY


@dataclass(frozen=True)
class MotionActionResult:
    action: str
    sent: bool
    outcome: MotionActionOutcome
    motion_stopped: bool
    target_guard_passed: bool
    generation: int
    started_monotonic_ns: int
    settled_monotonic_ns: int
    target_pose: Optional[PlatformPose]
    target_pulses: tuple[int, ...]
    final_pulses: tuple[int, ...]
    errors_pulses: tuple[int, ...]
    stationary_spans_pulses: tuple[int, ...]
    maximum_error_pulses: Optional[int]
    maximum_error_mm: Optional[float]
    details: str = ""

    @property
    def successful(self) -> bool:
        return self.motion_stopped and self.target_guard_passed


@dataclass(frozen=True)
class MotionCommandEvent:
    action: str
    raw_command: bytes
    target_pose: Optional[PlatformPose]
    target_pulses: tuple[int, ...]
    duration_ms: Optional[int]
    monotonic_ns: int
    generation: int


__all__ = [
    "MotionActionResult",
    "MotionActionOutcome",
    "MotionCommandEvent",
    "MotionPlatformConfig",
    "MotionPlatformError",
    "MotionPlatformSnapshot",
    "MotionPlatformState",
    "PlatformGeometry",
    "PlatformPose",
]
