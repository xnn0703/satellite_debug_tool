"""可复用的运动平台直接控制组件。"""

from .controller import DatagramTransport, DirectMotionPlatformController
from .evidence import MotionEvidenceRecorder
from .lingjing_protocol import (
    ActuatorTargets,
    ControllerFeedback,
    HOME_FRAME,
    LingjingProtocolError,
    build_position_frame,
    parse_feedback_frame,
    solve_actuator_targets,
)
from .models import (
    MotionActionOutcome,
    MotionActionResult,
    MotionCommandEvent,
    MotionPlatformConfig,
    MotionPlatformError,
    MotionPlatformSnapshot,
    MotionPlatformState,
    PlatformGeometry,
    PlatformPose,
)
from .trajectory import (
    MotionTrajectoryRunner,
    SineAxis,
    SineTrajectory,
    TrajectoryResult,
    validate_sine_trajectory,
)

__all__ = [
    "ActuatorTargets",
    "ControllerFeedback",
    "DatagramTransport",
    "DirectMotionPlatformController",
    "HOME_FRAME",
    "LingjingProtocolError",
    "MotionActionResult",
    "MotionActionOutcome",
    "MotionCommandEvent",
    "MotionEvidenceRecorder",
    "MotionPlatformConfig",
    "MotionPlatformError",
    "MotionPlatformSnapshot",
    "MotionPlatformState",
    "MotionTrajectoryRunner",
    "PlatformGeometry",
    "PlatformPose",
    "SineAxis",
    "SineTrajectory",
    "TrajectoryResult",
    "build_position_frame",
    "parse_feedback_frame",
    "solve_actuator_targets",
    "validate_sine_trajectory",
]
