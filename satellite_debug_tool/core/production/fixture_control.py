"""Background A6/A6T command scheduler for the fixture debug workspace."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import queue
import threading
from typing import Callable, Optional
import uuid

from PySide6.QtCore import QThread, Signal, Slot

from .fixture_profile import WorkstationFixtureProfile
from .fixture_session import FixtureSessionRecorder
from .motion_platform import (
    AbsoluteMoveRunner,
    CombinedSineProfile,
    LingjingPlatformAdapter,
    MotionPlatformError,
    MotionTrajectoryRunner,
    PlatformSendResult,
    PlatformPose,
)


class FixtureControlRequestType(str, Enum):
    ABSOLUTE_AXIS = "absolute_axis"
    ABSOLUTE_POSE = "absolute_pose"
    TRAJECTORY = "trajectory"
    CENTER = "center"
    RESET = "reset"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class FixtureControlRequest:
    request_id: str
    request_type: FixtureControlRequestType
    axis: str = ""
    target_deg: float = 0.0
    duration_ms: int = 0
    trajectory: Optional[CombinedSineProfile] = None
    explicit_reset_confirmation: bool = False
    target_pose: Optional[PlatformPose] = None


class FixtureControlWorker(QThread):
    request_started = Signal(str, str)
    send_result = Signal(object, str, int)
    request_finished = Signal(str, object)
    request_failed = Signal(str, str)
    evidence_failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(
        self,
        profile: WorkstationFixtureProfile,
        *,
        sender: Optional[Callable[[bytes, tuple[str, int]], bool]] = None,
        recorder: Optional[FixtureSessionRecorder] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        profile.validate()
        self._profile = profile
        self._adapter = LingjingPlatformAdapter(profile.to_motion_config(), sender=sender)
        self._queue: queue.Queue[FixtureControlRequest] = queue.Queue(maxsize=16)
        self._shutdown_event = threading.Event()
        self._current_stop_event = threading.Event()
        self._recorder = recorder
        self._sequence = 0
        self._preflight_confirmed = False
        self._evidence_ready = True

    @property
    def adapter(self) -> LingjingPlatformAdapter:
        return self._adapter

    @property
    def preflight_confirmed(self) -> bool:
        return self._preflight_confirmed

    @property
    def evidence_ready(self) -> bool:
        return self._evidence_ready

    def confirm_preflight(self) -> None:
        if self.isRunning():
            raise MotionPlatformError("preflight must be confirmed before starting control")
        self._adapter.confirm_preflight(
            self._profile.calibration_id,
            safety_confirmed=True,
            center_confirmed=True,
        )
        self._preflight_confirmed = True

    def clear_preflight(self) -> None:
        self._adapter.clear_safety_confirmation()
        self._preflight_confirmed = False

    def submit_axis_move(
        self,
        axis: str,
        target_deg: float,
        *,
        total_duration_ms: int,
    ) -> str:
        return self._submit(
            FixtureControlRequest(
                request_id=uuid.uuid4().hex,
                request_type=FixtureControlRequestType.ABSOLUTE_AXIS,
                axis=str(axis),
                target_deg=float(target_deg),
                duration_ms=int(total_duration_ms),
            )
        )

    def submit_trajectory(self, trajectory: CombinedSineProfile) -> str:
        return self._submit(
            FixtureControlRequest(
                request_id=uuid.uuid4().hex,
                request_type=FixtureControlRequestType.TRAJECTORY,
                trajectory=trajectory,
            )
        )

    def submit_pose(
        self,
        pose: PlatformPose,
        *,
        total_duration_ms: int,
    ) -> str:
        return self._submit(
            FixtureControlRequest(
                request_id=uuid.uuid4().hex,
                request_type=FixtureControlRequestType.ABSOLUTE_POSE,
                duration_ms=int(total_duration_ms),
                target_pose=pose,
            )
        )

    def submit_center(self, *, duration_ms: int) -> str:
        return self._submit(
            FixtureControlRequest(
                request_id=uuid.uuid4().hex,
                request_type=FixtureControlRequestType.CENTER,
                duration_ms=int(duration_ms),
            )
        )

    def submit_reset(self, *, duration_ms: int, explicit_confirmation: bool) -> str:
        return self._submit(
            FixtureControlRequest(
                request_id=uuid.uuid4().hex,
                request_type=FixtureControlRequestType.RESET,
                duration_ms=int(duration_ms),
                explicit_reset_confirmation=bool(explicit_confirmation),
            )
        )

    def _submit(self, request: FixtureControlRequest) -> str:
        if not self._preflight_confirmed or not self._adapter.ready:
            raise MotionPlatformError("fixture control preflight is not confirmed")
        if not self._evidence_ready:
            raise MotionPlatformError("fixture evidence recording has failed")
        if not self.isRunning():
            raise MotionPlatformError("fixture control worker is not running")
        try:
            self._queue.put_nowait(request)
        except queue.Full as exc:
            raise MotionPlatformError("fixture control queue is full") from exc
        return request.request_id

    @Slot()
    def stop_sequence(self) -> None:
        self._current_stop_event.set()
        retained_shutdown: Optional[FixtureControlRequest] = None
        while True:
            try:
                request = self._queue.get_nowait()
            except queue.Empty:
                break
            if request.request_type == FixtureControlRequestType.SHUTDOWN:
                retained_shutdown = request
        if retained_shutdown is not None:
            self._queue.put_nowait(retained_shutdown)

    @Slot()
    def shutdown(self) -> None:
        self._shutdown_event.set()
        self.stop_sequence()
        try:
            self._queue.put_nowait(
                FixtureControlRequest(
                    request_id=uuid.uuid4().hex,
                    request_type=FixtureControlRequestType.SHUTDOWN,
                )
            )
        except queue.Full:
            pass

    def run(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                request = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if request.request_type == FixtureControlRequestType.SHUTDOWN:
                break
            self._current_stop_event = threading.Event()
            self.busy_changed.emit(True)
            self.request_started.emit(request.request_id, request.request_type.value)
            try:
                result = self._execute(request)
            except Exception as exc:
                self.request_failed.emit(request.request_id, str(exc))
                self._record_control_failure(request, str(exc))
            else:
                self.request_finished.emit(request.request_id, result)
            finally:
                self.busy_changed.emit(False)
        self._adapter.close()

    def _execute(self, request: FixtureControlRequest):
        if request.request_type == FixtureControlRequestType.ABSOLUTE_AXIS:
            return AbsoluteMoveRunner(
                self._adapter,
                sleep=self._interruptible_sleep,
            ).run_axis(
                request.axis,
                request.target_deg,
                total_duration_ms=request.duration_ms,
                stop_event=self._current_stop_event,
                on_send=lambda result: self._handle_send(result, "absolute_axis"),
            )
        if request.request_type == FixtureControlRequestType.TRAJECTORY:
            if request.trajectory is None:
                raise MotionPlatformError("trajectory request has no profile")
            return MotionTrajectoryRunner(
                self._adapter,
                sleep=self._interruptible_sleep,
            ).run(
                request.trajectory,
                stop_event=self._current_stop_event,
                on_send=lambda result: self._handle_send(result, "trajectory"),
            )
        if request.request_type == FixtureControlRequestType.ABSOLUTE_POSE:
            if request.target_pose is None:
                raise MotionPlatformError("absolute pose request has no target")
            return AbsoluteMoveRunner(
                self._adapter,
                sleep=self._interruptible_sleep,
            ).run_pose(
                request.target_pose,
                total_duration_ms=request.duration_ms,
                stop_event=self._current_stop_event,
                on_send=lambda result: self._handle_send(result, "absolute_pose"),
            )
        if request.request_type == FixtureControlRequestType.CENTER:
            return AbsoluteMoveRunner(
                self._adapter,
                sleep=self._interruptible_sleep,
            ).run_pose(
                self._profile.center_pose,
                total_duration_ms=request.duration_ms,
                stop_event=self._current_stop_event,
                on_send=lambda result: self._handle_send(result, "center"),
            )
        if request.request_type == FixtureControlRequestType.RESET:
            if not request.explicit_reset_confirmation:
                raise MotionPlatformError("reset to Z=0 requires explicit confirmation")
            return AbsoluteMoveRunner(
                self._adapter,
                sleep=self._interruptible_sleep,
            ).run_pose(
                self._profile.reset_pose,
                total_duration_ms=request.duration_ms,
                stop_event=self._current_stop_event,
                on_send=lambda result: self._handle_send(result, "reset"),
            )
        raise MotionPlatformError("unsupported fixture control request")

    def _interruptible_sleep(self, seconds: float) -> None:
        # A normal Stop must let the current A6T completion interval elapse.
        # Process shutdown may wake the scheduler because no further command is
        # sent and the platform continues the already-issued movement itself.
        self._shutdown_event.wait(max(0.0, float(seconds)))

    def _handle_send(self, result: PlatformSendResult, action: str) -> None:
        self._sequence += 1
        self.send_result.emit(result, action, self._sequence)
        if self._recorder is not None:
            try:
                self._recorder.record_command(
                    result,
                    action=action,
                    sequence=self._sequence,
                )
            except Exception as exc:
                self._fail_evidence(str(exc))

    def _record_control_failure(
        self,
        request: FixtureControlRequest,
        details: str,
    ) -> None:
        if self._recorder is None or not self._evidence_ready:
            return
        try:
            self._recorder.record_event(
                "control_failed",
                details,
                details={
                    "request_id": request.request_id,
                    "request_type": request.request_type.value,
                },
            )
        except Exception as exc:
            self._fail_evidence(str(exc))

    def _fail_evidence(self, details: str) -> None:
        if not self._evidence_ready:
            return
        self._evidence_ready = False
        self._current_stop_event.set()
        self._shutdown_event.set()
        self.evidence_failed.emit(str(details))


__all__ = [
    "FixtureControlRequest",
    "FixtureControlRequestType",
    "FixtureControlWorker",
]
