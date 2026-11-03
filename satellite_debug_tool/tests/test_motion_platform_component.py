"""直接运动平台组件的协议、安全边界和反馈闭环回归。"""

from __future__ import annotations

from dataclasses import replace
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
    MotionCommandEvent,
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


class _DelayedFeedbackSocket(_FakeSocket):
    """延迟首条目标帧交接；已经排队的两帧在同一接收刻度交付。"""

    def __init__(self) -> None:
        super().__init__()
        self._enqueued = 0
        self._received = 0
        self._release_feedback = threading.Event()
        self._delivery_timer: threading.Timer | None = None
        self._batch_tick_ns: int | None = None
        self._processing_batch = False
        self._receiver_ident: int | None = None

    def feedback(self, frame: bytes) -> None:
        self._enqueued += 1
        super().feedback(frame)
        if self._enqueued == 2:
            self._delivery_timer = threading.Timer(0.08, self._release_target_feedback)
            self._delivery_timer.start()

    def _release_target_feedback(self) -> None:
        if self._enqueued >= 3:
            self._batch_tick_ns = time.monotonic_ns()
        self._release_feedback.set()

    def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
        self._receiver_ident = threading.get_ident()
        self._processing_batch = False
        item = super().recvfrom(size)
        self._received += 1
        if self._received == 2:
            assert self._release_feedback.wait(1.0)
        self._processing_batch = self._received in (2, 3)
        return item

    def clock_ns(self) -> int:
        if (
            self._processing_batch
            and threading.get_ident() == self._receiver_ident
            and self._batch_tick_ns is not None
        ):
            return self._batch_tick_ns
        return time.monotonic_ns()

    def close(self) -> None:
        if self._delivery_timer is not None:
            self._delivery_timer.cancel()
        self._release_feedback.set()
        super().close()


def _feed_stable_feedback(
    controller: DirectMotionPlatformController,
    transport: _FakeSocket,
    pulses: tuple[int, ...],
    *,
    command: int = 0xC0,
    result: int = 0,
) -> None:
    """首帧已消费且真实稳定窗口已到后，再交付第二帧反馈。"""
    first_received = threading.Event()

    def observe_feedback(snapshot) -> None:
        if (
            snapshot.actual_pulses == pulses
            and snapshot.received_command == command
            and snapshot.received_command_result == result
        ):
            first_received.set()

    controller.add_snapshot_callback(observe_feedback)
    transport.feedback(
        _feedback_frame(pulses, sequence=2, controller_time=2, command=command, result=result)
    )
    assert first_received.wait(1.0)
    first_received_ns = controller.snapshot.feedback_monotonic_ns
    window_ns = controller.config.stationary_window_ms * 1_000_000
    deadline = time.monotonic() + 1.0
    while time.monotonic_ns() - first_received_ns < window_ns and time.monotonic() < deadline:
        time.sleep(0.005)
    assert time.monotonic_ns() - first_received_ns >= window_ns
    transport.feedback(
        _feedback_frame(pulses, sequence=3, controller_time=3, command=command, result=result)
    )


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


@pytest.mark.parametrize("delayed_feedback", [False, True])
def test_move_completes_only_after_stable_controller_feedback(delayed_feedback: bool) -> None:
    transport = _DelayedFeedbackSocket() if delayed_feedback else _FakeSocket()
    clock_ns = transport.clock_ns if delayed_feedback else time.monotonic_ns
    controller = DirectMotionPlatformController(
        _config(), socket_factory=lambda: transport, clock_ns=clock_ns,
    )
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
    _feed_stable_feedback(controller, transport, target)
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
    _feed_stable_feedback(controller, transport, (0, 0, 0, 0, 0, 0), command=3, result=1)
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
    _feed_stable_feedback(controller, transport, stopped)
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
    _feed_stable_feedback(controller, transport, stopped)
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


def test_combined_pose_preflight_matches_field_14_and_15_degree_boundary() -> None:
    from satellite_debug_tool.core.production import fixture_model_presets
    from satellite_debug_tool.tests.motion_fakes import ConfirmedMotionController

    controller = ConfirmedMotionController(
        fixture_model_presets()[0].create_profile().to_motion_config()
    )

    def trajectory(amplitude: float) -> SineTrajectory:
        return SineTrajectory(
            sample_period_ms=50,
            ramp_in_s=5.0,
            steady_duration_s=20.0,
            ramp_out_s=5.0,
            roll=SineAxis(amplitude, 0.25),
            pitch=SineAxis(amplitude, 0.25),
            yaw=SineAxis(amplitude, 0.1),
        )

    accepted = MotionTrajectoryRunner(controller).preflight(trajectory(14.0))
    assert accepted.limiting_axis == 4
    assert accepted.minimum_extension_mm == pytest.approx(0.51)

    with pytest.raises(
        MotionPlatformError,
        match=r"axis 4 extension -0\.21 mm.*roll=-14\.113.*pitch=-14\.113.*yaw=-13\.491",
    ):
        MotionTrajectoryRunner(controller).preflight(trajectory(15.0))


def test_trajectory_minor_timing_gap_is_recorded_without_stopping_motion() -> None:
    from satellite_debug_tool.core.production import fixture_model_presets
    from satellite_debug_tool.tests.motion_fakes import ConfirmedMotionController

    now_ns = 0
    sleep_count = 0

    def clock_ns() -> int:
        return now_ns

    def sleep(seconds: float) -> None:
        nonlocal now_ns, sleep_count
        sleep_count += 1
        now_ns += round(seconds * 1_000_000_000)
        if sleep_count == 1:
            now_ns += 30_000_000

    controller = ConfirmedMotionController(
        fixture_model_presets()[0].create_profile().to_motion_config(),
        clock_ns=clock_ns,
    )
    trajectory = SineTrajectory(
        sample_period_ms=50,
        ramp_in_s=0.0,
        steady_duration_s=0.15,
        ramp_out_s=0.0,
        roll=SineAxis(1.0, 0.1),
        pitch=SineAxis(0.0, 0.0, enabled=False),
        yaw=SineAxis(0.0, 0.0, enabled=False),
    )
    result = MotionTrajectoryRunner(
        controller,
        clock_ns=clock_ns,
        sleep=sleep,
    ).run(trajectory)

    assert not result.timing_fault
    assert result.control_gap_count == 1
    assert result.maximum_send_interval_ms == pytest.approx(80.0)
    assert result.sent_points == 3
    assert result.arrival is not None


def test_trajectory_missing_a_complete_slot_resynchronizes_without_skipping() -> None:
    from satellite_debug_tool.core.production import fixture_model_presets
    from satellite_debug_tool.tests.motion_fakes import ConfirmedMotionController

    now_ns = 0
    sleep_count = 0

    def clock_ns() -> int:
        return now_ns

    def sleep(seconds: float) -> None:
        nonlocal now_ns, sleep_count
        sleep_count += 1
        now_ns += round(seconds * 1_000_000_000)
        if sleep_count == 1:
            now_ns += 55_000_000

    controller = ConfirmedMotionController(
        fixture_model_presets()[0].create_profile().to_motion_config(),
        clock_ns=clock_ns,
    )
    trajectory = SineTrajectory(
        sample_period_ms=50,
        ramp_in_s=0.0,
        steady_duration_s=0.15,
        ramp_out_s=0.0,
        roll=SineAxis(1.0, 0.1),
        pitch=SineAxis(0.0, 0.0, enabled=False),
        yaw=SineAxis(0.0, 0.0, enabled=False),
    )

    result = MotionTrajectoryRunner(
        controller,
        clock_ns=clock_ns,
        sleep=sleep,
    ).run(trajectory)

    assert result.timing_fault
    assert result.skipped_points == 0
    assert result.control_gap_count == 1
    assert result.maximum_send_interval_ms == pytest.approx(105.0)
    assert result.sent_points == 3
    assert result.arrival is not None


def test_isolated_timeline_reports_late_slot_and_keeps_sending() -> None:
    from satellite_debug_tool.core.motion_platform.timeline_process import (
        run_timeline_process,
    )

    class DelayedEmptyQueue:
        def __init__(self) -> None:
            self.wait_count = 0

        def get(self, *, timeout: float):
            self.wait_count += 1
            time.sleep(timeout + (0.025 if self.wait_count == 2 else 0.0))
            raise queue.Empty

        def get_nowait(self):
            raise queue.Empty

    class RecordingTransport:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.closed = False

        def sendto(self, frame: bytes, _endpoint) -> int:
            self.sent.append(frame)
            return len(frame)

        def close(self) -> None:
            self.closed = True

    transport = RecordingTransport()
    events: queue.Queue[tuple] = queue.Queue()
    frames = (b"one", b"two", b"three")

    run_timeline_process(
        transport,
        ("127.0.0.1", 7408),
        frames,
        (),
        (),
        len(frames),
        10_000_000,
        object(),
        object(),
        12,
        DelayedEmptyQueue(),
        events,
    )

    recorded = []
    while not events.empty():
        recorded.append(events.get_nowait())
    assert transport.sent == list(frames)
    assert transport.closed
    assert [event[0] for event in recorded].count("timing_fault") == 1
    assert recorded[-1][0] == "finished"


def test_finite_trajectory_execution_uses_precompiled_frames(monkeypatch) -> None:
    import satellite_debug_tool.core.motion_platform.trajectory as trajectory_module
    from satellite_debug_tool.core.production import fixture_model_presets
    from satellite_debug_tool.tests.motion_fakes import ConfirmedMotionController

    now_ns = 0

    def clock_ns() -> int:
        return now_ns

    def sleep(seconds: float) -> None:
        nonlocal now_ns
        now_ns += round(seconds * 1_000_000_000)

    controller = ConfirmedMotionController(
        fixture_model_presets()[0].create_profile().to_motion_config(),
        clock_ns=clock_ns,
    )
    trajectory = SineTrajectory(
        sample_period_ms=50,
        ramp_in_s=0.0,
        steady_duration_s=0.15,
        ramp_out_s=0.0,
        roll=SineAxis(1.0, 0.1),
        pitch=SineAxis(0.0, 0.0, enabled=False),
        yaw=SineAxis(0.0, 0.0, enabled=False),
    )
    runner = MotionTrajectoryRunner(controller, clock_ns=clock_ns, sleep=sleep)
    checked = runner.preflight(trajectory)

    assert len(checked.compiled.finite_points) == trajectory.sample_count == 3
    assert all(point.frame.startswith(b"\xAA\x55\x00\x1b\xC0") for point in checked.compiled.finite_points)

    monkeypatch.setattr(
        trajectory_module,
        "solve_actuator_targets",
        lambda *_args, **_kwargs: pytest.fail("runtime must not solve inverse kinematics"),
    )
    result = runner.run(trajectory, preflight=checked)

    assert result.sent_points == 3
    assert result.arrival is not None and result.arrival.successful

    changed = replace(trajectory, roll=SineAxis(1.1, 0.1))
    with pytest.raises(MotionPlatformError, match="does not match"):
        runner.run(changed, preflight=checked)


def test_soft_center_fades_from_current_phase_and_confirms_center() -> None:
    from satellite_debug_tool.core.production import fixture_model_presets
    from satellite_debug_tool.tests.motion_fakes import ConfirmedMotionController

    now_ns = 0
    soft_center = threading.Event()
    commands: list = []
    events = []
    phases = []

    def clock_ns() -> int:
        return now_ns

    def sleep(seconds: float) -> None:
        nonlocal now_ns
        now_ns += round(seconds * 1_000_000_000)
        if now_ns >= 200_000_000:
            soft_center.set()

    controller = ConfirmedMotionController(
        fixture_model_presets()[0].create_profile().to_motion_config(),
        commands,
        clock_ns=clock_ns,
    )
    controller.add_command_callback(events.append)
    trajectory = SineTrajectory(
        sample_period_ms=100,
        ramp_in_s=0.0,
        steady_duration_s=2.0,
        ramp_out_s=0.2,
        roll=SineAxis(2.0, 0.2),
        pitch=SineAxis(0.0, 0.0, enabled=False),
        yaw=SineAxis(0.0, 0.0, enabled=False),
    )

    result = MotionTrajectoryRunner(
        controller,
        clock_ns=clock_ns,
        sleep=sleep,
    ).run(
        trajectory,
        soft_center_event=soft_center,
        soft_center_phase_callback=phases.append,
    )

    assert result.soft_center_requested
    assert result.fade_points == 2
    assert result.center_confirmed
    assert result.arrival is not None and result.arrival.successful
    assert result.stopped
    assert len(commands) == result.sent_points + 1
    trajectory_events = [event for event in events if event.action == "trajectory_point"]
    before_fade = trajectory_events[-result.fade_points - 1].target_pose
    first_fade = trajectory_events[-result.fade_points].target_pose
    final_fade = trajectory_events[-1].target_pose
    assert abs(first_fade.roll_deg - before_fade.roll_deg) < 0.3
    assert final_fade.roll_deg == pytest.approx(0.0, abs=1e-12)
    assert [phase.value for phase in phases] == [
        "fading",
        "centering",
        "center_confirmed",
    ]


def test_soft_center_failure_keeps_an_explicit_failed_phase() -> None:
    from satellite_debug_tool.core.production import fixture_model_presets
    from satellite_debug_tool.tests.motion_fakes import ConfirmedMotionController

    class FailingCenterController(ConfirmedMotionController):
        def recover_to_center(self, *, duration_ms: int = 3000):
            raise MotionPlatformError("center feedback timeout")

    now_ns = 0
    soft_center = threading.Event()
    phases = []

    def clock_ns() -> int:
        return now_ns

    def sleep(seconds: float) -> None:
        nonlocal now_ns
        now_ns += round(seconds * 1_000_000_000)
        if now_ns >= 100_000_000:
            soft_center.set()

    controller = FailingCenterController(
        fixture_model_presets()[0].create_profile().to_motion_config(),
        clock_ns=clock_ns,
    )
    trajectory = SineTrajectory(
        sample_period_ms=100,
        ramp_in_s=0.0,
        steady_duration_s=1.0,
        ramp_out_s=0.1,
        roll=SineAxis(1.0, 0.1),
        pitch=SineAxis(0.0, 0.0, enabled=False),
        yaw=SineAxis(0.0, 0.0, enabled=False),
    )

    with pytest.raises(MotionPlatformError, match="center feedback timeout"):
        MotionTrajectoryRunner(
            controller,
            clock_ns=clock_ns,
            sleep=sleep,
        ).run(
            trajectory,
            soft_center_event=soft_center,
            soft_center_phase_callback=phases.append,
        )

    assert [phase.value for phase in phases] == [
        "fading",
        "centering",
        "center_failed",
    ]


def test_real_socket_trajectory_uses_isolated_timeline_process() -> None:
    from satellite_debug_tool.core.production import fixture_model_presets
    from satellite_debug_tool.tests.motion_fakes import ConfirmedMotionController

    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(1.0)
    endpoint = receiver.getsockname()
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    config = replace(
        fixture_model_presets()[0].create_profile().to_motion_config(),
        local_ipv4="127.0.0.1",
        controller_ipv4="127.0.0.1",
        command_port=endpoint[1],
    )

    class ProcessController(ConfirmedMotionController):
        def trajectory_process_socket(self):
            return sender

        def accept_external_trajectory_send(
            self,
            pose,
            *,
            target_pulses,
            frame,
            duration_ms,
            sent_monotonic_ns,
        ):
            event = MotionCommandEvent(
                action="trajectory_point",
                raw_command=frame,
                target_pose=pose,
                target_pulses=tuple(target_pulses),
                duration_ms=duration_ms,
                monotonic_ns=sent_monotonic_ns,
                generation=1,
            )
            for callback in self._command_callbacks:
                callback(event)
            # 父进程即使处理UI/证据较慢，也不能拖慢子进程的UDP时间线。
            time.sleep(0.08)
            return event

    controller = ProcessController(config)
    trajectory = SineTrajectory(
        sample_period_ms=50,
        ramp_in_s=0.0,
        steady_duration_s=0.5,
        ramp_out_s=0.0,
        roll=SineAxis(1.0, 0.1),
        pitch=SineAxis(0.0, 0.0, enabled=False),
        yaw=SineAxis(0.0, 0.0, enabled=False),
    )
    received_at = []

    def receive() -> None:
        while len(received_at) < trajectory.sample_count:
            receiver.recvfrom(4096)
            received_at.append(time.monotonic_ns())

    receive_thread = threading.Thread(target=receive)
    receive_thread.start()
    try:
        run_results = []
        run_errors = []

        def execute() -> None:
            try:
                run_results.append(MotionTrajectoryRunner(controller).run(trajectory))
            except Exception as exc:
                run_errors.append(exc)

        control_thread = threading.Thread(target=execute)
        control_thread.start()
        control_thread.join(timeout=3.0)
        assert not control_thread.is_alive()
        assert run_errors == []
        result = run_results[0]
        receive_thread.join(timeout=2.0)
        receiver.settimeout(0.05)
        soft_center = threading.Event()
        receive_done = threading.Event()
        soft_received_at = []
        phases = []
        soft_trajectory = replace(
            trajectory,
            steady_duration_s=2.0,
            ramp_out_s=0.2,
        )

        def receive_soft() -> None:
            while not receive_done.is_set():
                try:
                    receiver.recvfrom(4096)
                except socket.timeout:
                    continue
                soft_received_at.append(time.monotonic_ns())

        soft_receiver = threading.Thread(target=receive_soft)
        soft_receiver.start()
        timer = threading.Timer(0.2, soft_center.set)
        timer.start()
        soft_results = []
        soft_errors = []

        def execute_soft() -> None:
            try:
                soft_results.append(
                    MotionTrajectoryRunner(controller).run(
                        soft_trajectory,
                        soft_center_event=soft_center,
                        soft_center_phase_callback=phases.append,
                    )
                )
            except Exception as exc:
                soft_errors.append(exc)

        soft_control_thread = threading.Thread(target=execute_soft)
        soft_control_thread.start()
        soft_control_thread.join(timeout=3.0)
        assert not soft_control_thread.is_alive()
        assert soft_errors == []
        soft_result = soft_results[0]
        timer.cancel()
        receive_done.set()
        soft_receiver.join(timeout=1.0)
    finally:
        receiver.close()
        sender.close()

    assert result.sent_points == trajectory.sample_count == 10
    assert not result.timing_fault
    assert len(received_at) == 10
    intervals_ms = [
        (current - previous) / 1_000_000.0
        for previous, current in zip(received_at, received_at[1:])
    ]
    assert max(intervals_ms) < 100.0
    assert soft_result.center_confirmed
    assert soft_result.fade_points == 4
    assert len(soft_received_at) == soft_result.sent_points
    assert [phase.value for phase in phases] == [
        "fading",
        "centering",
        "center_confirmed",
    ]


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
