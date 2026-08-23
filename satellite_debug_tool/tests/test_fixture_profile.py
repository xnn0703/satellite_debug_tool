"""Fixture profile hard limits, revisioning, leases, and segmented moves."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from satellite_debug_tool.core.production.fixture_profile import (
    FixtureAxisLimits,
    FixtureControlLease,
    FixtureLeaseError,
    FixtureProfileError,
    FixtureProfileStore,
    WorkstationFixtureProfile,
)
from satellite_debug_tool.core.production.motion_platform import (
    AbsoluteMoveRunner,
    CombinedSineProfile,
    LingjingPlatformAdapter,
    MotionPlatformError,
    PlatformPose,
    SineAxis,
    validate_sine_profile,
)


def _profile(**overrides) -> WorkstationFixtureProfile:
    limits = FixtureAxisLimits(15.0, 2.0, 0.5, 30.0, 50.0)
    values = {
        "profile_id": "fixture-a",
        "revision": 1,
        "host": "192.168.1.50",
        "port": 9800,
        "center_pose": PlatformPose(0, 0, 0, 0, 0, 100),
        "reset_pose": PlatformPose(0, 0, 0, 0, 0, 0),
        "roll_limits": limits,
        "pitch_limits": limits,
        "yaw_limits": limits,
        "calibration_id": "axis-map-1",
    }
    values.update(overrides)
    return WorkstationFixtureProfile(**values)


def _trajectory(**overrides) -> CombinedSineProfile:
    values = {
        "profile_id": "wave",
        "sample_period_ms": 100,
        "ramp_in_s": 1.0,
        "steady_duration_s": 5.0,
        "ramp_out_s": 1.0,
        "roll": SineAxis(2.0, 0.2),
        "pitch": SineAxis(2.0, 0.2, 90.0),
        "yaw": SineAxis(1.0, 0.1),
    }
    values.update(overrides)
    return CombinedSineProfile(**values)


def test_profile_hash_roundtrip_and_revision_conflict(tmp_path: Path) -> None:
    store = FixtureProfileStore(tmp_path)
    profile = _profile()
    path = store.save(profile)
    loaded = store.load(path)
    assert loaded == profile
    assert loaded.sha256 == profile.sha256
    with pytest.raises(FixtureProfileError, match="expected_revision"):
        store.save(replace(profile, revision=2))
    with pytest.raises(FixtureProfileError, match="revision conflict"):
        store.save(replace(profile, revision=2), expected_revision=2)
    store.save(replace(profile, revision=2), expected_revision=1)
    assert store.load("fixture-a").revision == 2


def test_profile_requires_hash_and_rejects_tampering(tmp_path: Path) -> None:
    profile = _profile()
    payload = profile.to_payload()
    with pytest.raises(FixtureProfileError, match="SHA-256 is missing"):
        WorkstationFixtureProfile.from_mapping(payload)

    path = FixtureProfileStore(tmp_path).save(profile)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["platform"]["endpoint"]["port"] = 9801
    path.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(FixtureProfileError, match="does not match"):
        FixtureProfileStore(tmp_path).load(path)


def test_profile_rejects_implicitly_coerced_json_numeric_types() -> None:
    payload = _profile().to_payload()
    payload["revision"] = True
    with pytest.raises(FixtureProfileError, match="revision must be an integer"):
        WorkstationFixtureProfile.from_mapping(payload, verify_hash=False)

    payload = _profile().to_payload()
    payload["platform"]["endpoint"]["port"] = "9800"
    with pytest.raises(FixtureProfileError, match="port must be an integer"):
        WorkstationFixtureProfile.from_mapping(payload, verify_hash=False)

    payload = _profile().to_payload()
    payload["platform"]["axis_limits"]["roll"]["abs_angle_deg"] = "15"
    with pytest.raises(FixtureProfileError, match="abs_angle_deg must be a JSON number"):
        WorkstationFixtureProfile.from_mapping(payload, verify_hash=False)


def test_invalid_endpoint_and_incomplete_periodic_limits_are_gated() -> None:
    with pytest.raises(FixtureProfileError, match="IPv4"):
        _profile(host="fixture.local").validate()
    with pytest.raises(FixtureProfileError, match="IPv4"):
        _profile(host="192.168.1").validate()
    incomplete = FixtureAxisLimits(15.0, 2.0, None, None, None)
    profile = _profile(roll_limits=incomplete)
    profile.validate()
    assert not profile.periodic_limits_complete
    with pytest.raises(MotionPlatformError, match="hard limits are incomplete"):
        validate_sine_profile(profile.to_motion_config(), _trajectory())


@pytest.mark.parametrize(
    ("axis", "expected"),
    [
        (SineAxis(16.0, 0.1), "amplitude"),
        (SineAxis(1.0, 0.6), "frequency"),
        (SineAxis(15.0, 0.4), "velocity"),
        (SineAxis(15.0, 0.3), "acceleration"),
    ],
)
def test_periodic_motion_checks_every_hard_limit(axis: SineAxis, expected: str) -> None:
    with pytest.raises(MotionPlatformError, match=expected):
        validate_sine_profile(
            _profile().to_motion_config(),
            _trajectory(roll=axis, pitch=SineAxis(0, 0, enabled=False)),
        )


def test_exclusive_lease_rejects_batch_and_debug_overlap() -> None:
    lease = FixtureControlLease()
    debug = lease.acquire("fixture-debug")
    with pytest.raises(FixtureLeaseError, match="fixture-debug"):
        lease.acquire("batch:PILOT-1")
    lease.release(debug)
    batch = lease.acquire("batch:PILOT-1")
    with pytest.raises(FixtureLeaseError):
        lease.release(debug)
    lease.release(batch)
    assert lease.owner == ""


def test_segmented_axis_move_preserves_other_axes_and_total_duration() -> None:
    sent = []
    adapter = LingjingPlatformAdapter(
        _profile().to_motion_config(),
        sender=lambda data, _endpoint: not sent.append(data),
    )
    adapter.confirm_preflight("axis-map-1", safety_confirmed=True, center_confirmed=True)
    statistics = AbsoluteMoveRunner(adapter, sleep=lambda _seconds: None).run_axis(
        "roll", 5.0, total_duration_ms=1000
    )
    assert statistics.intended_points == 3
    assert statistics.sent_points == 3
    assert statistics.final_pose == PlatformPose(5, 0, 0, 0, 0, 100)
    assert sent[-1].startswith(b"@A6T:5,0,0,0,0,100,")
    durations = [int(command.rstrip(b"#").split(b",")[-1]) for command in sent]
    assert sum(durations) == 1000


def test_segmented_center_move_obeys_axis_step_limit() -> None:
    sent = []
    adapter = LingjingPlatformAdapter(
        _profile().to_motion_config(),
        sender=lambda data, _endpoint: not sent.append(data),
    )
    adapter.confirm_preflight("axis-map-1", safety_confirmed=True, center_confirmed=True)
    AbsoluteMoveRunner(adapter, sleep=lambda _seconds: None).run_axis(
        "yaw", 6.0, total_duration_ms=300
    )
    sent.clear()

    statistics = AbsoluteMoveRunner(adapter, sleep=lambda _seconds: None).run_pose(
        adapter.config.center_pose,
        total_duration_ms=300,
    )

    assert statistics.intended_points == 3
    assert statistics.final_pose == adapter.config.center_pose
    assert len(sent) == 3
    assert sent[-1].startswith(b"@A6T:0,0,0,0,0,100,")


def test_multi_hour_trajectory_uses_constant_time_discrete_gate() -> None:
    profile = _trajectory(
        sample_period_ms=50,
        ramp_in_s=60.0,
        steady_duration_s=21600.0,
        ramp_out_s=60.0,
        roll=SineAxis(0.1, 0.01),
        pitch=SineAxis(0, 0, enabled=False),
        yaw=SineAxis(0, 0, enabled=False),
    )

    validate_sine_profile(_profile().to_motion_config(), profile)

    assert profile.sample_count > 400_000
