"""Project support recordings onto the fixed customer playback channel set."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Optional

from satellite_debug_tool.core.profile import (
    CHANNEL_ROLE_ANTENNA_AZ,
    CHANNEL_ROLE_ANTENNA_EL,
    CHANNEL_ROLE_GPS_ALT,
    CHANNEL_ROLE_GPS_LAT,
    CHANNEL_ROLE_GPS_LON,
    CHANNEL_ROLE_PITCH,
    CHANNEL_ROLE_ROLL,
    CHANNEL_ROLE_SNR,
    CHANNEL_ROLE_YAW,
    ProfileStore,
)
from satellite_debug_tool.core.protocol import (
    CHANNEL_FLAG_DEFAULT_VISIBLE,
    ChannelDefEntry,
    ChannelSample,
    DataReport,
    DataType,
    ServiceFastState,
    ServiceSlowState,
)

from .timestamps import U32_MODULUS


CUSTOMER_PLAYBACK_HW_TYPE = "customer_playback"


class CustomerPlaybackChannel(IntEnum):
    ROLL = 0
    PITCH = 1
    YAW = 2
    BEAM_AZ = 3
    BEAM_EL = 4
    SNR = 5
    LONGITUDE = 6
    LATITUDE = 7
    ALTITUDE = 8


@dataclass(frozen=True)
class CustomerChannelSpec:
    channel: CustomerPlaybackChannel
    source_name: str
    unit: str
    group_id: int
    display_min: float
    display_max: float
    legacy_role: str


CUSTOMER_CHANNEL_SPECS = (
    CustomerChannelSpec(
        CustomerPlaybackChannel.ROLL,
        "Roll",
        "deg",
        0,
        -180.0,
        180.0,
        CHANNEL_ROLE_ROLL,
    ),
    CustomerChannelSpec(
        CustomerPlaybackChannel.PITCH,
        "Pitch",
        "deg",
        0,
        -90.0,
        90.0,
        CHANNEL_ROLE_PITCH,
    ),
    CustomerChannelSpec(
        CustomerPlaybackChannel.YAW,
        "Yaw",
        "deg",
        0,
        0.0,
        360.0,
        CHANNEL_ROLE_YAW,
    ),
    CustomerChannelSpec(
        CustomerPlaybackChannel.BEAM_AZ,
        "Beam azimuth",
        "deg",
        1,
        0.0,
        360.0,
        CHANNEL_ROLE_ANTENNA_AZ,
    ),
    CustomerChannelSpec(
        CustomerPlaybackChannel.BEAM_EL,
        "Beam elevation",
        "deg",
        1,
        -10.0,
        90.0,
        CHANNEL_ROLE_ANTENNA_EL,
    ),
    CustomerChannelSpec(
        CustomerPlaybackChannel.SNR,
        "SNR",
        "dB",
        2,
        -10.0,
        30.0,
        CHANNEL_ROLE_SNR,
    ),
    CustomerChannelSpec(
        CustomerPlaybackChannel.LONGITUDE,
        "Longitude",
        "deg",
        4,
        -180.0,
        180.0,
        CHANNEL_ROLE_GPS_LON,
    ),
    CustomerChannelSpec(
        CustomerPlaybackChannel.LATITUDE,
        "Latitude",
        "deg",
        4,
        -90.0,
        90.0,
        CHANNEL_ROLE_GPS_LAT,
    ),
    CustomerChannelSpec(
        CustomerPlaybackChannel.ALTITUDE,
        "Altitude",
        "m",
        4,
        -1000.0,
        20000.0,
        CHANNEL_ROLE_GPS_ALT,
    ),
)


def customer_channel_entries(
    translate: Callable[[str], str] = lambda text: text,
) -> list[ChannelDefEntry]:
    """Build the synthetic profile used only by the customer playback chart."""

    return [
        ChannelDefEntry(
            channel_id=int(spec.channel),
            data_type=int(DataType.FLOAT32),
            group_id=spec.group_id,
            flags=CHANNEL_FLAG_DEFAULT_VISIBLE,
            name=translate(spec.source_name),
            unit=spec.unit,
            display_min=spec.display_min,
            display_max=spec.display_max,
        )
        for spec in CUSTOMER_CHANNEL_SPECS
    ]


class _PlaybackU32Mapper:
    """Map u32 uptime values near a high-water mark while tolerating reordering."""

    def __init__(self) -> None:
        self._high_watermark: Optional[int] = None

    def add(self, timestamp_ms: int) -> int:
        raw = int(timestamp_ms) & (U32_MODULUS - 1)
        if self._high_watermark is None:
            self._high_watermark = raw
            return raw
        epoch = self._high_watermark - (self._high_watermark % U32_MODULUS)
        candidates = (
            epoch + raw,
            epoch + raw - U32_MODULUS,
            epoch + raw + U32_MODULUS,
        )
        mapped = min(candidates, key=lambda value: abs(value - self._high_watermark))
        if mapped < 0:
            mapped += U32_MODULUS
        if mapped > self._high_watermark:
            self._high_watermark = mapped
        return mapped


class CustomerPlaybackProjector:
    """Convert product-service or legacy Debug records into nine stable curves."""

    def __init__(
        self,
        *,
        source_profile: Optional[ProfileStore] = None,
        source_hw_type: Optional[str] = None,
        prefer_product_service: bool = False,
    ) -> None:
        self._prefer_product_service = bool(prefer_product_service)
        self._clock = _PlaybackU32Mapper()
        self._legacy_ids: dict[int, int] = {}
        if source_profile is not None and source_hw_type:
            for spec in CUSTOMER_CHANNEL_SPECS:
                entry = source_profile.find_channel_by_role(
                    source_hw_type, spec.legacy_role
                )
                if entry is not None:
                    self._legacy_ids[entry.channel_id] = int(spec.channel)

    def project(self, record: object) -> Optional[DataReport]:
        if isinstance(record, ServiceFastState):
            return self._project_fast(record)
        if isinstance(record, ServiceSlowState):
            return self._project_slow(record)
        if isinstance(record, DataReport) and not self._prefer_product_service:
            return self._project_legacy(record)
        return None

    def _report(
        self, timestamp: int, samples: list[ChannelSample]
    ) -> Optional[DataReport]:
        if not samples:
            return None
        return DataReport(self._clock.add(timestamp), samples)

    def _project_fast(self, record: ServiceFastState) -> Optional[DataReport]:
        fields = (
            (6, CustomerPlaybackChannel.ROLL, record.roll_deg),
            (7, CustomerPlaybackChannel.PITCH, record.pitch_deg),
            (8, CustomerPlaybackChannel.YAW, record.yaw_deg),
            (9, CustomerPlaybackChannel.BEAM_AZ, record.beam_az_deg),
            (10, CustomerPlaybackChannel.BEAM_EL, record.beam_el_deg),
            (11, CustomerPlaybackChannel.SNR, record.snr_db),
        )
        samples = [
            ChannelSample(int(channel), float(value))
            for bit, channel, value in fields
            if record.valid_mask & (1 << bit)
        ]
        return self._report(record.timestamp, samples)

    def _project_slow(self, record: ServiceSlowState) -> Optional[DataReport]:
        fields = (
            (0, CustomerPlaybackChannel.LATITUDE, record.latitude_deg),
            (1, CustomerPlaybackChannel.LONGITUDE, record.longitude_deg),
            (2, CustomerPlaybackChannel.ALTITUDE, record.altitude_m),
        )
        samples = [
            ChannelSample(int(channel), float(value))
            for bit, channel, value in fields
            if record.valid_mask & (1 << bit)
        ]
        return self._report(record.timestamp, samples)

    def _project_legacy(self, record: DataReport) -> Optional[DataReport]:
        samples = [
            ChannelSample(self._legacy_ids[sample.channel_id], float(sample.value))
            for sample in record.samples
            if sample.channel_id in self._legacy_ids
        ]
        return self._report(record.timestamp, samples)
