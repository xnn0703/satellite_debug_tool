"""Lingjing A6/A6T serialization, safety gates, and absolute scheduler tests."""

from __future__ import annotations

import math

import pytest

from satellite_debug_tool.core.production import (
    CombinedSineProfile,
    EvidenceLevel,
    LingjingPlatformAdapter,
    MotionPlatformConfig,
    MotionPlatformError,
    MotionTrajectoryRunner,
    PlatformPose,
    SineAxis,
    serialize_a6,
    serialize_a6t,
)


def _config(**overrides) -> MotionPlatformConfig:
    values = {
        "host": "192.168.1.50",
        "port": 9800,
        "center_pose": PlatformPose(0, 0, 0, 0, 0, 100),
        "reset_pose": PlatformPose(0, 0, 0, 0, 0, 0),
        "roll_abs_limit_deg": 15.0,
        "pitch_abs_limit_deg": 15.0,
        "yaw_abs_limit_deg": 30.0,
        "minimum_duration_ms": 50,
        "calibration_id": "LJ-A6-2026-001",
    }
    values.update(overrides)
    return MotionPlatformConfig(**values)


def _profile(*, duration_s: float = 1.0, period_ms: int = 100) -> CombinedSineProfile:
    return CombinedSineProfile(
        profile_id="marine-test",
        sample_period_ms=period_ms,
        ramp_in_s=0.0,
        steady_duration_s=duration_s,
        ramp_out_s=0.0,
        roll=SineAxis(1.0, 0.25, 0.0),
        pitch=SineAxis(1.0, 0.25, 90.0),
        yaw=SineAxis(1.0, 0.1, 0.0),
    )


def test_a6_and_a6t_golden_vectors() -> None:
    pose = PlatformPose(1.25, -2.0, 3.5, 0.0, 0.0, 100.0)
    assert serialize_a6(pose) == b"@A6:1.25,-2,3.5,0,0,100#"
    assert serialize_a6t(pose, 500) == b"@A6T:1.25,-2,3.5,0,0,100,500#"
    assert serialize_a6t(PlatformPose(0, 0, 0, 0, 0, 100), 1000) == (
        b"@A6T:0,0,0,0,0,100,1000#"
    )
    assert serialize_a6t(PlatformPose(0, 0, 0, 0, 0, 0), 1000) == (
        b"@A6T:0,0,0,0,0,0,1000#"
    )


def test_adapter_requires_matching_calibration_safety_and_center() -> None:
    sent: list[bytes] = []
    adapter = LingjingPlatformAdapter(
        _config(),
        sender=lambda data, _endpoint: not sent.append(data),
    )
    with pytest.raises(MotionPlatformError, match="not ready"):
        adapter.send_center(duration_ms=1000)
    with pytest.raises(MotionPlatformError, match="calibration ID"):
        adapter.confirm_preflight(
            "wrong",
            safety_confirmed=True,
            center_confirmed=True,
        )

    adapter.confirm_preflight(
        "LJ-A6-2026-001",
        safety_confirmed=True,
        center_confirmed=False,
    )
    assert not adapter.ready
    assert adapter.status_text == "center_not_confirmed"
    adapter.confirm_preflight(
        "LJ-A6-2026-001",
        safety_confirmed=True,
        center_confirmed=True,
    )
    result = adapter.send_center(duration_ms=1000)
    assert result.sent
    assert result.evidence_level == EvidenceLevel.COMMAND_SENT
    assert sent == [b"@A6T:0,0,0,0,0,100,1000#"]


def test_adapter_applies_axis_calibration_but_checks_logical_limits() -> None:
    sent: list[bytes] = []
    adapter = LingjingPlatformAdapter(
        _config(roll_sign=-1, yaw_sign=-1),
        sender=lambda data, _endpoint: not sent.append(data),
    )
    adapter.confirm_preflight(
        "LJ-A6-2026-001",
        safety_confirmed=True,
        center_confirmed=True,
    )
    result = adapter.send_pose(PlatformPose(1, 2, 3), duration_ms=100)
    assert result.raw_command == b"@A6T:-1,2,-3,0,0,100,100#"
    assert result.logical_pose.roll_deg == 1
    assert result.command_pose.roll_deg == -1

    with pytest.raises(MotionPlatformError, match="roll exceeds"):
        adapter.send_pose(PlatformPose(16, 0, 0), duration_ms=100)
    full_range = adapter.send_pose(PlatformPose(10, 0, 0), duration_ms=100)
    assert full_range.raw_command == b"@A6T:-10,0,0,0,0,100,100#"
    with pytest.raises(MotionPlatformError, match="X=0"):
        adapter.send_pose(PlatformPose(0, 0, 0, x_mm=1), duration_ms=100)


def test_reset_is_distinct_and_requires_explicit_confirmation() -> None:
    sent: list[bytes] = []
    adapter = LingjingPlatformAdapter(
        _config(), sender=lambda data, _endpoint: not sent.append(data)
    )
    adapter.confirm_preflight(
        "LJ-A6-2026-001",
        safety_confirmed=True,
        center_confirmed=True,
    )
    with pytest.raises(MotionPlatformError, match="explicit confirmation"):
        adapter.send_reset(duration_ms=1000)
    adapter.send_reset(duration_ms=1000, explicit_confirmation=True)
    assert sent == [b"@A6T:0,0,0,0,0,0,1000#"]


def test_combined_profile_phase_and_sixty_minute_point_count() -> None:
    profile = _profile(duration_s=3600.0)
    assert profile.sample_count == 36_000
    start = profile.pose_at(0.0)
    assert start.roll_deg == pytest.approx(0.0)
    assert start.pitch_deg == pytest.approx(1.0)
    quarter = profile.pose_at(1.0)
    assert quarter.roll_deg == pytest.approx(1.0)
    assert quarter.pitch_deg == pytest.approx(0.0, abs=1e-12)
    assert quarter.yaw_deg == pytest.approx(math.sin(math.pi * 0.2))


class _FakeClock:
    def __init__(self, send_cost_ms: int = 0) -> None:
        self.now_ns = 0
        self.send_cost_ns = send_cost_ms * 1_000_000
        self.commands: list[bytes] = []

    def clock_ns(self) -> int:
        return self.now_ns

    def sleep(self, seconds: float) -> None:
        self.now_ns += int(seconds * 1_000_000_000)

    def sender(self, data: bytes, _endpoint: tuple[str, int]) -> bool:
        self.commands.append(data)
        self.now_ns += self.send_cost_ns
        return True


def _runner(clock: _FakeClock) -> MotionTrajectoryRunner:
    adapter = LingjingPlatformAdapter(
        _config(),
        sender=clock.sender,
        clock_ns=clock.clock_ns,
    )
    adapter.confirm_preflight(
        "LJ-A6-2026-001",
        safety_confirmed=True,
        center_confirmed=True,
    )
    return MotionTrajectoryRunner(
        adapter,
        clock_ns=clock.clock_ns,
        sleep=clock.sleep,
    )


def test_absolute_scheduler_sends_every_point_without_clock_drift() -> None:
    clock = _FakeClock(send_cost_ms=10)
    statistics = _runner(clock).run(_profile())
    assert statistics.intended_points == 10
    assert statistics.sent_points == 10
    assert statistics.skipped_points == 0
    assert statistics.failed_points == 0
    assert statistics.max_abs_jitter_ms == pytest.approx(0.0)
    assert statistics.coverage_ratio == pytest.approx(1.0)
    assert not statistics.timing_fault
    assert clock.commands[0].startswith(b"@A6T:0.156434")
    assert clock.commands[-1].endswith(b",100#")


def test_overdue_scheduler_stops_instead_of_bursting() -> None:
    clock = _FakeClock(send_cost_ms=250)
    statistics = _runner(clock).run(_profile())
    assert statistics.intended_points == 10
    assert statistics.skipped_points > 0
    assert statistics.sent_points < statistics.intended_points
    assert statistics.sent_points + statistics.skipped_points == statistics.intended_points
    assert statistics.coverage_ratio < 1.0
    assert statistics.timing_fault
    assert "missed deadline" in statistics.timing_fault_details
