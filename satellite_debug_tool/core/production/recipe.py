"""Immutable, hash-addressed production recipe loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Tuple

from satellite_debug_tool.core.product import production_recipe_product_policy
from satellite_debug_tool.core.motion_platform import CONTINUOUS_TRAJECTORY_PERIOD_MS
from satellite_debug_tool.core.production.tracking_criteria import TrackingCriteriaPolicy


_RECIPE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_RECIPE_BYTES = 1024 * 1024
_FORMAL_MINIMUM_OBSERVATION_S = 3600
TIMED_TEST_IDS = frozenset({
    "static_acquisition", "locked_rocking", "power_on_rocking", "locked_drive", "power_on_drive",
})


def resolve_test_plan(tests: Mapping[str, Any], duration_policy: Mapping[str, Any]) -> tuple[dict, dict]:
    """仅在创建新配方时将旧模板时间字段迁入逐项目合同。"""
    resolved_tests = json.loads(json.dumps(tests))
    policy = json.loads(json.dumps(duration_policy))
    legacy_duration = policy.pop("minimum_effective_observation_s", None)
    legacy_convergence = policy.pop("convergence_is_outside_observation", None)
    if legacy_duration is not None or legacy_convergence is not None:
        policy["timing_migration"] = {
            "source": "legacy_global_duration",
            "minimum_effective_observation_s": legacy_duration,
            "convergence_is_outside_observation": legacy_convergence,
        }
    policy["window_includes_acquisition"] = True
    policy["tracking_criteria"] = TrackingCriteriaPolicy.from_mapping(policy.get("tracking_criteria")).to_payload()
    for test_id, config in resolved_tests.items():
        if not isinstance(config, dict):
            raise ValueError(f"tests.{test_id} must be an object")
        if test_id in TIMED_TEST_IDS and config.get("enabled"):
            config.setdefault("duration_s", legacy_duration)
    return resolved_tests, policy


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
    target_device_count: int
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

        product_policy = production_recipe_product_policy(normalized["product"])
        if product_policy is None or product_policy.production_recipe_product is None:
            raise RecipeValidationError(("product must name a registered production product",))
        normalized["product"] = product_policy.production_recipe_product
        canonical = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

        durations = [int(config["duration_s"]) for test_id, config in normalized["tests"].items()
                     if normalized["schema_version"] == 2 and test_id in TIMED_TEST_IDS
                     and config.get("enabled") and "duration_s" in config]
        minimum_observation_s = min(durations, default=int(
            normalized["duration_policy"].get("minimum_effective_observation_s", _FORMAL_MINIMUM_OBSERVATION_S)))
        return cls(
            recipe_id=str(normalized["recipe_id"]),
            product=str(normalized["product"]),
            schema_version=int(normalized["schema_version"]),
            sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            engineering_only=minimum_observation_s < _FORMAL_MINIMUM_OBSERVATION_S,
            target_device_count=int(normalized.get("target_device_count", 1)),
            _canonical_json=canonical,
        )

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self._canonical_json)

    @property
    def canonical_json(self) -> str:
        return self._canonical_json

    def duration_for(self, test_id: str) -> float:
        payload = self.payload
        if self.schema_version == 1:
            return float(payload["duration_policy"]["minimum_effective_observation_s"])
        config = payload["tests"].get(test_id, {})
        if "duration_s" in config:
            return float(config["duration_s"])
        raise RecipeValidationError((f"tests.{test_id}.duration_s is required",))

    def write_snapshot(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(self._canonical_json + "\n", encoding="utf-8")
        temporary.replace(destination)
        return destination


def _validate_recipe(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    schema = payload.get("schema_version")
    if schema not in {1, 2}:
        errors.append("schema_version must be 1 or 2")

    recipe_id = payload.get("recipe_id")
    if not isinstance(recipe_id, str) or not _RECIPE_ID_RE.fullmatch(recipe_id):
        errors.append("recipe_id must use 1-64 letters, digits, '.', '_' or '-'")

    product = payload.get("product")
    if not isinstance(product, str) or production_recipe_product_policy(product) is None:
        errors.append("product must name a registered production product")

    target_count = payload.get("target_device_count", 1)
    if (
        isinstance(target_count, bool)
        or not isinstance(target_count, int)
        or not (1 <= target_count <= 4)
    ):
        errors.append("target_device_count must be an integer within 1..4")

    for key in ("expected", "fixtures", "tests", "duration_policy"):
        if not isinstance(payload.get(key), dict):
            errors.append(f"{key} must be an object")

    expected = payload.get("expected")
    if isinstance(expected, dict):
        _validate_expected(expected, errors)

    duration = payload.get("duration_policy")
    if isinstance(duration, dict):
        if schema == 1:
            minimum = duration.get("minimum_effective_observation_s")
            if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= 0:
                errors.append("minimum_effective_observation_s must be a positive integer")
        first_last = duration.get("report_first_last_window_s")
        if isinstance(first_last, bool) or not isinstance(first_last, int) or first_last <= 0:
            errors.append("report_first_last_window_s must be a positive integer")
        if schema == 1 and duration.get("convergence_is_outside_observation") is not True:
            errors.append("convergence_is_outside_observation must be true")
        if schema == 2:
            if duration.get("window_includes_acquisition") is not True:
                errors.append("window_includes_acquisition must be true")
            if "minimum_effective_observation_s" in duration or "convergence_is_outside_observation" in duration:
                errors.append("schema 2 uses per-test duration_s and window_includes_acquisition")
            if not isinstance(duration.get("tracking_criteria"), dict):
                errors.append("tracking_criteria must be an object")
            else:
                try:
                    if set(duration["tracking_criteria"]) != set(TrackingCriteriaPolicy.__dataclass_fields__):
                        errors.append("schema 2 tracking_criteria requires every policy field")
                    TrackingCriteriaPolicy.from_mapping(duration["tracking_criteria"])
                except (ValueError, TypeError) as exc:
                    errors.append(str(exc))

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
            if test_id in TIMED_TEST_IDS and (schema == 2 and enabled or "duration_s" in test_config):
                seconds = test_config.get("duration_s")
                if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
                    errors.append(f"tests.{test_id}.duration_s must be a positive integer")
                elif schema == 2 and enabled and isinstance(duration, dict):
                    try:
                        criteria = TrackingCriteriaPolicy.from_mapping(duration.get("tracking_criteria"))
                        if test_id in {"static_acquisition", "power_on_rocking", "power_on_drive"} and seconds <= criteria.acquisition_timeout_s + criteria.recovery_stable_s:
                            errors.append(f"tests.{test_id}.duration_s must exceed acquisition timeout plus recovery confirmation")
                        elif seconds <= criteria.recovery_stable_s:
                            errors.append(f"tests.{test_id}.duration_s must exceed recovery confirmation")
                    except (ValueError, TypeError):
                        pass
        if enabled_count == 0:
            errors.append("at least one test must be enabled")

    fixtures = payload.get("fixtures")
    if isinstance(fixtures, dict):
        power_profile = fixtures.get("power_profile")
        if not isinstance(power_profile, str) or not power_profile.strip():
            errors.append("fixtures.power_profile must be a non-empty string")
        platform = fixtures.get("motion_platform")
        if platform is not None:
            _validate_motion_platform(platform, errors, schema=schema)
        vehicle = fixtures.get("vehicle")
        if not isinstance(vehicle, dict) or vehicle.get("driver") != "manual":
            errors.append("fixtures.vehicle.driver must be 'manual'")

    if schema == 1 and isinstance(duration, dict) and isinstance(fixtures, dict):
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
                "the configured observation duration"
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
        if per_device:
            # 逐设备期望来自该设备注册事实；配方不得自带比较目标。
            if value_type not in {"ip", "string"}:
                errors.append(f"{prefix}.per_device requires type 'ip' or 'string'")
            for comparator in ("value", "range", "one_of", "abs_tol"):
                if comparator in rule:
                    errors.append(
                        f"{prefix}.per_device must not define {comparator}; "
                        "the expectation comes from the registered device facts"
                    )
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


def _validate_motion_platform(platform: Any, errors: list[str], *, schema: int) -> None:
    if not isinstance(platform, dict):
        errors.append("fixtures.motion_platform must be an object")
        return
    if platform.get("command_family") != "binary_position_feedback":
        errors.append(
            "fixtures.motion_platform.command_family must be 'binary_position_feedback'"
        )
    if platform.get("driver") != "lingjing_udp_v1":
        errors.append(
            "fixtures.motion_platform.driver must be 'lingjing_udp_v1'"
        )
    if platform.get("home_command") != "controller_home":
        errors.append(
            "fixtures.motion_platform.home_command must be 'controller_home'"
        )
    for pose_name, expected_z in (("center_pose", 100),):
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
    if sample_period != CONTINUOUS_TRAJECTORY_PERIOD_MS:
        errors.append(
            "motion profile sample_period_ms must equal the fixed "
            f"{CONTINUOUS_TRAJECTORY_PERIOD_MS} ms platform contract"
        )
    if schema == 2:
        if profile.get("execution_mode") != "until_observation_complete":
            errors.append("motion profile execution_mode must be 'until_observation_complete'")
        if "steady_duration_s" in profile:
            errors.append("motion duration is owned by tests.duration_s")
        if profile.get("ramp_out_s", 0) != 0:
            errors.append("continuous motion finishes through controlled stop and center")
    else:
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
