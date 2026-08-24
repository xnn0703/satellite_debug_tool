"""Canonical product values shared by customer live and playback views."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import Enum
from typing import Generic, Optional, TypeVar


T = TypeVar("T")


class Availability(str, Enum):
    PENDING = "pending"
    VALID = "valid"
    STALE = "stale"
    UNSUPPORTED = "unsupported"


class ProductSource(str, Enum):
    UNKNOWN = "unknown"
    PRODUCT_SERVICE = "product_service"
    LEGACY_V2 = "legacy_v2"


class ValueQuality(str, Enum):
    UNKNOWN = "unknown"
    MEASURED = "measured"
    DERIVED = "derived"


class ControlMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"
    UNKNOWN = "unknown"


class TrackingPhase(str, Enum):
    STANDBY = "standby"
    ACQUIRING = "acquiring"
    FINE_TRACKING = "fine_tracking"
    LOCKED = "locked"
    REACQUIRING = "reacquiring"
    FAULT = "fault"
    UNKNOWN = "unknown"


class NavigationState(str, Enum):
    UNAVAILABLE = "unavailable"
    INITIALIZING = "initializing"
    ALIGNING = "aligning"
    READY = "ready"
    DEGRADED = "degraded"
    FAULT = "fault"
    UNKNOWN = "unknown"


class SatelliteMode(str, Enum):
    UNKNOWN = "unknown"
    GEO = "geo"
    LEO_TLE = "leo_tle"


class NavigationSource(str, Enum):
    NONE = "none"
    ICM42688 = "icm42688"
    MG902 = "mg902"
    BYNAV = "bynav"
    TRACE = "trace"
    IAM20680 = "iam20680"
    MS6222 = "ms6222"
    DEBUG_ORACLE = "debug_oracle"
    UNKNOWN = "unknown"


class ExternalInsState(str, Enum):
    NONE = "none"
    STALE = "stale"
    UNALIGNED = "unaligned"
    ROLL_PITCH_READY = "roll_pitch_ready"
    YAW_ALIGNED = "yaw_aligned"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProductValue(Generic[T]):
    value: Optional[T] = None
    availability: Availability = Availability.UNSUPPORTED
    device_timestamp_ms: Optional[int] = None
    source: ProductSource = ProductSource.UNKNOWN
    received_monotonic_s: Optional[float] = None
    quality: ValueQuality = ValueQuality.UNKNOWN

    @classmethod
    def pending(cls) -> "ProductValue[T]":
        return cls(availability=Availability.PENDING)

    @classmethod
    def valid(
        cls,
        value: T,
        timestamp_ms: Optional[int] = None,
        *,
        source: ProductSource = ProductSource.UNKNOWN,
        received_monotonic_s: Optional[float] = None,
        quality: ValueQuality = ValueQuality.UNKNOWN,
    ) -> "ProductValue[T]":
        return cls(
            value=value,
            availability=Availability.VALID,
            device_timestamp_ms=timestamp_ms,
            source=source,
            received_monotonic_s=received_monotonic_s,
            quality=quality,
        )

    @classmethod
    def stale(
        cls,
        value: Optional[T] = None,
        timestamp_ms: Optional[int] = None,
        *,
        source: ProductSource = ProductSource.UNKNOWN,
        received_monotonic_s: Optional[float] = None,
        quality: ValueQuality = ValueQuality.UNKNOWN,
    ) -> "ProductValue[T]":
        return cls(
            value=value,
            availability=Availability.STALE,
            device_timestamp_ms=timestamp_ms,
            source=source,
            received_monotonic_s=received_monotonic_s,
            quality=quality,
        )

    @classmethod
    def unsupported(cls) -> "ProductValue[T]":
        return cls()

    def age_s(self, now_monotonic_s: float) -> Optional[float]:
        if self.received_monotonic_s is None:
            return None
        return max(0.0, float(now_monotonic_s) - self.received_monotonic_s)


@dataclass(frozen=True)
class DeviceIdentity:
    model: ProductValue[str] = field(default_factory=ProductValue.unsupported)
    serial_number: ProductValue[str] = field(default_factory=ProductValue.unsupported)
    device_uid: ProductValue[str] = field(default_factory=ProductValue.unsupported)
    mac_address: ProductValue[str] = field(default_factory=ProductValue.unsupported)
    mac_source: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    main_firmware: ProductValue[str] = field(default_factory=ProductValue.unsupported)
    boot_firmware: ProductValue[str] = field(default_factory=ProductValue.unsupported)
    protocol_version: ProductValue[str] = field(default_factory=ProductValue.unsupported)


@dataclass(frozen=True)
class ComponentHealth:
    online: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    temperature_c: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    voltage_v: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    version: ProductValue[str] = field(default_factory=ProductValue.unsupported)
    fault: ProductValue[bool] = field(default_factory=ProductValue.unsupported)


@dataclass(frozen=True)
class RfCapabilities:
    rx_frequency_min_mhz: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    rx_frequency_max_mhz: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    tx_frequency_min_mhz: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    tx_frequency_max_mhz: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    polarization_mask: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    independent_polarization: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    tx_control: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    support_full_capture: ProductValue[bool] = field(default_factory=ProductValue.unsupported)


@dataclass(frozen=True)
class NavigationSourceInfo:
    gnss_source: ProductValue[NavigationSource] = field(default_factory=ProductValue.unsupported)
    imu_source: ProductValue[NavigationSource] = field(default_factory=ProductValue.unsupported)
    attitude_source: ProductValue[NavigationSource] = field(default_factory=ProductValue.unsupported)
    external_ins_source: ProductValue[NavigationSource] = field(default_factory=ProductValue.unsupported)
    external_role_mask: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    external_ins_supported: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    external_ins_configured: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    external_data_seen: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    external_online: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    imu_mount_rotation: ProductValue[int] = field(default_factory=ProductValue.unsupported)


@dataclass(frozen=True)
class ExternalInsDiagnostics:
    source: ProductValue[NavigationSource] = field(default_factory=ProductValue.unsupported)
    role_mask: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    online: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    state: ProductValue[ExternalInsState] = field(default_factory=ProductValue.unsupported)
    aligned: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    raw_ins_status: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    raw_position_type: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    gnss_position_type: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    satellite_count: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    inspvax_count: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    rawimuxa_count: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    bestpvt_count: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    inspvax_hz: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    rawimuxa_hz: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    bestpvt_hz: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    ascii_crc_errors: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    binary_crc_errors: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    binary_format_errors: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    rx_overflow_bytes: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    yaw_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    pitch_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    roll_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    yaw_std_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    pitch_std_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    roll_std_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    latitude_std_m: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    longitude_std_m: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    height_std_m: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    velocity_north_std_mps: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    velocity_east_std_mps: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    velocity_up_std_mps: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    solution_age_s: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    differential_age_s: ProductValue[float] = field(default_factory=ProductValue.unsupported)


@dataclass(frozen=True)
class OperationalSnapshot:
    control_mode: ProductValue[ControlMode] = field(default_factory=ProductValue.unsupported)
    tracking_phase: ProductValue[TrackingPhase] = field(default_factory=ProductValue.unsupported)
    locked: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    navigation: ProductValue[NavigationState] = field(default_factory=ProductValue.unsupported)
    gnss_fix: ProductValue[str] = field(default_factory=ProductValue.unsupported)
    roll_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    pitch_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    yaw_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    beam_az_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    beam_el_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    snr_db: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    longitude_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    latitude_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    altitude_m: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    tx_enabled: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    rx_frequency_mhz: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    tx_frequency_mhz: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    rx_polarization: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    tx_polarization: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    modem_online: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    rx_lo_mhz: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    tx_lo_mhz: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    clock_pll_locked: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    tx_pll_locked: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    rx_pll_locked: ProductValue[bool] = field(default_factory=ProductValue.unsupported)
    satellite_mode: ProductValue[SatelliteMode] = field(default_factory=ProductValue.unsupported)
    satellite_longitude_deg: ProductValue[float] = field(default_factory=ProductValue.unsupported)
    satellite_id: ProductValue[int] = field(default_factory=ProductValue.unsupported)
    satellite_name: ProductValue[str] = field(default_factory=ProductValue.unsupported)


@dataclass(frozen=True)
class ProductSnapshot:
    identity: DeviceIdentity = field(default_factory=DeviceIdentity)
    operation: OperationalSnapshot = field(default_factory=OperationalSnapshot)
    converter: ComponentHealth = field(default_factory=ComponentHealth)
    tx_array: ComponentHealth = field(default_factory=ComponentHealth)
    rx_array: ComponentHealth = field(default_factory=ComponentHealth)
    rf_capabilities: RfCapabilities = field(default_factory=RfCapabilities)
    navigation_sources: NavigationSourceInfo = field(default_factory=NavigationSourceInfo)
    external_ins: ExternalInsDiagnostics = field(default_factory=ExternalInsDiagnostics)
    source: str = "none"


def _map_product_values(value, transform):
    if isinstance(value, ProductValue):
        return transform(value)
    if is_dataclass(value):
        return replace(
            value,
            **{
                item.name: _map_product_values(getattr(value, item.name), transform)
                for item in fields(value)
            },
        )
    return value


def stamp_snapshot_source(
    snapshot: ProductSnapshot,
    source: ProductSource,
) -> ProductSnapshot:
    quality = (
        ValueQuality.MEASURED
        if source is ProductSource.PRODUCT_SERVICE
        else ValueQuality.DERIVED
    )

    def stamp(value: ProductValue) -> ProductValue:
        return replace(
            value,
            source=source,
            quality=quality if value.quality is ValueQuality.UNKNOWN else value.quality,
        )

    mapped = _map_product_values(snapshot, stamp)
    return replace(mapped, source=source.value)


def stamp_snapshot_received(snapshot, received_monotonic_s: Optional[float]):
    """Attach one record's host receipt time to its product-value tree."""

    if received_monotonic_s is None:
        return snapshot

    def stamp(value: ProductValue) -> ProductValue:
        if value.device_timestamp_ms is None or value.received_monotonic_s is not None:
            return value
        return replace(
            value,
            received_monotonic_s=float(received_monotonic_s),
        )

    return _map_product_values(snapshot, stamp)


def pending_product_snapshot() -> ProductSnapshot:
    def pending(value: ProductValue) -> ProductValue:
        return replace(value, availability=Availability.PENDING)

    return replace(_map_product_values(ProductSnapshot(), pending), source="pending")
