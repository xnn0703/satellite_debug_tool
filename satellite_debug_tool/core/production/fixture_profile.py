"""Versioned workstation fixture profiles and exclusive control leases."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import threading
from typing import Any, Mapping, Optional
import uuid

from .motion_platform import MotionPlatformConfig, MotionPlatformError, PlatformPose


FIXTURE_PROFILE_SCHEMA = "satellite.fixture-profile"
FIXTURE_PROFILE_SCHEMA_VERSION = 1
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class FixtureProfileError(ValueError):
    pass


class FixtureLeaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class FixtureAxisLimits:
    abs_angle_deg: float
    max_step_deg: float
    max_frequency_hz: Optional[float]
    max_velocity_deg_s: Optional[float]
    max_acceleration_deg_s2: Optional[float]

    @property
    def periodic_complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.max_frequency_hz,
                self.max_velocity_deg_s,
                self.max_acceleration_deg_s2,
            )
        )

    def validate(self, axis: str) -> None:
        _positive_finite(f"{axis}.abs_angle_deg", self.abs_angle_deg)
        _positive_finite(f"{axis}.max_step_deg", self.max_step_deg)
        for name, value in (
            ("max_frequency_hz", self.max_frequency_hz),
            ("max_velocity_deg_s", self.max_velocity_deg_s),
            ("max_acceleration_deg_s2", self.max_acceleration_deg_s2),
        ):
            if value is not None:
                _positive_finite(f"{axis}.{name}", value)

    @classmethod
    def from_mapping(cls, axis: str, payload: Mapping[str, object]) -> "FixtureAxisLimits":
        try:
            result = cls(
                abs_angle_deg=_required_float(payload, "abs_angle_deg"),
                max_step_deg=_required_float(payload, "max_step_deg"),
                max_frequency_hz=_optional_float(payload.get("max_frequency_hz")),
                max_velocity_deg_s=_optional_float(payload.get("max_velocity_deg_s")),
                max_acceleration_deg_s2=_optional_float(
                    payload.get("max_acceleration_deg_s2")
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FixtureProfileError(f"invalid {axis} hard limits: {exc}") from exc
        result.validate(axis)
        return result

    def to_mapping(self) -> dict[str, Optional[float]]:
        return {
            "abs_angle_deg": self.abs_angle_deg,
            "max_step_deg": self.max_step_deg,
            "max_frequency_hz": self.max_frequency_hz,
            "max_velocity_deg_s": self.max_velocity_deg_s,
            "max_acceleration_deg_s2": self.max_acceleration_deg_s2,
        }


@dataclass(frozen=True)
class WorkstationFixtureProfile:
    profile_id: str
    revision: int
    host: str
    port: int
    center_pose: PlatformPose
    reset_pose: PlatformPose
    roll_limits: FixtureAxisLimits
    pitch_limits: FixtureAxisLimits
    yaw_limits: FixtureAxisLimits
    calibration_id: str
    roll_sign: int = 1
    pitch_sign: int = 1
    yaw_sign: int = 1
    z_min_mm: float = 0.0
    z_max_mm: float = 100.0
    minimum_duration_ms: int = 50
    schema: str = FIXTURE_PROFILE_SCHEMA
    schema_version: int = FIXTURE_PROFILE_SCHEMA_VERSION

    @property
    def periodic_limits_complete(self) -> bool:
        return all(
            limits.periodic_complete
            for limits in (self.roll_limits, self.pitch_limits, self.yaw_limits)
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_payload()).encode("utf-8")).hexdigest()

    def validate(self) -> None:
        if self.schema != FIXTURE_PROFILE_SCHEMA:
            raise FixtureProfileError("unsupported fixture profile schema")
        if self.schema_version != FIXTURE_PROFILE_SCHEMA_VERSION:
            raise FixtureProfileError("unsupported fixture profile schema version")
        if not _PROFILE_ID_RE.fullmatch(self.profile_id):
            raise FixtureProfileError("fixture profile_id is not a safe path component")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision <= 0
        ):
            raise FixtureProfileError("fixture profile revision must be a positive integer")
        try:
            ipaddress.IPv4Address(self.host)
        except ipaddress.AddressValueError as exc:
            raise FixtureProfileError("fixture platform host must be a strict IPv4 address") from exc
        if not self.calibration_id.strip():
            raise FixtureProfileError("fixture calibration_id is required")
        for axis, limits in self.axis_limits.items():
            limits.validate(axis)
        try:
            self.to_motion_config().validate()
        except MotionPlatformError as exc:
            raise FixtureProfileError(str(exc)) from exc

    @property
    def axis_limits(self) -> dict[str, FixtureAxisLimits]:
        return {
            "roll": self.roll_limits,
            "pitch": self.pitch_limits,
            "yaw": self.yaw_limits,
        }

    def to_motion_config(self) -> MotionPlatformConfig:
        return MotionPlatformConfig(
            host=self.host,
            port=self.port,
            center_pose=self.center_pose,
            reset_pose=self.reset_pose,
            roll_abs_limit_deg=self.roll_limits.abs_angle_deg,
            pitch_abs_limit_deg=self.pitch_limits.abs_angle_deg,
            yaw_abs_limit_deg=self.yaw_limits.abs_angle_deg,
            z_min_mm=self.z_min_mm,
            z_max_mm=self.z_max_mm,
            max_step_deg=min(
                self.roll_limits.max_step_deg,
                self.pitch_limits.max_step_deg,
                self.yaw_limits.max_step_deg,
            ),
            minimum_duration_ms=self.minimum_duration_ms,
            roll_sign=self.roll_sign,
            pitch_sign=self.pitch_sign,
            yaw_sign=self.yaw_sign,
            calibration_id=self.calibration_id,
            roll_max_step_deg=self.roll_limits.max_step_deg,
            pitch_max_step_deg=self.pitch_limits.max_step_deg,
            yaw_max_step_deg=self.yaw_limits.max_step_deg,
            roll_max_frequency_hz=self.roll_limits.max_frequency_hz,
            pitch_max_frequency_hz=self.pitch_limits.max_frequency_hz,
            yaw_max_frequency_hz=self.yaw_limits.max_frequency_hz,
            roll_max_velocity_deg_s=self.roll_limits.max_velocity_deg_s,
            pitch_max_velocity_deg_s=self.pitch_limits.max_velocity_deg_s,
            yaw_max_velocity_deg_s=self.yaw_limits.max_velocity_deg_s,
            roll_max_acceleration_deg_s2=self.roll_limits.max_acceleration_deg_s2,
            pitch_max_acceleration_deg_s2=self.pitch_limits.max_acceleration_deg_s2,
            yaw_max_acceleration_deg_s2=self.yaw_limits.max_acceleration_deg_s2,
            require_periodic_hard_limits=True,
        )

    def to_payload(self, *, include_hash: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "revision": self.revision,
            "platform": {
                "endpoint": {"host": self.host, "port": self.port},
                "center_pose": _pose_mapping(self.center_pose),
                "reset_pose": _pose_mapping(self.reset_pose),
                "axis_signs": {
                    "roll": self.roll_sign,
                    "pitch": self.pitch_sign,
                    "yaw": self.yaw_sign,
                },
                "z_limits_mm": {"min": self.z_min_mm, "max": self.z_max_mm},
                "minimum_duration_ms": self.minimum_duration_ms,
                "axis_limits": {
                    axis: limits.to_mapping()
                    for axis, limits in self.axis_limits.items()
                },
            },
            "calibration_id": self.calibration_id,
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
    ) -> "WorkstationFixtureProfile":
        try:
            platform = _require_mapping(payload, "platform")
            endpoint = _require_mapping(platform, "endpoint")
            signs = _require_mapping(platform, "axis_signs")
            z_limits = _require_mapping(platform, "z_limits_mm")
            axis_limits = _require_mapping(platform, "axis_limits")
            profile = cls(
                schema=str(payload["schema"]),
                schema_version=_required_int(payload, "schema_version"),
                profile_id=str(payload["profile_id"]),
                revision=_required_int(payload, "revision"),
                host=str(endpoint["host"]),
                port=_required_int(endpoint, "port"),
                center_pose=_pose_from_mapping(_require_mapping(platform, "center_pose")),
                reset_pose=_pose_from_mapping(_require_mapping(platform, "reset_pose")),
                roll_limits=FixtureAxisLimits.from_mapping(
                    "roll", _require_mapping(axis_limits, "roll")
                ),
                pitch_limits=FixtureAxisLimits.from_mapping(
                    "pitch", _require_mapping(axis_limits, "pitch")
                ),
                yaw_limits=FixtureAxisLimits.from_mapping(
                    "yaw", _require_mapping(axis_limits, "yaw")
                ),
                calibration_id=str(payload["calibration_id"]),
                roll_sign=_required_int(signs, "roll"),
                pitch_sign=_required_int(signs, "pitch"),
                yaw_sign=_required_int(signs, "yaw"),
                z_min_mm=_required_float(z_limits, "min"),
                z_max_mm=_required_float(z_limits, "max"),
                minimum_duration_ms=_required_int(platform, "minimum_duration_ms"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FixtureProfileError(f"invalid fixture profile: {exc}") from exc
        profile.validate()
        stored_hash = str(payload.get("sha256", ""))
        if verify_hash:
            if not stored_hash:
                raise FixtureProfileError("fixture profile SHA-256 is missing")
            if stored_hash != profile.sha256:
                raise FixtureProfileError("fixture profile SHA-256 does not match")
        return profile


class FixtureProfileStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or (Path.home() / ".satellite_debug_tool" / "fixture_profiles")

    def list_profiles(self) -> tuple[WorkstationFixtureProfile, ...]:
        if not self.root.is_dir():
            return ()
        profiles: list[WorkstationFixtureProfile] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                profiles.append(self.load(path))
            except (FixtureProfileError, OSError, json.JSONDecodeError):
                continue
        return tuple(profiles)

    def path_for(self, profile_id: str) -> Path:
        if not _PROFILE_ID_RE.fullmatch(str(profile_id)):
            raise FixtureProfileError("fixture profile_id is not a safe path component")
        return self.root / f"{profile_id}.json"

    def load(self, profile: str | Path) -> WorkstationFixtureProfile:
        path = Path(profile)
        if not path.is_absolute() and path.parent == Path("."):
            path = self.path_for(str(profile).removesuffix(".json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise FixtureProfileError("fixture profile root must be an object")
        return WorkstationFixtureProfile.from_mapping(payload)

    def save(
        self,
        profile: WorkstationFixtureProfile,
        *,
        expected_revision: Optional[int] = None,
    ) -> Path:
        profile.validate()
        path = self.path_for(profile.profile_id)
        if path.exists():
            current = self.load(path)
            if expected_revision is None:
                raise FixtureProfileError("existing fixture profile requires expected_revision")
            if current.revision != int(expected_revision):
                raise FixtureProfileError("fixture profile revision conflict")
            if profile.revision <= current.revision:
                raise FixtureProfileError("fixture profile revision must increase")
        elif expected_revision is not None:
            raise FixtureProfileError("fixture profile does not exist for expected_revision")
        self.root.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temp.write_text(
            json.dumps(profile.to_payload(include_hash=True), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
        return path


@dataclass(frozen=True)
class FixtureLeaseHandle:
    token: str
    owner: str


class FixtureControlLease:
    """Process-local exclusive lease shared by batch and fixture-debug control."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handle: Optional[FixtureLeaseHandle] = None

    @property
    def owner(self) -> str:
        with self._lock:
            return "" if self._handle is None else self._handle.owner

    def acquire(self, owner: str) -> FixtureLeaseHandle:
        normalized = str(owner).strip()
        if not normalized:
            raise FixtureLeaseError("fixture lease owner is required")
        with self._lock:
            if self._handle is not None:
                raise FixtureLeaseError(
                    f"fixture control is already held by {self._handle.owner}"
                )
            handle = FixtureLeaseHandle(uuid.uuid4().hex, normalized)
            self._handle = handle
            return handle

    def release(self, handle: FixtureLeaseHandle) -> None:
        with self._lock:
            if self._handle is None:
                return
            if self._handle.token != handle.token:
                raise FixtureLeaseError("fixture lease token does not match")
            self._handle = None

    def held_by(self, handle: Optional[FixtureLeaseHandle]) -> bool:
        if handle is None:
            return False
        with self._lock:
            return self._handle is not None and self._handle.token == handle.token


def _require_mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload[key]
    if not isinstance(value, Mapping):
        raise FixtureProfileError(f"fixture field {key} must be an object")
    return value


def _optional_float(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FixtureProfileError("fixture numeric field must be a JSON number")
    return float(value)


def _required_float(payload: Mapping[str, object], key: str) -> float:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FixtureProfileError(f"fixture field {key} must be a JSON number")
    return float(value)


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise FixtureProfileError(f"fixture field {key} must be an integer")
    return value


def _positive_finite(name: str, value: float) -> None:
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise FixtureProfileError(f"{name} must be positive and finite")


def _pose_mapping(pose: PlatformPose) -> dict[str, float]:
    return {
        "roll_deg": float(pose.roll_deg),
        "pitch_deg": float(pose.pitch_deg),
        "yaw_deg": float(pose.yaw_deg),
        "x_mm": float(pose.x_mm),
        "y_mm": float(pose.y_mm),
        "z_mm": float(pose.z_mm),
    }


def _pose_from_mapping(payload: Mapping[str, object]) -> PlatformPose:
    try:
        return PlatformPose(
            roll_deg=_required_float(payload, "roll_deg"),
            pitch_deg=_required_float(payload, "pitch_deg"),
            yaw_deg=_required_float(payload, "yaw_deg"),
            x_mm=_required_float(payload, "x_mm"),
            y_mm=_required_float(payload, "y_mm"),
            z_mm=_required_float(payload, "z_mm"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FixtureProfileError(f"invalid fixture pose: {exc}") from exc


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "FIXTURE_PROFILE_SCHEMA",
    "FIXTURE_PROFILE_SCHEMA_VERSION",
    "FixtureAxisLimits",
    "FixtureControlLease",
    "FixtureLeaseError",
    "FixtureLeaseHandle",
    "FixtureProfileError",
    "FixtureProfileStore",
    "WorkstationFixtureProfile",
]
