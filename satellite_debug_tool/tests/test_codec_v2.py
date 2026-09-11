"""codec_v2 编解码测试（正反样本 + 错误样本）。"""

import struct
import pytest

from satellite_debug_tool.core.protocol import (
    ChannelSample,
    CmdType,
    CodecError,
    DATA_REPORT_MAX_CHANNELS,
    DEVICE_TYPE,
    FRAME_FOOTER,
    FRAME_HEADER_0,
    FRAME_HEADER_1,
    MAX_DATA_LENGTH,
    MAX_FRAME_LENGTH,
    OTA_DATA_SEQUENCE_MAX,
    PROTOCOL_VERSION,
    SubCmd,
    build_control,
    build_debug_enable_v2,
    build_frame,
    build_ota_data,
    build_request_channel_define,
    build_request_event_define,
    build_request_meta_info,
    build_request_profile_semantics,
    build_request_state_define,
    build_set_sample_rate,
    build_set_trace_mode,
    build_user_mark,
)
from satellite_debug_tool.core.protocol.codec_v2 import (
    decode_channel_define,
    decode_command_response,
    decode_data_report,
    decode_event_define,
    decode_event_report,
    decode_heartbeat,
    decode_meta_info,
    decode_profile_semantics,
    decode_state_define,
    decode_state_report,
)
from satellite_debug_tool.core.protocol.crc16 import Crc16


# -----------------------------------------------------------------------------
# Envelope / build_frame
# -----------------------------------------------------------------------------

class TestBuildFrame:
    def test_envelope_structure(self):
        frame = build_frame(CmdType.DATA_REPORT, b"\x01\x02\x03")
        assert frame[0] == FRAME_HEADER_0
        assert frame[1] == FRAME_HEADER_1
        assert frame[2] == DEVICE_TYPE
        assert frame[3] == CmdType.DATA_REPORT
        # len LE = 3
        assert frame[4] == 0x03 and frame[5] == 0x00
        assert frame[6:9] == b"\x01\x02\x03"
        assert frame[-1] == FRAME_FOOTER

    def test_crc_is_correct(self):
        frame = build_frame(CmdType.HEARTBEAT, b"x" * 17)
        # 帧头 ~ data 末尾做 CRC
        envelope_end = -3  # 2B CRC + 1B footer
        crc_le = frame[envelope_end:-1]
        assert Crc16.calculate(frame[:envelope_end]) == Crc16.from_le(crc_le)

    def test_empty_data_allowed(self):
        frame = build_frame(CmdType.CONTROL, b"")
        assert len(frame) == 4 + 2 + 0 + 2 + 1   # header4 + len2 + data0 + crc2 + footer1

    def test_maximum_data_length_accepted(self):
        assert MAX_DATA_LENGTH == 1536
        assert MAX_FRAME_LENGTH == 1548

        frame = build_frame(CmdType.TRACKING_SIMULATION, b"\xA5" * 1536)
        assert int.from_bytes(frame[4:6], "little") == 1536
        # MAX_FRAME_LENGTH 保留设备端 +12B 缓冲合同；实际 wire envelope 是 9B。
        assert len(frame) == 1545

    def test_oversize_data_rejected(self):
        with pytest.raises(CodecError):
            build_frame(CmdType.DATA_REPORT, b"\x00" * 1537)


# -----------------------------------------------------------------------------
# CONTROL builders
# -----------------------------------------------------------------------------

class TestControlBuilders:
    def _data_of(self, frame: bytes) -> bytes:
        length = int.from_bytes(frame[4:6], "little")
        return frame[6:6 + length]

    def test_debug_enable(self):
        f_on = build_debug_enable_v2(True)
        f_off = build_debug_enable_v2(False)
        assert self._data_of(f_on) == bytes([SubCmd.DEBUG_ENABLE, 1])
        assert self._data_of(f_off) == bytes([SubCmd.DEBUG_ENABLE, 0])

    def test_request_define_frames(self):
        assert self._data_of(build_request_meta_info()) == bytes([SubCmd.REQUEST_META_INFO])
        assert self._data_of(build_request_channel_define()) == bytes([SubCmd.REQUEST_CHANNEL_DEFINE])
        assert self._data_of(build_request_state_define()) == bytes([SubCmd.REQUEST_STATE_DEFINE])
        assert self._data_of(build_request_event_define()) == bytes([SubCmd.REQUEST_EVENT_DEFINE])
        assert self._data_of(build_request_profile_semantics()) == bytes([
            SubCmd.REQUEST_PROFILE_SEMANTICS
        ])

    def test_user_mark(self):
        frame = build_user_mark(0x1234, "pt A")
        data = self._data_of(frame)
        assert data[0] == SubCmd.USER_MARK
        mark_id, text_len = struct.unpack_from("<HB", data, 1)
        assert mark_id == 0x1234
        assert text_len == 4
        assert data[4:4 + text_len] == b"pt A"

    def test_user_mark_text_too_long(self):
        with pytest.raises(CodecError):
            build_user_mark(1, "x" * 256)

    def test_set_sample_rate(self):
        frame = build_set_sample_rate(100)
        assert self._data_of(frame) == bytes([SubCmd.SET_SAMPLE_RATE]) + struct.pack("<H", 100)

    def test_set_sample_rate_out_of_range(self):
        with pytest.raises(CodecError):
            build_set_sample_rate(-1)
        with pytest.raises(CodecError):
            build_set_sample_rate(0x10000)

    def test_set_trace_mode(self):
        frame = build_set_trace_mode(3)
        assert self._data_of(frame) == bytes([SubCmd.SET_TRACE_MODE, 3])

    def test_ota_chunk_keeps_legacy_1021_byte_limit(self):
        frame = build_ota_data(0x1234, b"\x5A" * 1021)
        data = self._data_of(frame)
        assert len(data) == 1024
        assert data[:3] == bytes([SubCmd.OTA_DATA, 0x34, 0x12])

        with pytest.raises(CodecError, match="1..1021"):
            build_ota_data(0x1234, b"")
        with pytest.raises(CodecError, match="1021"):
            build_ota_data(0x1234, b"\x5A" * 1022)

    def test_ota_sequence_is_exactly_u16(self):
        frame = build_ota_data(OTA_DATA_SEQUENCE_MAX, b"\x5A")
        assert self._data_of(frame)[:3] == bytes([SubCmd.OTA_DATA, 0xFF, 0xFF])

        for sequence in (-1, OTA_DATA_SEQUENCE_MAX + 1, True, 1.0):
            with pytest.raises(CodecError, match="u16"):
                build_ota_data(sequence, b"\x5A")


# -----------------------------------------------------------------------------
# DATA_REPORT decoding
# -----------------------------------------------------------------------------

def _build_data_report_payload(timestamp_ms: int, samples: list[tuple[int, float]]) -> bytes:
    buf = bytearray()
    buf += timestamp_ms.to_bytes(4, "little")
    buf.append(len(samples))
    for cid, val in samples:
        buf.append(cid)
        buf += struct.pack("<f", val)
    return bytes(buf)


class TestDecodeDataReport:
    def test_single_sample(self):
        payload = _build_data_report_payload(100, [(3, 104.421)])
        rep = decode_data_report(payload)
        assert rep.timestamp == 100
        assert len(rep.samples) == 1
        assert rep.samples[0].channel_id == 3
        assert rep.samples[0].value == pytest.approx(104.421, rel=1e-5)

    def test_multiple_samples(self):
        payload = _build_data_report_payload(1000, [(0, 16.0), (3, 104.421), (10, 34.65)])
        rep = decode_data_report(payload)
        assert rep.timestamp == 1000
        assert [s.channel_id for s in rep.samples] == [0, 3, 10]

    def test_too_short_header_raises(self):
        with pytest.raises(CodecError):
            decode_data_report(b"\x00\x00")

    def test_truncated_samples_raises(self):
        # count=2 but only 1 sample provided
        payload = (100).to_bytes(4, "little") + b"\x02" + b"\x00" + struct.pack("<f", 1.0)
        with pytest.raises(CodecError):
            decode_data_report(payload)

    def test_max_channels_is_accepted(self):
        samples = [(cid, float(cid)) for cid in range(DATA_REPORT_MAX_CHANNELS)]
        report = decode_data_report(_build_data_report_payload(0, samples))
        assert len(report.samples) == DATA_REPORT_MAX_CHANNELS
        assert report.samples[-1].channel_id == DATA_REPORT_MAX_CHANNELS - 1

    def test_exceed_max_channels_raises(self):
        payload = (
            (0).to_bytes(4, "little")
            + bytes([DATA_REPORT_MAX_CHANNELS + 1])
        )
        with pytest.raises(CodecError):
            decode_data_report(payload)


# -----------------------------------------------------------------------------
# META_INFO decoding
# -----------------------------------------------------------------------------

class TestDecodeMetaInfo:
    @staticmethod
    def _build_payload(protocol_ver, fw, hw, sn):
        b = bytearray([protocol_ver])
        for s in (fw, hw, sn):
            enc = s.encode("utf-8")
            b.append(len(enc))
            b += enc
        return bytes(b)

    def test_decode_normal(self):
        data = self._build_payload(0x02, "afd01-1.3.2", "afd01", "SN-001")
        meta = decode_meta_info(data)
        assert meta.protocol_ver == 0x02
        assert meta.fw_ver == "afd01-1.3.2"
        assert meta.hw_type == "afd01"
        assert meta.device_sn == "SN-001"

    def test_decode_empty_strings(self):
        data = self._build_payload(0x02, "", "", "")
        meta = decode_meta_info(data)
        assert meta.fw_ver == ""
        assert meta.hw_type == ""

    def test_decode_missing_fields_raises(self):
        # 只有 protocol_ver
        with pytest.raises(CodecError):
            decode_meta_info(bytes([0x02]))


# -----------------------------------------------------------------------------
# CHANNEL_DEFINE decoding
# -----------------------------------------------------------------------------

class TestDecodeChannelDefine:
    @staticmethod
    def _build(table_ver, entries):
        b = bytearray([table_ver, len(entries)])
        for (cid, dtype, grp, flags, name, unit, dmin, dmax) in entries:
            b.extend([cid, dtype, grp, flags])
            nb, ub = name.encode("utf-8"), unit.encode("utf-8")
            b.append(len(nb)); b += nb
            b.append(len(ub)); b += ub
            b += struct.pack("<ff", dmin, dmax)
        return bytes(b)

    def test_decode_single(self):
        data = self._build(
            5,
            [(0, 0x01, 0, 0x02, "roll", "°", -180.0, 180.0)],
        )
        t = decode_channel_define(data)
        assert t.table_ver == 5
        assert len(t.channels) == 1
        c = t.channels[0]
        assert c.channel_id == 0
        assert c.name == "roll"
        assert c.unit == "°"
        assert c.display_min == pytest.approx(-180.0)
        assert c.display_max == pytest.approx(180.0)
        assert c.critical

    def test_decode_multiple(self):
        data = self._build(
            1,
            [
                (0, 0x01, 0, 0x03, "roll", "°", -180.0, 180.0),
                (10, 0x01, 2, 0x02, "snr", "dB", 0.0, 60.0),
            ],
        )
        t = decode_channel_define(data)
        assert len(t.channels) == 2
        assert t.channels[1].channel_id == 10
        assert t.channels[1].unit == "dB"


# -----------------------------------------------------------------------------
# STATE_DEFINE decoding（覆盖 BOOL + ENUM）
# -----------------------------------------------------------------------------

class TestDecodeStateDefine:
    def test_decode_bool_and_enum(self):
        # 手工构造：一个 BOOL + 一个 ENUM(3 项)
        b = bytearray([7, 2])  # table_ver, count

        # state 0: BOOL, critical, name="LOCK_FLAG", enum_count=0
        name0 = b"LOCK_FLAG"
        b.extend([0, 0, 0x01, len(name0)])
        b += name0
        b.append(0)

        # state 1: ENUM, critical, name="TRACE_MODE", enum_count=3
        name1 = b"TRACE_MODE"
        b.extend([1, 1, 0x01, len(name1)])
        b += name1
        b.append(3)
        for val, level, item_name in [(0, 3, "STANDBY"), (2, 1, "WIDE"), (3, 0, "LOCK")]:
            ib = item_name.encode("utf-8")
            b.extend([val, level, len(ib)])
            b += ib

        t = decode_state_define(bytes(b))
        assert t.table_ver == 7
        assert len(t.states) == 2
        assert t.states[0].name == "LOCK_FLAG"
        assert t.states[0].state_type == 0
        assert t.states[1].name == "TRACE_MODE"
        assert [e.name for e in t.states[1].enums] == ["STANDBY", "WIDE", "LOCK"]


# -----------------------------------------------------------------------------
# EVENT_DEFINE decoding
# -----------------------------------------------------------------------------

class TestDecodeEventDefine:
    def test_decode_events(self):
        b = bytearray([3, 2])
        for eid, lvl, name in [(0x0003, 1, "LOCK_ACQUIRED"), (0xFFFF, 1, "USER_MARK")]:
            b.extend(eid.to_bytes(2, "little"))
            b.append(lvl)
            nb = name.encode("utf-8")
            b.append(len(nb)); b += nb
        t = decode_event_define(bytes(b))
        assert t.table_ver == 3
        assert len(t.events) == 2
        assert t.events[0].event_id == 0x0003
        assert t.events[1].event_id == 0xFFFF


# -----------------------------------------------------------------------------
# STATE_REPORT / EVENT_REPORT / HEARTBEAT / COMMAND_RESPONSE
# -----------------------------------------------------------------------------

class TestDecodeOthers:
    def test_state_report(self):
        # timestamp=100, count=2, (state_id=0, val=3), (state_id=1, val=1)
        payload = (100).to_bytes(4, "little") + b"\x02" + b"\x00\x03\x01\x01"
        rep = decode_state_report(payload)
        assert rep.timestamp == 100
        assert len(rep.states) == 2
        assert rep.states[0].state_id == 0 and rep.states[0].value == 3
        assert rep.states[1].state_id == 1 and rep.states[1].value == 1

    def test_event_report_empty_payload(self):
        payload = (100).to_bytes(4, "little") + (0x0003).to_bytes(2, "little") + b"\x00"
        ev = decode_event_report(payload)
        assert ev.timestamp == 100
        assert ev.event_id == 0x0003
        assert ev.payload == b""

    def test_event_report_with_payload(self):
        text = b"point A"
        payload = (50).to_bytes(4, "little") + (0xFFFF).to_bytes(2, "little") + bytes([len(text)]) + text
        ev = decode_event_report(payload)
        assert ev.event_id == 0xFFFF
        assert ev.payload == text

    def test_event_report_payload_too_long_header(self):
        # payload_len=201 但实际没带数据
        payload = (0).to_bytes(4, "little") + (0x0001).to_bytes(2, "little") + bytes([201])
        with pytest.raises(CodecError):
            decode_event_report(payload)

    def test_heartbeat(self):
        payload = struct.pack("<IBIHHI", 12345, 37, 65536, 100, 100, 0)
        hb = decode_heartbeat(payload)
        assert hb.uptime_ms == 12345
        assert hb.cpu_load == 37
        assert hb.free_heap == 65536
        assert hb.rx_frame_rate == 100
        assert hb.tx_frame_rate == 100

    def test_heartbeat_too_short(self):
        with pytest.raises(CodecError):
            decode_heartbeat(b"\x00" * 10)

    def test_command_response(self):
        payload = bytes([0]) + b"OK"
        resp = decode_command_response(payload)
        assert resp.code == 0
        assert resp.msg == "OK"

    def test_command_response_empty_msg(self):
        resp = decode_command_response(bytes([5]))
        assert resp.code == 5
        assert resp.msg == ""

    def test_command_response_trailing_null(self):
        resp = decode_command_response(bytes([0]) + b"OK\x00\x00")
        assert resp.msg == "OK"


# -----------------------------------------------------------------------------
# PROFILE_SEMANTICS decoding
# -----------------------------------------------------------------------------

class TestDecodeProfileSemantics:
    def _s(self, text: str) -> bytes:
        raw = text.encode("utf-8")
        return bytes([len(raw)]) + raw

    def test_decode_roles_states_capabilities(self):
        payload = bytearray()
        payload += bytes([7, 1])              # table_ver, channel_count
        payload += bytes([22, 1])             # channel_id, role_count
        payload += self._s("gps_lat")
        payload += bytes([1])                 # state_count
        payload += bytes([5])
        payload += self._s("trace_mode")
        payload += bytes([SubCmd.SET_TRACE_MODE, 0])
        payload += bytes([2])                 # capability_count
        payload += self._s("parameters") + bytes([1])
        payload += self._s("channel_enable_mask") + bytes([0])

        report = decode_profile_semantics(bytes(payload))

        assert report.table_ver == 7
        assert report.channels[0].channel_id == 22
        assert report.channels[0].roles == ["gps_lat"]
        assert report.states[0].state_id == 5
        assert report.states[0].role == "trace_mode"
        assert report.states[0].control_subcmd == SubCmd.SET_TRACE_MODE
        assert report.capabilities[0].name == "parameters"
        assert report.capabilities[0].supported is True
        assert report.capabilities[1].supported is False


# -----------------------------------------------------------------------------
# Round-trip through build_frame（参照协议文档 §A.1 示例大小）
# -----------------------------------------------------------------------------

class TestRoundTripFrames:
    def test_data_report_3ch(self):
        payload = _build_data_report_payload(100, [(0, 16.0), (3, 104.421), (10, 34.65)])
        frame = build_frame(CmdType.DATA_REPORT, payload)
        # 协议文档 §A.1 给出此例为 9 + 15 + 2 + 1 = 27 字节？重算：
        # header4 + len2 + (4ts + 1count + 3*5samples) + crc2 + footer1
        # = 4 + 2 + 4 + 1 + 15 + 2 + 1 = 29
        assert len(frame) == 29
