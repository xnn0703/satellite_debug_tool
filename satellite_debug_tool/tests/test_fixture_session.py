"""Fixture engineering session persistence and recovery tests."""

from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

from satellite_debug_tool.core.production.fixture_profile import (
    FixtureAxisLimits,
    WorkstationFixtureProfile,
)
from satellite_debug_tool.core.production.fixture_session import (
    FixtureSessionConclusion,
    FixtureSessionError,
    FixtureSessionRecorder,
)
from satellite_debug_tool.core.production.fixture_analysis import AttitudeComparison
from satellite_debug_tool.core.production.fixture_analysis import (
    FixtureCalibration,
    TimedAttitude,
)
from satellite_debug_tool.core.motion_platform import (
    MotionCommandEvent,
    PlatformPose,
)
from satellite_debug_tool.core.production.ms6222_protocol import Ms6222StreamParser
from satellite_debug_tool.tests.test_ms6222_protocol import _ins_frame


def _profile() -> WorkstationFixtureProfile:
    limits = FixtureAxisLimits(15.0, 0.5, 30.0, 100.0)
    return WorkstationFixtureProfile(
        profile_id="fixture-a",
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
        calibration_id="engineering-axis-map-1",
    )


def _command() -> MotionCommandEvent:
    pose = PlatformPose(1, 2, 3)
    return MotionCommandEvent(
        action="absolute",
        raw_command=bytes.fromhex("aa55001bc000640000000000000000000000000000000000000000000000fbfb"),
        target_pose=pose,
        target_pulses=(0, 0, 0, 0, 0, 0),
        duration_ms=100,
        monotonic_ns=123,
        generation=1,
    )


def test_complete_session_contains_snapshot_evidence_and_hashes(tmp_path: Path) -> None:
    recorder = FixtureSessionRecorder(
        _profile(), root=tmp_path, session_id="session-complete", operator="operator-a"
    )
    recorder.start()
    recorder.record_command(_command(), sequence=1)
    recorder.record("commands", "sent", _command())
    recorder.record("feedback", "received", {"raw": b"\xAA\x55", "generation": 1})
    recorder.record("events", "state", {"state": "ready"})
    recorder.record("actions", "completed", {"maximum_error_pulses": 0})
    envelope = next(event for event in Ms6222StreamParser().feed(_ins_frame()) if event.valid)
    recorder.record_ms_frame(envelope)
    recorder.record_event("note", "fixture moved")
    comparison = AttitudeComparison(
        monotonic_ns=456,
        target_roll_deg=1.0,
        target_pitch_deg=2.0,
        target_yaw_deg=3.0,
        measured_roll_deg=1.1,
        measured_pitch_deg=2.1,
        measured_yaw_deg=3.1,
        error_roll_deg=0.1,
        error_pitch_deg=0.1,
        error_yaw_deg=0.1,
        error_angle_deg=0.173,
    )
    recorder.record_comparisons((comparison, comparison))
    result = recorder.complete(
        conclusion=FixtureSessionConclusion.PASS,
        notes="engineering check",
        summary={"metric": 1.0},
    )
    manifest = json.loads((result.session_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["conclusion"] == "PASS"
    assert manifest["result_class"] == "ENGINEERING_ONLY"
    assert "summary.json" in manifest["file_sha256"]
    assert (result.session_dir / "ms6222_parsed.csv").read_text(encoding="utf-8").count("\n") == 2
    assert (result.session_dir / "platform_commands.jsonl").is_file()
    assert (result.session_dir / "platform_controller_commands.jsonl").is_file()
    assert (result.session_dir / "platform_feedback.jsonl").is_file()
    assert (result.session_dir / "platform_state_events.jsonl").is_file()
    assert (result.session_dir / "platform_actions.jsonl").is_file()
    assert (result.session_dir / "motion_config.json").is_file()
    feedback = json.loads(
        (result.session_dir / "platform_feedback.jsonl").read_text(encoding="utf-8")
    )
    assert feedback["host_monotonic_ns"] > 0
    assert feedback["host_wall_time_ns"] > 0
    summary = json.loads(
        (result.session_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["counts"]["comparisons"] == 2
    assert (
        result.session_dir / "attitude_comparison.csv"
    ).read_text(encoding="utf-8").count("\n") == 3
    command = json.loads(
        (result.session_dir / "platform_commands.jsonl").read_text(encoding="utf-8")
    )
    assert command["planned_completion_monotonic_ns"] is None
    assert command["raw_command_hex"].startswith("aa55")


def test_abort_keeps_incomplete_manifest_and_raw_frames(tmp_path: Path) -> None:
    recorder = FixtureSessionRecorder(
        _profile(), root=tmp_path, session_id="session-aborted"
    )
    recorder.start()
    envelope = next(event for event in Ms6222StreamParser().feed(_ins_frame()) if event.valid)
    recorder.record_ms_frame(envelope)
    result = recorder.abort(reason="serial disconnected")
    manifest = json.loads((result.session_dir / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((result.session_dir / "summary.json").read_text(encoding="utf-8"))
    assert result.status == "incomplete"
    assert manifest["status"] == "incomplete"
    assert manifest["conclusion"] == "INCONCLUSIVE"
    assert summary["metrics"]["abort_reason"] == "serial disconnected"
    assert (result.session_dir / "ms6222_raw.jsonl").stat().st_size > 0


def _calibration(profile: WorkstationFixtureProfile) -> FixtureCalibration:
    return FixtureCalibration(
        calibration_id="MS6222-CAL-STREAMING",
        profile_id=profile.profile_id,
        profile_sha256=profile.sha256,
        created_utc="2026-08-24T00:00:00+00:00",
        sensor_axis_for_logical=("roll", "pitch", "yaw"),
        logical_signs=(1, 1, 1),
        zero_offsets_deg=(0.0, 0.0, 0.0),
        response_matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        cross_coupling_ratio=(0.0, 0.0, 0.0),
        static_noise_std_deg=(0.0, 0.0, 0.0),
        sample_coverage_ratio=1.0,
        confirmed=True,
    )


def test_fixture_comparison_generation_persists_all_and_bounds_metric_sample(
    tmp_path: Path,
) -> None:
    profile = _profile()
    recorder = FixtureSessionRecorder(
        profile,
        root=tmp_path,
        session_id="session-streaming-comparison",
    )
    recorder.start()
    recorder.record_target(TimedAttitude(0, 0.0, 0.0, 0.0))
    recorder.record_target(TimedAttitude(2_000_000_000, 10.0, 0.0, 0.0))
    for index in range(1000):
        timestamp = (index + 1) * 1_000_000
        recorder.record_measurement(
            TimedAttitude(timestamp, timestamp / 2_000_000_000 * 10.0, 0.0, 0.0)
        )

    sample = recorder.generate_comparison_sample(
        calibration=_calibration(profile),
        max_target_gap_s=3.0,
        max_samples=100,
    )
    result = recorder.complete(
        conclusion=FixtureSessionConclusion.PASS,
        notes="bounded sample",
    )

    assert 1 <= len(sample) <= 100
    assert sample[-1].monotonic_ns == 1_000_000_000
    comparison_lines = (
        result.session_dir / "attitude_comparison.csv"
    ).read_text(encoding="utf-8").count("\n")
    assert comparison_lines == 1001


def test_fixture_queue_overflow_is_explicit_evidence_failure(tmp_path: Path) -> None:
    recorder = FixtureSessionRecorder(
        _profile(),
        root=tmp_path,
        session_id="session-queue-full",
        queue_size=1,
    )
    release_writer = threading.Event()

    def blocked_writer() -> None:
        recorder._ready.set()
        release_writer.wait(timeout=2.0)

    recorder._writer_main = blocked_writer
    recorder.start()
    recorder.record_event("first", "fills queue")
    with pytest.raises(FixtureSessionError, match="queue is full"):
        recorder.record_event("second", "must fail")
    assert recorder.writer_error == "fixture evidence queue is full"
    assert recorder.queue_capacity == 1

    release_writer.set()
    assert recorder._thread is not None
    recorder._thread.join(timeout=2.0)
    result = recorder.abort(reason="queue overflow")
    assert result.status == "incomplete"


def test_fixture_hashing_reads_files_in_chunks(tmp_path: Path, monkeypatch) -> None:
    recorder = FixtureSessionRecorder(
        _profile(),
        root=tmp_path,
        session_id="session-chunked-hash",
    )
    recorder.start()
    recorder.record_event("note", "chunked hashing")
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("fixture hashing must not call Path.read_bytes")
        ),
    )
    result = recorder.complete(
        conclusion=FixtureSessionConclusion.PASS,
        notes="chunked hashing",
    )
    assert result.file_hashes["events.jsonl"]
