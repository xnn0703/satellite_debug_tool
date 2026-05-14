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
    build_frame,
)
from satellite_debug_tool.io.data_importer import DataImporter, SdbFormatError
from satellite_debug_tool.io.data_recorder import (
    DataRecorder,
    SDB_FOOTER,
    SDB_MAGIC,
    SDB_VERSION_V2,
)


# ---------- helpers ----------

def _build_data_report_bytes(ts_ms: int, samples: list[tuple[int, float]]) -> bytes:
    payload = struct.pack("<I", ts_ms) + bytes([len(samples)])
    for cid, val in samples:
        payload += bytes([cid]) + struct.pack("<f", val)
    return build_frame(CmdType.DATA_REPORT, payload)


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
