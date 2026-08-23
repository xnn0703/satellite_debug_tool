"""Fixture coordinate calibration and engineering metrics tests."""

from __future__ import annotations

from dataclasses import replace
import json
import math

import pytest

from satellite_debug_tool.core.production.fixture_analysis import (
    CALIBRATION_SEQUENCE,
    CalibrationStage,
    FixtureAnalysisError,
    FixtureCalibration,
    FixtureCalibrationStore,
    GuidedFixtureCalibration,
    TimedAttitude,
    compare_attitude_streams,
    compute_dynamic_metrics,
    compute_static_metrics,
    quaternion_attitude_error,
)


def _samples(values: tuple[float, float, float], count: int = 30):
    return [
        TimedAttitude(index * 10_000_000, values[0], values[1], values[2])
        for index in range(count)
    ]


def test_guided_calibration_recovers_axis_swap_sign_and_zero_offset() -> None:
    calibration = GuidedFixtureCalibration(
        "fixture-a",
        "a" * 64,
        minimum_samples_per_stage=5,
    )
    # Raw sensor [roll, pitch, yaw] observes logical [pitch, -yaw, roll].
    center = (10.0, -20.0, 179.0)
    stage_values = {
        CalibrationStage.CENTER_START: center,
        CalibrationStage.ROLL_PLUS: (10.0, -20.0, -178.0),
        CalibrationStage.ROLL_MINUS: (10.0, -20.0, 176.0),
        CalibrationStage.PITCH_PLUS: (13.0, -20.0, 179.0),
        CalibrationStage.PITCH_MINUS: (7.0, -20.0, 179.0),
        CalibrationStage.YAW_PLUS: (10.0, -23.0, 179.0),
        CalibrationStage.YAW_MINUS: (10.0, -17.0, 179.0),
        CalibrationStage.CENTER_END: center,
    }
    for stage in CALIBRATION_SEQUENCE:
        calibration.record_stage(stage, _samples(stage_values[stage], count=5))
    result = calibration.evaluate(confirmed=True)
    assert result.sensor_axis_for_logical == ("yaw", "roll", "pitch")
    assert result.logical_signs == (1, 1, -1)
    assert result.coordinate_valid
    transformed = result.apply(TimedAttitude(1, 12.0, -24.0, -179.0))
    assert transformed.roll_deg == pytest.approx(2.0)
    assert transformed.pitch_deg == pytest.approx(2.0)
    assert transformed.yaw_deg == pytest.approx(4.0)

    assert not replace(
        result,
        cross_coupling_ratio=(0.1, 0.4, 0.1),
    ).coordinate_valid
    assert not replace(
        result,
        static_noise_std_deg=(0.1, 0.1, 1.1),
    ).coordinate_valid

    repeated = calibration.evaluate(confirmed=True)
    assert repeated.calibration_id != result.calibration_id


def test_calibration_store_requires_hash_and_rejects_tampering(tmp_path) -> None:
    guided = GuidedFixtureCalibration(
        "fixture-a",
        "a" * 64,
        minimum_samples_per_stage=1,
    )
    for stage in CALIBRATION_SEQUENCE:
        values = {
            CalibrationStage.ROLL_PLUS: (3.0, 0.0, 0.0),
            CalibrationStage.ROLL_MINUS: (-3.0, 0.0, 0.0),
            CalibrationStage.PITCH_PLUS: (0.0, 3.0, 0.0),
            CalibrationStage.PITCH_MINUS: (0.0, -3.0, 0.0),
            CalibrationStage.YAW_PLUS: (0.0, 0.0, 3.0),
            CalibrationStage.YAW_MINUS: (0.0, 0.0, -3.0),
        }.get(stage, (0.0, 0.0, 0.0))
        guided.record_stage(stage, _samples(values, count=1))
    calibration = guided.evaluate(confirmed=True)
    with pytest.raises(FixtureAnalysisError, match="SHA-256 is missing"):
        FixtureCalibration.from_mapping(calibration.to_payload())

    store = FixtureCalibrationStore(tmp_path)
    path = store.save(calibration)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["logical_signs"][0] = -1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FixtureAnalysisError, match="does not match"):
        store.load(path)


def test_quaternion_error_handles_yaw_wrap() -> None:
    roll, pitch, yaw, angle = quaternion_attitude_error((0, 0, 179), (0, 0, -179))
    assert roll == pytest.approx(0.0, abs=1e-9)
    assert pitch == pytest.approx(0.0, abs=1e-9)
    assert yaw == pytest.approx(2.0, abs=1e-9)
    assert angle == pytest.approx(2.0, abs=1e-9)


def test_comparison_does_not_interpolate_across_gap() -> None:
    targets = [
        TimedAttitude(0, 0, 0, 179),
        TimedAttitude(100_000_000, 1, 0, -179),
        TimedAttitude(2_000_000_000, 2, 0, -178),
    ]
    measurements = [
        TimedAttitude(50_000_000, 0.5, 0, 180),
        TimedAttitude(1_000_000_000, 1.5, 0, -178.5),
    ]
    comparisons = compare_attitude_streams(targets, measurements, max_target_gap_s=0.5)
    assert len(comparisons) == 1
    assert comparisons[0].error_angle_deg == pytest.approx(0.0, abs=1e-8)


def test_static_and_dynamic_synthetic_metrics() -> None:
    target_samples = []
    measured_samples = []
    frequency = 0.25
    phase_rad = math.radians(20.0)
    for index in range(2001):
        elapsed = index * 0.01
        monotonic_ns = int(elapsed * 1e9)
        target = 10.0 * math.sin(2 * math.pi * frequency * elapsed)
        measured = 9.0 * math.sin(2 * math.pi * frequency * elapsed + phase_rad)
        target_samples.append(TimedAttitude(monotonic_ns, target, 0.0, 0.0))
        measured_samples.append(
            TimedAttitude(
                monotonic_ns,
                measured,
                0.2 * math.sin(2 * math.pi * frequency * elapsed),
                0.0,
            )
        )
    comparisons = compare_attitude_streams(
        target_samples,
        measured_samples,
        max_target_gap_s=0.05,
    )
    dynamic = compute_dynamic_metrics(comparisons, primary_axis="roll")
    assert dynamic["valid"]
    assert dynamic["amplitude_ratio"] == pytest.approx(0.9, rel=0.03)
    assert dynamic["target_frequency_hz"] == pytest.approx(frequency, rel=0.03)
    assert dynamic["measured_frequency_hz"] == pytest.approx(frequency, rel=0.03)
    assert dynamic["estimated_phase_deg"] == pytest.approx(20.0, abs=1.0)
    assert dynamic["cross_axis_coupling_ratio"]["pitch"] == pytest.approx(0.02, rel=0.1)
    static = compute_static_metrics(comparisons)
    assert static["valid"]
    assert static["sample_count"] == len(comparisons)
    assert "overshoot_deg" in static


def test_dynamic_metrics_reject_non_periodic_target() -> None:
    targets = [TimedAttitude(index * 10_000_000, 0.0, 0.0, 0.0) for index in range(20)]
    measurements = [
        TimedAttitude(index * 10_000_000, 0.1, 0.0, 0.0) for index in range(20)
    ]
    comparisons = compare_attitude_streams(targets, measurements, max_target_gap_s=0.1)

    result = compute_dynamic_metrics(comparisons, primary_axis="roll")

    assert not result["valid"]
    assert result["reason"] == "NO_PERIODIC_EXCITATION"
