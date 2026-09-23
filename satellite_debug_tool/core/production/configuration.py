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


PRODUCTION_CONFIGURATION_SCHEMA = "satellite-debug-tool/production-configuration"
PRODUCTION_CONFIGURATION_SCHEMA_VERSION = 1
PRODUCTION_BUNDLE_SCHEMA = "satellite-debug-tool/production-configuration-bundle"
PRODUCTION_BUNDLE_SCHEMA_VERSION = 1
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
        if self.motion_profile_id:
            _stable_id(self.motion_profile_id, "motion_profile_id")
        if self.reference_profile_id:
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
    device_count: int
    total_current_a: float
    total_power_w: float

    @property
    def product(self) -> str:
        return self.product_template.product

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "schema": "satellite-debug-tool/production-batch-configuration",
            "schema_version": 1,
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
        return {
            "schema_version": 1,
            "recipe_id": f"{template.template_id}-R{template.revision}",
            "product": template.product,
            "target_device_count": self.device_count,
            "expected": _copy_mapping(template.expected, "expected"),
            "fixtures": {
                "power_profile": self.power_profile.profile_id,
                "motion_platform": None,
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

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path is not None else (
            Path.home() / ".satellite_debug_tool" / "production_configuration.json"
        )

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
        return self._parse_catalog(payload)

    def save_catalog(
        self,
        product_templates: tuple[ProductTestTemplate, ...] | list[ProductTestTemplate],
        power_profiles: tuple[PowerSupplyProfile, ...] | list[PowerSupplyProfile],
        station_profiles: tuple[StationProfile, ...] | list[StationProfile],
    ) -> None:
        products = tuple(product_templates)
        powers = tuple(power_profiles)
        stations = tuple(station_profiles)
        self._validate_catalog(products, powers, stations)
        payload = {
            "schema": PRODUCTION_CONFIGURATION_SCHEMA,
            "schema_version": PRODUCTION_CONFIGURATION_SCHEMA_VERSION,
            "product_templates": [item.to_payload() for item in products],
            "power_profiles": [item.to_payload() for item in powers],
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
        products, powers, stations = self.load_catalog()
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
            "station_profiles": [
                {**item.to_payload(), "sha256": item.sha256} for item in stations
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
        if payload.get("schema_version") != PRODUCTION_BUNDLE_SCHEMA_VERSION:
            raise ProductionConfigurationError("unsupported configuration bundle version")
        incoming = self._parse_catalog(
            {
                "schema": PRODUCTION_CONFIGURATION_SCHEMA,
                "schema_version": PRODUCTION_CONFIGURATION_SCHEMA_VERSION,
                "product_templates": payload.get("product_templates"),
                "power_profiles": payload.get("power_profiles"),
                "station_profiles": payload.get("station_profiles"),
            },
            verify_hashes=True,
        )
        current = self.load_catalog()
        merged = tuple(
            self._merge_kind(existing, added, id_attribute)
            for existing, added, id_attribute in zip(
                current,
                incoming,
                ("template_id", "profile_id", "station_profile_id"),
            )
        )
        self.save_catalog(*merged)

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
        if payload.get("schema_version") != PRODUCTION_CONFIGURATION_SCHEMA_VERSION:
            raise ProductionConfigurationError("unsupported production configuration version")
        collections = []
        for key, model in (
            ("product_templates", ProductTestTemplate),
            ("power_profiles", PowerSupplyProfile),
            ("station_profiles", StationProfile),
        ):
            raw_items = payload.get(key)
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
        products, powers, stations = collections
        ProductionConfigurationStore._validate_catalog(products, powers, stations)
        return products, powers, stations

    @staticmethod
    def _validate_catalog(products, powers, stations) -> None:
        for values, attribute, label in (
            (products, "template_id", "product template"),
            (powers, "profile_id", "power profile"),
            (stations, "station_profile_id", "station profile"),
        ):
            identifiers = [getattr(item, attribute) for item in values]
            if len(identifiers) != len(set(identifiers)):
                raise ProductionConfigurationError(f"duplicate {label} ID")
            for item in values:
                item.validate()
        power_ids = {item.profile_id for item in powers}
        for station in stations:
            if station.power_profile_id not in power_ids:
                raise ProductionConfigurationError(
                    f"station power profile is missing: {station.power_profile_id}"
                )


__all__ = [
    "PowerSupplyProfile",
    "ProductTestTemplate",
    "ProductionConfigurationError",
    "ProductionConfigurationStore",
    "REGISTERED_POWER_DRIVERS",
    "ResolvedProductionConfiguration",
    "StationProfile",
]
