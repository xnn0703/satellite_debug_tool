"""DataRecorder / DataImporter (SDB v2) 单元测试。"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from satellite_debug_tool.core.profile import DeviceProfile, ProfileStore
from satellite_debug_tool.core.profile.cache import profile_to_dict
from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    CmdType,
    DataReport,
    StateDefineTable,
    build_frame,
)
from satellite_debug_tool.io.data_importer import DataImporter, SdbFormatError
from satellite_debug_tool.io.data_recorder import (
    DataRecorder,
    SDB_FOOTER,
    SDB_MAGIC,
    SDB_VERSION_V2,
    SDB_VERSION_V3,
)


# ---------- helpers ----------

def _build_data_report_bytes(ts_ms: int, samples: list[tuple[int, float]]) -> bytes:
    payload = struct.pack("<I", ts_ms) + bytes([len(samples)])
    for cid, val in samples:
        payload += bytes([cid]) + struct.pack("<f", val)
    return build_frame(CmdType.DATA_REPORT, payload)


def _build_afd01_state_define_payload() -> bytes:
    """按当前 AFD01 的 21-state 注册表构造真实长帧 fixture。"""
    info, warn, error, neutral = 0, 1, 2, 3
    critical = 0x01
    states = [
        ("TRACKING_MODE", critical, [
            (0, neutral, "STANDBY"),
            (1, warn, "SCAN_GLOBAL"),
            (2, warn, "SCAN_WIDE"),
            (3, info, "LOCK"),
            (4, neutral, "MANUAL"),
        ]),
        ("LOCK_FLAG", critical, []),
        ("GPS_FIX", critical, [
            (0, error, "NO_FIX"),
            (1, warn, "2D"),
            (2, info, "3D"),
            (3, info, "RTK_FIXED"),
            (4, info, "DGNSS"),
            (5, warn, "RTK_FLOAT"),
            (6, error, "STALE"),
        ]),
        ("EXTERNAL_INS_STATUS", critical, [
            (0, error, "INACTIVE"),
            (1, warn, "ALIGNING"),
            (2, warn, "HIGH_VAR"),
            (3, info, "GOOD"),
            (6, error, "FREE"),
            (7, info, "ALIGN_DONE"),
            (8, warn, "DET_ORI"),
            (9, warn, "WAIT_POS"),
            (10, warn, "WAIT_AZ"),
            (11, warn, "INIT_BIAS"),
            (12, warn, "MOTION_DET"),
        ]),
        ("PLL_LOCKED", critical, []),
        ("PA_ENABLED", 0, []),
        ("ANT_ENABLED", 0, []),
        ("WIZNET_LINK", 0, []),
        ("MODEM_CONNECTED", critical, []),
        ("TLE_LOADED", 0, []),
        ("EXTERNAL_INS_POS_TYPE", 0, [
            (0, error, "NONE"),
            (1, info, "FIXEDPOS"),
            (16, warn, "SINGLE"),
            (17, warn, "PSRDIFF"),
            (34, warn, "NARROW_FLOAT"),
            (50, info, "NARROW_INT"),
            (53, warn, "INS_PSRSP"),
            (55, warn, "INS_RTKFLOAT"),
            (56, info, "INS_RTKFIXED"),
        ]),
        ("GNSS_SOURCE", 0, [
            (0, neutral, "UNKNOWN"),
            (1, info, "MG902"),
            (2, info, "BYNAV"),
            (3, warn, "MANUAL"),
            (4, info, "MS6222"),
        ]),
        ("INTERNAL_INS_STATE", 0, [
            (0, neutral, "NONE"),
            (1, warn, "INITIALIZING"),
            (2, info, "ATTITUDE_READY"),
            (3, info, "NAVIGATION_READY"),
            (4, warn, "RP_READY"),
        ]),
        ("INTERNAL_INS_YAW_REFERENCE", 0, [
            (0, neutral, "UNAVAILABLE"),
            (1, warn, "RELATIVE"),
            (2, info, "ABSOLUTE"),
        ]),
        ("OWN", 0, [
            (0, neutral, "NONE"),
            (1, info, "EXTERNAL_INS"),
            (2, neutral, "RESERVED"),
            (3, info, "INTERNAL_ESKF"),
        ]),
        ("SUP", 0, [
            (0, info, "INTERNAL"),
            (1, warn, "QUALIFYING"),
            (2, warn, "BLEND_EXT"),
            (3, info, "EXTERNAL"),
            (4, warn, "BLEND_INT"),
        ]),
        ("REJ", 0, [
            (0, info, "NONE"),
            (1, error, "INTERNAL_UNAVAILABLE"),
            (2, warn, "EXTERNAL_MISSING"),
            (3, error, "FRAME_UNVERIFIED"),
            (4, warn, "HEALTH"),
            (5, warn, "SOLUTION_BAD"),
            (6, warn, "STD"),
            (7, error, "STALE"),
            (8, error, "FUTURE"),
            (9, warn, "TRACKING_UNSTABLE"),
            (10, warn, "TIME_ALIGNMENT"),
            (11, warn, "RP_RESIDUAL"),
            (12, warn, "YAW_RESIDUAL"),
            (13, error, "NUMERIC"),
        ]),
        ("RBV", 0, []),
        ("HOLD", 0, []),
        ("TRUST", 0, [
            (0, warn, "LOW"),
            (1, warn, "MID"),
            (2, info, "HIGH"),
        ]),
        ("EXIT", 0, [
            (0, neutral, "N"),
            (1, warn, "FAST"),
            (2, warn, "LOSS"),
            (3, error, "NORM"),
            (4, error, "ENV"),
            (5, error, "P"),
            (6, error, "TGT"),
            (7, warn, "PR"),
        ]),
    ]

    payload = bytearray((21, len(states)))
    for state_id, (name, flags, enums) in enumerate(states):
        name_bytes = name.encode("utf-8")
        state_type = 1 if enums else 0
        payload.extend((state_id, state_type, flags, len(name_bytes)))
        payload.extend(name_bytes)
        payload.append(len(enums))
        for value, level, enum_name in enums:
            enum_bytes = enum_name.encode("utf-8")
            payload.extend((value, level, len(enum_bytes)))
            payload.extend(enum_bytes)

    assert len(states) == 21
    assert len(payload) == 1170
    return bytes(payload)


def _wait_recorder_flush(recorder: DataRecorder, expected: int, timeout: float = 2.0):
    """等待后台线程把 expected 帧写出去。"""
    import time
    t0 = time.time()
    while recorder.written_count < expected and time.time() - t0 < timeout:
        time.sleep(0.01)
    assert recorder.written_count >= expected, (
        f"recorder only wrote {recorder.written_count}/{expected}"
    )


# ============================================================
# DataRecorder
# ============================================================

class TestRecorderBasic:
    def test_write_without_profile(self, tmp_path: Path):
        path = tmp_path / "r1.sdb"
        rec = DataRecorder(path)
        assert rec.start()
        for i in range(3):
            assert rec.write_frame(_build_data_report_bytes(i * 10, [(0, float(i))]))
        _wait_recorder_flush(rec, 3)
        assert rec.stop()

        raw = path.read_bytes()
        assert raw[:4] == SDB_MAGIC
        assert struct.unpack("<H", raw[4:6])[0] == SDB_VERSION_V2
        # profile_len == 0（无 profile）
        assert struct.unpack("<I", raw[14:18])[0] == 0
        assert raw.endswith(SDB_FOOTER)

    def test_write_with_profile_embedded(self, tmp_path: Path):
        path = tmp_path / "r2.sdb"
        profile_dict = {
            "schema_version": 1, "hw_type": "afd01",
            "channel_table_ver": 1, "state_table_ver": None, "event_table_ver": None,
            "channels": [], "states": [], "events": [], "meta": None,
        }
        rec = DataRecorder(path, profile_dict=profile_dict)
        assert rec.start()
        rec.write_frame(_build_data_report_bytes(0, [(0, 1.0)]))
        _wait_recorder_flush(rec, 1)
        assert rec.stop()

        raw = path.read_bytes()
        profile_len = struct.unpack("<I", raw[14:18])[0]
        assert profile_len > 0
        # 内嵌 JSON 可读回
        import json
        emb = json.loads(raw[18:18 + profile_len].decode("utf-8"))
        assert emb["hw_type"] == "afd01"


class TestRecorderRoundTrip:
    def test_round_trip(self, tmp_path: Path):
        path = tmp_path / "rt.sdb"
        frames = [_build_data_report_bytes(i * 10, [(0, i * 0.5)]) for i in range(5)]
        rec = DataRecorder(path)
        assert rec.start()
        for f in frames:
            rec.write_frame(f)
        _wait_recorder_flush(rec, 5)
        rec.stop()

        sdb = DataImporter.open_sdb(path)
        assert sdb.version == SDB_VERSION_V2
        reports = list(sdb.iter_data_reports())
        assert len(reports) == 5
        assert reports[0].timestamp == 0
        assert reports[4].timestamp == 40
        assert reports[4].samples[0].value == pytest.approx(2.0)

    def test_round_trip_with_profile(self, tmp_path: Path):
        path = tmp_path / "rtp.sdb"

        # 造一个真实 profile
        store = ProfileStore()
        store.apply_channel_define("afd01", 1, [
            ChannelDefEntry(0, 1, 0, 0x02, "roll", "°", -180, 180),
        ])
        prof_dict = profile_to_dict(store.get_profile("afd01"))

        rec = DataRecorder(path, profile_dict=prof_dict)
        assert rec.start()
        rec.write_frame(_build_data_report_bytes(100, [(0, 3.14)]))
        _wait_recorder_flush(rec, 1)
        rec.stop()

        sdb = DataImporter.open_sdb(path)
        assert sdb.profile is not None
        assert sdb.profile["hw_type"] == "afd01"
        # 新 store 可直接 import_dict
        new_store = ProfileStore()
        hw = new_store.import_dict(sdb.profile)
        assert hw == "afd01"
        assert new_store.get_channel("afd01", 0).name == "roll"

    @pytest.mark.parametrize("format_version", [SDB_VERSION_V2, SDB_VERSION_V3])
    def test_afd01_1170_byte_state_define_round_trip(
        self, tmp_path: Path, format_version: int
    ):
        payload = _build_afd01_state_define_payload()
        frame = build_frame(CmdType.STATE_DEFINE, payload)
        assert len(payload) == 1170
        assert len(frame) == 1179

        path = tmp_path / f"afd01-state-v{format_version}.sdb"
        recorder = DataRecorder(path, format_version=format_version)
        assert recorder.start()
        assert recorder.write_frame(frame, host_timestamp_ns=123_000_000)
        _wait_recorder_flush(recorder, 1)
        assert recorder.stop()

        sdb = DataImporter.open_sdb(path)
        records = list(sdb.iter_records())
        assert len(records) == 1
        assert isinstance(records[0], StateDefineTable)
        assert records[0].table_ver == 21
        assert len(records[0].states) == 21
        assert records[0].states[0].name == "TRACKING_MODE"
        assert records[0].states[-1].name == "EXIT"
        assert [item.value for item in records[0].states[14].enums] == [0, 1, 2, 3]
        assert [item.name for item in records[0].states[14].enums] == [
            "NONE", "EXTERNAL_INS", "RESERVED", "INTERNAL_ESKF",
        ]
        assert [item.value for item in records[0].states[10].enums] == [
            0, 1, 16, 17, 34, 50, 53, 55, 56,
        ]

    def test_v3_preserves_host_time_metadata_controls_and_quality(self, tmp_path: Path):
        path = tmp_path / "support-v3.sdb"
        frame = _build_data_report_bytes(250, [(0, 1.25)])
        control = build_frame(CmdType.SERVICE_CONTROL_REQUEST, b"\x01\x01\x00\x00\x00\x00")
        profile = {"schema_version": 1, "hw_type": "afd01", "channels": []}
        recorder = DataRecorder(
            path,
            profile_dict=profile,
            format_version=SDB_VERSION_V3,
            metadata={"capture_profile": "support_full", "serial_number": "AFD01-TEST"},
        )
        assert recorder.start()
        assert recorder.write_frame(frame, host_timestamp_ns=123_000_000)
        assert recorder.write_control_frame(control, host_timestamp_ns=124_000_000)
        _wait_recorder_flush(recorder, 1)
        assert recorder.stop()

        sdb = DataImporter.open_sdb(path)
        assert sdb.version == SDB_VERSION_V3
        assert sdb.profile == profile
        assert sdb.metadata["capture_profile"] == "support_full"
        assert sdb.raw_records[0].host_timestamp_ns == 123_000_000
        assert sdb.control_records[0].data == control
        assert sdb.quality["complete"] is True
        assert sdb.quality["dropped_chunks"] == 0
        timed = list(sdb.iter_timed_records())
        assert timed[0][0] == 123_000_000
        assert isinstance(timed[0][1], DataReport)


# ============================================================
# Importer error handling
# ============================================================

class TestImporterErrors:
    def test_bad_magic(self, tmp_path: Path):
        bad = tmp_path / "bad.sdb"
        bad.write_bytes(b"\x00" * 64)
        with pytest.raises(SdbFormatError):
            DataImporter.open_sdb(bad)

    def test_v1_rejected(self, tmp_path: Path):
        """写一个 v1 header，open_sdb 应该拒绝。"""
        v1 = tmp_path / "v1.sdb"
        with open(v1, "wb") as f:
            f.write(SDB_MAGIC)
            f.write(struct.pack("<H", 0x0001))      # version = 1
            f.write(struct.pack("<Q", 0))           # timestamp
            f.write(b"\x00" * 8)                    # reserved
            f.write(SDB_FOOTER)
        with pytest.raises(SdbFormatError):
            DataImporter.open_sdb(v1)

    def test_truncated_header(self, tmp_path: Path):
        trunc = tmp_path / "t.sdb"
        trunc.write_bytes(SDB_MAGIC + b"\x02\x00")  # 只到 version
        with pytest.raises(SdbFormatError):
            DataImporter.open_sdb(trunc)
