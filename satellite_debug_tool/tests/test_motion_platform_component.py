"""直接运动平台组件的协议、安全边界和反馈闭环回归。"""

from __future__ import annotations

import errno
import hashlib
import json
import queue
import socket
import struct
import threading
import time

import pytest

from satellite_debug_tool.core.motion_platform import (
    ControllerFeedback,
    DirectMotionPlatformController,
    LingjingProtocolError,
    MotionActionOutcome,
    MotionPlatformConfig,
    MotionPlatformError,
    MotionPlatformState,
    MotionEvidenceRecorder,
    PlatformPose,
    SineAxis,
    SineTrajectory,
    MotionTrajectoryRunner,
    build_position_frame,
    parse_feedback_frame,
    solve_actuator_targets,
)


def _config(**overrides) -> MotionPlatformConfig:
    values = {
        "model_id": "lingjing-a6-200mm",
        "local_ipv4": "192.168.15.101",
        "feedback_port": 8410,
        "controller_ipv4": "192.168.15.201",
        "command_port": 7408,
        "center_pose": PlatformPose(0, 0, 0, 0, 0, 100),
        "roll_abs_limit_deg": 30.0,
        "pitch_abs_limit_deg": 30.0,
        "yaw_abs_limit_deg": 30.0,
        "z_min_mm": 0.0,
        "z_max_mm": 100.0,
        "stationary_window_ms": 20,
        "stationary_span_pulses": 100,
        "target_guard_mm": 0.1,
        "feedback_fresh_ms": 500,
        "action_timeout_ms": 1000,
    }
    values.update(overrides)
    return MotionPlatformConfig(**values)


def _feedback_frame(
    pulses: tuple[int, ...] = (0, 0, 0, 0, 0, 0),
    *,
    sequence: int = 1,
    controller_time: int = 1,
    system_state: int = 4,
    command: int = 0xC0,
    result: int = 0,
    errors: tuple[int, ...] = (0, 0, 0, 0, 0, 0),
) -> bytes:
    return (
        b"\xAA\x55"
        + struct.pack("<iI", sequence, controller_time)
        + bytes((system_state, 0))
        + struct.pack("<4i", 0, 0, 0, 0)
        + bytes((command, result))
        + struct.pack("<6i", *pulses)
        + struct.pack("<6H", *errors)
        + b"\xFB\xFB"
    )


class _FakeSocket:
    def __init__(self) -> None:
        self.incoming: queue.Queue[tuple[bytes, tuple[str, int]] | None] = queue.Queue()
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.bound: tuple[str, int] | None = None
        self.closed = False

    def bind(self, endpoint: tuple[str, int]) -> None:
        self.bound = endpoint

    def settimeout(self, _value: float) -> None:
        return

    def sendto(self, data: bytes, endpoint: tuple[str, int]) -> int:
        self.sent.append((data, endpoint))
        return len(data)

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        try:
            item = self.incoming.get(timeout=0.02)
        except queue.Empty as exc:
            raise socket.timeout from exc
        if item is None:
            raise OSError("closed")
        return item

    def close(self) -> None:
        self.closed = True
        self.incoming.put(None)

    def feedback(self, frame: bytes) -> None:
        self.incoming.put((frame, ("192.168.15.201", 7408)))


class _UnavailableAddressSocket(_FakeSocket):
    def bind(self, endpoint: tuple[str, int]) -> None:
        raise OSError(errno.EADDRNOTAVAIL, "Can't assign requested address")


def test_protocol_golden_vector_and_strict_range_rejection() -> None:
    config = _config()
    targets = solve_actuator_targets(config, PlatformPose(3, -2, 1, 0, 0, 50))
    assert targets.total_lengths_mm == (480.66, 475.24, 460.07, 455.15, 469.45, 468.44)
    assert targets.pulses == (2_198_570, 1_984_480, 1_385_265, 1_190_925, 1_755_775, 1_715_880)
    assert build_position_frame(50, targets.pulses).hex() == (
        "aa55001bc000322a8c2100e0471e00312315000d2c1200"
        "7fca1a00a82e1a00fbfb"
    )
    with pytest.raises(LingjingProtocolError, match="extension|roll angle"):
        solve_actuator_targets(config, PlatformPose(32, 0, 0, 0, 0, 100))


def test_frd_pose_is_converted_to_vendor_platform_axes() -> None:
    logical_pose = PlatformPose(3, 2, 1, 0, 0, 50)
    converted = solve_actuator_targets(
        _config(roll_sign=-1, pitch_sign=1, yaw_sign=-1),
        logical_pose,
    )
    vendor_pose = solve_actuator_targets(
        _config(),
        PlatformPose(-3, 2, -1, 0, 0, 50),
    )

    assert converted.pose == logical_pose
    assert converted.pulses == vendor_pose.pulses


def test_feedback_parser_preserves_controller_facts() -> None:
    frame = _feedback_frame(
        (100, -200, 300, -400, 500, -600),
        sequence=17,
        controller_time=0xFEDCBA98,
        system_state=6,
        command=3,
        result=1,
        errors=(0, 1, 0x1234, 0, 5, 0xFFFF),
    )
    feedback = parse_feedback_frame(frame)
    assert feedback.sequence_id == 17
    assert feedback.controller_time == 0xFEDCBA98
    assert feedback.system_state == 6
    assert feedback.received_command == 3
    assert feedback.received_command_result == 1
    assert feedback.axis_pulses == (100, -200, 300, -400, 500, -600)
    assert feedback.servo_error_codes == (0, 1, 0x1234, 0, 5, 0xFFFF)


def test_connect_explains_missing_local_feedback_address() -> None:
    transport = _UnavailableAddressSocket()
    controller = DirectMotionPlatformController(_config(), socket_factory=lambda: transport)

    with pytest.raises(MotionPlatformError, match="192.168.15.101 is not assigned"):
        controller.connect()

    assert transport.closed


def test_move_completes_only_after_stable_controller_feedback() -> None:
    transport = _FakeSocket()
    controller = DirectMotionPlatformController(_config(), socket_factory=lambda: transport)
    transport.feedback(_feedback_frame(sequence=1, controller_time=1))
    assert controller.connect().state == MotionPlatformState.READY
    pose = PlatformPose(1, 0, 0, 0, 0, 100)
    target = solve_actuator_targets(controller.config, pose).pulses
    completed: list[object] = []
    failures: list[Exception] = []

    def run() -> None:
        try:
            completed.append(controller.move_to(pose, duration_ms=100))
        except Exception as exc:  # pragma: no cover - assertion captures details
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1.0
    while len(transport.sent) < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert transport.sent and transport.sent[0][1] == ("192.168.15.201", 7408)
    time.sleep(0.03)
    assert thread.is_alive(), "UDP send must not complete the action"
    transport.feedback(_feedback_frame(target, sequence=2, controller_time=2))
    time.sleep(0.03)
    transport.feedback(_feedback_frame(target, sequence=3, controller_time=3))
    thread.join(1.0)
    controller.close()
    assert not failures
    assert completed and completed[0].successful
    assert completed[0].outcome == MotionActionOutcome.SETTLED
    assert completed[0].motion_stopped
    assert completed[0].target_guard_passed
    assert completed[0].final_pulses == target


def test_controller_restart_invalidates_active_action() -> None:
    transport = _FakeSocket()
    controller = DirectMotionPlatformController(_config(), socket_factory=lambda: transport)
    transport.feedback(_feedback_frame(sequence=10, controller_time=100))
    controller.connect()
    pose = PlatformPose(1, 0, 0, 0, 0, 100)
    failures: list[Exception] = []
    thread = threading.Thread(
        target=lambda: _capture_failure(failures, lambda: controller.move_to(pose, duration_ms=100))
    )
    thread.start()
    deadline = time.monotonic() + 1.0
    while not transport.sent and time.monotonic() < deadline:
        time.sleep(0.005)
    transport.feedback(_feedback_frame(sequence=1, controller_time=1))
    thread.join(1.0)
    assert failures and "restarted" in str(failures[0])
    assert controller.snapshot.state == MotionPlatformState.RECOVERY_REQUIRED
    controller.close()


def test_servo_error_fails_motion_with_axis_and_code() -> None:
    transport = _FakeSocket()
    controller = DirectMotionPlatformController(_config(), socket_factory=lambda: transport)
    transport.feedback(_feedback_frame(sequence=1, controller_time=1))
    controller.connect()
    failures: list[Exception] = []
    thread = threading.Thread(
        target=lambda: _capture_failure(
            failures,
            lambda: controller.move_to(PlatformPose(1, 0, 0, 0, 0, 100), duration_ms=100),
        )
    )
    thread.start()
    deadline = time.monotonic() + 1.0
    while not transport.sent and time.monotonic() < deadline:
        time.sleep(0.005)
    transport.feedback(_feedback_frame(sequence=2, controller_time=2, errors=(0x7121, 0, 0, 0, 0, 0)))
    thread.join(1.0)
    assert failures and "axis 1=0x7121" in str(failures[0])
    assert controller.snapshot.state == MotionPlatformState.FAULT
    controller.close()


def test_home_requires_command_three_result_and_stable_zero_feedback() -> None:
    transport = _FakeSocket()
    controller = DirectMotionPlatformController(_config(), socket_factory=lambda: transport)
    transport.feedback(_feedback_frame(sequence=1, controller_time=1))
    controller.connect()
    completed = []
    failures = []
    thread = threading.Thread(
        target=lambda: _capture_result(completed, failures, controller.home)
    )
    thread.start()
    deadline = time.monotonic() + 1.0
    while not transport.sent and time.monotonic() < deadline:
        time.sleep(0.005)
    assert transport.sent[0][0] == b"\xAA\x55\x00\x01\x03\xFB\xFB"
    transport.feedback(_feedback_frame(sequence=2, controller_time=2, command=3, result=1))
    time.sleep(0.03)
    transport.feedback(_feedback_frame(sequence=3, controller_time=3, command=3, result=1))
    thread.join(1.0)
    assert not failures
    assert completed and completed[0].successful
    assert completed[0].outcome == MotionActionOutcome.HOME_CONFIRMED
    assert controller.snapshot.state == MotionPlatformState.READY
    controller.close()


def test_field_observed_residual_is_settled_within_physical_guard() -> None:
    transport = _FakeSocket()
    controller = DirectMotionPlatformController(_config(), socket_factory=lambda: transport)
    transport.feedback(_feedback_frame(sequence=1, controller_time=1))
    controller.connect()
    pose = PlatformPose(1, 0, 0, 0, 0, 100)
    target = solve_actuator_targets(controller.config, pose).pulses
    residual = 2191
    stopped = tuple(value + residual for value in target)
    completed: list[object] = []
    failures: list[Exception] = []
    thread = threading.Thread(
        target=lambda: _capture_result(
            completed,
            failures,
            lambda: controller.move_to(pose, duration_ms=100),
        )
    )
    thread.start()
    deadline = time.monotonic() + 1.0
    while not transport.sent and time.monotonic() < deadline:
        time.sleep(0.005)
    transport.feedback(_feedback_frame(stopped, sequence=2, controller_time=2))
    time.sleep(0.03)
    transport.feedback(_feedback_frame(stopped, sequence=3, controller_time=3))
    thread.join(1.0)
    controller.close()
    assert failures == []
    assert completed and completed[0].successful
    assert completed[0].maximum_error_pulses == residual
    assert completed[0].maximum_error_mm == pytest.approx(residual / 39_500.0)


def test_stopped_outside_target_is_reported_without_recovery_lock() -> None:
    transport = _FakeSocket()
    controller = DirectMotionPlatformController(_config(), socket_factory=lambda: transport)
    transport.feedback(_feedback_frame(sequence=1, controller_time=1))
    controller.connect()
    pose = PlatformPose(1, 0, 0, 0, 0, 100)
    target = solve_actuator_targets(controller.config, pose).pulses
    stopped = tuple(value + 5000 for value in target)
    completed: list[object] = []
    failures: list[Exception] = []
    thread = threading.Thread(
        target=lambda: _capture_result(
            completed,
            failures,
            lambda: controller.move_to(pose, duration_ms=100),
        )
    )
    thread.start()
    deadline = time.monotonic() + 1.0
    while not transport.sent and time.monotonic() < deadline:
        time.sleep(0.005)
    transport.feedback(_feedback_frame(stopped, sequence=2, controller_time=2))
    time.sleep(0.03)
    transport.feedback(_feedback_frame(stopped, sequence=3, controller_time=3))
    thread.join(1.0)
    assert failures == []
    assert completed and not completed[0].successful
    assert completed[0].motion_stopped
    assert not completed[0].target_guard_passed
    assert completed[0].outcome == MotionActionOutcome.SETTLED_OUTSIDE_TARGET
    assert controller.snapshot.state == MotionPlatformState.READY
    controller.close()


def test_motion_that_never_settles_times_out_into_recovery() -> None:
    transport = _FakeSocket()
    controller = DirectMotionPlatformController(_config(), socket_factory=lambda: transport)
    transport.feedback(_feedback_frame(sequence=1, controller_time=1))
    controller.connect()
    pose = PlatformPose(1, 0, 0, 0, 0, 100)
    target = solve_actuator_targets(controller.config, pose).pulses
    failures: list[Exception] = []
    thread = threading.Thread(
        target=lambda: _capture_failure(
            failures,
            lambda: controller.move_to(pose, duration_ms=100, timeout_ms=90),
        )
    )
    thread.start()
    deadline = time.monotonic() + 1.0
    while not transport.sent and time.monotonic() < deadline:
        time.sleep(0.005)
    for sequence in range(2, 10):
        offset = sequence * 1000
        transport.feedback(
            _feedback_frame(
                tuple(value + offset for value in target),
                sequence=sequence,
                controller_time=sequence,
            )
        )
        time.sleep(0.012)
    thread.join(1.0)
    assert failures and "did not stop" in str(failures[0])
    assert controller.snapshot.state == MotionPlatformState.RECOVERY_REQUIRED
    controller.close()


def test_trajectory_preflight_rejects_entire_invalid_path_before_send() -> None:
    transport = _FakeSocket()
    controller = DirectMotionPlatformController(_config(), socket_factory=lambda: transport)
    trajectory = SineTrajectory(
        sample_period_ms=20,
        ramp_in_s=0.1,
        steady_duration_s=0.1,
        ramp_out_s=0.1,
        roll=SineAxis(31.0, 0.1),
        pitch=SineAxis(0, 0, enabled=False),
        yaw=SineAxis(0, 0, enabled=False),
    )
    with pytest.raises(MotionPlatformError, match="amplitude"):
        MotionTrajectoryRunner(controller).preflight(trajectory)
    assert transport.sent == []


def test_motion_evidence_manifest_hashes_every_record(tmp_path) -> None:
    recorder = MotionEvidenceRecorder(tmp_path / "motion-session")
    recorder.write_config({"model_id": "lingjing-a6-200mm"})
    recorder.record("commands", "sent", {"raw": b"\xAA\x55"})
    manifest_path = recorder.close(summary={"result": "PASS"})
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "satellite.motion-platform-evidence"
    for item in manifest["files"]:
        path = manifest_path.parent / item["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]


def _capture_failure(failures: list[Exception], action) -> None:
    try:
        action()
    except Exception as exc:
        failures.append(exc)


def _capture_result(completed: list, failures: list[Exception], action) -> None:
    try:
        completed.append(action())
    except Exception as exc:
        failures.append(exc)
