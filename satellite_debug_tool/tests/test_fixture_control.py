"""夹具线程对直接运动平台组件的适配测试。"""

from __future__ import annotations

import time

from satellite_debug_tool.core.production import (
    FixtureAxisLimits,
    FixtureControlWorker,
    PlatformPose,
    WorkstationFixtureProfile,
)
from satellite_debug_tool.tests.motion_fakes import ConfirmedMotionController


def _profile() -> WorkstationFixtureProfile:
    limits = FixtureAxisLimits(15.0, 0.5, 30.0, 100.0)
    return WorkstationFixtureProfile(
        profile_id="fixture-control",
        revision=1,
        model_id="lingjing-a6-200mm",
        local_ipv4="192.168.15.101",
        feedback_port=8410,
        controller_ipv4="192.168.15.201",
        command_port=7408,
        center_pose=PlatformPose(0, 0, 0, 0, 0, 100),
        roll_limits=limits,
        pitch_limits=limits,
        yaw_limits=limits,
        calibration_id="axis-map-1",
    )


def _wait_until(qapp, predicate, *, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return bool(predicate())


def _factory(commands):
    return lambda profile: ConfirmedMotionController(profile.to_motion_config(), commands)


def test_axis_move_records_direct_frame_and_feedback_confirmed_result(
    qapplication_session,
) -> None:
    qapp = qapplication_session
    commands = []
    results = []
    worker = FixtureControlWorker(_profile(), controller_factory=_factory(commands))
    worker.confirm_preflight()
    worker.request_finished.connect(lambda _request_id, result: results.append(result))
    worker.start()
    assert _wait_until(qapp, lambda: worker.controller.snapshot.ready)

    worker.submit_axis_move("roll", 4.0, total_duration_ms=400)
    assert _wait_until(qapp, lambda: len(results) == 1)

    assert len(commands) == 1
    assert commands[0][0].startswith(b"\xAA\x55\x00\x1b\xC0")
    assert commands[0][1] == ("192.168.15.201", 7408)
    assert results[0].successful
    assert results[0].motion_stopped
    assert results[0].target_guard_passed
    assert results[0].maximum_error_pulses == 0
    worker.shutdown()
    assert worker.wait(2000)


def test_stop_drops_queued_requests_without_claiming_emergency_stop(
    qapplication_session,
) -> None:
    qapp = qapplication_session
    commands = []
    worker = FixtureControlWorker(_profile(), controller_factory=_factory(commands))
    worker.confirm_preflight()
    worker.start()
    assert _wait_until(qapp, lambda: worker.controller.snapshot.ready)
    worker.stop_sequence()
    assert commands == []
    worker.shutdown()
    assert worker.wait(2000)


def test_command_dispatch_is_reported_before_evidence_failure(
    qapplication_session,
) -> None:
    class _FailingRecorder:
        def record_command(self, *_args, **_kwargs) -> None:
            raise RuntimeError("evidence disk failed")

        def record_event(self, *_args, **_kwargs) -> None:
            raise RuntimeError("evidence disk failed")

    qapp = qapplication_session
    commands = []
    sent_results = []
    evidence_failures = []
    worker = FixtureControlWorker(
        _profile(),
        controller_factory=_factory(commands),
        recorder=_FailingRecorder(),
    )
    worker.confirm_preflight()
    worker.send_result.connect(
        lambda result, action, sequence: sent_results.append((result.action, action, sequence))
    )
    worker.evidence_failed.connect(evidence_failures.append)
    worker.start()
    assert _wait_until(qapp, lambda: worker.controller.snapshot.ready)

    worker.submit_axis_move("roll", 1.0, total_duration_ms=200)

    assert _wait_until(qapp, lambda: bool(evidence_failures))
    assert sent_results == [("absolute_axis", "absolute_axis", 1)]
    assert evidence_failures == ["evidence disk failed"]
    assert not worker.evidence_ready
    assert worker.wait(2000)
