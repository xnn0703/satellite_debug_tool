"""Passive MS-6222 binary protocol parser aligned with the firmware C codec."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
import struct
import time
from typing import Optional, Union


MS6222_HEAD = b"\xAA\x44"
MS6222_INS_HEAD3 = 0x12
MS6222_GNSS_HEAD3 = 0xBD
MS6222_RAWIMU_HEAD3 = 0x13
MS6222_INS_HEADER_LEN = 0x1C
MS6222_GNSS_HEADER_LEN = 0x0C
MS6222_INS_MSG_LEN = 0x7E
MS6222_GNSS_MSG_LEN = 0x6C
MS6222_RAWIMU_MSG_LEN = 0x28
MS6222_INS_FRAME_LEN = 158
MS6222_GNSS_FRAME_LEN = 124
# Firmware uses sizeof(ms6222_rawimusb_msg_t): 4-byte prefix, 48-byte
# packed body (whose header_len field remains 0x28), and a 4-byte CRC.
MS6222_RAWIMU_FRAME_LEN = 56
MS6222_RAWIMU_ACCEL_SCALE = 52621241.37840640
MS6222_RAWIMU_GYRO_SCALE = 1499226.41161356
MS6222_G_TO_M_S2 = 9.80147


class Ms6222FrameType(str, Enum):
    INSPVAXB = "inspvaxb"
    GNSS = "gnss"
    RAWIMUSB = "rawimusb"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Ms6222InsRecord:
    gps_week: int
    gps_ms: int
    message_sequence: int
    time_state: int
    gnss_state: int
    fix_type: int
    ekf_state: int
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    height_m: float
    velocity_n_m_s: float
    velocity_e_m_s: float
    velocity_u_m_s: float
    roll_deg: float
    pitch_deg: float
    yaw_deg: float
    latitude_std: float
    longitude_std: float
    altitude_std: float
    velocity_n_std: float
    velocity_e_std: float
    velocity_u_std: float
    roll_std_deg: float
    pitch_std_deg: float
    yaw_std_deg: float


@dataclass(frozen=True)
class Ms6222GnssRecord:
    gps_week: int
    gps_ms: int
    time_state: int
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: float
    fix_sign: int
    used_satellites: int
    tracked_satellites: int
    base_station_count: int
    fix_frequency: int
    pdop: float
    hdop: float
    longitude_deg: float
    latitude_deg: float
    altitude_m: float
    velocity_n_m_s: float
    velocity_e_m_s: float
    velocity_u_m_s: float
    position_x_std: float
    position_y_std: float
    position_z_std: float
    velocity_n_std: float
    velocity_e_std: float
    velocity_u_std: float
    altitude_error: float
    ground_speed_m_s: float
    yaw_deg: float
    yaw_sign: int
    baseline_length_mm: int
    secondary_antenna_satellites: int
    heading_std_deg: float


@dataclass(frozen=True)
class Ms6222RawImuRecord:
    gps_week: int
    gps_ms: int
    secondary_gps_week: int
    gps_seconds: float
    imu_status: int
    imu_status_valid: bool
    temperature_c: float
    accel_x_m_s2: float
    accel_y_m_s2: float
    accel_z_m_s2: float
    gyro_x_rad_s: float
    gyro_y_rad_s: float
    gyro_z_rad_s: float


Ms6222Record = Union[Ms6222InsRecord, Ms6222GnssRecord, Ms6222RawImuRecord]


@dataclass(frozen=True)
class Ms6222FrameEnvelope:
    frame_type: Ms6222FrameType
    raw: bytes
    host_monotonic_ns: int
    host_wall_time_ns: int
    crc_valid: bool
    protocol_version: int
    record: Optional[Ms6222Record]
    error: str = ""

    @property
    def valid(self) -> bool:
        return self.crc_valid and self.record is not None and not self.error

    @property
    def gps_time_s(self) -> Optional[float]:
        record = self.record
        if record is None:
            return None
        gps_week = getattr(record, "gps_week", None)
        gps_ms = getattr(record, "gps_ms", None)
        if gps_week is None or gps_ms is None:
            return None
        return float(gps_week) * 604800.0 + float(gps_ms) / 1000.0


@dataclass(frozen=True)
class Ms6222ParserStatistics:
    bytes_received: int = 0
    noise_bytes: int = 0
    valid_frames: int = 0
    invalid_frames: int = 0
    crc_errors: int = 0
    length_errors: int = 0
    ins_frames: int = 0
    gnss_frames: int = 0
    rawimu_frames: int = 0
    buffer_recoveries: int = 0


class Ms6222StreamParser:
    def __init__(self, *, max_buffer_bytes: int = 65536) -> None:
        if max_buffer_bytes < MS6222_INS_FRAME_LEN * 2:
            raise ValueError("MS-6222 parser buffer is too small")
        self._buffer = bytearray()
        self._max_buffer_bytes = int(max_buffer_bytes)
        self._statistics = Ms6222ParserStatistics()

    @property
    def statistics(self) -> Ms6222ParserStatistics:
        return self._statistics

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self._buffer.clear()
        self._statistics = Ms6222ParserStatistics()

    def feed(
        self,
        data: bytes,
        *,
        host_monotonic_ns: Optional[int] = None,
        host_wall_time_ns: Optional[int] = None,
    ) -> tuple[Ms6222FrameEnvelope, ...]:
        chunk = bytes(data)
        if not chunk:
            return ()
        monotonic_ns = time.monotonic_ns() if host_monotonic_ns is None else int(host_monotonic_ns)
        wall_ns = time.time_ns() if host_wall_time_ns is None else int(host_wall_time_ns)
        self._buffer.extend(chunk)
        self._statistics = replace(
            self._statistics,
            bytes_received=self._statistics.bytes_received + len(chunk),
        )
        events: list[Ms6222FrameEnvelope] = []

        if len(self._buffer) > self._max_buffer_bytes:
            overflow = len(self._buffer) - 2
            raw = bytes(self._buffer[:overflow])
            del self._buffer[:overflow]
            events.append(
                self._invalid_event(
                    Ms6222FrameType.UNKNOWN,
                    raw,
                    monotonic_ns,
                    wall_ns,
                    "buffer_overflow",
                )
            )
            self._statistics = replace(
                self._statistics,
                noise_bytes=self._statistics.noise_bytes + len(raw),
                buffer_recoveries=self._statistics.buffer_recoveries + 1,
            )

        while self._buffer:
            header_index = self._buffer.find(MS6222_HEAD)
            if header_index < 0:
                keep = 1 if self._buffer[-1] == MS6222_HEAD[0] else 0
                noise_len = len(self._buffer) - keep
                if noise_len:
                    raw = bytes(self._buffer[:noise_len])
                    del self._buffer[:noise_len]
                    events.append(
                        self._invalid_event(
                            Ms6222FrameType.UNKNOWN,
                            raw,
                            monotonic_ns,
                            wall_ns,
                            "noise",
                        )
                    )
                    self._statistics = replace(
                        self._statistics,
                        noise_bytes=self._statistics.noise_bytes + len(raw),
                    )
                break
            if header_index:
                raw = bytes(self._buffer[:header_index])
                del self._buffer[:header_index]
                events.append(
                    self._invalid_event(
                        Ms6222FrameType.UNKNOWN,
                        raw,
                        monotonic_ns,
                        wall_ns,
                        "noise",
                    )
                )
                self._statistics = replace(
                    self._statistics,
                    noise_bytes=self._statistics.noise_bytes + len(raw),
                )
            if len(self._buffer) < 4:
                break

            frame_type = _frame_type(self._buffer[2])
            if frame_type == Ms6222FrameType.UNKNOWN:
                raw = bytes(self._buffer[:3])
                del self._buffer[0]
                events.append(
                    self._invalid_event(
                        frame_type,
                        raw,
                        monotonic_ns,
                        wall_ns,
                        "unsupported_header",
                    )
                )
                continue

            expected_length, length_error, need_more_header = _expected_length(
                self._buffer, frame_type
            )
            if need_more_header:
                break
            if length_error:
                inspect_len = min(len(self._buffer), 10)
                raw = bytes(self._buffer[:inspect_len])
                del self._buffer[0]
                events.append(
                    self._invalid_event(
                        frame_type,
                        raw,
                        monotonic_ns,
                        wall_ns,
                        length_error,
                        length_error=True,
                    )
                )
                continue
            if len(self._buffer) < expected_length:
                break

            raw = bytes(self._buffer[:expected_length])
            if not ms6222_crc32_valid(raw):
                # Match the firmware stream parser: discard only the first byte
                # so a valid frame nested in a damaged candidate is recoverable.
                del self._buffer[0]
                events.append(
                    self._invalid_event(
                        frame_type,
                        raw,
                        monotonic_ns,
                        wall_ns,
                        "crc_mismatch",
                        crc_error=True,
                    )
                )
                continue
            try:
                record, protocol_version = _parse_record(frame_type, raw)
            except (ValueError, struct.error) as exc:
                events.append(
                    self._invalid_event(
                        frame_type,
                        raw,
                        monotonic_ns,
                        wall_ns,
                        f"decode_error:{exc}",
                    )
                )
                del self._buffer[0]
                continue
            del self._buffer[:expected_length]
            envelope = Ms6222FrameEnvelope(
                frame_type=frame_type,
                raw=raw,
                host_monotonic_ns=monotonic_ns,
                host_wall_time_ns=wall_ns,
                crc_valid=True,
                protocol_version=protocol_version,
                record=record,
            )
            events.append(envelope)
            counts = {
                "ins_frames": self._statistics.ins_frames,
                "gnss_frames": self._statistics.gnss_frames,
                "rawimu_frames": self._statistics.rawimu_frames,
            }
            counts[
                {
                    Ms6222FrameType.INSPVAXB: "ins_frames",
                    Ms6222FrameType.GNSS: "gnss_frames",
                    Ms6222FrameType.RAWIMUSB: "rawimu_frames",
                }[frame_type]
            ] += 1
            self._statistics = replace(
                self._statistics,
                valid_frames=self._statistics.valid_frames + 1,
                **counts,
            )
        return tuple(events)

    def _invalid_event(
        self,
        frame_type: Ms6222FrameType,
        raw: bytes,
        monotonic_ns: int,
        wall_ns: int,
        error: str,
        *,
        crc_error: bool = False,
        length_error: bool = False,
    ) -> Ms6222FrameEnvelope:
        self._statistics = replace(
            self._statistics,
            invalid_frames=self._statistics.invalid_frames + 1,
            crc_errors=self._statistics.crc_errors + int(crc_error),
            length_errors=self._statistics.length_errors + int(length_error),
        )
        return Ms6222FrameEnvelope(
            frame_type=frame_type,
            raw=bytes(raw),
            host_monotonic_ns=monotonic_ns,
            host_wall_time_ns=wall_ns,
            crc_valid=False,
            protocol_version=0,
            record=None,
            error=error,
        )


def ms6222_crc32(data: bytes) -> int:
    crc = 0
    for value in bytes(data):
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return crc & 0xFFFFFFFF


def ms6222_crc32_valid(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    expected = struct.unpack_from("<I", frame, len(frame) - 4)[0]
    return ms6222_crc32(frame[:-4]) == expected


def append_ms6222_crc(payload: bytes) -> bytes:
    return bytes(payload) + struct.pack("<I", ms6222_crc32(payload))


def _frame_type(head3: int) -> Ms6222FrameType:
    return {
        MS6222_INS_HEAD3: Ms6222FrameType.INSPVAXB,
        MS6222_GNSS_HEAD3: Ms6222FrameType.GNSS,
        MS6222_RAWIMU_HEAD3: Ms6222FrameType.RAWIMUSB,
    }.get(int(head3), Ms6222FrameType.UNKNOWN)


def _expected_length(
    buffer: bytearray,
    frame_type: Ms6222FrameType,
) -> tuple[int, str, bool]:
    if frame_type == Ms6222FrameType.INSPVAXB:
        if len(buffer) < 10:
            return 0, "", True
        header_len = buffer[3]
        msg_len = struct.unpack_from("<H", buffer, 8)[0]
        if header_len != MS6222_INS_HEADER_LEN or msg_len != MS6222_INS_MSG_LEN:
            return 0, "invalid_inspvaxb_length", False
        return header_len + msg_len + 4, "", False
    if frame_type == Ms6222FrameType.GNSS:
        if buffer[3] != MS6222_GNSS_MSG_LEN:
            return 0, "invalid_gnss_length", False
        return MS6222_GNSS_FRAME_LEN, "", False
    if buffer[3] != MS6222_RAWIMU_MSG_LEN:
        return 0, "invalid_rawimu_length", False
    return MS6222_RAWIMU_FRAME_LEN, "", False


def _parse_record(
    frame_type: Ms6222FrameType,
    raw: bytes,
) -> tuple[Ms6222Record, int]:
    if frame_type == Ms6222FrameType.INSPVAXB:
        return _parse_ins(raw), 1
    if frame_type == Ms6222FrameType.GNSS:
        record = _parse_gnss(raw)
        version = 2 if (
            record.baseline_length_mm
            or record.secondary_antenna_satellites
            or record.heading_std_deg != 0.0
        ) else 1
        return record, version
    if frame_type == Ms6222FrameType.RAWIMUSB:
        return _parse_rawimu(raw), 1
    raise ValueError("unsupported MS-6222 frame type")


def _parse_ins(raw: bytes) -> Ms6222InsRecord:
    return Ms6222InsRecord(
        gps_week=struct.unpack_from("<H", raw, 14)[0],
        gps_ms=struct.unpack_from("<I", raw, 16)[0],
        message_sequence=struct.unpack_from("<H", raw, 10)[0],
        time_state=raw[13],
        gnss_state=struct.unpack_from("<I", raw, 28)[0],
        fix_type=struct.unpack_from("<I", raw, 32)[0],
        ekf_state=struct.unpack_from("<I", raw, 148)[0],
        latitude_deg=struct.unpack_from("<d", raw, 36)[0],
        longitude_deg=struct.unpack_from("<d", raw, 44)[0],
        altitude_m=struct.unpack_from("<d", raw, 52)[0],
        height_m=struct.unpack_from("<f", raw, 60)[0],
        velocity_n_m_s=struct.unpack_from("<d", raw, 64)[0],
        velocity_e_m_s=struct.unpack_from("<d", raw, 72)[0],
        velocity_u_m_s=struct.unpack_from("<d", raw, 80)[0],
        roll_deg=struct.unpack_from("<d", raw, 88)[0],
        pitch_deg=struct.unpack_from("<d", raw, 96)[0],
        yaw_deg=struct.unpack_from("<d", raw, 104)[0],
        latitude_std=struct.unpack_from("<f", raw, 112)[0],
        longitude_std=struct.unpack_from("<f", raw, 116)[0],
        altitude_std=struct.unpack_from("<f", raw, 120)[0],
        velocity_n_std=struct.unpack_from("<f", raw, 124)[0],
        velocity_e_std=struct.unpack_from("<f", raw, 128)[0],
        velocity_u_std=struct.unpack_from("<f", raw, 132)[0],
        roll_std_deg=struct.unpack_from("<f", raw, 136)[0],
        pitch_std_deg=struct.unpack_from("<f", raw, 140)[0],
        yaw_std_deg=struct.unpack_from("<f", raw, 144)[0],
    )


def _parse_gnss(raw: bytes) -> Ms6222GnssRecord:
    baseline_length_mm = struct.unpack_from("<H", raw, 105)[0]
    secondary_satellites = raw[107]
    heading_std_deg = struct.unpack_from("<f", raw, 108)[0]
    return Ms6222GnssRecord(
        gps_week=struct.unpack_from("<H", raw, 6)[0],
        gps_ms=struct.unpack_from("<I", raw, 8)[0],
        time_state=raw[12],
        year=struct.unpack_from("<H", raw, 13)[0],
        month=raw[15],
        day=raw[16],
        hour=raw[17],
        minute=raw[18],
        second=struct.unpack_from("<f", raw, 19)[0],
        fix_sign=raw[23],
        used_satellites=raw[24],
        tracked_satellites=raw[25],
        base_station_count=raw[26],
        fix_frequency=raw[27],
        pdop=struct.unpack_from("<f", raw, 28)[0],
        hdop=struct.unpack_from("<f", raw, 32)[0],
        longitude_deg=struct.unpack_from("<d", raw, 36)[0],
        latitude_deg=struct.unpack_from("<d", raw, 44)[0],
        altitude_m=struct.unpack_from("<f", raw, 52)[0],
        velocity_n_m_s=struct.unpack_from("<f", raw, 56)[0],
        velocity_e_m_s=struct.unpack_from("<f", raw, 60)[0],
        velocity_u_m_s=struct.unpack_from("<f", raw, 64)[0],
        position_x_std=struct.unpack_from("<f", raw, 68)[0],
        position_y_std=struct.unpack_from("<f", raw, 72)[0],
        position_z_std=struct.unpack_from("<f", raw, 76)[0],
        velocity_n_std=struct.unpack_from("<f", raw, 80)[0],
        velocity_e_std=struct.unpack_from("<f", raw, 84)[0],
        velocity_u_std=struct.unpack_from("<f", raw, 88)[0],
        altitude_error=struct.unpack_from("<f", raw, 92)[0],
        ground_speed_m_s=struct.unpack_from("<f", raw, 96)[0],
        yaw_deg=struct.unpack_from("<f", raw, 100)[0],
        yaw_sign=raw[104],
        baseline_length_mm=baseline_length_mm,
        secondary_antenna_satellites=secondary_satellites,
        heading_std_deg=heading_std_deg,
    )


def _parse_rawimu(raw: bytes) -> Ms6222RawImuRecord:
    imu_status = struct.unpack_from("<H", raw, 24)[0]
    accel_z, accel_neg_y, accel_x = struct.unpack_from("<iii", raw, 28)
    gyro_z, gyro_neg_y, gyro_x = struct.unpack_from("<iii", raw, 40)
    accel_factor = MS6222_G_TO_M_S2 / MS6222_RAWIMU_ACCEL_SCALE
    gyro_factor = math.pi / 180.0 / MS6222_RAWIMU_GYRO_SCALE
    return Ms6222RawImuRecord(
        gps_week=struct.unpack_from("<H", raw, 6)[0],
        gps_ms=struct.unpack_from("<I", raw, 8)[0],
        secondary_gps_week=struct.unpack_from("<H", raw, 14)[0],
        gps_seconds=struct.unpack_from("<d", raw, 16)[0],
        imu_status=imu_status,
        imu_status_valid=(imu_status & 0x7F) == 0x7F,
        temperature_c=struct.unpack_from("<H", raw, 26)[0] / 100.0,
        accel_x_m_s2=accel_x * accel_factor,
        accel_y_m_s2=accel_neg_y * accel_factor,
        accel_z_m_s2=accel_z * accel_factor,
        gyro_x_rad_s=gyro_x * gyro_factor,
        gyro_y_rad_s=gyro_neg_y * gyro_factor,
        gyro_z_rad_s=gyro_z * gyro_factor,
    )


__all__ = [
    "MS6222_GNSS_FRAME_LEN",
    "MS6222_INS_FRAME_LEN",
    "MS6222_RAWIMU_FRAME_LEN",
    "Ms6222FrameEnvelope",
    "Ms6222FrameType",
    "Ms6222GnssRecord",
    "Ms6222InsRecord",
    "Ms6222ParserStatistics",
    "Ms6222RawImuRecord",
    "Ms6222Record",
    "Ms6222StreamParser",
    "append_ms6222_crc",
    "ms6222_crc32",
    "ms6222_crc32_valid",
]
