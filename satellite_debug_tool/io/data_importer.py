import struct
import csv
from datetime import datetime
from typing import Iterator
from satellite_debug_tool.core.protocol import DataFrame, ChannelData


SDB_MAGIC = b"SDB\x00"
SDB_FOOTER = b"\xee\xee\xee\xee"


class DataImporter:
    @staticmethod
    def read_sdb(filepath: str) -> Iterator[DataFrame]:
        with open(filepath, "rb") as f:
            magic = f.read(4)
            if magic != SDB_MAGIC:
                raise ValueError("Invalid SDB file format")

            version = struct.unpack("<H", f.read(2))[0]
            timestamp = struct.unpack("<Q", f.read(8))[0]
            f.read(8)

            buffer = bytearray()
            while True:
                chunk = f.read(4096)
                if not chunk:
                    break
                buffer.extend(chunk)

                while buffer:
                    idx = buffer.find(b"\xaa\x55")
                    if idx < 0:
                        break
                    buffer = buffer[idx:]
                    if len(buffer) < 9:
                        break
                    data_len = struct.unpack("<H", bytes(buffer[4:6]))[0]
                    frame_len = 9 + data_len
                    if len(buffer) < frame_len:
                        break
                    frame_data = bytes(buffer[:frame_len])
                    buffer = buffer[frame_len:]

                    if frame_data[-1:] != b"\xee":
                        continue

                    yield DataImporter._parse_frame(frame_data)

                    if buffer[-4:] == SDB_FOOTER:
                        return

    @staticmethod
    def _parse_frame(frame_data: bytes) -> DataFrame:
        cmd_type = frame_data[3]
        data_len = struct.unpack("<H", frame_data[4:6])[0]
        data = frame_data[6 : 6 + data_len]

        timestamp = struct.unpack("<I", data[:4])[0]
        channel_count = data[4]

        channels = []
        offset = 5
        for _ in range(channel_count):
            if offset + 36 > len(data):
                break
            name_bytes = data[offset : offset + 32]
            name = name_bytes.rstrip(b"\x00").decode("utf-8", errors="ignore")
            offset += 32
            value = struct.unpack("<f", data[offset : offset + 4])[0]
            offset += 4
            channels.append(ChannelData(name=name, value=value))

        return DataFrame(cmd_type=cmd_type, timestamp=timestamp, channels=channels)

    @staticmethod
    def read_csv(filepath: str) -> Iterator[DataFrame]:
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                timestamp = int(float(row.get("timestamp", i * 100)))
                channels = []
                for name, value in row.items():
                    if name == "timestamp":
                        continue
                    try:
                        channels.append(ChannelData(name=name, value=float(value)))
                    except ValueError:
                        pass
                if channels:
                    yield DataFrame(cmd_type=1, timestamp=timestamp, channels=channels)
