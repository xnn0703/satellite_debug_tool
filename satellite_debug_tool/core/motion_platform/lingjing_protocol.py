"""灵境六自由度平台的确定性运动学与控制器帧合同。"""

from __future__ import annotations

from dataclasses import dataclass
import math
import struct
from typing import Sequence

from .models import MotionPlatformConfig, MotionPlatformError, PlatformGeometry, PlatformPose


CONTROLLER_HEADER = b"\xAA\x55"
CONTROLLER_TAIL = b"\xFB\xFB"
POSITION_COMMAND = 0xC0
HOME_COMMAND = 0x03
HOME_FRAME = b"\xAA\x55\x00\x01\x03\xFB\xFB"
RUNNING_SYSTEM_STATE = 4


class LingjingProtocolError(MotionPlatformError):
    pass


@dataclass(frozen=True)
class ActuatorTargets:
    pose: PlatformPose
    total_lengths_mm: tuple[float, ...]
    extensions_mm: tuple[float, ...]
    pulses: tuple[int, ...]


def solve_actuator_targets(config: MotionPlatformConfig, pose: PlatformPose) -> ActuatorTargets:
    """计算六轴目标；任一逻辑或物理边界超出时整条拒绝。"""

    config.validate()
    pose.validate_finite()
    command_pose = config.command_pose(pose)
    _validate_pose(config, command_pose)
    geometry = config.geometry
    rotation = _rotation_matrix(command_pose)
    top, bottom = _connection_points(geometry)
    lengths: list[float] = []
    extensions: list[float] = []
    pulses: list[int] = []
    for axis, (top_point, bottom_point) in enumerate(zip(top, bottom), start=1):
        rotated = _mat_vec(rotation, top_point)
        moved = (
            rotated[0] + command_pose.x_mm,
            rotated[1] + command_pose.y_mm,
            rotated[2] + command_pose.z_mm,
        )
        length = round(math.sqrt(sum((moved[i] - bottom_point[i]) ** 2 for i in range(3))), 2)
        extension = length - geometry.fixed_leg_length_mm
        if extension < 0.0 or extension > geometry.maximum_extension_mm:
            raise LingjingProtocolError(
                f"axis {axis} extension {extension:.2f} mm is outside "
                f"0..{geometry.maximum_extension_mm:g} mm"
            )
        pulse = round(
            geometry.pulses_per_extension_mm
            * extension
            * geometry.electronic_gear_ratio
        )
        lengths.append(length)
        extensions.append(extension)
        pulses.append(pulse)
    return ActuatorTargets(
        pose=pose,
        total_lengths_mm=tuple(lengths),
        extensions_mm=tuple(extensions),
        pulses=tuple(pulses),
    )


def build_position_frame(duration_ms: int, pulses: Sequence[int]) -> bytes:
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
        raise LingjingProtocolError("duration must be an integer")
    if not 1 <= duration_ms <= 0xFFFF:
        raise LingjingProtocolError("duration is outside 1..65535 ms")
    if len(pulses) != 6:
        raise LingjingProtocolError("position command requires six axis targets")
    normalized: list[int] = []
    for pulse in pulses:
        if isinstance(pulse, bool) or not isinstance(pulse, int):
            raise LingjingProtocolError("axis targets must be integers")
        if not -(2**31) <= pulse < 2**31:
            raise LingjingProtocolError("axis target is outside int32 range")
        normalized.append(pulse)
    body = bytes((POSITION_COMMAND,)) + struct.pack(">H", duration_ms) + struct.pack("<6i", *normalized)
    return CONTROLLER_HEADER + struct.pack(">H", len(body)) + body + CONTROLLER_TAIL


@dataclass(frozen=True)
class ControllerFeedback:
    sequence_id: int
    controller_time: int
    system_state: int
    idle_path_count: int
    all_path_count: int
    completed_path_count: int
    fifo_count: int
    fifo_index: int
    received_command: int
    received_command_result: int
    axis_pulses: tuple[int, ...]
    servo_error_codes: tuple[int, ...]


def parse_feedback_frame(frame: bytes) -> ControllerFeedback:
    if len(frame) != 68 or not frame.startswith(CONTROLLER_HEADER):
        raise LingjingProtocolError("feedback header or length is invalid")
    if not frame.endswith(CONTROLLER_TAIL):
        raise LingjingProtocolError("feedback tail is invalid")
    offset = 2
    sequence_id, controller_time = struct.unpack_from("<iI", frame, offset)
    offset += 8
    system_state, idle_path_count = frame[offset : offset + 2]
    offset += 2
    all_path_count, completed_path_count, fifo_count, fifo_index = struct.unpack_from("<4i", frame, offset)
    offset += 16
    received_command, received_command_result = frame[offset : offset + 2]
    offset += 2
    axis_pulses = struct.unpack_from("<6i", frame, offset)
    offset += 24
    servo_error_codes = struct.unpack_from("<6H", frame, offset)
    return ControllerFeedback(
        sequence_id=sequence_id,
        controller_time=controller_time,
        system_state=system_state,
        idle_path_count=idle_path_count,
        all_path_count=all_path_count,
        completed_path_count=completed_path_count,
        fifo_count=fifo_count,
        fifo_index=fifo_index,
        received_command=received_command,
        received_command_result=received_command_result,
        axis_pulses=tuple(axis_pulses),
        servo_error_codes=tuple(servo_error_codes),
    )


def _validate_pose(config: MotionPlatformConfig, pose: PlatformPose) -> None:
    values = (
        ("roll", pose.roll_deg, config.roll_abs_limit_deg),
        ("pitch", pose.pitch_deg, config.pitch_abs_limit_deg),
        ("yaw", pose.yaw_deg, config.yaw_abs_limit_deg),
    )
    for name, value, limit in values:
        if abs(value) > limit:
            raise LingjingProtocolError(f"{name} angle exceeds configured limit {limit:g} deg")
    if pose.x_mm != 0.0 or pose.y_mm != 0.0:
        raise LingjingProtocolError("this platform profile requires X=0 and Y=0")
    if not config.z_min_mm <= pose.z_mm <= config.z_max_mm:
        raise LingjingProtocolError("Z position exceeds configured limits")


def _connection_points(
    geometry: PlatformGeometry,
) -> tuple[tuple[tuple[float, float, float], ...], tuple[tuple[float, float, float], ...]]:
    top = _platform_points(geometry.top_diameter_mm, geometry.top_short_edge_mm, top=True)
    bottom_xy = _platform_points(geometry.bottom_diameter_mm, geometry.bottom_short_edge_mm, top=False)
    horizontal_offset = math.hypot(top[0][0] - bottom_xy[0][0], top[0][1] - bottom_xy[0][1])
    remaining = geometry.fixed_leg_length_mm**2 - horizontal_offset**2
    if remaining <= 0:
        raise LingjingProtocolError("fixed leg length cannot reach platform geometry")
    zero_height = round(math.sqrt(remaining), 2)
    bottom = tuple((x, y, z - zero_height) for x, y, z in bottom_xy)
    return top, bottom


def _platform_points(diameter_mm: float, short_edge_mm: float, *, top: bool) -> tuple[tuple[float, float, float], ...]:
    radius = diameter_mm / 2.0
    half = math.acos((2.0 * radius * radius - short_edge_mm * short_edge_mm) / (2.0 * radius * radius)) / 2.0
    if top:
        angles = (
            math.pi / 2.0 - half, math.pi / 2.0 + half,
            7.0 * math.pi / 6.0 - half, 7.0 * math.pi / 6.0 + half,
            11.0 * math.pi / 6.0 - half, 11.0 * math.pi / 6.0 + half,
        )
    else:
        angles = (
            math.pi / 6.0 + half, 5.0 * math.pi / 6.0 - half,
            5.0 * math.pi / 6.0 + half, 3.0 * math.pi / 2.0 - half,
            3.0 * math.pi / 2.0 + half, math.pi / 6.0 - half,
        )
    return tuple((radius * math.cos(angle), radius * math.sin(angle), 0.0) for angle in angles)


def _rotation_matrix(pose: PlatformPose) -> tuple[tuple[float, float, float], ...]:
    roll, pitch, yaw = map(math.radians, (pose.roll_deg, pose.pitch_deg, pose.yaw_deg))
    sa, sb, sc = math.sin(roll), math.sin(pitch), math.sin(yaw)
    ca, cb, cc = math.cos(roll), math.cos(pitch), math.cos(yaw)
    return (
        (cc * cb, cc * sb * sa - sc * ca, sc * sa + cc * sb * ca),
        (sc * cb, cc * ca + sc * sb * sa, -cc * sa + sc * sb * ca),
        (-sb, cb * sa, cb * ca),
    )


def _mat_vec(matrix: Sequence[Sequence[float]], vector: Sequence[float]) -> tuple[float, float, float]:
    return tuple(
        sum(matrix[row][column] * vector[column] for column in range(3))
        for row in range(3)
    )  # type: ignore[return-value]


__all__ = [
    "ActuatorTargets",
    "ControllerFeedback",
    "HOME_COMMAND",
    "HOME_FRAME",
    "LingjingProtocolError",
    "POSITION_COMMAND",
    "RUNNING_SYSTEM_STATE",
    "build_position_frame",
    "parse_feedback_frame",
    "solve_actuator_targets",
]
