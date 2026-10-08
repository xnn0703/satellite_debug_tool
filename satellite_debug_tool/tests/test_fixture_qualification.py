"""Automatic platform qualification state machine and field archive tests."""

from __future__ import annotations

import time
import zipfile

from satellite_debug_tool.core.production import (
    FixtureQualificationConfig,
    FixtureQualificationController,
    PlatformPose,
    TimedAttitude,
    create_fixture_session_zip,
)


def test_qualification_runs_all_axes_and_proposes_direction_mapping(
    qapplication_session,
) -> None:
    qapp = qapplication_session
    controller = FixtureQualificationController(
        profile_id="fixture-a",
        profile_sha256="a" * 64,
        center_pose=PlatformPose(0, 0, 0, 0, 0, 100),
        config=FixtureQualificationConfig(
            move_duration_ms=1,
            settle_duration_ms=1,
            capture_duration_ms=1,
            minimum_samples_per_stage=20,
        ),
    )
    results = []
    failures = []
    commands = []
    sample_ns = 1_000_000_000
    previous = PlatformPose(0, 0, 0)

    def add_sample(pose: PlatformPose) -> None:
        nonlocal sample_ns
        sample_ns += 5_000_000
        controller.add_sample(
            TimedAttitude(
                sample_ns,
                pose.roll_deg,
                pose.pitch_deg,
                150.0 + pose.yaw_deg,
            )
        )

    def on_move(stage: str, pose: PlatformPose, duration_ms: int) -> None:
        nonlocal previous
        commands.append((stage, pose, duration_ms))
        controller.record_command_sent(sample_ns)
        for _ in range(5):
            add_sample(previous)
        for index in range(1, 11):
            fraction = index / 10.0
            add_sample(
                PlatformPose(
                    previous.roll_deg + (pose.roll_deg - previous.roll_deg) * fraction,
                    previous.pitch_deg + (pose.pitch_deg - previous.pitch_deg) * fraction,
                    previous.yaw_deg + (pose.yaw_deg - previous.yaw_deg) * fraction,
                )
            )
        previous = pose
        controller.motion_completed()

    def on_stage(_stage: str, state: str, _index: int, _total: int) -> None:
        if state == "capturing":
            for _ in range(25):
                add_sample(previous)

    controller.move_requested.connect(on_move)
    controller.stage_changed.connect(on_stage)
    controller.completed.connect(results.append)
    controller.failed.connect(failures.append)
    controller.start()

    deadline = time.monotonic() + 2.0
    while not results and not failures and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.001)

    assert failures == []
    assert len(results) == 1
    assert len(commands) == 8
    result = results[0]
    assert result.verdict == "INCONCLUSIVE"
    assert result.measurement_basis == "SELF_DERIVED_ENGINEERING_MAPPING"
    assert result.proposed_calibration.sensor_axis_for_logical == (
        "roll",
        "pitch",
        "yaw",
    )
    assert result.proposed_calibration.logical_signs == (1, 1, 1)
    assert all(count == 25 for count in result.stage_sample_counts.values())
    assert result.stage_metrics["roll_plus"]["response"]["valid"] is True


def test_fixture_session_zip_contains_complete_session_tree(tmp_path) -> None:
    session = tmp_path / "session-a"
    session.mkdir()
    (session / "manifest.json").write_text('{"status":"complete"}\n')
    (session / "events.jsonl").write_text('{"event":"done"}\n')

    archive = create_fixture_session_zip(session)

    assert archive == tmp_path / "session-a-diagnostics.zip"
    with zipfile.ZipFile(archive) as payload:
        assert sorted(payload.namelist()) == [
            "session-a/events.jsonl",
            "session-a/manifest.json",
        ]
