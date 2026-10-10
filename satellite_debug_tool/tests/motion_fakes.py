"""运动平台 UI/线程测试使用的已确认反馈替身。"""

from __future__ import annotations

import time

from satellite_debug_tool.core.motion_platform import (
    MotionActionOutcome,
    MotionActionResult,
    MotionCommandEvent,
    MotionPlatformSnapshot,
    MotionPlatformState,
    build_position_frame,
    solve_actuator_targets,
)


class ConfirmedMotionController:
    def __init__(self, config, commands: list | None = None) -> None:
        self.config = config
        self.commands = commands if commands is not None else []
        self.snapshot = MotionPlatformSnapshot(
            MotionPlatformState.READY,
            1,
            feedback_monotonic_ns=time.monotonic_ns(),
            feedback_age_ms=0.0,
            actual_pulses=(0,) * 6,
            servo_error_codes=(0,) * 6,
            system_state=4,
            details="test feedback ready",
        )
        self._command_callbacks = []
        self._snapshot_callbacks = []

    def add_command_callback(self, callback) -> None:
        self._command_callbacks.append(callback)

    def add_snapshot_callback(self, callback) -> None:
        self._snapshot_callbacks.append(callback)

    def connect(self):
        return self.snapshot

    def close(self) -> None:
        return

    def move_to(self, pose, *, duration_ms: int, action: str = "move"):
        targets = solve_actuator_targets(self.config, pose).pulses
        frame = build_position_frame(duration_ms, targets)
        now = time.monotonic_ns()
        event = MotionCommandEvent(
            action=action,
            raw_command=frame,
            target_pose=pose,
            target_pulses=targets,
            duration_ms=duration_ms,
            monotonic_ns=now,
            generation=1,
        )
        self.commands.append((frame, (self.config.controller_ipv4, self.config.command_port)))
        for callback in self._command_callbacks:
            callback(event)
        return MotionActionResult(
            action=action,
            sent=True,
            outcome=MotionActionOutcome.SETTLED,
            motion_stopped=True,
            target_guard_passed=True,
            generation=1,
            started_monotonic_ns=now,
            settled_monotonic_ns=time.monotonic_ns(),
            target_pose=pose,
            target_pulses=targets,
            final_pulses=targets,
            errors_pulses=(0,) * 6,
            stationary_spans_pulses=(0,) * 6,
            maximum_error_pulses=0,
            maximum_error_mm=0.0,
            details="confirmed test feedback",
        )

    def recover_to_center(self, *, duration_ms: int = 3000):
        return self.move_to(self.config.center_pose, duration_ms=duration_ms, action="recover_to_center")

    def home(self):
        now = time.monotonic_ns()
        return MotionActionResult(
            action="home",
            sent=True,
            outcome=MotionActionOutcome.HOME_CONFIRMED,
            motion_stopped=True,
            target_guard_passed=True,
            generation=1,
            started_monotonic_ns=now,
            settled_monotonic_ns=now,
            target_pose=None,
            target_pulses=(0,) * 6,
            final_pulses=(0,) * 6,
            errors_pulses=(0,) * 6,
            stationary_spans_pulses=(0,) * 6,
            maximum_error_pulses=0,
            maximum_error_mm=0.0,
            details="confirmed test feedback",
        )

    def mark_trajectory_active(self) -> None:
        self.snapshot = MotionPlatformSnapshot(MotionPlatformState.TRAJECTORY, 1)

    def mark_trajectory_stopped(self) -> None:
        self.snapshot = MotionPlatformSnapshot(MotionPlatformState.READY, 1)

    def send_trajectory_point(self, pose, *, duration_ms: int):
        return self.move_to(pose, duration_ms=duration_ms, action="trajectory_point")

    def wait_for_target(self, pose, *, timeout_ms=None, action="trajectory"):
        targets = solve_actuator_targets(self.config, pose).pulses
        now = time.monotonic_ns()
        return MotionActionResult(
            action=action,
            sent=True,
            outcome=MotionActionOutcome.SETTLED,
            motion_stopped=True,
            target_guard_passed=True,
            generation=1,
            started_monotonic_ns=now,
            settled_monotonic_ns=now,
            target_pose=pose,
            target_pulses=targets,
            final_pulses=targets,
            errors_pulses=(0,) * 6,
            stationary_spans_pulses=(0,) * 6,
            maximum_error_pulses=0,
            maximum_error_mm=0.0,
            details="confirmed test feedback",
        )
