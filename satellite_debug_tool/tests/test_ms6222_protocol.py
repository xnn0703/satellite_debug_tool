"""MS-6222 parser compatibility and stream-recovery tests."""

from __future__ import annotations

import math
import struct

import pytest

from satellite_debug_tool.core.production.ms6222_protocol import (
    MS6222_GNSS_FRAME_LEN,
    MS6222_INS_FRAME_LEN,
    MS6222_RAWIMU_FRAME_LEN,
    Ms6222FrameType,
    Ms6222GnssRecord,
    Ms6222InsRecord,
    Ms6222RawImuRecord,
    Ms6222StreamParser,
    append_ms6222_crc,
    ms6222_crc32,
)


def _ins_frame() -> bytes:
    frame = bytearray(MS6222_INS_FRAME_LEN - 4)
    frame[:4] = b"\xAA\x44\x12\x1C"
    struct.pack_into("<H", frame, 4, 1465)
    struct.pack_into("<H", frame, 8, 0x7E)
    struct.pack_into("<H", frame, 10, 7)
    frame[13] = 3
    struct.pack_into("<H", frame, 14, 2401)
    struct.pack_into("<I", frame, 16, 123456)
    struct.pack_into("<II", frame, 28, 3, 56)
    struct.pack_into("<ddd", frame, 36, 31.2, 118.8, 12.3)
    struct.pack_into("<f", frame, 60, 12.0)
    struct.pack_into("<ddd", frame, 64, 1.0, 2.0, 3.0)
    struct.pack_into("<ddd", frame, 88, -2.5, 1.25, 359.5)
    struct.pack_into("<fffffffff", frame, 112, *[0.1 * i for i in range(1, 10)])
    struct.pack_into("<I", frame, 148, 9)
    return append_ms6222_crc(frame)


def _gnss_frame(*, v2: bool) -> bytes:
    frame = bytearray(MS6222_GNSS_FRAME_LEN - 4)
    frame[:4] = b"\xAA\x44\xBD\x6C"
    struct.pack_into("<H", frame, 4, 0x220)
    struct.pack_into("<H", frame, 6, 2401)
    struct.pack_into("<I", frame, 8, 654321)
    frame[12] = 3
    struct.pack_into("<H", frame, 13, 2026)
    frame[15:19] = bytes((8, 23, 12, 34))
    struct.pack_into("<f", frame, 19, 56.5)
    frame[23:28] = bytes((2, 18, 24, 1, 2))
    struct.pack_into("<ff", frame, 28, 1.2, 0.8)
    struct.pack_into("<dd", frame, 36, 118.8, 31.2)
    struct.pack_into("<f", frame, 52, 16.5)
    struct.pack_into("<fff", frame, 56, 1.0, 2.0, 3.0)
    struct.pack_into("<ffffff", frame, 68, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6)
    struct.pack_into("<fff", frame, 92, 0.7, 2.25, 123.5)
    frame[104] = 1
    if v2:
        struct.pack_into("<H", frame, 105, 2000)
        frame[107] = 11
        struct.pack_into("<f", frame, 108, 0.15)
    return append_ms6222_crc(frame)


def _rawimu_frame() -> bytes:
    frame = bytearray(MS6222_RAWIMU_FRAME_LEN - 4)
    frame[:4] = b"\xAA\x44\x13\x28"
    struct.pack_into("<H", frame, 4, 0x145)
    struct.pack_into("<H", frame, 6, 2401)
    struct.pack_into("<I", frame, 8, 222333)
    struct.pack_into("<H", frame, 14, 2401)
    struct.pack_into("<d", frame, 16, 222.333)
    struct.pack_into("<HH", frame, 24, 0x7F, 2500)
    accel_1g = int(52621241.37840640)
    gyro_1dps = int(1499226.41161356)
    struct.pack_into("<iii", frame, 28, accel_1g, accel_1g, accel_1g)
    struct.pack_into("<iii", frame, 40, gyro_1dps, gyro_1dps, gyro_1dps)
    return append_ms6222_crc(frame)


def test_crc_golden_vector_matches_firmware_algorithm() -> None:
    assert ms6222_crc32(b"123456789") == 0x2DFD2D88


def test_all_three_frame_types_and_gnss_versions_parse() -> None:
    parser = Ms6222StreamParser()
    events = parser.feed(
        _ins_frame() + _gnss_frame(v2=False) + _gnss_frame(v2=True) + _rawimu_frame(),
        host_monotonic_ns=100,
        host_wall_time_ns=200,
    )
    valid = [event for event in events if event.valid]
    assert [event.frame_type for event in valid] == [
        Ms6222FrameType.INSPVAXB,
        Ms6222FrameType.GNSS,
        Ms6222FrameType.GNSS,
        Ms6222FrameType.RAWIMUSB,
    ]
    ins = valid[0].record
    assert isinstance(ins, Ms6222InsRecord)
    assert ins.roll_deg == pytest.approx(-2.5)
    assert ins.pitch_deg == pytest.approx(1.25)
    assert ins.yaw_deg == pytest.approx(359.5)
    assert valid[1].protocol_version == 1
    gnss = valid[2].record
    assert isinstance(gnss, Ms6222GnssRecord)
    assert valid[2].protocol_version == 2
    assert gnss.baseline_length_mm == 2000
    assert gnss.secondary_antenna_satellites == 11
    assert gnss.heading_std_deg == pytest.approx(0.15)
    rawimu = valid[3].record
    assert isinstance(rawimu, Ms6222RawImuRecord)
    assert rawimu.imu_status_valid
    assert rawimu.temperature_c == pytest.approx(25.0)
    assert rawimu.accel_z_m_s2 == pytest.approx(9.80147, rel=1e-6)
    assert rawimu.gyro_z_rad_s == pytest.approx(math.pi / 180.0, rel=1e-6)


@pytest.mark.parametrize("split", range(1, MS6222_INS_FRAME_LEN))
def test_inspvaxb_recovers_at_every_fragment_position(split: int) -> None:
    frame = _ins_frame()
    parser = Ms6222StreamParser()
    assert not [event for event in parser.feed(frame[:split]) if event.valid]
    valid = [event for event in parser.feed(frame[split:]) if event.valid]
    assert len(valid) == 1
    assert valid[0].frame_type == Ms6222FrameType.INSPVAXB


@pytest.mark.parametrize(
    ("frame", "expected_type"),
    [
        (_gnss_frame(v2=False), Ms6222FrameType.GNSS),
        (_gnss_frame(v2=True), Ms6222FrameType.GNSS),
        (_rawimu_frame(), Ms6222FrameType.RAWIMUSB),
    ],
    ids=("gnss-v1", "gnss-v2", "rawimusb"),
)
def test_other_frame_types_recover_at_every_fragment_position(
    frame: bytes,
    expected_type: Ms6222FrameType,
) -> None:
    for split in range(1, len(frame)):
        parser = Ms6222StreamParser()
        assert not [event for event in parser.feed(frame[:split]) if event.valid]
        valid = [event for event in parser.feed(frame[split:]) if event.valid]
        assert len(valid) == 1
        assert valid[0].frame_type == expected_type


def test_noise_bad_length_bad_crc_and_nested_header_recover() -> None:
    parser = Ms6222StreamParser()
    bad_length = bytearray(_gnss_frame(v2=False))
    bad_length[3] = 1
    damaged = bytearray(_ins_frame())
    nested = _rawimu_frame()
    damaged[30 : 30 + len(nested)] = nested
    damaged[-4:] = struct.pack("<I", 0)
    events = parser.feed(b"noise" + bytes(bad_length) + bytes(damaged) + _gnss_frame(v2=True))
    valid_types = [event.frame_type for event in events if event.valid]
    assert Ms6222FrameType.RAWIMUSB in valid_types
    assert valid_types[-1] == Ms6222FrameType.GNSS
    assert parser.statistics.noise_bytes >= 5
    assert parser.statistics.length_errors >= 1
    assert parser.statistics.crc_errors >= 1


def test_empty_and_partial_header_are_retained_without_false_frame() -> None:
    parser = Ms6222StreamParser()
    assert parser.feed(b"") == ()
    parser.feed(b"noise\xAA")
    assert parser.buffered_bytes == 1
    valid = [event for event in parser.feed(_ins_frame()[1:]) if event.valid]
    assert len(valid) == 1
