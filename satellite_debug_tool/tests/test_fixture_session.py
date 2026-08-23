"""Fixture engineering session persistence and recovery tests."""

from __future__ import annotations

import json
from pathlib import Path

from satellite_debug_tool.core.production.fixture_profile import (
    FixtureAxisLimits,
    WorkstationFixtureProfile,
)
from satellite_debug_tool.core.production.fixture_session import (
    FixtureSessionConclusion,
    FixtureSessionRecorder,
)
from satellite_debug_tool.core.production.fixture_analysis import AttitudeComparison
from satellite_debug_tool.core.production.models import EvidenceLevel
from satellite_debug_tool.core.production.motion_platform import (
    PlatformPose,
    PlatformSendResult,
)
from satellite_debug_tool.core.production.ms6222_protocol import Ms6222StreamParser
from satellite_debug_tool.tests.test_ms6222_protocol import _ins_frame


def _profile() -> WorkstationFixtureProfile:
    limits = FixtureAxisLimits(15.0, 2.0, 0.5, 30.0, 100.0)
    return WorkstationFixtureProfile(
        profile_id="fixture-a",
        revision=1,
        host="192.168.1.50",
        port=9800,
        center_pose=PlatformPose(0, 0, 0, 0, 0, 100),
        reset_pose=PlatformPose(0, 0, 0, 0, 0, 0),
        roll_limits=limits,
        pitch_limits=limits,
        yaw_limits=limits,
        calibration_id="engineering-axis-map-1",
    )


def _command() -> PlatformSendResult:
    pose = PlatformPose(1, 2, 3)
    return PlatformSendResult(
        sent=True,
        raw_command=b"@A6T:1,2,3,0,0,100,100#",
        logical_pose=pose,
        command_pose=pose,
        duration_ms=100,
        monotonic_ns=123,
        evidence_level=EvidenceLevel.COMMAND_SENT,
    )


def test_complete_session_contains_snapshot_evidence_and_hashes(tmp_path: Path) -> None:
    recorder = FixtureSessionRecorder(
        _profile(), root=tmp_path, session_id="session-complete", operator="operator-a"
    )
    recorder.start()
    recorder.record_command(_command(), action="absolute", sequence=1)
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
    assert command["planned_completion_monotonic_ns"] == 100_000_123


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
