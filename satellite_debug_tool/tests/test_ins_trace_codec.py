"""INS工程流的字节合同：原始单位、精度、身份和SDB持久化。"""
from __future__ import annotations

import math
import struct

import pytest

from satellite_debug_tool.core.protocol import (
    CmdType, FrameReceiverV2, InsTraceRecord, build_frame, decode_ins_trace,
)
from satellite_debug_tool.core.protocol.codec_v2 import CodecError
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.io.data_recorder import DataRecorder, SDB_VERSION_V3


def _header(kind: int, flags: int = 0) -> bytes:
    return struct.pack("<4BIIQQI", 1, kind, 3, 1, 0x12345678, 0x23456789,
                       0x0102030405060708, 0x1112131415161718, flags)


def test_imu_delta_wire_keeps_dt_and_increment_units():
    data = _header(1, 1) + struct.pack("<BBH8f", 1, 3, 7, .01, 25, .1, -.2, .3, 1, -2, 3)
    record = decode_ins_trace(data)
    assert record.kind == 1
    assert record.seq == 0x12345678
    assert record.timestamp_us == 0x0102030405060708
    assert record.received_timestamp_us == 0x1112131415161718
    assert record.values["sample_type"] == 1
    assert record.values["dt_s"] == pytest.approx(.01)
    assert record.values["delta_ang_rad"] == pytest.approx((.1, -.2, .3))
    assert "gyro_rad_s" not in record.values
    rate = bytearray(data)
    rate[32] = 0
    rate_record = decode_ins_trace(bytes(rate))
    assert rate_record.values["gyro_rad_s"] == pytest.approx((.1, -.2, .3))


def test_gnss_float64_and_invalid_edge_are_preserved():
    data = _header(2, 4) + struct.pack("<dd12f4B", 31.1234567890123, 121.234567890123,
        *range(12), 2, 3, 4, 18)
    record = decode_ins_trace(data)
    assert record.values["lat_deg"] == 31.1234567890123
    assert record.values["lon_deg"] == 121.234567890123
    assert record.flags == 4
    assert record.values["vel_n_ms"] == 4
    assert record.values["num_svs"] == 18


def test_filter_and_assisted_direction_have_separate_time():
    data = _header(3, 3) + struct.pack("<23f7I12BQI3f4Bf", *range(23),
        123, 4, 5, 6, 7, 8, 17, *range(12), 900000, 7, -4, .5, 0, 2, 1, 1, 0, -12.5)
    assert len(data) == 196
    record = decode_ins_trace(data)
    assert record.values["gyro_bias_dps"] == (7, 8, 9)
    assert record.values["zaru_update_count"] == 123
    assert record.values["trace_dropped_records"] == 17
    assert record.values["direction_timestamp_us"] == 900000
    assert record.values["longitudinal_m_s"] == -4
    assert record.values["external_assisted"] is True
    assert record.values["yaw_correction_sum_deg"] == -12.5


def test_rf_ack_and_nonfinite_fault_evidence_are_preserved():
    data = _header(4, 1) + struct.pack("<3I4B7fI", 8, 5, 1900, 1, 2, 0, 0,
        .2, .01, float("nan"), .1, 90, 30, 10, 17)
    record = decode_ins_trace(data)
    assert record.values["target_generation"] == 8
    assert record.values["authority_epoch"] == 5
    assert record.values["rf_measurement_timestamp_ms"] == 1900
    assert record.values["source_command_seq"] == record.source_seq
    assert record.values["internal_shadow_correction_sequence"] == 17
    assert math.isnan(record.values["innovation_rad"])
    assert record.flags & 2 == 0  # 影子ACK不表示影响当前业务姿态。


def test_update_groups_gain_and_numeric_status_are_preserved():
    groups = b"".join(struct.pack("<I3f2I4B", 100 + i, .5 + i, 2, .2, 7, 8, 1, 0, 3, 0) for i in range(4))
    data = _header(5, 7) + groups + struct.pack("<6f4I4BI", .5, .1, 2.5, 3, .2, .05,
        9, 10, 11, 12, 1, 2, 3, 4, 17)
    assert len(data) == 192
    record = decode_ins_trace(data)
    assert record.values["gnss_groups"][2]["nis"] == 2.5
    assert record.values["effective_gyro_bias_gain_max"] == pytest.approx(.1)
    assert record.values["numeric_fault_count"] == 9
    assert record.values["yaw_correction_seq"] == 17


@pytest.mark.parametrize("kind,length", [(1, 68), (2, 100), (3, 196), (4, 80), (5, 192)])
def test_invalid_lengths_schema_and_flags_are_rejected(kind, length):
    data = _header(kind) + bytes(length - 32)
    for invalid in (data[:-1], data + b"\0", b"\2" + data[1:], data[:28] + struct.pack("<I", 0x80000000) + data[32:]):
        with pytest.raises(CodecError):
            decode_ins_trace(invalid)


def test_framing_chunk_split_and_sdb_round_trip(tmp_path):
    payload = _header(1) + struct.pack("<BBH8f", 0, 0, 0, .005, 25, 1, 2, 3, 4, 5, 6)
    frame = build_frame(CmdType.INS_TRACE, payload)
    receiver = FrameReceiverV2()
    assert receiver.feed(frame[:19]) == []
    result = receiver.feed(frame[19:])
    assert len(result) == 1 and isinstance(result[0], InsTraceRecord)
    assert receiver.crc_errors == 0
    path = tmp_path / "trace.sdb"
    recorder = DataRecorder(path, format_version=SDB_VERSION_V3)
    assert recorder.start()
    assert recorder.write_frame(frame[:19], host_timestamp_ns=10)
    assert recorder.write_frame(frame[19:], host_timestamp_ns=20)
    recorder.stop()
    sdb = DataImporter.open_sdb(path)
    records = list(sdb.iter_timed_records())
    assert len(records) == 1
    assert records[0][1] == result[0]
    assert sdb.quality["complete"]
    # 工程流具有多个测量时钟，不进入业务回放的单一U32显示轴。
    from satellite_debug_tool.core.session.telemetry_clock import TelemetryClock
    from satellite_debug_tool.core.playback.loader import build_engineering_playback
    from satellite_debug_tool.core.product.service_store import ProductServiceStore
    assert TelemetryClock.timestamp(result[0]) is None
    assert ProductServiceStore().feed(result[0]) is False
    playback = build_engineering_playback(path)
    playback.close()
