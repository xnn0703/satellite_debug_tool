import pytest
import struct
import os
import tempfile
from satellite_debug_tool.io.data_recorder import DataRecorder, SDB_MAGIC, SDB_FOOTER
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.core.protocol import DataFrame, ChannelData


class TestDataRecorder:
    def test_init(self):
        recorder = DataRecorder("test.sdb")
        assert recorder._filepath == "test.sdb"
        assert recorder.is_recording is False

    def test_start_stop(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sdb") as f:
            filepath = f.name
        recorder = DataRecorder(filepath)
        assert recorder.start() is True
        assert recorder.is_recording is True
        assert recorder.stop() is True
        assert recorder.is_recording is False
        os.unlink(filepath)

    def test_write_frame(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sdb") as f:
            filepath = f.name
        recorder = DataRecorder(filepath)
        recorder.start()
        frame = bytes([0xAA, 0x55, 0x0D, 0x01, 0x00, 0x00, 0xEE])
        assert recorder.write_frame(frame) is True
        recorder.stop()
        with open(filepath, "rb") as f:
            data = f.read()
            assert data.startswith(SDB_MAGIC)
            assert data.endswith(SDB_FOOTER)
        os.unlink(filepath)

    def test_write_without_start(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sdb") as f:
            filepath = f.name
        recorder = DataRecorder(filepath)
        assert recorder.write_frame(b"\x00") is False
        os.unlink(filepath)


class TestDataImporter:
    def test_parse_frame(self):
        from satellite_debug_tool.core.protocol.crc16 import Crc16

        header = bytes([0xAA, 0x55, 0x0D, 0x01])
        data_len = bytes([0x05, 0x00])
        data = bytes([0x00, 0x00, 0x00, 0x00, 0x00])
        payload = header + data_len + data
        crc = Crc16.calculate(payload)
        crc_bytes = bytes([crc & 0xFF, (crc >> 8) & 0xFF])
        frame_data = payload + crc_bytes + bytes([0xEE])

        frame = DataImporter._parse_frame(frame_data)
        assert frame.cmd_type == 0x01
        assert frame.timestamp == 0
        assert frame.channel_count == 0

    def test_parse_frame_with_channels(self):
        from satellite_debug_tool.core.protocol.crc16 import Crc16

        header = bytes([0xAA, 0x55, 0x0D, 0x01])
        timestamp = struct.pack("<I", 1000)
        count = bytes([1])
        name = b"GPS_LAT".ljust(32, b"\x00")[:32]
        value = struct.pack("<f", 31.23)
        data = timestamp + count + name + value
        data_len = struct.pack("<H", len(data))
        payload = header + data_len + data
        crc = Crc16.calculate(payload)
        crc_bytes = bytes([crc & 0xFF, (crc >> 8) & 0xFF])
        frame_data = payload + crc_bytes + bytes([0xEE])

        frame = DataImporter._parse_frame(frame_data)
        assert frame.timestamp == 1000
        assert frame.channel_count == 1
        assert frame.channels[0].name == "GPS_LAT"
        assert abs(frame.channels[0].value - 31.23) < 0.01


class TestDataImporterCSV:
    def test_read_csv(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w") as f:
            f.write("timestamp,CH1,CH2\n")
            f.write("0,1.0,2.0\n")
            f.write("100,1.5,2.5\n")
            filepath = f.name

        frames = list(DataImporter.read_csv(filepath))
        assert len(frames) == 2
        assert frames[0].timestamp == 0
        assert frames[0].channels[0].name == "CH1"
        assert frames[0].channels[0].value == 1.0
        os.unlink(filepath)
