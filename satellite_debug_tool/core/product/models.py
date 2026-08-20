"""Canonical product values shared by customer live and playback views."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Optional, TypeVar


T = TypeVar("T")


class Availability(str, Enum):
    VALID = "valid"
    STALE = "stale"
    UNSUPPORTED = "unsupported"


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

    @classmethod
    def valid(cls, value: T, timestamp_ms: Optional[int] = None) -> "ProductValue[T]":
        return cls(value=value, availability=Availability.VALID, device_timestamp_ms=timestamp_ms)

    @classmethod
    def stale(cls, value: Optional[T] = None, timestamp_ms: Optional[int] = None) -> "ProductValue[T]":
        return cls(value=value, availability=Availability.STALE, device_timestamp_ms=timestamp_ms)

    @classmethod
    def unsupported(cls) -> "ProductValue[T]":
        return cls()


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
