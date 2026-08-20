"""FrameReceiverV2 状态机测试。"""

import struct
import pytest

from satellite_debug_tool.core.protocol import (
    CmdType,
    DataReport,
    FrameReceiverV2,
    Heartbeat,
    RawFrame,
    build_frame,
)
from satellite_debug_tool.core.protocol.crc16 import Crc16


def _make_data_report(samples):
    payload = struct.pack("<I", 1000) + bytes([len(samples)])
    for cid, val in samples:
        payload += bytes([cid]) + struct.pack("<f", val)
    return build_frame(CmdType.DATA_REPORT, payload)


def _make_heartbeat():
    payload = struct.pack("<IBIHHI", 9999, 42, 2048, 50, 50, 0)
    return build_frame(CmdType.HEARTBEAT, payload)


class TestBasicRecv:
    def test_empty_feed(self):
        r = FrameReceiverV2()
        assert r.feed(b"") == []
        assert r.error_count == 0

    def test_single_data_report(self):
        r = FrameReceiverV2()
        frame = _make_data_report([(0, 1.0), (1, 2.0)])
        records = r.feed(frame)
        assert len(records) == 1
        assert isinstance(records[0], DataReport)
        assert records[0].timestamp == 1000
        assert [s.channel_id for s in records[0].samples] == [0, 1]
        assert r.frames_ok == 1
        assert r.error_count == 0

    def test_heartbeat_decoded(self):
        r = FrameReceiverV2()
        records = r.feed(_make_heartbeat())
        assert len(records) == 1
        assert isinstance(records[0], Heartbeat)
        assert records[0].cpu_load == 42

    def test_1536_byte_data_received(self):
        r = FrameReceiverV2()
        payload = b"\xA5" * 1536
        records = r.feed(build_frame(0x11, payload))

        assert len(records) == 1
        assert isinstance(records[0], RawFrame)
        assert records[0].data == payload
        assert r.frames_ok == 1
        assert r.error_count == 0


class TestFragmentation:
    def test_byte_by_byte(self):
        r = FrameReceiverV2()
        frame = _make_data_report([(0, 3.14)])
        records = []
        for b in frame:
            records.extend(r.feed(bytes([b])))
        assert len(records) == 1
        assert isinstance(records[0], DataReport)

    def test_split_into_two_chunks(self):
        r = FrameReceiverV2()
        frame = _make_data_report([(2, 1.5), (5, 6.5)])
        half = len(frame) // 2
        part1 = r.feed(frame[:half])
        part2 = r.feed(frame[half:])
        assert len(part1) == 0
        assert len(part2) == 1

    def test_multiple_frames_in_one_feed(self):
        r = FrameReceiverV2()
        data = _make_data_report([(0, 1.0)]) + _make_heartbeat() + _make_data_report([(1, 2.0)])
        records = r.feed(data)
        assert len(records) == 3
        assert isinstance(records[0], DataReport)
        assert isinstance(records[1], Heartbeat)
        assert isinstance(records[2], DataReport)


class TestErrors:
    def test_crc_error_counted(self):
        r = FrameReceiverV2()
        frame = bytearray(_make_data_report([(0, 1.0)]))
        # 篡改 CRC
        frame[-3] ^= 0xFF
        records = r.feed(bytes(frame))
        assert len(records) == 0
        assert r.crc_errors == 1
        assert r.frames_ok == 0

    def test_wrong_device_type(self):
        r = FrameReceiverV2()
        # header OK, device_type 错为 0x00
        bad = bytes([0xAA, 0x55, 0x00, 0x01, 0x00, 0x00])
        records = r.feed(bad)
        assert records == []
        assert r.framing_errors >= 1

    def test_wrong_footer(self):
        r = FrameReceiverV2()
        frame = bytearray(_make_data_report([(0, 0.0)]))
        frame[-1] = 0x00   # footer 错
        records = r.feed(bytes(frame))
        assert records == []
        assert r.framing_errors >= 1

    def test_oversize_len_field_discarded(self):
        r = FrameReceiverV2()
        length = (1537).to_bytes(2, "little")
        bad = bytes([0xAA, 0x55, 0x0D, 0x01]) + length
        records = r.feed(bad)
        assert records == []
        assert r.framing_errors == 1

    def test_decode_error_counted(self):
        """DATA_REPORT 段 channel_count=3 但只带 1 个样本 → decode_errors+1。"""
        r = FrameReceiverV2()
        broken_payload = struct.pack("<I", 100) + b"\x03" + b"\x00" + struct.pack("<f", 1.0)
        bad_frame = build_frame(CmdType.DATA_REPORT, broken_payload)
        records = r.feed(bad_frame)
        assert records == []
        assert r.decode_errors == 1
        assert r.crc_errors == 0

    def test_recover_after_bad_frame(self):
        r = FrameReceiverV2()
        bad = bytearray(_make_data_report([(0, 1.0)]))
        bad[-3] ^= 0xFF   # CRC 错
        good = _make_heartbeat()
        records = r.feed(bytes(bad) + good)
        assert len(records) == 1
        assert isinstance(records[0], Heartbeat)


class TestUnknownCmd:
    def test_unknown_cmd_returns_raw_frame(self):
        r = FrameReceiverV2()
        # 0x11 未定义，预期 receiver 返回 RawFrame
        frame = build_frame(0x11, b"\x11\x22\x33")
        records = r.feed(frame)
        assert len(records) == 1
        assert isinstance(records[0], RawFrame)
        assert records[0].cmd_type == 0x11
        assert records[0].data == b"\x11\x22\x33"

    def test_gnss_extensions_are_raw_frames_without_new_decoders(self, monkeypatch):
        """模拟旧 receiver：未注册 0x0F/0x10 时应安全忽略且不记解码错误。"""
        import satellite_debug_tool.core.protocol.frame_receiver_v2 as receiver_module

        sat_cmd = int(CmdType.GNSS_SAT_REPORT)
        signal_cmd = int(CmdType.GNSS_SIGNAL_REPORT)
        monkeypatch.delitem(receiver_module._DECODERS, sat_cmd)
        monkeypatch.delitem(receiver_module._DECODERS, signal_cmd)

        r = FrameReceiverV2()
        records = r.feed(
            build_frame(sat_cmd, b"\x11\x22")
            + build_frame(signal_cmd, b"\x33\x44\x55")
        )

        assert [(record.cmd_type, record.data) for record in records] == [
            (sat_cmd, b"\x11\x22"),
            (signal_cmd, b"\x33\x44\x55"),
        ]
        assert all(isinstance(record, RawFrame) for record in records)
        assert r.frames_ok == 2
        assert r.decode_errors == 0
        assert r.error_count == 0
