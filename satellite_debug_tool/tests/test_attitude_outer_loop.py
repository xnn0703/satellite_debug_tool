"""Bounded MS-6222 attitude outer-loop tests."""

from __future__ import annotations

import pytest

from satellite_debug_tool.core.motion_platform import PlatformPose
from satellite_debug_tool.core.production import (
    AttitudeOuterLoopConfig,
    BoundedAttitudeOuterLoop,
    OuterLoopStatus,
    TimedAttitude,
)


def test_outer_loop_applies_bounded_correction_then_verifies_target() -> None:
    loop = BoundedAttitudeOuterLoop(
        AttitudeOuterLoopConfig(gain=0.5, maximum_step_deg=0.4)
    )
    loop.begin(PlatformPose(3.0, -2.0, 1.0, z_mm=100.0))

    correction = loop.evaluate(TimedAttitude(1, 2.0, -1.8, 0.0))

    assert correction.status == OuterLoopStatus.CORRECTION_REQUIRED
    assert correction.command_pose is not None
    assert correction.command_pose.roll_deg == pytest.approx(3.4)
    assert correction.command_pose.pitch_deg == pytest.approx(-2.1)
    assert correction.command_pose.yaw_deg == pytest.approx(1.4)
    verified = loop.evaluate(TimedAttitude(2, 3.01, -2.02, 1.03))
    assert verified.status == OuterLoopStatus.TARGET_VERIFIED
    assert not loop.active


def test_outer_loop_stops_when_error_diverges() -> None:
    loop = BoundedAttitudeOuterLoop()
    loop.begin(PlatformPose(0.0, 0.0, 0.0, z_mm=100.0))
    assert loop.evaluate(TimedAttitude(1, 0.2, 0.0, 0.0)).status == OuterLoopStatus.CORRECTION_REQUIRED

    result = loop.evaluate(TimedAttitude(2, 0.4, 0.0, 0.0))

    assert result.status == OuterLoopStatus.DIVERGING
    assert not loop.active


def test_outer_loop_wraps_yaw_error_at_north() -> None:
    loop = BoundedAttitudeOuterLoop()
    loop.begin(PlatformPose(0.0, 0.0, -179.0, z_mm=100.0))

    result = loop.evaluate(TimedAttitude(1, 0.0, 0.0, 179.0))

    assert result.error_deg[2] == pytest.approx(2.0)
    assert result.command_pose is not None
    assert result.command_pose.yaw_deg == pytest.approx(-178.5)
