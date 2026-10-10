"""夹具页面的运动平台组件线程适配层。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import queue
import threading
from typing import Callable, Optional
import uuid

from PySide6.QtCore import QThread, Signal, Slot

from satellite_debug_tool.core.motion_platform import (
    DirectMotionPlatformController,
    MotionCommandEvent,
    MotionPlatformError,
    MotionPlatformState,
    MotionTrajectoryRunner,
    PlatformPose,
    SineTrajectory,
)
from .fixture_profile import WorkstationFixtureProfile
from .fixture_session import FixtureSessionRecorder


class FixtureControlRequestType(str, Enum):
    ABSOLUTE_AXIS = "absolute_axis"
    ABSOLUTE_POSE = "absolute_pose"
    TRAJECTORY = "trajectory"
    CENTER = "center"
    HOME = "home"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class FixtureControlRequest:
    request_id: str
    request_type: FixtureControlRequestType
    axis: str = ""
    target_deg: float = 0.0
    duration_ms: int = 0
    trajectory: Optional[SineTrajectory] = None
    target_pose: Optional[PlatformPose] = None


ControllerFactory = Callable[[WorkstationFixtureProfile], DirectMotionPlatformController]


class FixtureControlWorker(QThread):
    request_started = Signal(str, str)
    send_result = Signal(object, str, int)
    request_finished = Signal(str, object)
    request_failed = Signal(str, str)
    evidence_failed = Signal(str)
    busy_changed = Signal(bool)
    snapshot_changed = Signal(object)
    connected = Signal()

    def __init__(
        self,
        profile: WorkstationFixtureProfile,
        *,
        controller_factory: Optional[ControllerFactory] = None,
        recorder: Optional[FixtureSessionRecorder] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        profile.validate()
        self._profile = profile
        factory = controller_factory or (
            lambda selected: DirectMotionPlatformController(
                selected.to_motion_config(), recorder=recorder
            )
        )
        self._controller = factory(profile)
        self._controller.add_command_callback(self._handle_command)
        self._controller.add_snapshot_callback(self.snapshot_changed.emit)
        self._queue: queue.Queue[FixtureControlRequest] = queue.Queue(maxsize=16)
        self._shutdown_event = threading.Event()
        self._current_stop_event = threading.Event()
        self._recorder = recorder
        self._sequence = 0
        self._preflight_confirmed = False
        self._evidence_ready = True

    @property
    def controller(self) -> DirectMotionPlatformController:
        return self._controller

    @property
    def preflight_confirmed(self) -> bool:
        return self._preflight_confirmed

    @property
    def evidence_ready(self) -> bool:
        return self._evidence_ready

    def confirm_preflight(self) -> None:
        if self.isRunning():
            raise MotionPlatformError("preflight must be confirmed before starting control")
        self._preflight_confirmed = True

    def clear_preflight(self) -> None:
        self._preflight_confirmed = False

    def submit_axis_move(self, axis: str, target_deg: float, *, total_duration_ms: int) -> str:
        return self._submit(FixtureControlRequest(
            uuid.uuid4().hex,
            FixtureControlRequestType.ABSOLUTE_AXIS,
            axis=str(axis),
            target_deg=float(target_deg),
            duration_ms=int(total_duration_ms),
        ))

    def submit_trajectory(self, trajectory: SineTrajectory) -> str:
        return self._submit(FixtureControlRequest(
            uuid.uuid4().hex,
            FixtureControlRequestType.TRAJECTORY,
            trajectory=trajectory,
        ))

    def submit_pose(self, pose: PlatformPose, *, total_duration_ms: int) -> str:
        return self._submit(FixtureControlRequest(
            uuid.uuid4().hex,
            FixtureControlRequestType.ABSOLUTE_POSE,
            duration_ms=int(total_duration_ms),
            target_pose=pose,
        ))

    def submit_center(self, *, duration_ms: int) -> str:
        return self._submit(FixtureControlRequest(
            uuid.uuid4().hex,
            FixtureControlRequestType.CENTER,
            duration_ms=int(duration_ms),
        ))

    def submit_home(self) -> str:
        return self._submit(FixtureControlRequest(uuid.uuid4().hex, FixtureControlRequestType.HOME))

    def submit_reset(self, *, duration_ms: int, explicit_confirmation: bool) -> str:
        if not explicit_confirmation:
            raise MotionPlatformError("home requires explicit confirmation")
        return self.submit_home()

    def _submit(self, request: FixtureControlRequest) -> str:
        if not self._preflight_confirmed:
            raise MotionPlatformError("fixture control preflight is not confirmed")
        if not self._evidence_ready:
            raise MotionPlatformError("fixture evidence recording has failed")
        if not self.isRunning():
            raise MotionPlatformError("fixture control worker is not running")
        if self._controller.snapshot.state != MotionPlatformState.READY:
            raise MotionPlatformError(
                f"motion platform is not ready: {self._controller.snapshot.state.value}"
            )
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
        self._controller.close()
        try:
            self._queue.put_nowait(FixtureControlRequest(
                uuid.uuid4().hex,
                FixtureControlRequestType.SHUTDOWN,
            ))
        except queue.Full:
            pass

    def run(self) -> None:
        try:
            self._controller.connect()
        except Exception as exc:
            self.request_failed.emit("", str(exc))
            self._controller.close()
            return
        self.connected.emit()
        self.snapshot_changed.emit(self._controller.snapshot)
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
        self._controller.close()

    def _execute(self, request: FixtureControlRequest):
        if request.request_type == FixtureControlRequestType.ABSOLUTE_AXIS:
            if request.axis not in {"roll", "pitch", "yaw"}:
                raise MotionPlatformError("unsupported platform axis")
            values = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
            values[request.axis] = request.target_deg
            pose = PlatformPose(
                values["roll"], values["pitch"], values["yaw"],
                z_mm=self._profile.center_pose.z_mm,
            )
            return self._controller.move_to(
                pose, duration_ms=request.duration_ms, action="absolute_axis"
            )
        if request.request_type == FixtureControlRequestType.ABSOLUTE_POSE:
            if request.target_pose is None:
                raise MotionPlatformError("absolute pose request has no target")
            return self._controller.move_to(
                request.target_pose,
                duration_ms=request.duration_ms,
                action="absolute_pose",
            )
        if request.request_type == FixtureControlRequestType.CENTER:
            return self._controller.recover_to_center(duration_ms=request.duration_ms)
        if request.request_type == FixtureControlRequestType.HOME:
            return self._controller.home()
        if request.request_type == FixtureControlRequestType.TRAJECTORY:
            if request.trajectory is None:
                raise MotionPlatformError("trajectory request has no profile")
            return MotionTrajectoryRunner(self._controller).run(
                request.trajectory, stop_event=self._current_stop_event
            )
        raise MotionPlatformError("unsupported fixture control request")

    def _handle_command(self, event: MotionCommandEvent) -> None:
        self._sequence += 1
        self.send_result.emit(event, event.action, self._sequence)
        if self._recorder is not None:
            try:
                self._recorder.record_command(event, sequence=self._sequence)
            except Exception as exc:
                self._fail_evidence(str(exc))

    def _record_control_failure(self, request: FixtureControlRequest, details: str) -> None:
        if self._recorder is None or not self._evidence_ready:
            return
        try:
            self._recorder.record_event(
                "control_failed",
                details,
                details={"request_id": request.request_id, "request_type": request.request_type.value},
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


__all__ = ["ControllerFactory", "FixtureControlRequest", "FixtureControlRequestType", "FixtureControlWorker"]
