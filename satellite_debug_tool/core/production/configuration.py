"""Versioned production templates, station profiles, and portable bundles."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Optional

from satellite_debug_tool import __version__
from satellite_debug_tool.core.product import production_recipe_product_policy
from satellite_debug_tool.core.production.fixture_analysis import (
    FixtureAnalysisError,
    FixtureCalibration,
    FixtureCalibrationStore,
)
from satellite_debug_tool.core.production.fixture_profile import (
    FixtureProfileError,
    FixtureProfileStore,
    WorkstationFixtureProfile,
)


PRODUCTION_CONFIGURATION_SCHEMA = "satellite-debug-tool/production-configuration"
PRODUCTION_CONFIGURATION_SCHEMA_VERSION = 2
PRODUCTION_BUNDLE_SCHEMA = "satellite-debug-tool/production-configuration-bundle"
PRODUCTION_BUNDLE_SCHEMA_VERSION = 2
REGISTERED_POWER_DRIVERS = {"gwinstek_psw80_27": "GW Instek PSW80-27"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ProductionConfigurationError(ValueError):
    pass


def _finite_positive(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ProductionConfigurationError(f"{name} must be a positive number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProductionConfigurationError(f"{name} must be a positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ProductionConfigurationError(f"{name} must be a positive number")
    return number


def _positive_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProductionConfigurationError("revision must be a positive integer")
    return value


def _stable_id(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not _ID_RE.fullmatch(text):
        raise ProductionConfigurationError(
            f"{name} must use 1-64 letters, digits, '.', '_' or '-'"
        )
    return text


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProductionConfigurationError(f"{name} is required")
    return text


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _copy_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProductionConfigurationError(f"{name} must be an object")
    try:
        return json.loads(_canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ProductionConfigurationError(f"{name} is not valid JSON data") from exc


@dataclass(frozen=True)
class ProductTestTemplate:
    template_id: str
    revision: int
    display_name: str
    product: str
    supply_voltage_v: float
    per_device_current_a: float
    expected: dict[str, Any]
    tests: dict[str, Any]
    duration_policy: dict[str, Any]

    def validate(self) -> None:
        _stable_id(self.template_id, "template_id")
        _positive_revision(self.revision)
        _required_text(self.display_name, "display_name")
        if production_recipe_product_policy(self.product) is None:
            raise ProductionConfigurationError("product is not registered for production")
        _finite_positive(self.supply_voltage_v, "supply_voltage_v")
        _finite_positive(self.per_device_current_a, "per_device_current_a")
        _copy_mapping(self.expected, "expected")
        tests = _copy_mapping(self.tests, "tests")
        if not tests or not any(
            isinstance(value, Mapping) and value.get("enabled") is True
            for value in tests.values()
        ):
            raise ProductionConfigurationError("at least one test must be enabled")
        duration = _copy_mapping(self.duration_policy, "duration_policy")
        minimum = duration.get("minimum_effective_observation_s")
        window = duration.get("report_first_last_window_s")
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= 0:
            raise ProductionConfigurationError(
                "minimum_effective_observation_s must be a positive integer"
            )
        if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
            raise ProductionConfigurationError(
                "report_first_last_window_s must be a positive integer"
            )
        if duration.get("convergence_is_outside_observation") is not True:
            raise ProductionConfigurationError(
                "convergence_is_outside_observation must be true"
            )

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "template_id": self.template_id,
            "revision": self.revision,
            "display_name": self.display_name,
            "product": self.product.strip().lower(),
            "supply_voltage_v": float(self.supply_voltage_v),
            "per_device_current_a": float(self.per_device_current_a),
            "expected": _copy_mapping(self.expected, "expected"),
            "tests": _copy_mapping(self.tests, "tests"),
            "duration_policy": _copy_mapping(
                self.duration_policy, "duration_policy"
            ),
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.to_payload())

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ProductTestTemplate":
        value = cls(
            template_id=_stable_id(payload.get("template_id"), "template_id"),
            revision=_positive_revision(payload.get("revision")),
            display_name=_required_text(payload.get("display_name"), "display_name"),
            product=_required_text(payload.get("product"), "product").lower(),
            supply_voltage_v=_finite_positive(
                payload.get("supply_voltage_v"), "supply_voltage_v"
            ),
            per_device_current_a=_finite_positive(
                payload.get("per_device_current_a"), "per_device_current_a"
            ),
            expected=_copy_mapping(payload.get("expected"), "expected"),
            tests=_copy_mapping(payload.get("tests"), "tests"),
            duration_policy=_copy_mapping(
                payload.get("duration_policy"), "duration_policy"
            ),
        )
        value.validate()
        return value


@dataclass(frozen=True)
class PowerSupplyProfile:
    profile_id: str
    revision: int
    display_name: str
    driver_id: str
    host: str
    port: int
    manufacturer: str
    model: str
    serial_number: str
    rated_voltage_v: float
    rated_current_a: float
    rated_power_w: float
    output_settle_timeout_s: float = 3.0

    def validate(self) -> None:
        _stable_id(self.profile_id, "profile_id")
        _positive_revision(self.revision)
        _required_text(self.display_name, "display_name")
        if self.driver_id not in REGISTERED_POWER_DRIVERS:
            raise ProductionConfigurationError("power driver is not registered")
        try:
            ipaddress.IPv4Address(self.host)
        except ipaddress.AddressValueError as exc:
            raise ProductionConfigurationError("power host must be an IPv4 address") from exc
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise ProductionConfigurationError("power port must be within 1..65535")
        _required_text(self.manufacturer, "manufacturer")
        _required_text(self.model, "model")
        voltage = _finite_positive(self.rated_voltage_v, "rated_voltage_v")
        current = _finite_positive(self.rated_current_a, "rated_current_a")
        power = _finite_positive(self.rated_power_w, "rated_power_w")
        _finite_positive(self.output_settle_timeout_s, "output_settle_timeout_s")
        if self.driver_id == "gwinstek_psw80_27":
            if voltage > 80 or current > 27 or power > 720:
                raise ProductionConfigurationError(
                    "GW Instek PSW80-27 rating cannot exceed 80 V, 27 A, or 720 W"
                )

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "profile_id": self.profile_id,
            "revision": self.revision,
            "display_name": self.display_name,
            "driver_id": self.driver_id,
            "host": self.host,
            "port": self.port,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "serial_number": self.serial_number.strip(),
            "rated_voltage_v": float(self.rated_voltage_v),
            "rated_current_a": float(self.rated_current_a),
            "rated_power_w": float(self.rated_power_w),
            "output_settle_timeout_s": float(self.output_settle_timeout_s),
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.to_payload())

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "PowerSupplyProfile":
        value = cls(
            profile_id=_stable_id(payload.get("profile_id"), "profile_id"),
            revision=_positive_revision(payload.get("revision")),
            display_name=_required_text(payload.get("display_name"), "display_name"),
            driver_id=_required_text(payload.get("driver_id"), "driver_id"),
            host=_required_text(payload.get("host"), "host"),
            port=int(payload.get("port")),
            manufacturer=_required_text(payload.get("manufacturer"), "manufacturer"),
            model=_required_text(payload.get("model"), "model"),
            serial_number=str(payload.get("serial_number", "") or ""),
            rated_voltage_v=_finite_positive(
                payload.get("rated_voltage_v"), "rated_voltage_v"
            ),
            rated_current_a=_finite_positive(
                payload.get("rated_current_a"), "rated_current_a"
            ),
            rated_power_w=_finite_positive(
                payload.get("rated_power_w"), "rated_power_w"
            ),
            output_settle_timeout_s=_finite_positive(
                payload.get("output_settle_timeout_s", 3.0),
                "output_settle_timeout_s",
            ),
        )
        value.validate()
        return value


@dataclass(frozen=True)
class ReferenceSensorProfile:
    """Versioned MS-6222 installation and serial connection contract."""

    profile_id: str
    revision: int
    display_name: str
    driver_id: str
    serial_port: str
    baud_rate: int
    installation_id: str
    serial_number: str = ""
    calibration_id: str = ""
    freshness_timeout_s: float = 1.0
    max_attitude_std_deg: float = 1.0
    stability_window_s: float = 2.0

    def validate(self) -> None:
        _stable_id(self.profile_id, "profile_id")
        _positive_revision(self.revision)
        _required_text(self.display_name, "display_name")
        if self.driver_id != "ms6222_serial_v1":
            raise ProductionConfigurationError("reference driver is not registered")
        _required_text(self.serial_port, "serial_port")
        if self.baud_rate != 460800:
            raise ProductionConfigurationError("MS-6222 baud rate must be 460800")
        _stable_id(self.installation_id, "installation_id")
        _required_text(self.calibration_id, "calibration_id")
        _finite_positive(self.freshness_timeout_s, "freshness_timeout_s")
        _finite_positive(self.max_attitude_std_deg, "max_attitude_std_deg")
        _finite_positive(self.stability_window_s, "stability_window_s")

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "profile_id": self.profile_id,
            "revision": self.revision,
            "display_name": self.display_name,
            "driver_id": self.driver_id,
            "serial_port": self.serial_port,
            "baud_rate": self.baud_rate,
            "installation_id": self.installation_id,
            "serial_number": self.serial_number.strip(),
            "calibration_id": self.calibration_id,
            "freshness_timeout_s": float(self.freshness_timeout_s),
            "max_attitude_std_deg": float(self.max_attitude_std_deg),
            "stability_window_s": float(self.stability_window_s),
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.to_payload())

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ReferenceSensorProfile":
        value = cls(
            profile_id=_stable_id(payload.get("profile_id"), "profile_id"),
            revision=_positive_revision(payload.get("revision")),
            display_name=_required_text(payload.get("display_name"), "display_name"),
            driver_id=_required_text(payload.get("driver_id"), "driver_id"),
            serial_port=_required_text(payload.get("serial_port"), "serial_port"),
            baud_rate=int(payload.get("baud_rate", 460800)),
            installation_id=_stable_id(payload.get("installation_id"), "installation_id"),
            serial_number=str(payload.get("serial_number", "") or ""),
            calibration_id=_required_text(payload.get("calibration_id"), "calibration_id"),
            freshness_timeout_s=_finite_positive(
                payload.get("freshness_timeout_s", 1.0), "freshness_timeout_s"
            ),
            max_attitude_std_deg=_finite_positive(
                payload.get("max_attitude_std_deg", 1.0), "max_attitude_std_deg"
            ),
            stability_window_s=_finite_positive(
                payload.get("stability_window_s", 2.0), "stability_window_s"
            ),
        )
        value.validate()
        return value


@dataclass(frozen=True)
class StationProfile:
    station_profile_id: str
    revision: int
    display_name: str
    power_profile_id: str
    motion_profile_id: str
    reference_profile_id: str
    branch_count: int
    branch_current_a: float
    report_branding: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        _stable_id(self.station_profile_id, "station_profile_id")
        _positive_revision(self.revision)
        _required_text(self.display_name, "display_name")
        _stable_id(self.power_profile_id, "power_profile_id")
        _stable_id(self.motion_profile_id, "motion_profile_id")
        _stable_id(self.reference_profile_id, "reference_profile_id")
        if isinstance(self.branch_count, bool) or not isinstance(self.branch_count, int):
            raise ProductionConfigurationError("branch_count must be an integer")
        if not (1 <= self.branch_count <= 4):
            raise ProductionConfigurationError("branch_count must be within 1..4")
        _finite_positive(self.branch_current_a, "branch_current_a")
        branding = _copy_mapping(self.report_branding, "report_branding")
        allowed = {
            "company_name", "logo_base64", "logo_filename", "header", "footer",
            "tester_role", "reviewer_role",
        }
        if set(branding) - allowed:
            raise ProductionConfigurationError("report_branding contains unsupported fields")
        if any(not isinstance(value, str) for value in branding.values()):
            raise ProductionConfigurationError("report_branding values must be strings")
        if len(branding.get("logo_base64", "")) > 4 * 1024 * 1024:
            raise ProductionConfigurationError("report logo exceeds 3 MiB")

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "station_profile_id": self.station_profile_id,
            "revision": self.revision,
            "display_name": self.display_name,
            "power_profile_id": self.power_profile_id,
            "motion_profile_id": self.motion_profile_id.strip(),
            "reference_profile_id": self.reference_profile_id.strip(),
            "branch_count": self.branch_count,
            "branch_current_a": float(self.branch_current_a),
            "report_branding": _copy_mapping(self.report_branding, "report_branding"),
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.to_payload())

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "StationProfile":
        value = cls(
            station_profile_id=_stable_id(
                payload.get("station_profile_id"), "station_profile_id"
            ),
            revision=_positive_revision(payload.get("revision")),
            display_name=_required_text(payload.get("display_name"), "display_name"),
            power_profile_id=_stable_id(
                payload.get("power_profile_id"), "power_profile_id"
            ),
            motion_profile_id=str(payload.get("motion_profile_id", "") or "").strip(),
            reference_profile_id=str(
                payload.get("reference_profile_id", "") or ""
            ).strip(),
            branch_count=int(payload.get("branch_count")),
            branch_current_a=_finite_positive(
                payload.get("branch_current_a"), "branch_current_a"
            ),
            report_branding={
                str(key): str(value)
                for key, value in _copy_mapping(
                    payload.get("report_branding", {}), "report_branding"
                ).items()
            },
        )
        value.validate()
        return value


@dataclass(frozen=True)
class ResolvedProductionConfiguration:
    product_template: ProductTestTemplate
    power_profile: PowerSupplyProfile
    station_profile: StationProfile
    motion_profile: WorkstationFixtureProfile
    reference_profile: ReferenceSensorProfile
    fixture_calibration: FixtureCalibration
    device_count: int
    total_current_a: float
    total_power_w: float

    @property
    def product(self) -> str:
        return self.product_template.product

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "schema": "satellite-debug-tool/production-batch-configuration",
            "schema_version": 2,
            "product_template": {
                **self.product_template.to_payload(),
                "sha256": self.product_template.sha256,
            },
            "power_profile": {
                **self.power_profile.to_payload(),
                "sha256": self.power_profile.sha256,
            },
            "station_profile": {
                **self.station_profile.to_payload(),
                "sha256": self.station_profile.sha256,
            },
            "motion_profile": self.motion_profile.to_payload(include_hash=True),
            "reference_profile": {
                **self.reference_profile.to_payload(),
                "sha256": self.reference_profile.sha256,
            },
            "fixture_calibration": self.fixture_calibration.to_payload(include_hash=True),
            "device_count": self.device_count,
            "single_device_supply": {
                "voltage_v": self.product_template.supply_voltage_v,
                "current_a": self.product_template.per_device_current_a,
            },
            "combined_supply": {
                "voltage_v": self.product_template.supply_voltage_v,
                "current_a": self.total_current_a,
                "power_w": self.total_power_w,
            },
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.to_snapshot())

    def to_recipe_payload(self) -> dict[str, Any]:
        template = self.product_template
        snapshot = self.to_snapshot()
        motion = self.motion_profile
        steady_duration = int(
            template.duration_policy["minimum_effective_observation_s"]
        )
        sample_period_ms = max(20, int(motion.minimum_duration_ms))
        motion_contract = {
            "driver": "lingjing_udp_v1",
            "command_family": "binary_position_feedback",
            "home_command": "controller_home",
            "center_pose": {
                "roll_deg": motion.center_pose.roll_deg,
                "pitch_deg": motion.center_pose.pitch_deg,
                "yaw_deg": motion.center_pose.yaw_deg,
                "x_mm": motion.center_pose.x_mm,
                "y_mm": motion.center_pose.y_mm,
                "z_mm": motion.center_pose.z_mm,
            },
            "limits": {
                "roll_abs_deg": motion.roll_limits.abs_angle_deg,
                "pitch_abs_deg": motion.pitch_limits.abs_angle_deg,
                "yaw_abs_deg": motion.yaw_limits.abs_angle_deg,
            },
            "profile": {
                "profile_id": motion.profile_id,
                "sample_period_ms": sample_period_ms,
                "steady_duration_s": steady_duration,
                "ramp_in_s": 5,
                "ramp_out_s": 5,
                "roll": {
                    "amplitude_deg": min(10.0, motion.roll_limits.abs_angle_deg),
                    "frequency_hz": min(0.2, motion.roll_limits.max_frequency_hz or 0.2),
                    "phase_deg": 0.0,
                },
                "pitch": {
                    "amplitude_deg": min(10.0, motion.pitch_limits.abs_angle_deg),
                    "frequency_hz": min(0.2, motion.pitch_limits.max_frequency_hz or 0.2),
                    "phase_deg": 90.0,
                },
                "yaw": {
                    "amplitude_deg": min(10.0, motion.yaw_limits.abs_angle_deg),
                    "frequency_hz": min(0.1, motion.yaw_limits.max_frequency_hz or 0.1),
                    "phase_deg": 0.0,
                },
            },
        }
        return {
            "schema_version": 1,
            "recipe_id": f"{template.template_id}-R{template.revision}",
            "product": template.product,
            "target_device_count": self.device_count,
            "expected": _copy_mapping(template.expected, "expected"),
            "fixtures": {
                "power_profile": self.power_profile.profile_id,
                "motion_platform": motion_contract,
                "reference_sensor": self.reference_profile.profile_id,
                "vehicle": {"driver": "manual"},
            },
            "duration_policy": _copy_mapping(
                template.duration_policy, "duration_policy"
            ),
            "tests": _copy_mapping(template.tests, "tests"),
            "resolved_configuration": snapshot,
            "resolved_configuration_sha256": self.sha256,
        }


class ProductionConfigurationStore:
    """Single atomic owner for production templates and station profiles."""

    def __init__(
        self,
        path: Optional[str | Path] = None,
        *,
        fixture_profile_store: FixtureProfileStore | None = None,
        calibration_store: FixtureCalibrationStore | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else (
            Path.home() / ".satellite_debug_tool" / "production_configuration.json"
        )
        self.fixture_profile_store = fixture_profile_store or FixtureProfileStore()
        self.calibration_store = calibration_store or FixtureCalibrationStore()

    def load_catalog(
        self,
    ) -> tuple[
        tuple[ProductTestTemplate, ...],
        tuple[PowerSupplyProfile, ...],
        tuple[StationProfile, ...],
    ]:
        if not self.path.exists():
            return (), (), ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionConfigurationError(
                f"cannot read production configuration: {exc}"
            ) from exc
        products, powers, _references, stations = self._parse_catalog(payload)
        return products, powers, stations

    def load_full_catalog(self) -> tuple[
        tuple[ProductTestTemplate, ...],
        tuple[PowerSupplyProfile, ...],
        tuple[ReferenceSensorProfile, ...],
        tuple[StationProfile, ...],
    ]:
        if not self.path.exists():
            return (), (), (), ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionConfigurationError(
                f"cannot read production configuration: {exc}"
            ) from exc
        return self._parse_catalog(payload)

    def save_catalog(
        self,
        product_templates: tuple[ProductTestTemplate, ...] | list[ProductTestTemplate],
        power_profiles: tuple[PowerSupplyProfile, ...] | list[PowerSupplyProfile],
        station_profiles: tuple[StationProfile, ...] | list[StationProfile],
        reference_profiles: tuple[ReferenceSensorProfile, ...] | list[ReferenceSensorProfile] = (),
    ) -> None:
        products = tuple(product_templates)
        powers = tuple(power_profiles)
        stations = tuple(station_profiles)
        references = tuple(reference_profiles)
        self._validate_catalog(products, powers, references, stations)
        payload = {
            "schema": PRODUCTION_CONFIGURATION_SCHEMA,
            "schema_version": PRODUCTION_CONFIGURATION_SCHEMA_VERSION,
            "product_templates": [item.to_payload() for item in products],
            "power_profiles": [item.to_payload() for item in powers],
            "reference_profiles": [item.to_payload() for item in references],
            "station_profiles": [item.to_payload() for item in stations],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            temporary.write_text(_canonical_json(payload) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            raise ProductionConfigurationError(
                f"cannot save production configuration: {exc}"
            ) from exc

    def resolve(
        self,
        template_id: str,
        station_profile_id: str,
        device_count: int,
    ) -> ResolvedProductionConfiguration:
        if isinstance(device_count, bool) or not isinstance(device_count, int):
            raise ProductionConfigurationError("device_count must be an integer")
        if not (1 <= device_count <= 4):
            raise ProductionConfigurationError("device_count must be within 1..4")
        products, powers, stations = self.load_catalog()
        _products, _powers, references, _stations = self.load_full_catalog()
        product = next((item for item in products if item.template_id == template_id), None)
        station = next(
            (item for item in stations if item.station_profile_id == station_profile_id),
            None,
        )
        if product is None:
            raise ProductionConfigurationError(f"unknown product template: {template_id}")
        if station is None:
            raise ProductionConfigurationError(
                f"unknown station profile: {station_profile_id}"
            )
        power = next(
            (item for item in powers if item.profile_id == station.power_profile_id),
            None,
        )
        if power is None:
            raise ProductionConfigurationError(
                f"station power profile is missing: {station.power_profile_id}"
            )
        reference = next(
            (item for item in references if item.profile_id == station.reference_profile_id),
            None,
        )
        if reference is None:
            raise ProductionConfigurationError(
                f"station reference profile is missing: {station.reference_profile_id}"
            )
        try:
            motion = self.fixture_profile_store.load(station.motion_profile_id)
        except (FixtureProfileError, OSError, json.JSONDecodeError) as exc:
            raise ProductionConfigurationError(
                f"station motion profile is missing: {station.motion_profile_id}"
            ) from exc
        if motion.calibration_id.startswith("PENDING-"):
            raise ProductionConfigurationError("station fixture calibration requires confirmation")
        if reference.calibration_id != motion.calibration_id:
            raise ProductionConfigurationError(
                "reference and motion profiles use different calibration IDs"
            )
        try:
            calibration = self.calibration_store.load(motion.calibration_id)
        except (FixtureAnalysisError, OSError, json.JSONDecodeError) as exc:
            raise ProductionConfigurationError(
                f"fixture calibration is missing: {motion.calibration_id}"
            ) from exc
        if (
            not calibration.confirmed
            or calibration.profile_id != motion.profile_id
            or calibration.profile_sha256 != motion.sha256
        ):
            raise ProductionConfigurationError("fixture calibration is not confirmed for this profile")
        total_current = product.per_device_current_a * device_count
        total_power = product.supply_voltage_v * total_current
        if product.supply_voltage_v > power.rated_voltage_v:
            raise ProductionConfigurationError("required voltage exceeds power profile rating")
        if total_current > power.rated_current_a:
            raise ProductionConfigurationError("required current exceeds power profile rating")
        if total_power > power.rated_power_w:
            raise ProductionConfigurationError("required power exceeds power profile rating")
        if device_count > station.branch_count:
            raise ProductionConfigurationError("device count exceeds station branch count")
        if product.per_device_current_a > station.branch_current_a:
            raise ProductionConfigurationError(
                "per-device current exceeds station branch rating"
            )
        return ResolvedProductionConfiguration(
            product,
            power,
            station,
            motion,
            reference,
            calibration,
            device_count,
            total_current,
            total_power,
        )

    def export_bundle(
        self,
        destination: str | Path,
        *,
        station_profile_id: str = "",
    ) -> Path:
        products, powers, references, stations = self.load_full_catalog()
        if station_profile_id:
            stations = tuple(
                item for item in stations if item.station_profile_id == station_profile_id
            )
            if not stations:
                raise ProductionConfigurationError(
                    f"unknown station profile: {station_profile_id}"
                )
            power_ids = {item.power_profile_id for item in stations}
            powers = tuple(item for item in powers if item.profile_id in power_ids)
            reference_ids = {item.reference_profile_id for item in stations}
            references = tuple(item for item in references if item.profile_id in reference_ids)
        motion_ids = {item.motion_profile_id for item in stations if item.motion_profile_id}
        motion_profiles: list[WorkstationFixtureProfile] = []
        calibrations: list[FixtureCalibration] = []
        for motion_id in sorted(motion_ids):
            try:
                motion = self.fixture_profile_store.load(motion_id)
            except (FixtureProfileError, OSError, json.JSONDecodeError) as exc:
                raise ProductionConfigurationError(
                    f"station motion profile is missing: {motion_id}"
                ) from exc
            motion_profiles.append(motion)
            if not motion.calibration_id.startswith("PENDING-"):
                try:
                    calibrations.append(self.calibration_store.load(motion.calibration_id))
                except (FixtureAnalysisError, OSError, json.JSONDecodeError) as exc:
                    raise ProductionConfigurationError(
                        f"fixture calibration is missing: {motion.calibration_id}"
                    ) from exc
        payload = {
            "schema": PRODUCTION_BUNDLE_SCHEMA,
            "schema_version": PRODUCTION_BUNDLE_SCHEMA_VERSION,
            "exported_utc": datetime.now(timezone.utc).isoformat(),
            "application_version": __version__,
            "product_templates": [
                {**item.to_payload(), "sha256": item.sha256} for item in products
            ],
            "power_profiles": [
                {**item.to_payload(), "sha256": item.sha256} for item in powers
            ],
            "reference_profiles": [
                {**item.to_payload(), "sha256": item.sha256} for item in references
            ],
            "station_profiles": [
                {**item.to_payload(), "sha256": item.sha256} for item in stations
            ],
            "motion_profiles": [
                item.to_payload(include_hash=True) for item in motion_profiles
            ],
            "fixture_calibrations": [
                item.to_payload(include_hash=True) for item in calibrations
            ],
        }
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_canonical_json(payload) + "\n", encoding="utf-8")
        return target

    def import_bundle(self, source: str | Path) -> None:
        try:
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionConfigurationError(f"cannot read configuration bundle: {exc}") from exc
        if payload.get("schema") != PRODUCTION_BUNDLE_SCHEMA:
            raise ProductionConfigurationError("unsupported configuration bundle schema")
        if payload.get("schema_version") not in (1, PRODUCTION_BUNDLE_SCHEMA_VERSION):
            raise ProductionConfigurationError("unsupported configuration bundle version")
        motion_profiles: list[WorkstationFixtureProfile] = []
        calibrations: list[FixtureCalibration] = []
        for raw in payload.get("motion_profiles", []):
            if not isinstance(raw, Mapping):
                raise ProductionConfigurationError("motion_profiles entries must be objects")
            try:
                motion_profiles.append(WorkstationFixtureProfile.from_mapping(raw))
            except FixtureProfileError as exc:
                raise ProductionConfigurationError(str(exc)) from exc
        for raw in payload.get("fixture_calibrations", []):
            if not isinstance(raw, Mapping):
                raise ProductionConfigurationError(
                    "fixture_calibrations entries must be objects"
                )
            try:
                calibrations.append(FixtureCalibration.from_mapping(raw))
            except FixtureAnalysisError as exc:
                raise ProductionConfigurationError(str(exc)) from exc
        incoming = self._parse_catalog(
            {
                "schema": PRODUCTION_CONFIGURATION_SCHEMA,
                "schema_version": PRODUCTION_CONFIGURATION_SCHEMA_VERSION,
                "product_templates": payload.get("product_templates"),
                "power_profiles": payload.get("power_profiles"),
                "reference_profiles": payload.get("reference_profiles", []),
                "station_profiles": payload.get("station_profiles"),
            },
            verify_hashes=True,
        )
        current = self.load_full_catalog()
        merged = tuple(
            self._merge_kind(existing, added, id_attribute)
            for existing, added, id_attribute in zip(
                current,
                incoming,
                ("template_id", "profile_id", "profile_id", "station_profile_id"),
            )
        )
        for profile in motion_profiles:
            existing = None
            try:
                existing = self.fixture_profile_store.load(profile.profile_id)
            except (FixtureProfileError, OSError, json.JSONDecodeError):
                pass
            if existing is not None:
                if profile.revision < existing.revision:
                    raise ProductionConfigurationError(
                        f"configuration {profile.profile_id} revision is older than the installed revision"
                    )
                if profile.revision == existing.revision and profile.sha256 != existing.sha256:
                    raise ProductionConfigurationError(
                        f"configuration conflict for {profile.profile_id} revision {profile.revision}"
                    )
        for calibration in calibrations:
            try:
                existing_calibration = self.calibration_store.load(calibration.calibration_id)
            except (FixtureAnalysisError, OSError, json.JSONDecodeError):
                existing_calibration = None
            if (
                existing_calibration is not None
                and existing_calibration.sha256 != calibration.sha256
            ):
                raise ProductionConfigurationError(
                    f"configuration conflict for calibration {calibration.calibration_id}"
                )
        self.save_catalog(merged[0], merged[1], merged[3], merged[2])
        for profile in motion_profiles:
            try:
                existing = self.fixture_profile_store.load(profile.profile_id)
            except (FixtureProfileError, OSError, json.JSONDecodeError):
                existing = None
            if existing is None or profile.revision > existing.revision:
                self.fixture_profile_store.save(
                    profile,
                    expected_revision=(existing.revision if existing is not None else None),
                )
        for calibration in calibrations:
            try:
                self.calibration_store.load(calibration.calibration_id)
            except (FixtureAnalysisError, OSError, json.JSONDecodeError):
                self.calibration_store.save(calibration)

    @staticmethod
    def _merge_kind(existing, incoming, id_attribute: str):
        result = {getattr(item, id_attribute): item for item in existing}
        for item in incoming:
            item_id = getattr(item, id_attribute)
            current = result.get(item_id)
            if current is None:
                result[item_id] = item
                continue
            if current.revision == item.revision:
                if current.sha256 != item.sha256:
                    raise ProductionConfigurationError(
                        f"configuration conflict for {item_id} revision {item.revision}"
                    )
                continue
            if item.revision < current.revision:
                raise ProductionConfigurationError(
                    f"configuration {item_id} revision is older than the installed revision"
                )
            result[item_id] = item
        return tuple(sorted(result.values(), key=lambda value: getattr(value, id_attribute)))

    @staticmethod
    def _parse_catalog(payload: Mapping[str, Any], *, verify_hashes: bool = False):
        if not isinstance(payload, Mapping):
            raise ProductionConfigurationError("production configuration root must be an object")
        if payload.get("schema") != PRODUCTION_CONFIGURATION_SCHEMA:
            raise ProductionConfigurationError("unsupported production configuration schema")
        schema_version = payload.get("schema_version")
        if schema_version not in (1, PRODUCTION_CONFIGURATION_SCHEMA_VERSION):
            raise ProductionConfigurationError("unsupported production configuration version")
        collections = []
        for key, model in (
            ("product_templates", ProductTestTemplate),
            ("power_profiles", PowerSupplyProfile),
            ("reference_profiles", ReferenceSensorProfile),
            ("station_profiles", StationProfile),
        ):
            raw_items = payload.get(key, [] if schema_version == 1 and key == "reference_profiles" else None)
            if not isinstance(raw_items, list):
                raise ProductionConfigurationError(f"{key} must be an array")
            parsed = []
            for raw in raw_items:
                if not isinstance(raw, Mapping):
                    raise ProductionConfigurationError(f"{key} entries must be objects")
                item = model.from_mapping(raw)
                if verify_hashes and raw.get("sha256") != item.sha256:
                    raise ProductionConfigurationError(
                        f"{key} entry SHA-256 does not match: {raw.get('sha256', '')}"
                    )
                parsed.append(item)
            collections.append(tuple(parsed))
        products, powers, references, stations = collections
        ProductionConfigurationStore._validate_catalog(products, powers, references, stations)
        return products, powers, references, stations

    @staticmethod
    def _validate_catalog(products, powers, references, stations) -> None:
        for values, attribute, label in (
            (products, "template_id", "product template"),
            (powers, "profile_id", "power profile"),
            (references, "profile_id", "reference profile"),
            (stations, "station_profile_id", "station profile"),
        ):
            identifiers = [getattr(item, attribute) for item in values]
            if len(identifiers) != len(set(identifiers)):
                raise ProductionConfigurationError(f"duplicate {label} ID")
            for item in values:
                item.validate()
        power_ids = {item.profile_id for item in powers}
        reference_ids = {item.profile_id for item in references}
        for station in stations:
            if station.power_profile_id not in power_ids:
                raise ProductionConfigurationError(
                    f"station power profile is missing: {station.power_profile_id}"
                )
            if references and station.reference_profile_id not in reference_ids:
                raise ProductionConfigurationError(
                    f"station reference profile is missing: {station.reference_profile_id}"
                )


__all__ = [
    "PowerSupplyProfile",
    "ReferenceSensorProfile",
    "ProductTestTemplate",
    "ProductionConfigurationError",
    "ProductionConfigurationStore",
    "REGISTERED_POWER_DRIVERS",
    "ResolvedProductionConfiguration",
    "StationProfile",
]
