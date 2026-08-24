"""MS-6222 coordinate calibration and engineering-only motion metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import itertools
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Optional, Sequence
import uuid

import numpy as np


_CALIBRATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FixtureAnalysisError(ValueError):
    pass


class CalibrationStage(str, Enum):
    CENTER_START = "center_start"
    ROLL_PLUS = "roll_plus"
    ROLL_MINUS = "roll_minus"
    PITCH_PLUS = "pitch_plus"
    PITCH_MINUS = "pitch_minus"
    YAW_PLUS = "yaw_plus"
    YAW_MINUS = "yaw_minus"
    CENTER_END = "center_end"


CALIBRATION_SEQUENCE = (
    CalibrationStage.CENTER_START,
    CalibrationStage.ROLL_PLUS,
    CalibrationStage.ROLL_MINUS,
    CalibrationStage.PITCH_PLUS,
    CalibrationStage.PITCH_MINUS,
    CalibrationStage.YAW_PLUS,
    CalibrationStage.YAW_MINUS,
    CalibrationStage.CENTER_END,
)


@dataclass(frozen=True)
class TimedAttitude:
    monotonic_ns: int
    roll_deg: float
    pitch_deg: float
    yaw_deg: float
    valid: bool = True

    def values(self) -> tuple[float, float, float]:
        return (self.roll_deg, self.pitch_deg, self.yaw_deg)


@dataclass(frozen=True)
class AttitudeComparison:
    monotonic_ns: int
    target_roll_deg: float
    target_pitch_deg: float
    target_yaw_deg: float
    measured_roll_deg: float
    measured_pitch_deg: float
    measured_yaw_deg: float
    error_roll_deg: float
    error_pitch_deg: float
    error_yaw_deg: float
    error_angle_deg: float


@dataclass(frozen=True)
class FixtureCalibration:
    calibration_id: str
    profile_id: str
    profile_sha256: str
    created_utc: str
    sensor_axis_for_logical: tuple[str, str, str]
    logical_signs: tuple[int, int, int]
    zero_offsets_deg: tuple[float, float, float]
    response_matrix: tuple[tuple[float, float, float], ...]
    cross_coupling_ratio: tuple[float, float, float]
    static_noise_std_deg: tuple[float, float, float]
    sample_coverage_ratio: float
    confirmed: bool
    timing_mode: str = "HOST_ARRIVAL_ONLY"
    result_class: str = "ENGINEERING_ONLY"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_payload()).encode("utf-8")).hexdigest()

    @property
    def coordinate_valid(self) -> bool:
        primary_response = (
            tuple(
                abs(
                    self.response_matrix[logical][
                        ("roll", "pitch", "yaw").index(sensor)
                    ]
                )
                for logical, sensor in enumerate(self.sensor_axis_for_logical)
            )
            if len(self.sensor_axis_for_logical) == 3
            and len(self.response_matrix) == 3
            else ()
        )
        return (
            self.confirmed
            and set(self.sensor_axis_for_logical) == {"roll", "pitch", "yaw"}
            and all(sign in (-1, 1) for sign in self.logical_signs)
            and self.sample_coverage_ratio >= 1.0
            and len(primary_response) == 3
            and all(math.isfinite(value) and value >= 0.5 for value in primary_response)
            and all(
                math.isfinite(value) and value <= 0.35
                for value in self.cross_coupling_ratio
            )
            and all(
                math.isfinite(value) and value <= 1.0
                for value in self.static_noise_std_deg
            )
        )

    @property
    def precise_timing_valid(self) -> bool:
        return False

    def validate(self) -> None:
        if not _CALIBRATION_ID_RE.fullmatch(self.calibration_id):
            raise FixtureAnalysisError("fixture calibration_id is not a safe path component")
        if not self.profile_id.strip():
            raise FixtureAnalysisError("fixture calibration profile_id is required")
        if not _SHA256_RE.fullmatch(self.profile_sha256):
            raise FixtureAnalysisError("fixture calibration profile SHA-256 is invalid")
        try:
            created = datetime.fromisoformat(self.created_utc)
        except ValueError as exc:
            raise FixtureAnalysisError("fixture calibration creation time is invalid") from exc
        if created.tzinfo is None:
            raise FixtureAnalysisError("fixture calibration creation time must include a timezone")
        if self.result_class != "ENGINEERING_ONLY":
            raise FixtureAnalysisError("fixture calibration result class must be ENGINEERING_ONLY")
        if not self.timing_mode.strip():
            raise FixtureAnalysisError("fixture calibration timing mode is required")
        if not isinstance(self.confirmed, bool):
            raise FixtureAnalysisError("fixture calibration confirmation must be boolean")
        if any(
            len(value) != 3
            for value in (
                self.sensor_axis_for_logical,
                self.logical_signs,
                self.zero_offsets_deg,
                self.cross_coupling_ratio,
                self.static_noise_std_deg,
            )
        ) or len(self.response_matrix) != 3 or any(
            len(row) != 3 for row in self.response_matrix
        ):
            raise FixtureAnalysisError("fixture calibration dimensions are invalid")
        if set(self.sensor_axis_for_logical) != {"roll", "pitch", "yaw"}:
            raise FixtureAnalysisError("fixture calibration axis mapping is invalid")
        if any(sign not in (-1, 1) for sign in self.logical_signs):
            raise FixtureAnalysisError("fixture calibration signs are invalid")
        numeric_values = (
            tuple(self.zero_offsets_deg)
            + tuple(value for row in self.response_matrix for value in row)
            + tuple(self.cross_coupling_ratio)
            + tuple(self.static_noise_std_deg)
            + (self.sample_coverage_ratio,)
        )
        if any(not math.isfinite(float(value)) for value in numeric_values):
            raise FixtureAnalysisError("fixture calibration contains non-finite values")
        if not 0.0 <= self.sample_coverage_ratio <= 1.0:
            raise FixtureAnalysisError("fixture calibration sample coverage is out of range")

    def apply(self, sample: TimedAttitude) -> TimedAttitude:
        raw = dict(zip(("roll", "pitch", "yaw"), sample.values()))
        logical = []
        for sensor_axis, sign in zip(self.sensor_axis_for_logical, self.logical_signs):
            sensor_index = ("roll", "pitch", "yaw").index(sensor_axis)
            delta = _angle_delta(raw[sensor_axis], self.zero_offsets_deg[sensor_index])
            logical.append(sign * delta)
        return TimedAttitude(sample.monotonic_ns, *logical, valid=sample.valid)

    def to_payload(self, *, include_hash: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "satellite.fixture-calibration",
            "schema_version": 1,
            "calibration_id": self.calibration_id,
            "profile_id": self.profile_id,
            "profile_sha256": self.profile_sha256,
            "created_utc": self.created_utc,
            "sensor_axis_for_logical": list(self.sensor_axis_for_logical),
            "logical_signs": list(self.logical_signs),
            "zero_offsets_deg": list(self.zero_offsets_deg),
            "response_matrix": [list(row) for row in self.response_matrix],
            "cross_coupling_ratio": list(self.cross_coupling_ratio),
            "static_noise_std_deg": list(self.static_noise_std_deg),
            "sample_coverage_ratio": self.sample_coverage_ratio,
            "confirmed": self.confirmed,
            "timing_mode": self.timing_mode,
            "result_class": self.result_class,
        }
        if include_hash:
            payload["sha256"] = self.sha256
        return payload

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        verify_hash: bool = True,
    ) -> "FixtureCalibration":
        if payload.get("schema") != "satellite.fixture-calibration":
            raise FixtureAnalysisError("unsupported fixture calibration schema")
        if int(payload.get("schema_version", 0)) != 1:
            raise FixtureAnalysisError("unsupported fixture calibration schema version")
        try:
            result = cls(
                calibration_id=str(payload["calibration_id"]),
                profile_id=str(payload["profile_id"]),
                profile_sha256=str(payload["profile_sha256"]),
                created_utc=str(payload["created_utc"]),
                sensor_axis_for_logical=tuple(
                    str(value) for value in payload["sensor_axis_for_logical"]  # type: ignore[index]
                ),
                logical_signs=tuple(
                    int(value) for value in payload["logical_signs"]  # type: ignore[index]
                ),
                zero_offsets_deg=tuple(
                    float(value) for value in payload["zero_offsets_deg"]  # type: ignore[index]
                ),
                response_matrix=tuple(
                    tuple(float(value) for value in row)
                    for row in payload["response_matrix"]  # type: ignore[index]
                ),
                cross_coupling_ratio=tuple(
                    float(value) for value in payload["cross_coupling_ratio"]  # type: ignore[index]
                ),
                static_noise_std_deg=tuple(
                    float(value) for value in payload["static_noise_std_deg"]  # type: ignore[index]
                ),
                sample_coverage_ratio=float(payload["sample_coverage_ratio"]),
                confirmed=payload["confirmed"],  # type: ignore[arg-type]
                timing_mode=str(payload.get("timing_mode", "HOST_ARRIVAL_ONLY")),
                result_class=str(payload.get("result_class", "ENGINEERING_ONLY")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FixtureAnalysisError(f"invalid fixture calibration: {exc}") from exc
        result.validate()
        stored_hash = str(payload.get("sha256", ""))
        if verify_hash:
            if not stored_hash:
                raise FixtureAnalysisError("fixture calibration SHA-256 is missing")
            if stored_hash != result.sha256:
                raise FixtureAnalysisError("fixture calibration SHA-256 does not match")
        return result


class GuidedFixtureCalibration:
    def __init__(
        self,
        profile_id: str,
        profile_sha256: str,
        *,
        command_angle_deg: float = 3.0,
        minimum_samples_per_stage: int = 20,
    ) -> None:
        if command_angle_deg <= 0 or not math.isfinite(command_angle_deg):
            raise FixtureAnalysisError("calibration command angle must be positive")
        if minimum_samples_per_stage <= 0:
            raise FixtureAnalysisError("minimum calibration sample count must be positive")
        if not _SHA256_RE.fullmatch(str(profile_sha256)):
            raise FixtureAnalysisError("calibration profile SHA-256 is invalid")
        self.profile_id = str(profile_id)
        self.profile_sha256 = str(profile_sha256)
        self.command_angle_deg = float(command_angle_deg)
        self.minimum_samples_per_stage = int(minimum_samples_per_stage)
        self._samples: dict[CalibrationStage, tuple[TimedAttitude, ...]] = {}

    @property
    def completed_stages(self) -> tuple[CalibrationStage, ...]:
        return tuple(stage for stage in CALIBRATION_SEQUENCE if stage in self._samples)

    @property
    def next_stage(self) -> Optional[CalibrationStage]:
        for stage in CALIBRATION_SEQUENCE:
            if stage not in self._samples:
                return stage
        return None

    def record_stage(
        self,
        stage: CalibrationStage | str,
        samples: Iterable[TimedAttitude],
    ) -> None:
        normalized = CalibrationStage(stage)
        valid = tuple(
            sample
            for sample in samples
            if sample.valid and all(math.isfinite(value) for value in sample.values())
        )
        if len(valid) < self.minimum_samples_per_stage:
            raise FixtureAnalysisError(
                f"calibration stage requires at least {self.minimum_samples_per_stage} valid samples"
            )
        self._samples[normalized] = valid

    def evaluate(self, *, confirmed: bool) -> FixtureCalibration:
        missing = [stage.value for stage in CALIBRATION_SEQUENCE if stage not in self._samples]
        if missing:
            raise FixtureAnalysisError(
                f"calibration stages are incomplete: {', '.join(missing)}"
            )
        stage_means = {
            stage: tuple(_circular_mean([sample.values()[axis] for sample in samples]) for axis in range(3))
            for stage, samples in self._samples.items()
        }
        center_samples = (
            self._samples[CalibrationStage.CENTER_START]
            + self._samples[CalibrationStage.CENTER_END]
        )
        zero_offsets = tuple(
            _circular_mean([sample.values()[axis] for sample in center_samples])
            for axis in range(3)
        )
        response_rows = []
        for axis in ("roll", "pitch", "yaw"):
            plus = stage_means[CalibrationStage(f"{axis}_plus")]
            minus = stage_means[CalibrationStage(f"{axis}_minus")]
            response_rows.append(
                tuple(
                    _angle_delta(plus[index], minus[index])
                    / (2.0 * self.command_angle_deg)
                    for index in range(3)
                )
            )
        response_matrix = tuple(response_rows)
        permutation = max(
            itertools.permutations(range(3)),
            key=lambda candidate: sum(
                abs(response_matrix[logical][sensor])
                for logical, sensor in enumerate(candidate)
            ),
        )
        sensor_names = ("roll", "pitch", "yaw")
        mapping = tuple(sensor_names[index] for index in permutation)
        signs = tuple(
            1 if response_matrix[logical][sensor] >= 0 else -1
            for logical, sensor in enumerate(permutation)
        )
        cross_coupling = []
        for logical, primary_sensor in enumerate(permutation):
            primary = abs(response_matrix[logical][primary_sensor])
            secondary = max(
                abs(response_matrix[logical][sensor])
                for sensor in range(3)
                if sensor != primary_sensor
            )
            cross_coupling.append(secondary / primary if primary > 1e-12 else math.inf)
        static_noise = tuple(
            _circular_std([sample.values()[axis] for sample in center_samples])
            for axis in range(3)
        )
        coverage = len(self._samples) / len(CALIBRATION_SEQUENCE)
        identity_payload = {
            "profile_id": self.profile_id,
            "profile_sha256": self.profile_sha256,
            "mapping": mapping,
            "signs": signs,
            "zero": zero_offsets,
            "response": response_matrix,
        }
        digest = hashlib.sha256(_canonical_json(identity_payload).encode("utf-8")).hexdigest()[:12]
        created = datetime.now(timezone.utc)
        return FixtureCalibration(
            calibration_id=(
                f"MS6222-CAL-{created.strftime('%Y%m%dT%H%M%S%fZ')}-"
                f"{digest.upper()}-{uuid.uuid4().hex[:8].upper()}"
            ),
            profile_id=self.profile_id,
            profile_sha256=self.profile_sha256,
            created_utc=created.isoformat(),
            sensor_axis_for_logical=mapping,
            logical_signs=signs,
            zero_offsets_deg=zero_offsets,
            response_matrix=response_matrix,
            cross_coupling_ratio=tuple(cross_coupling),
            static_noise_std_deg=static_noise,
            sample_coverage_ratio=coverage,
            confirmed=bool(confirmed),
        )


class FixtureCalibrationStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or (Path.home() / ".satellite_debug_tool" / "fixture_calibrations")

    def save(self, calibration: FixtureCalibration) -> Path:
        calibration.validate()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{calibration.calibration_id}.json"
        if path.exists():
            raise FixtureAnalysisError("fixture calibration already exists")
        temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temp.write_text(
            json.dumps(calibration.to_payload(include_hash=True), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
        return path

    def load(self, path_or_id: str | Path) -> FixtureCalibration:
        path = Path(path_or_id)
        if not path.is_absolute() and path.parent == Path("."):
            path = self.root / f"{str(path_or_id).removesuffix('.json')}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise FixtureAnalysisError("fixture calibration root must be an object")
        return FixtureCalibration.from_mapping(payload)

    def list_for_profile(self, profile_id: str) -> tuple[FixtureCalibration, ...]:
        if not self.root.is_dir():
            return ()
        results = []
        for path in sorted(self.root.glob("*.json")):
            try:
                calibration = self.load(path)
            except (FixtureAnalysisError, OSError, json.JSONDecodeError):
                continue
            if calibration.profile_id == profile_id:
                results.append(calibration)
        return tuple(results)


def compare_attitude_streams(
    targets: Sequence[TimedAttitude],
    measurements: Sequence[TimedAttitude],
    *,
    calibration: Optional[FixtureCalibration] = None,
    max_target_gap_s: float = 0.5,
) -> tuple[AttitudeComparison, ...]:
    valid_targets = sorted((sample for sample in targets if sample.valid), key=lambda item: item.monotonic_ns)
    valid_measurements = sorted(
        (sample for sample in measurements if sample.valid), key=lambda item: item.monotonic_ns
    )
    return tuple(
        iter_attitude_comparisons(
            valid_targets,
            valid_measurements,
            calibration=calibration,
            max_target_gap_s=max_target_gap_s,
        )
    )


def iter_attitude_comparisons(
    targets: Iterable[TimedAttitude],
    measurements: Iterable[TimedAttitude],
    *,
    calibration: Optional[FixtureCalibration] = None,
    max_target_gap_s: float = 0.5,
) -> Iterable[AttitudeComparison]:
    """Compare ordered streams with constant memory."""
    target_iterator = (sample for sample in targets if sample.valid)
    try:
        left_target = next(target_iterator)
        right_target = next(target_iterator)
    except StopIteration:
        return
    max_gap_ns = int(max_target_gap_s * 1_000_000_000)
    for raw_measurement in measurements:
        if not raw_measurement.valid:
            continue
        measurement = calibration.apply(raw_measurement) if calibration else raw_measurement
        while right_target.monotonic_ns <= measurement.monotonic_ns:
            left_target = right_target
            try:
                right_target = next(target_iterator)
            except StopIteration:
                return
        if measurement.monotonic_ns < left_target.monotonic_ns:
            continue
        gap_ns = right_target.monotonic_ns - left_target.monotonic_ns
        if gap_ns <= 0 or gap_ns > max_gap_ns:
            continue
        fraction = (measurement.monotonic_ns - left_target.monotonic_ns) / gap_ns
        target = TimedAttitude(
            measurement.monotonic_ns,
            left_target.roll_deg + (right_target.roll_deg - left_target.roll_deg) * fraction,
            left_target.pitch_deg + (right_target.pitch_deg - left_target.pitch_deg) * fraction,
            left_target.yaw_deg
            + _angle_delta(right_target.yaw_deg, left_target.yaw_deg) * fraction,
        )
        error_roll, error_pitch, error_yaw, error_angle = quaternion_attitude_error(
            target.values(), measurement.values()
        )
        yield AttitudeComparison(
            monotonic_ns=measurement.monotonic_ns,
            target_roll_deg=target.roll_deg,
            target_pitch_deg=target.pitch_deg,
            target_yaw_deg=target.yaw_deg,
            measured_roll_deg=measurement.roll_deg,
            measured_pitch_deg=measurement.pitch_deg,
            measured_yaw_deg=measurement.yaw_deg,
            error_roll_deg=error_roll,
            error_pitch_deg=error_pitch,
            error_yaw_deg=error_yaw,
            error_angle_deg=error_angle,
        )


def quaternion_attitude_error(
    target_rpy_deg: Sequence[float],
    measured_rpy_deg: Sequence[float],
) -> tuple[float, float, float, float]:
    target = _euler_zyx_quaternion(*target_rpy_deg)
    measured = _euler_zyx_quaternion(*measured_rpy_deg)
    error = _quat_multiply(_quat_conjugate(target), measured)
    if error[0] < 0:
        error = tuple(-value for value in error)
    roll, pitch, yaw = _quaternion_to_euler_zyx(error)
    angle = math.degrees(2.0 * math.acos(min(1.0, max(-1.0, error[0]))))
    return roll, pitch, yaw, angle


def compute_static_metrics(
    comparisons: Sequence[AttitudeComparison],
    *,
    settle_threshold_deg: float = 1.0,
    settle_dwell_s: float = 2.0,
) -> dict[str, Any]:
    if not comparisons:
        return {"sample_count": 0, "valid": False}
    times = np.asarray(
        [(item.monotonic_ns - comparisons[0].monotonic_ns) / 1e9 for item in comparisons],
        dtype=float,
    )
    axes = {
        "roll": np.asarray([item.error_roll_deg for item in comparisons], dtype=float),
        "pitch": np.asarray([item.error_pitch_deg for item in comparisons], dtype=float),
        "yaw": np.unwrap(
            np.radians([item.error_yaw_deg for item in comparisons])
        ) * 180.0 / math.pi,
        "attitude": np.asarray([item.error_angle_deg for item in comparisons], dtype=float),
    }
    metrics: dict[str, Any] = {
        "sample_count": len(comparisons),
        "valid": True,
        "duration_s": float(times[-1] - times[0]) if len(times) > 1 else 0.0,
        "settling_time_s": _settling_time(
            times,
            axes["attitude"],
            threshold=settle_threshold_deg,
            dwell_s=settle_dwell_s,
        ),
    }
    for name, values in axes.items():
        metrics[name] = _series_metrics(times, values)
    metrics["overshoot_deg"] = max(
        metrics[axis]["overshoot_deg"] for axis in ("roll", "pitch", "yaw")
    )
    return metrics


def compute_dynamic_metrics(
    comparisons: Sequence[AttitudeComparison],
    *,
    primary_axis: str,
) -> dict[str, Any]:
    if primary_axis not in {"roll", "pitch", "yaw"}:
        raise FixtureAnalysisError("dynamic primary axis must be roll, pitch, or yaw")
    if len(comparisons) < 8:
        return {"sample_count": len(comparisons), "valid": False}
    origin_ns = comparisons[0].monotonic_ns
    times = np.asarray([(item.monotonic_ns - origin_ns) / 1e9 for item in comparisons], dtype=float)
    target = np.asarray(
        [getattr(item, f"target_{primary_axis}_deg") for item in comparisons], dtype=float
    )
    measured = np.asarray(
        [getattr(item, f"measured_{primary_axis}_deg") for item in comparisons], dtype=float
    )
    if primary_axis == "yaw":
        target = np.unwrap(np.radians(target)) * 180.0 / math.pi
        measured = np.unwrap(np.radians(measured)) * 180.0 / math.pi
    target_amplitude = _robust_amplitude(target)
    measured_amplitude = _robust_amplitude(measured)
    target_frequency = _estimate_frequency(times, target)
    measured_frequency = _estimate_frequency(times, measured)
    if target_amplitude <= 1e-9 or target_frequency is None:
        return {
            "sample_count": len(comparisons),
            "valid": False,
            "reason": "NO_PERIODIC_EXCITATION",
        }
    phase_deg: Optional[float] = None
    if target_frequency and target_frequency > 0:
        target_phase = _fit_phase(times, target, target_frequency)
        measured_phase = _fit_phase(times, measured, target_frequency)
        if target_phase is not None and measured_phase is not None:
            phase_deg = _angle_delta(measured_phase, target_phase)
    residual = measured - target
    other_axes = [axis for axis in ("roll", "pitch", "yaw") if axis != primary_axis]
    coupling = {
        axis: _robust_amplitude(
            np.asarray(
                [getattr(item, f"measured_{axis}_deg") for item in comparisons],
                dtype=float,
            )
        ) / target_amplitude
        if target_amplitude > 1e-12
        else math.nan
        for axis in other_axes
    }
    return {
        "sample_count": len(comparisons),
        "valid": True,
        "timing_quality": "HOST_ARRIVAL_ENGINEERING_ESTIMATE",
        "target_amplitude_deg": target_amplitude,
        "measured_amplitude_deg": measured_amplitude,
        "amplitude_ratio": measured_amplitude / target_amplitude
        if target_amplitude > 1e-12
        else math.nan,
        "target_frequency_hz": target_frequency,
        "measured_frequency_hz": measured_frequency,
        "frequency_error_hz": (
            measured_frequency - target_frequency
            if target_frequency is not None and measured_frequency is not None
            else None
        ),
        "estimated_phase_deg": phase_deg,
        "estimated_delay_s": (
            phase_deg / 360.0 / target_frequency
            if phase_deg is not None and target_frequency
            else None
        ),
        "residual_rms_deg": float(np.sqrt(np.mean(residual**2))),
        "residual_p95_deg": float(np.percentile(np.abs(residual), 95)),
        "residual_max_deg": float(np.max(np.abs(residual))),
        "cross_axis_coupling_ratio": coupling,
    }


def _series_metrics(times: np.ndarray, values: np.ndarray) -> dict[str, float]:
    count = max(1, int(math.ceil(len(values) * 0.05)))
    slope = 0.0
    if len(values) >= 2 and times[-1] > times[0]:
        slope = float(np.polyfit(times, values, 1)[0])
    return {
        "mean_deg": float(np.mean(values)),
        "std_deg": float(np.std(values)),
        "rms_deg": float(np.sqrt(np.mean(values**2))),
        "p95_abs_deg": float(np.percentile(np.abs(values), 95)),
        "max_abs_deg": float(np.max(np.abs(values))),
        "overshoot_deg": _overshoot(values),
        "drift_deg": float(np.median(values[-count:]) - np.median(values[:count])),
        "trend_deg_per_s": slope,
    }


def _overshoot(values: np.ndarray) -> float:
    count = max(1, int(math.ceil(len(values) * 0.05)))
    initial = float(np.median(values[:count]))
    final = float(np.median(values[-count:]))
    if final > initial:
        return max(0.0, float(np.max(values)) - final)
    if final < initial:
        return max(0.0, final - float(np.min(values)))
    return max(0.0, float(np.max(np.abs(values - final))))


def _settling_time(
    times: np.ndarray,
    values: np.ndarray,
    *,
    threshold: float,
    dwell_s: float,
) -> Optional[float]:
    for index, start in enumerate(times):
        end = int(np.searchsorted(times, start + dwell_s, side="left"))
        if end >= len(times):
            break
        if np.all(values[index : end + 1] <= threshold):
            return float(start)
    return None


def _robust_amplitude(values: np.ndarray) -> float:
    return float((np.percentile(values, 95) - np.percentile(values, 5)) / 2.0)


def _estimate_frequency(times: np.ndarray, values: np.ndarray) -> Optional[float]:
    if len(values) < 4 or times[-1] <= times[0]:
        return None
    centered = values - np.median(values)
    signs = centered >= 0
    crossings = np.flatnonzero(signs[1:] != signs[:-1])
    if len(crossings) < 2:
        return None
    duration = times[crossings[-1] + 1] - times[crossings[0]]
    if duration <= 0:
        return None
    return float((len(crossings) - 1) / (2.0 * duration))


def _fit_phase(times: np.ndarray, values: np.ndarray, frequency_hz: float) -> Optional[float]:
    if len(values) < 4 or frequency_hz <= 0:
        return None
    omega_t = 2.0 * math.pi * frequency_hz * times
    design = np.column_stack((np.sin(omega_t), np.cos(omega_t), np.ones_like(times)))
    coefficients, *_ = np.linalg.lstsq(design, values, rcond=None)
    return math.degrees(math.atan2(float(coefficients[1]), float(coefficients[0])))


def _euler_zyx_quaternion(roll_deg: float, pitch_deg: float, yaw_deg: float) -> tuple[float, float, float, float]:
    roll = math.radians(roll_deg) / 2.0
    pitch = math.radians(pitch_deg) / 2.0
    yaw = math.radians(yaw_deg) / 2.0
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    )


def _quat_conjugate(quaternion: Sequence[float]) -> tuple[float, float, float, float]:
    return (quaternion[0], -quaternion[1], -quaternion[2], -quaternion[3])


def _quat_multiply(left: Sequence[float], right: Sequence[float]) -> tuple[float, float, float, float]:
    lw, lx, ly, lz = left
    rw, rx, ry, rz = right
    return (
        lw * rw - lx * rx - ly * ry - lz * rz,
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
    )


def _quaternion_to_euler_zyx(quaternion: Sequence[float]) -> tuple[float, float, float]:
    w, x, y, z = quaternion
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    pitch_value = 2.0 * (w * y - z * x)
    pitch = math.asin(min(1.0, max(-1.0, pitch_value)))
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return tuple(math.degrees(value) for value in (roll, pitch, yaw))


def _angle_delta(value: float, reference: float) -> float:
    return (float(value) - float(reference) + 180.0) % 360.0 - 180.0


def _circular_mean(values: Sequence[float]) -> float:
    radians = np.radians(np.asarray(values, dtype=float))
    return math.degrees(math.atan2(float(np.mean(np.sin(radians))), float(np.mean(np.cos(radians)))))


def _circular_std(values: Sequence[float]) -> float:
    mean = _circular_mean(values)
    deltas = np.asarray([_angle_delta(value, mean) for value in values], dtype=float)
    return float(np.std(deltas))


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "AttitudeComparison",
    "CALIBRATION_SEQUENCE",
    "CalibrationStage",
    "FixtureAnalysisError",
    "FixtureCalibration",
    "FixtureCalibrationStore",
    "GuidedFixtureCalibration",
    "TimedAttitude",
    "compare_attitude_streams",
    "compute_dynamic_metrics",
    "compute_static_metrics",
    "quaternion_attitude_error",
]
