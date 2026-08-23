"""Fixture control worker stop and queue semantics."""

from __future__ import annotations

import time

from satellite_debug_tool.core.production import (
    FixtureAxisLimits,
    FixtureControlWorker,
    PlatformPose,
    WorkstationFixtureProfile,
)


def _profile() -> WorkstationFixtureProfile:
    limits = FixtureAxisLimits(15.0, 2.0, 0.5, 30.0, 100.0)
    return WorkstationFixtureProfile(
        profile_id="fixture-control",
        revision=1,
        host="192.168.1.50",
        port=9800,
        center_pose=PlatformPose(0, 0, 0, 0, 0, 100),
        reset_pose=PlatformPose(0, 0, 0, 0, 0, 0),
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


def test_stop_finishes_current_interval_drops_queue_and_does_not_center(
    qapplication_session,
) -> None:
    qapp = qapplication_session
    sent: list[bytes] = []
    results = []
    worker = FixtureControlWorker(
        _profile(),
        sender=lambda data, _endpoint: not sent.append(data),
    )
    worker.confirm_preflight()
    worker.request_finished.connect(lambda _request_id, result: results.append(result))
    worker.start()
    worker.submit_axis_move("roll", 4.0, total_duration_ms=400)
    worker.submit_center(duration_ms=200)
    assert _wait_until(qapp, lambda: len(sent) == 1)

    stop_started = time.monotonic()
    worker.stop_sequence()
    assert _wait_until(qapp, lambda: len(results) == 1)

    assert time.monotonic() - stop_started >= 0.15
    assert len(sent) == 1
    assert sent[0].startswith(b"@A6T:2,0,0,0,0,100,200#")
    assert results[0].stopped
    worker.shutdown()
    assert worker.wait(2000)


def test_single_step_request_stays_busy_until_a6t_completion(
    qapplication_session,
) -> None:
    qapp = qapplication_session
    finished_at = []
    worker = FixtureControlWorker(
        _profile(),
        sender=lambda _data, _endpoint: True,
    )
    worker.confirm_preflight()
    worker.request_finished.connect(
        lambda _request_id, _result: finished_at.append(time.monotonic())
    )
    worker.start()

    started_at = time.monotonic()
    worker.submit_axis_move("roll", 1.0, total_duration_ms=200)

    assert _wait_until(qapp, lambda: bool(finished_at))
    assert finished_at[0] - started_at >= 0.15
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
    sent_results = []
    evidence_failures = []
    worker = FixtureControlWorker(
        _profile(),
        sender=lambda _data, _endpoint: True,
        recorder=_FailingRecorder(),
    )
    worker.confirm_preflight()
    worker.send_result.connect(lambda result, action, sequence: sent_results.append(
        (result.sent, action, sequence)
    ))
    worker.evidence_failed.connect(evidence_failures.append)
    worker.start()

    worker.submit_axis_move("roll", 1.0, total_duration_ms=200)

    assert _wait_until(qapp, lambda: bool(evidence_failures))
    assert sent_results == [(True, "absolute_axis", 1)]
    assert evidence_failures == ["evidence disk failed"]
    assert not worker.evidence_ready
    assert worker.wait(2000)
