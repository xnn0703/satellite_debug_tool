"""灵境运动平台直接控制器：唯一 UDP owner 与反馈闭环状态机。"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
import errno
import socket
import threading
import time
from typing import Callable, Optional, Protocol

from .evidence import MotionEvidenceRecorder
from .lingjing_protocol import (
    ControllerFeedback,
    HOME_COMMAND,
    HOME_FRAME,
    RUNNING_SYSTEM_STATE,
    build_position_frame,
    parse_feedback_frame,
    solve_actuator_targets,
)
from .models import (
    MotionActionResult,
    MotionActionOutcome,
    MotionCommandEvent,
    MotionPlatformConfig,
    MotionPlatformError,
    MotionPlatformSnapshot,
    MotionPlatformState,
    PlatformPose,
)


class DatagramTransport(Protocol):
    def bind(self, endpoint: tuple[str, int]) -> None: ...
    def settimeout(self, value: float) -> None: ...
    def sendto(self, data: bytes, endpoint: tuple[str, int]) -> int: ...
    def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]: ...
    def close(self) -> None: ...


SnapshotCallback = Callable[[MotionPlatformSnapshot], None]
CommandCallback = Callable[[MotionCommandEvent], None]


class DirectMotionPlatformController:
    """基于控制器实时反馈确认动作结果的同步领域控制器。"""

    def __init__(
        self,
        config: MotionPlatformConfig,
        *,
        socket_factory: Callable[[], DatagramTransport] | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        recorder: Optional[MotionEvidenceRecorder] = None,
    ) -> None:
        config.validate()
        self._config = config
        self._socket_factory = socket_factory or self._new_socket
        self._clock_ns = clock_ns
        self._recorder = recorder
        self._condition = threading.Condition()
        self._socket: Optional[DatagramTransport] = None
        self._receiver: Optional[threading.Thread] = None
        self._closing = threading.Event()
        self._snapshot = MotionPlatformSnapshot(MotionPlatformState.OFFLINE, 0)
        self._last_feedback: Optional[ControllerFeedback] = None
        self._feedback_history: deque[tuple[int, tuple[int, ...]]] = deque(maxlen=8192)
        self._action_generation = 0
        self._snapshot_callbacks: list[SnapshotCallback] = []
        self._command_callbacks: list[CommandCallback] = []

    @staticmethod
    def _new_socket() -> DatagramTransport:
        return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    @property
    def config(self) -> MotionPlatformConfig:
        return self._config

    @property
    def snapshot(self) -> MotionPlatformSnapshot:
        with self._condition:
            snapshot = self._snapshot
            if snapshot.feedback_monotonic_ns:
                age = (self._clock_ns() - snapshot.feedback_monotonic_ns) / 1_000_000.0
                return replace(snapshot, feedback_age_ms=max(0.0, age))
            return snapshot

    def add_snapshot_callback(self, callback: SnapshotCallback) -> None:
        self._snapshot_callbacks.append(callback)

    def add_command_callback(self, callback: CommandCallback) -> None:
        self._command_callbacks.append(callback)

    def connect(self, *, wait_ready_ms: int = 3000) -> MotionPlatformSnapshot:
        details = ""
        with self._condition:
            if self._socket is not None:
                return self.snapshot
            transport = self._socket_factory()
            transport.settimeout(0.05)
            try:
                transport.bind((self._config.local_ipv4, self._config.feedback_port))
            except OSError as exc:
                transport.close()
                if exc.errno == errno.EADDRNOTAVAIL:
                    raise MotionPlatformError(
                        f"local feedback IPv4 {self._config.local_ipv4} is not assigned to this workstation"
                    ) from exc
                raise MotionPlatformError(
                    f"cannot bind motion feedback endpoint "
                    f"{self._config.local_ipv4}:{self._config.feedback_port}: {exc}"
                ) from exc
            self._socket = transport
            self._closing.clear()
            self._feedback_history.clear()
            self._set_state_locked(MotionPlatformState.STARTING, "waiting for controller feedback")
            self._receiver = threading.Thread(
                target=self._receive_loop,
                name="motion-platform-feedback",
                daemon=True,
            )
            self._receiver.start()
            deadline_ns = self._clock_ns() + int(wait_ready_ms) * 1_000_000
            while self._snapshot.state == MotionPlatformState.STARTING:
                remaining = (deadline_ns - self._clock_ns()) / 1_000_000_000.0
                if remaining <= 0:
                    break
                self._condition.wait(min(remaining, 0.05))
            if self._snapshot.state != MotionPlatformState.READY:
                details = (
                    "controller feedback timeout"
                    if self._snapshot.state == MotionPlatformState.STARTING
                    else (self._snapshot.details or "controller is not ready")
                )
        if details:
            self.close()
            raise MotionPlatformError(details)
        return self.snapshot

    def close(self) -> None:
        self._closing.set()
        with self._condition:
            transport = self._socket
            self._socket = None
            self._set_state_locked(MotionPlatformState.OFFLINE, "control session closed")
            self._condition.notify_all()
        if transport is not None:
            transport.close()
        receiver = self._receiver
        if receiver is not None and receiver is not threading.current_thread():
            receiver.join(timeout=1.0)
        self._receiver = None

    def move_to(
        self,
        pose: PlatformPose,
        *,
        duration_ms: int,
        action: str = "move",
        timeout_ms: Optional[int] = None,
    ) -> MotionActionResult:
        if duration_ms < self._config.minimum_duration_ms:
            raise MotionPlatformError("motion duration is shorter than the configured minimum")
        targets = solve_actuator_targets(self._config, pose)
        frame = build_position_frame(int(duration_ms), targets.pulses)
        return self._send_and_wait(
            action=action,
            frame=frame,
            target_pose=pose,
            target_pulses=targets.pulses,
            duration_ms=int(duration_ms),
            timeout_ms=int(timeout_ms or max(self._config.action_timeout_ms, duration_ms + 3000)),
        )

    def recover_to_center(self, *, duration_ms: int = 3000) -> MotionActionResult:
        with self._condition:
            if self._snapshot.state not in {
                MotionPlatformState.READY,
                MotionPlatformState.RECOVERY_REQUIRED,
                MotionPlatformState.FAULT,
            }:
                raise MotionPlatformError("controller state does not allow center recovery")
            if not self._feedback_is_fresh_locked():
                raise MotionPlatformError("fresh controller feedback is required for center recovery")
            feedback = self._last_feedback
            if feedback is None or feedback.system_state != RUNNING_SYSTEM_STATE:
                raise MotionPlatformError("controller running state is required for center recovery")
            if any(feedback.servo_error_codes):
                raise MotionPlatformError("servo errors must be cleared before center recovery")
            self._set_state_locked(MotionPlatformState.READY, "center recovery authorized")
        return self.move_to(
            self._config.center_pose,
            duration_ms=duration_ms,
            action="recover_to_center",
        )

    def home(self) -> MotionActionResult:
        started = self._clock_ns()
        with self._condition:
            self._require_ready_locked()
            generation = self._snapshot.generation
            self._action_generation = generation
            self._set_state_locked(MotionPlatformState.HOMING, "home command sent")
        self._send_frame(
            MotionCommandEvent(
                action="home",
                raw_command=HOME_FRAME,
                target_pose=None,
                target_pulses=(),
                duration_ms=None,
                monotonic_ns=started,
                generation=generation,
            )
        )
        deadline_ns = started + self._config.home_timeout_ms * 1_000_000
        observed_home = False
        with self._condition:
            while True:
                now = self._clock_ns()
                snapshot = self._snapshot
                feedback = self._last_feedback
                if snapshot.generation != generation:
                    raise MotionPlatformError("controller restarted during home")
                if snapshot.state == MotionPlatformState.FAULT:
                    raise MotionPlatformError(snapshot.details)
                if snapshot.state == MotionPlatformState.OFFLINE:
                    raise MotionPlatformError(snapshot.details or "controller is offline")
                if feedback is not None and feedback.received_command == HOME_COMMAND:
                    observed_home = True
                if (
                    observed_home
                    and feedback is not None
                    and feedback.system_state == RUNNING_SYSTEM_STATE
                    and feedback.received_command_result == 1
                ):
                    stopped, guard_passed, spans = self._settlement_locked(
                        started,
                        (0, 0, 0, 0, 0, 0),
                    )
                    if stopped:
                        outcome = (
                            MotionActionOutcome.HOME_CONFIRMED
                            if guard_passed
                            else MotionActionOutcome.SETTLED_OUTSIDE_TARGET
                        )
                        details = (
                            "home confirmed by controller feedback"
                            if guard_passed
                            else "home stopped outside the zero-position guard"
                        )
                        self._set_state_locked(MotionPlatformState.READY, details)
                        return self._result(
                            "home",
                            started,
                            None,
                            (0, 0, 0, 0, 0, 0),
                            outcome,
                            guard_passed,
                            spans,
                        )
                if now >= deadline_ns:
                    self._set_state_locked(MotionPlatformState.RECOVERY_REQUIRED, "home feedback timeout")
                    raise MotionPlatformError("home feedback timeout")
                self._condition.wait(min((deadline_ns - now) / 1_000_000_000.0, 0.05))

    def mark_trajectory_active(self) -> None:
        with self._condition:
            self._require_ready_locked()
            self._set_state_locked(MotionPlatformState.TRAJECTORY, "trajectory started")

    def mark_trajectory_stopped(self) -> None:
        with self._condition:
            if self._snapshot.state == MotionPlatformState.TRAJECTORY:
                state = MotionPlatformState.READY if self._feedback_is_fresh_locked() else MotionPlatformState.OFFLINE
                self._set_state_locked(state, "trajectory generation stopped")

    def send_trajectory_point(self, pose: PlatformPose, *, duration_ms: int) -> MotionCommandEvent:
        targets = solve_actuator_targets(self._config, pose)
        frame = build_position_frame(duration_ms, targets.pulses)
        with self._condition:
            if self._snapshot.state != MotionPlatformState.TRAJECTORY:
                raise MotionPlatformError("trajectory is not active")
            generation = self._snapshot.generation
        event = MotionCommandEvent(
            action="trajectory_point",
            raw_command=frame,
            target_pose=pose,
            target_pulses=targets.pulses,
            duration_ms=duration_ms,
            monotonic_ns=self._clock_ns(),
            generation=generation,
        )
        self._send_frame(event)
        with self._condition:
            self._snapshot = replace(self._snapshot, target_pulses=targets.pulses)
            self._condition.notify_all()
        return event

    def wait_for_target(
        self,
        pose: PlatformPose,
        *,
        timeout_ms: Optional[int] = None,
        action: str = "trajectory",
    ) -> MotionActionResult:
        targets = solve_actuator_targets(self._config, pose)
        started = self._clock_ns()
        with self._condition:
            generation = self._snapshot.generation
            if self._snapshot.state == MotionPlatformState.TRAJECTORY:
                self._snapshot = replace(self._snapshot, state=MotionPlatformState.MOVING)
        return self._wait_for_settlement(
            action,
            started,
            generation,
            pose,
            targets.pulses,
            int(timeout_ms or self._config.action_timeout_ms),
        )

    def _send_and_wait(
        self,
        *,
        action: str,
        frame: bytes,
        target_pose: PlatformPose,
        target_pulses: tuple[int, ...],
        duration_ms: int,
        timeout_ms: int,
    ) -> MotionActionResult:
        started = self._clock_ns()
        with self._condition:
            self._require_ready_locked()
            generation = self._snapshot.generation
            self._action_generation = generation
            self._snapshot = replace(
                self._snapshot,
                state=MotionPlatformState.MOVING,
                target_pulses=target_pulses,
                details=f"{action} command sent",
            )
        self._send_frame(MotionCommandEvent(
            action=action,
            raw_command=frame,
            target_pose=target_pose,
            target_pulses=target_pulses,
            duration_ms=duration_ms,
            monotonic_ns=started,
            generation=generation,
        ))
        return self._wait_for_settlement(action, started, generation, target_pose, target_pulses, timeout_ms)

    def _wait_for_settlement(
        self,
        action: str,
        started: int,
        generation: int,
        target_pose: PlatformPose,
        target_pulses: tuple[int, ...],
        timeout_ms: int,
    ) -> MotionActionResult:
        deadline_ns = started + timeout_ms * 1_000_000
        with self._condition:
            while True:
                now = self._clock_ns()
                snapshot = self._snapshot
                if snapshot.generation != generation:
                    raise MotionPlatformError("controller restarted during motion")
                if snapshot.state == MotionPlatformState.FAULT:
                    raise MotionPlatformError(snapshot.details)
                if snapshot.state == MotionPlatformState.RECOVERY_REQUIRED:
                    raise MotionPlatformError(snapshot.details)
                if snapshot.state == MotionPlatformState.OFFLINE:
                    raise MotionPlatformError(snapshot.details or "controller is offline")
                feedback = self._last_feedback
                if feedback is not None and feedback.system_state == RUNNING_SYSTEM_STATE:
                    stopped, guard_passed, spans = self._settlement_locked(
                        started,
                        target_pulses,
                    )
                    if stopped:
                        outcome = (
                            MotionActionOutcome.SETTLED
                            if guard_passed
                            else MotionActionOutcome.SETTLED_OUTSIDE_TARGET
                        )
                        details = (
                            f"{action} motion stopped within the target guard"
                            if guard_passed
                            else f"{action} motion stopped outside the target guard"
                        )
                        self._set_state_locked(MotionPlatformState.READY, details)
                        return self._result(
                            action,
                            started,
                            target_pose,
                            target_pulses,
                            outcome,
                            guard_passed,
                            spans,
                        )
                if now >= deadline_ns:
                    self._set_state_locked(MotionPlatformState.RECOVERY_REQUIRED, f"{action} motion did not stop before timeout")
                    raise MotionPlatformError(f"{action} motion did not stop before timeout")
                self._condition.wait(min((deadline_ns - now) / 1_000_000_000.0, 0.05))

    def _result(
        self,
        action: str,
        started: int,
        target_pose: Optional[PlatformPose],
        target_pulses: tuple[int, ...],
        outcome: MotionActionOutcome,
        target_guard_passed: bool,
        stationary_spans_pulses: tuple[int, ...],
    ) -> MotionActionResult:
        actual = self._last_feedback.axis_pulses if self._last_feedback is not None else ()
        errors = tuple(abs(a - b) for a, b in zip(actual, target_pulses))
        maximum = max(errors, default=None)
        pulses_per_mm = (
            self._config.geometry.pulses_per_extension_mm
            * self._config.geometry.electronic_gear_ratio
        )
        result = MotionActionResult(
            action=action,
            sent=True,
            outcome=outcome,
            motion_stopped=True,
            target_guard_passed=target_guard_passed,
            generation=self._snapshot.generation,
            started_monotonic_ns=started,
            settled_monotonic_ns=self._clock_ns(),
            target_pose=target_pose,
            target_pulses=target_pulses,
            final_pulses=actual,
            errors_pulses=errors,
            stationary_spans_pulses=stationary_spans_pulses,
            maximum_error_pulses=maximum,
            maximum_error_mm=(maximum / pulses_per_mm if maximum is not None else None),
            details=self._snapshot.details,
        )
        if self._recorder is not None:
            self._recorder.record("actions", "settled", result)
        return result

    def _settlement_locked(
        self,
        started_ns: int,
        target_pulses: tuple[int, ...],
    ) -> tuple[bool, bool, tuple[int, ...]]:
        """Return motion-stop and target-guard facts from one feedback window."""
        now = self._clock_ns()
        window_ns = self._config.stationary_window_ms * 1_000_000
        if now - started_ns < window_ns:
            return False, False, ()
        cutoff = now - window_ns
        after_command = [
            sample
            for sample in self._feedback_history
            if sample[0] >= started_ns
        ]
        if not after_command:
            return False, False, ()
        before_cutoff = [sample for sample in after_command if sample[0] <= cutoff]
        window = [sample for sample in after_command if sample[0] > cutoff]
        if before_cutoff:
            window.insert(0, before_cutoff[-1])
        if len(window) < 2 or window[0][0] > cutoff:
            return False, False, ()
        spans = tuple(
            max(sample[1][axis] for sample in window)
            - min(sample[1][axis] for sample in window)
            for axis in range(6)
        )
        stopped = all(
            span <= self._config.stationary_span_pulses
            for span in spans
        )
        if not stopped or self._last_feedback is None:
            return False, False, spans
        errors = tuple(
            abs(actual - target)
            for actual, target in zip(self._last_feedback.axis_pulses, target_pulses)
        )
        pulses_per_mm = (
            self._config.geometry.pulses_per_extension_mm
            * self._config.geometry.electronic_gear_ratio
        )
        guard_pulses = self._config.target_guard_mm * pulses_per_mm
        return True, bool(errors) and max(errors) <= guard_pulses, spans

    def _send_frame(self, event: MotionCommandEvent) -> None:
        with self._condition:
            transport = self._socket
        if transport is None:
            raise MotionPlatformError("motion platform is offline")
        try:
            size = transport.sendto(event.raw_command, (self._config.controller_ipv4, self._config.command_port))
        except OSError as exc:
            with self._condition:
                self._set_state_locked(MotionPlatformState.OFFLINE, f"command send failed: {exc}")
            raise MotionPlatformError(f"command send failed: {exc}") from exc
        if size != len(event.raw_command):
            raise MotionPlatformError("controller datagram was not sent completely")
        if self._recorder is not None:
            self._recorder.record("commands", "sent", event)
        for callback in tuple(self._command_callbacks):
            callback(event)

    def _receive_loop(self) -> None:
        while not self._closing.is_set():
            with self._condition:
                transport = self._socket
            if transport is None:
                return
            try:
                data, source = transport.recvfrom(4096)
            except (socket.timeout, TimeoutError):
                self._mark_stale_if_required()
                continue
            except OSError as exc:
                if not self._closing.is_set():
                    with self._condition:
                        self._set_state_locked(MotionPlatformState.OFFLINE, f"feedback receive failed: {exc}")
                return
            if source != (self._config.controller_ipv4, self._config.command_port):
                if self._recorder is not None:
                    self._recorder.record("events", "unexpected_source", {"source": list(source), "size": len(data)})
                continue
            try:
                feedback = parse_feedback_frame(data)
            except MotionPlatformError as exc:
                if self._recorder is not None:
                    self._recorder.record("events", "invalid_feedback", {"error": str(exc), "frame": data})
                continue
            try:
                self.process_feedback(feedback, raw_frame=data)
            except Exception as exc:
                with self._condition:
                    self._snapshot = replace(
                        self._snapshot,
                        state=MotionPlatformState.FAULT,
                        details=f"motion evidence recording failed: {exc}",
                    )
                    self._condition.notify_all()
                    callbacks = tuple(self._snapshot_callbacks)
                for callback in callbacks:
                    callback(self.snapshot)
                return

    def process_feedback(self, feedback: ControllerFeedback, *, raw_frame: bytes = b"") -> None:
        """接收已解析反馈；供 UDP 接收线程和确定性协议测试共同使用。"""
        now = self._clock_ns()
        callbacks: tuple[SnapshotCallback, ...]
        with self._condition:
            previous = self._last_feedback
            restarted = previous is not None and (
                feedback.sequence_id < previous.sequence_id
                or feedback.controller_time < previous.controller_time
            )
            generation = self._snapshot.generation + (1 if restarted else 0)
            self._last_feedback = feedback
            errors = tuple(
                actual - target
                for actual, target in zip(feedback.axis_pulses, self._snapshot.target_pulses)
            )
            state = self._snapshot.state
            details = self._snapshot.details
            if restarted:
                state = MotionPlatformState.RECOVERY_REQUIRED
                details = "controller sequence or time restarted"
                self._feedback_history.clear()
            elif any(feedback.servo_error_codes):
                state = MotionPlatformState.FAULT
                details = "servo error: " + ", ".join(
                    f"axis {index}=0x{code:04X}"
                    for index, code in enumerate(feedback.servo_error_codes, start=1)
                    if code
                )
            elif state in {MotionPlatformState.STARTING, MotionPlatformState.OFFLINE}:
                if feedback.system_state == RUNNING_SYSTEM_STATE:
                    state = MotionPlatformState.READY
                    details = "controller feedback is valid"
                else:
                    state = MotionPlatformState.STARTING
                    details = f"controller system state {feedback.system_state}"
            self._snapshot = MotionPlatformSnapshot(
                state=state,
                generation=generation,
                feedback_monotonic_ns=now,
                feedback_age_ms=0.0,
                target_pulses=self._snapshot.target_pulses,
                actual_pulses=feedback.axis_pulses,
                errors_pulses=errors,
                servo_error_codes=feedback.servo_error_codes,
                system_state=feedback.system_state,
                received_command=feedback.received_command,
                received_command_result=feedback.received_command_result,
                details=details,
            )
            self._feedback_history.append((now, feedback.axis_pulses))
            self._condition.notify_all()
            callbacks = tuple(self._snapshot_callbacks)
        if self._recorder is not None:
            self._recorder.record("feedback", "received", {"raw": raw_frame, "feedback": feedback, "generation": generation})
        for callback in callbacks:
            callback(self.snapshot)

    def _mark_stale_if_required(self) -> None:
        callbacks: tuple[SnapshotCallback, ...] = ()
        with self._condition:
            if self._snapshot.state == MotionPlatformState.HOMING:
                return
            if self._snapshot.feedback_monotonic_ns == 0:
                return
            age_ms = (self._clock_ns() - self._snapshot.feedback_monotonic_ns) / 1_000_000.0
            if age_ms <= self._config.feedback_fresh_ms:
                return
            if self._snapshot.state in {MotionPlatformState.MOVING, MotionPlatformState.TRAJECTORY}:
                state = MotionPlatformState.RECOVERY_REQUIRED
                details = "controller feedback lost during motion"
            else:
                state = MotionPlatformState.OFFLINE
                details = "controller feedback is stale"
            self._snapshot = replace(self._snapshot, state=state, feedback_age_ms=age_ms, details=details)
            self._condition.notify_all()
            callbacks = tuple(self._snapshot_callbacks)
        for callback in callbacks:
            callback(self.snapshot)

    def _require_ready_locked(self) -> None:
        if self._snapshot.state != MotionPlatformState.READY:
            raise MotionPlatformError(f"motion platform is not ready: {self._snapshot.state.value}")
        if not self._feedback_is_fresh_locked():
            raise MotionPlatformError("motion platform feedback is stale")

    def _feedback_is_fresh_locked(self) -> bool:
        timestamp = self._snapshot.feedback_monotonic_ns
        return bool(timestamp) and self._clock_ns() - timestamp <= self._config.feedback_fresh_ms * 1_000_000

    def _set_state_locked(self, state: MotionPlatformState, details: str) -> None:
        self._snapshot = replace(self._snapshot, state=state, details=details)
        if self._recorder is not None:
            self._recorder.record("events", "state", {"state": state.value, "details": details, "generation": self._snapshot.generation})


__all__ = ["DatagramTransport", "DirectMotionPlatformController"]
