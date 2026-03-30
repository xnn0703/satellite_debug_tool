"""Unit tests for FrameReceiver."""

import struct
import pytest
from satellite_debug_tool.core.protocol.frame_receiver import FrameReceiver
from satellite_debug_tool.core.protocol.crc16 import Crc16


def float_to_bytes(value: float) -> bytes:
    return struct.pack("<f", value)


def build_test_frame(cmd_type: int, data: bytes) -> bytes:
    header = bytes([0xAA, 0x55, 0x0D, cmd_type])
    data_len = len(data).to_bytes(2, "little")
    payload = header + data_len + data
    crc = Crc16.calculate(payload)
    crc_bytes = bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    footer = bytes([0xEE])
    return payload + crc_bytes + footer


def build_data_report_frame(timestamp: int, channels: list) -> bytes:
    data = timestamp.to_bytes(4, "little")
    data += bytes([len(channels)])
    for name, value in channels:
        name_bytes = name.encode("utf-8").ljust(32, b"\x00")[:32]
        data += name_bytes
        data += float_to_bytes(value)
    return build_test_frame(0x01, data)


class TestFrameReceiver:
    def test_reset(self):
        receiver = FrameReceiver()
        receiver._state = receiver.State.READ_DATA
        receiver.reset()
        assert receiver._state == receiver.State.WAIT_HEADER_1

    def test_error_count_init(self):
        receiver = FrameReceiver()
        assert receiver.error_count == 0

    def test_empty_data(self):
        receiver = FrameReceiver()
        frames = receiver.feed(b"")
        assert frames == []

    def test_no_header(self):
        receiver = FrameReceiver()
        frames = receiver.feed(b"\x00\x01\x02\x03")
        assert frames == []
        assert receiver.error_count == 0

    def test_incomplete_frame(self):
        receiver = FrameReceiver()
        frame = build_data_report_frame(1000, [("TEST", 1.5)])
        frames = receiver.feed(frame[:10])
        assert frames == []


class TestDataReportParsing:
    def test_single_channel(self):
        receiver = FrameReceiver()
        frame = build_data_report_frame(5000, [("GPS_LAT", 31.23)])
        frames = receiver.feed(frame)

        assert len(frames) == 1
        assert frames[0].cmd_type == 0x01
        assert frames[0].timestamp == 5000
        assert len(frames[0].channels) == 1
        assert frames[0].channels[0].name == "GPS_LAT"
        assert abs(frames[0].channels[0].value - 31.23) < 0.01

    def test_multiple_channels(self):
        receiver = FrameReceiver()
        frame = build_data_report_frame(
            1000,
            [
                ("GPS_LAT", 31.2304),
                ("GPS_LON", 121.4737),
                ("GPS_ALT", 10.5),
                ("SNR", 35.2),
            ],
        )
        frames = receiver.feed(frame)

        assert len(frames) == 1
        assert len(frames[0].channels) == 4
        assert frames[0].get_channel("GPS_LAT") is not None
        assert abs(frames[0].get_channel("SNR") - 35.2) < 0.01

    def test_get_all_values(self):
        receiver = FrameReceiver()
        frame = build_data_report_frame(100, [("A", 1.0), ("B", 2.0)])
        frames = receiver.feed(frame)
        values = frames[-1].get_all_values()
        assert "A" in values
        assert abs(values["A"] - 1.0) < 0.001
        assert abs(values["B"] - 2.0) < 0.001

    def test_invalid_device_type(self):
        receiver = FrameReceiver()
        frame = bytes([0xAA, 0x55, 0xFF, 0x01, 0x00, 0x00, 0x00, 0x00, 0xEE])
        frames = receiver.feed(frame)
        assert frames == []
        assert receiver.error_count >= 1


class TestFrameBoundary:
    def test_multiple_frames(self):
        receiver = FrameReceiver()
        frame1 = build_data_report_frame(1000, [("CH1", 1.0)])
        frame2 = build_data_report_frame(2000, [("CH1", 2.0)])
        frames = receiver.feed(frame1 + frame2)
        assert len(frames) == 2
        assert frames[0].timestamp == 1000
        assert frames[1].timestamp == 2000

    def test_frame_with_gaps(self):
        receiver = FrameReceiver()
        frame1 = build_data_report_frame(1000, [("A", 1.0)])
        gap = bytes([0x00, 0x00, 0xFF, 0xFE])
        frame2 = build_data_report_frame(2000, [("B", 2.0)])
        frames = receiver.feed(frame1 + gap + frame2)
        assert len(frames) == 2


class TestCrcValidation:
    def test_valid_crc(self):
        receiver = FrameReceiver()
        frame = build_data_report_frame(1000, [("TEST", 1.0)])
        frames = receiver.feed(frame)
        assert len(frames) == 1
        assert receiver.error_count == 0

    def test_invalid_crc(self):
        receiver = FrameReceiver()
        frame = build_data_report_frame(1000, [("TEST", 1.0)])
        frame = frame[:-2] + bytes([0x00, 0x00])
        frames = receiver.feed(frame)
        assert frames == []
        assert receiver.error_count >= 1


class TestProtocolExample:
    def test_example_frame_from_spec(self):
        receiver = FrameReceiver()
        # Build correct frame: header(2) + type(1) + cmd(1) + len(2) + data(5) + crc(2) + footer(1) = 14 bytes
        frame = bytes.fromhex("aa550d010500000000000059b1ee")
        frames = receiver.feed(frame)
        assert len(frames) == 1
        assert frames[0].cmd_type == 0x01
        assert frames[0].timestamp == 0
        assert frames[0].channel_count == 0

    def test_real_frame_with_data(self):
        receiver = FrameReceiver()
        frame = build_data_report_frame(12345, [("ROLL", 12.34)])
        frames = receiver.feed(frame)
        assert len(frames) == 1
        assert frames[0].timestamp == 12345
        assert frames[0].channels[0].name == "ROLL"
        assert abs(frames[0].channels[0].value - 12.34) < 0.01
