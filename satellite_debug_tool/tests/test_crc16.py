"""Unit tests for CRC16 implementation."""

import struct
import pytest
from satellite_debug_tool.core.protocol.crc16 import Crc16


class TestCrc16:
    def test_calculate_empty(self):
        crc = Crc16.calculate(b"")
        assert isinstance(crc, int)

    def test_calculate_aa55(self):
        crc = Crc16.calculate(b"\xaa\x55")
        assert isinstance(crc, int)
        assert 0 <= crc <= 0xFFFF

    def test_calculate_known_value(self):
        data = b"123456789"
        crc = Crc16.calculate(data)
        assert crc == 0x29B1

    def test_verify_correct(self):
        data = b"\xaa\x55\x0d\x01"
        crc = Crc16.calculate(data)
        assert Crc16.verify(data, crc) is True

    def test_verify_incorrect(self):
        data = b"\xaa\x55\x0d\x01"
        assert Crc16.verify(data, 0x1234) is False

    def test_calculate_le(self):
        data = b"\xaa\x55\x0d\x01"
        crc_le = Crc16.calculate_le(data)
        assert len(crc_le) == 2

    def test_from_le(self):
        crc_bytes = bytes([0x12, 0x34])
        crc = Crc16.from_le(crc_bytes)
        assert crc == 0x3412

    def test_roundtrip(self):
        data = b"Hello World"
        crc_le = Crc16.calculate_le(data)
        crc = Crc16.from_le(crc_le)
        assert Crc16.calculate(data) == crc


class TestCrc16Protocol:
    def test_frame_header_crc(self):
        frame_header = bytes([0xAA, 0x55, 0x0D, 0x01, 0x00, 0x20])
        crc = Crc16.calculate(frame_header)
        assert isinstance(crc, int)
