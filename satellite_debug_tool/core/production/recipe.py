"""Immutable, hash-addressed production recipe loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Tuple


_RECIPE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_RECIPE_BYTES = 1024 * 1024
_FORMAL_MINIMUM_OBSERVATION_S = 3600


class RecipeValidationError(ValueError):
    def __init__(self, errors: Tuple[str, ...] | list[str]) -> None:
        self.errors = tuple(str(error) for error in errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class ProductionRecipe:
    recipe_id: str
    product: str
    schema_version: int
    sha256: str
    engineering_only: bool
    _canonical_json: str

    @classmethod
    def from_path(cls, path: str | Path) -> "ProductionRecipe":
        source = Path(path)
        try:
            if source.stat().st_size > _MAX_RECIPE_BYTES:
                raise RecipeValidationError(("recipe file exceeds 1 MiB",))
            payload = json.loads(source.read_text(encoding="utf-8"))
        except RecipeValidationError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecipeValidationError((f"cannot read recipe: {exc}",)) from exc
        return cls.from_mapping(payload)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ProductionRecipe":
        if not isinstance(payload, Mapping):
            raise RecipeValidationError(("recipe root must be an object",))
        try:
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise RecipeValidationError((f"recipe is not valid JSON data: {exc}",)) from exc
        normalized = json.loads(canonical)
        errors = _validate_recipe(normalized)
        if errors:
            raise RecipeValidationError(errors)

        minimum_observation_s = int(
            normalized["duration_policy"]["minimum_effective_observation_s"]
        )
        return cls(
            recipe_id=str(normalized["recipe_id"]),
            product=str(normalized["product"]).lower(),
            schema_version=int(normalized["schema_version"]),
            sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            engineering_only=minimum_observation_s < _FORMAL_MINIMUM_OBSERVATION_S,
            _canonical_json=canonical,
        )

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self._canonical_json)

    @property
    def canonical_json(self) -> str:
        return self._canonical_json

    def write_snapshot(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(self._canonical_json + "\n", encoding="utf-8")
        temporary.replace(destination)
        return destination


def _validate_recipe(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    recipe_id = payload.get("recipe_id")
    if not isinstance(recipe_id, str) or not _RECIPE_ID_RE.fullmatch(recipe_id):
        errors.append("recipe_id must use 1-64 letters, digits, '.', '_' or '-'")

    product = payload.get("product")
    if not isinstance(product, str) or product.lower() != "afd01":
        errors.append("product must be 'afd01' for M19 v1")

    for key in ("expected", "fixtures", "tests", "duration_policy"):
        if not isinstance(payload.get(key), dict):
            errors.append(f"{key} must be an object")

    expected = payload.get("expected")
    if isinstance(expected, dict):
        _validate_expected(expected, errors)

    duration = payload.get("duration_policy")
    if isinstance(duration, dict):
        minimum = duration.get("minimum_effective_observation_s")
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= 0:
            errors.append("minimum_effective_observation_s must be a positive integer")
        first_last = duration.get("report_first_last_window_s")
        if isinstance(first_last, bool) or not isinstance(first_last, int) or first_last <= 0:
            errors.append("report_first_last_window_s must be a positive integer")
        if duration.get("convergence_is_outside_observation") is not True:
            errors.append("convergence_is_outside_observation must be true")

    tests = payload.get("tests")
    if isinstance(tests, dict):
        enabled_count = 0
        for test_id, test_config in tests.items():
            if not isinstance(test_id, str) or not test_id:
                errors.append("test IDs must be non-empty strings")
                continue
            if not isinstance(test_config, dict):
                errors.append(f"tests.{test_id} must be an object")
                continue
            enabled = test_config.get("enabled")
            if not isinstance(enabled, bool):
                errors.append(f"tests.{test_id}.enabled must be boolean")
            elif enabled:
                enabled_count += 1
        if enabled_count == 0:
            errors.append("at least one test must be enabled")

    fixtures = payload.get("fixtures")
    if isinstance(fixtures, dict):
        power_profile = fixtures.get("power_profile")
        if not isinstance(power_profile, str) or not power_profile.strip():
            errors.append("fixtures.power_profile must be a non-empty string")
        platform = fixtures.get("motion_platform")
        if platform is not None:
            _validate_motion_platform(platform, errors)
        vehicle = fixtures.get("vehicle")
        if not isinstance(vehicle, dict) or vehicle.get("driver") != "manual":
            errors.append("fixtures.vehicle.driver must be 'manual'")

    if isinstance(duration, dict) and isinstance(fixtures, dict):
        platform = fixtures.get("motion_platform")
        profile = platform.get("profile") if isinstance(platform, dict) else None
        steady = profile.get("steady_duration_s") if isinstance(profile, dict) else None
        minimum = duration.get("minimum_effective_observation_s")
        if (
            isinstance(steady, int)
            and not isinstance(steady, bool)
            and isinstance(minimum, int)
            and not isinstance(minimum, bool)
            and steady < minimum
        ):
            errors.append(
                "motion profile steady_duration_s cannot be shorter than "
                "minimum_effective_observation_s"
            )
    return errors


def _validate_expected(expected: dict[str, Any], errors: list[str]) -> None:
    firmware = expected.get("main_firmware")
    if not isinstance(firmware, dict):
        errors.append("expected.main_firmware must be an object")
    else:
        match = firmware.get("match")
        if match not in {"exact", "optional"}:
            errors.append("expected.main_firmware.match must be 'exact' or 'optional'")
        if match == "exact" and not str(firmware.get("value", "")).strip():
            errors.append("expected.main_firmware.value is required for exact matching")

    parameters = expected.get("parameters")
    if not isinstance(parameters, dict):
        errors.append("expected.parameters must be an object")
        return
    allowed_types = {"bool", "float", "int", "ip", "string", "uint8"}
    for name, rule in parameters.items():
        if not isinstance(name, str) or not name:
            errors.append("parameter names must be non-empty strings")
            continue
        prefix = f"expected.parameters.{name}"
        if not isinstance(rule, dict):
            errors.append(f"{prefix} must be an object")
            continue
        value_type = rule.get("type")
        if value_type not in allowed_types:
            errors.append(f"{prefix}.type is unsupported")
        per_device = rule.get("per_device", False)
        if not isinstance(per_device, bool):
            errors.append(f"{prefix}.per_device must be boolean")
        ignore = rule.get("ignore", False)
        if not isinstance(ignore, bool):
            errors.append(f"{prefix}.ignore must be boolean")
            ignore = False

        comparators = []
        if "range" in rule:
            comparators.append("range")
            limits = rule["range"]
            if (
                not isinstance(limits, list)
                or len(limits) != 2
                or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in limits)
                or (len(limits) == 2 and limits[0] > limits[1])
            ):
                errors.append(f"{prefix}.range must be [minimum, maximum]")
        if "one_of" in rule:
            comparators.append("one_of")
            values = rule["one_of"]
            if not isinstance(values, list) or not values:
                errors.append(f"{prefix}.one_of must be a non-empty array")
        if "abs_tol" in rule:
            comparators.append("abs_tol")
            tolerance = rule["abs_tol"]
            if (
                isinstance(tolerance, bool)
                or not isinstance(tolerance, (int, float))
                or tolerance < 0
            ):
                errors.append(f"{prefix}.abs_tol must be a non-negative number")
            if "value" not in rule:
                errors.append(f"{prefix}.value is required with abs_tol")
        if rule.get("match") == "exact" or (
            "value" in rule and "abs_tol" not in rule
        ):
            comparators.append("exact")
        elif "match" in rule and rule.get("match") != "exact":
            errors.append(f"{prefix}.match must be 'exact'")
        if ignore:
            comparators.append("ignore")
        if len(comparators) > 1:
            errors.append(f"{prefix} defines conflicting comparison rules")
        if not comparators and not per_device:
            errors.append(f"{prefix} must define a comparison rule or per_device")


def _validate_motion_platform(platform: Any, errors: list[str]) -> None:
    if not isinstance(platform, dict):
        errors.append("fixtures.motion_platform must be an object")
        return
    if platform.get("command_family") != "A6T":
        errors.append("fixtures.motion_platform.command_family must be 'A6T'")
    if platform.get("driver") != "lingjing_udp_v0_3":
        errors.append(
            "fixtures.motion_platform.driver must be 'lingjing_udp_v0_3'"
        )
    for pose_name, expected_z in (("center_pose", 100), ("reset_pose", 0)):
        pose = platform.get(pose_name)
        if not isinstance(pose, dict):
            errors.append(f"fixtures.motion_platform.{pose_name} must be an object")
            continue
        required = ("roll_deg", "pitch_deg", "yaw_deg", "x_mm", "y_mm", "z_mm")
        if any(
            isinstance(pose.get(key), bool)
            or not isinstance(pose.get(key), (int, float))
            for key in required
        ):
            errors.append(f"fixtures.motion_platform.{pose_name} is incomplete")
        elif float(pose["z_mm"]) != float(expected_z):
            errors.append(f"fixtures.motion_platform.{pose_name}.z_mm must be {expected_z}")

    limits = platform.get("limits")
    axis_limits: dict[str, float] = {}
    if not isinstance(limits, dict):
        errors.append("fixtures.motion_platform.limits must be an object")
    else:
        for axis, key in (
            ("roll", "roll_abs_deg"),
            ("pitch", "pitch_abs_deg"),
            ("yaw", "yaw_abs_deg"),
        ):
            value = limits.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
            ):
                errors.append(f"fixtures.motion_platform.limits.{key} must be positive")
            else:
                axis_limits[axis] = float(value)

    profile = platform.get("profile")
    if not isinstance(profile, dict):
        errors.append("fixtures.motion_platform.profile must be an object")
        return
    if not isinstance(profile.get("profile_id"), str) or not profile["profile_id"].strip():
        errors.append("motion profile profile_id must be a non-empty string")
    sample_period = profile.get("sample_period_ms")
    if isinstance(sample_period, bool) or not isinstance(sample_period, int) or sample_period <= 0:
        errors.append("motion profile sample_period_ms must be a positive integer")
    steady = profile.get("steady_duration_s")
    if isinstance(steady, bool) or not isinstance(steady, int) or steady <= 0:
        errors.append("motion profile steady_duration_s must be a positive integer")
    for key in ("ramp_in_s", "ramp_out_s"):
        if key not in profile:
            continue
        value = profile[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            errors.append(f"motion profile {key} must be a non-negative integer")
    for axis in ("roll", "pitch", "yaw"):
        config = profile.get(axis)
        if not isinstance(config, dict):
            errors.append(f"motion profile {axis} must be an object")
            continue
        for key in ("amplitude_deg", "frequency_hz", "phase_deg"):
            value = config.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"motion profile {axis}.{key} must be numeric")
        amplitude = config.get("amplitude_deg")
        if (
            axis in axis_limits
            and isinstance(amplitude, (int, float))
            and not isinstance(amplitude, bool)
            and abs(float(amplitude)) > axis_limits[axis]
        ):
            errors.append(f"motion profile {axis}.amplitude_deg exceeds its limit")
        frequency = config.get("frequency_hz")
        if (
            isinstance(frequency, (int, float))
            and not isinstance(frequency, bool)
            and frequency <= 0
        ):
            errors.append(f"motion profile {axis}.frequency_hz must be positive")
