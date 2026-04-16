"""ProfileStore + ProfileCache 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from satellite_debug_tool.core.profile import DeviceProfile, ProfileCache, ProfileStore
from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    EventDefEntry,
    MetaInfo,
    StateDefEntry,
    StateEnumItem,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def tmp_cache(tmp_path: Path) -> ProfileCache:
    return ProfileCache(cache_dir=tmp_path / "profiles")


@pytest.fixture
def sample_channels():
    return [
        ChannelDefEntry(
            channel_id=0, data_type=1, group_id=0, flags=0x03,
            name="roll", unit="°", display_min=-180.0, display_max=180.0,
        ),
        ChannelDefEntry(
            channel_id=10, data_type=1, group_id=2, flags=0x02,
            name="snr", unit="dB", display_min=0.0, display_max=60.0,
        ),
    ]


@pytest.fixture
def sample_states():
    return [
        StateDefEntry(
            state_id=0, state_type=1, flags=0x01, name="TRACE_MODE",
            enums=[
                StateEnumItem(value=0, level=3, name="STANDBY"),
                StateEnumItem(value=3, level=0, name="LOCK"),
            ],
        ),
        StateDefEntry(state_id=1, state_type=0, flags=0x01, name="LOCK_FLAG"),
    ]


@pytest.fixture
def sample_events():
    return [
        EventDefEntry(event_id=0x0003, level=1, name="LOCK_ACQUIRED"),
        EventDefEntry(event_id=0xFFFF, level=1, name="USER_MARK"),
    ]


# -----------------------------------------------------------------------------
# ProfileStore basic
# -----------------------------------------------------------------------------

class TestProfileStoreBasic:
    def test_apply_meta_sets_current_hw_type(self):
        store = ProfileStore()
        meta = MetaInfo(protocol_ver=2, fw_ver="afd01-1.0", hw_type="afd01", device_sn="SN1")
        store.apply_meta(meta)
        assert store.current_hw_type() == "afd01"
        assert store.get_profile("afd01").meta == meta

    def test_apply_channel_define(self, sample_channels):
        store = ProfileStore()
        store.apply_channel_define("afd01", 1, sample_channels)
        chs = store.get_channels("afd01")
        assert len(chs) == 2
        assert store.get_channel("afd01", 0).name == "roll"
        assert store.get_channel("afd01", 10).unit == "dB"

    def test_apply_state_and_event(self, sample_states, sample_events):
        store = ProfileStore()
        store.apply_state_define("afd01", 1, sample_states)
        store.apply_event_define("afd01", 1, sample_events)
        assert len(store.get_states("afd01")) == 2
        assert store.get_event("afd01", 0x0003).name == "LOCK_ACQUIRED"
        assert store.get_event("afd01", 0xFFFF).name == "USER_MARK"

    def test_unknown_hw_returns_empty(self):
        store = ProfileStore()
        assert store.get_channels("ufd45") == []
        assert store.get_channel("ufd45", 0) is None

    def test_is_ready(self, sample_channels, sample_states, sample_events):
        store = ProfileStore()
        store.apply_channel_define("afd01", 1, sample_channels)
        assert not store.is_ready("afd01")
        store.apply_state_define("afd01", 1, sample_states)
        assert not store.is_ready("afd01")
        store.apply_event_define("afd01", 1, sample_events)
        assert store.is_ready("afd01")

    def test_version_skip_reapply(self, sample_channels):
        store = ProfileStore()
        store.apply_channel_define("afd01", 1, sample_channels)
        # 重新 apply 相同 table_ver 且内容非空 → 应跳过（不重复触发）
        signal_fires = []
        store.profile_changed.connect(signal_fires.append)
        store.apply_channel_define("afd01", 1, sample_channels)
        assert signal_fires == []

    def test_version_bump_reapply(self, sample_channels):
        store = ProfileStore()
        store.apply_channel_define("afd01", 1, sample_channels)
        signal_fires = []
        store.profile_changed.connect(signal_fires.append)

        new_entry = [
            ChannelDefEntry(
                channel_id=1, data_type=1, group_id=0, flags=0x01,
                name="pitch", unit="°", display_min=-90.0, display_max=90.0,
            ),
        ]
        store.apply_channel_define("afd01", 2, new_entry)
        assert signal_fires == ["afd01"]
        assert store.get_channel("afd01", 0) is None   # 旧表清空
        assert store.get_channel("afd01", 1).name == "pitch"


# -----------------------------------------------------------------------------
# ProfileStore signals
# -----------------------------------------------------------------------------

class TestProfileStoreSignals:
    def test_profile_changed_emitted_per_table(self, sample_channels, sample_states, sample_events):
        store = ProfileStore()
        seen = []
        store.profile_changed.connect(seen.append)

        store.apply_meta(MetaInfo(2, "fw", "afd01", "sn"))
        store.apply_channel_define("afd01", 1, sample_channels)
        store.apply_state_define("afd01", 1, sample_states)
        store.apply_event_define("afd01", 1, sample_events)

        assert seen == ["afd01", "afd01", "afd01", "afd01"]


# -----------------------------------------------------------------------------
# Multi-hw 分桶
# -----------------------------------------------------------------------------

class TestMultiHw:
    def test_buckets_are_isolated(self):
        store = ProfileStore()
        afd01_ch = [ChannelDefEntry(0, 1, 0, 0x02, "roll_a", "°", -180, 180)]
        ufd45_ch = [ChannelDefEntry(0, 1, 0, 0x02, "channel_X", "x", 0, 1)]
        store.apply_channel_define("afd01", 1, afd01_ch)
        store.apply_channel_define("ufd45", 1, ufd45_ch)

        assert store.get_channel("afd01", 0).name == "roll_a"
        assert store.get_channel("ufd45", 0).name == "channel_X"
        assert store.has_profile("afd01") and store.has_profile("ufd45")


# -----------------------------------------------------------------------------
# Cache
# -----------------------------------------------------------------------------

class TestProfileCache:
    def test_save_and_load_roundtrip(
        self, tmp_cache: ProfileCache, sample_channels, sample_states, sample_events
    ):
        p = DeviceProfile(
            hw_type="afd01",
            channel_table_ver=5, state_table_ver=3, event_table_ver=1,
            meta=MetaInfo(2, "afd01-1", "afd01", "SN-A"),
        )
        p.channels = {c.channel_id: c for c in sample_channels}
        p.states = {s.state_id: s for s in sample_states}
        p.events = {e.event_id: e for e in sample_events}

        tmp_cache.save(p)
        loaded = tmp_cache.load("afd01")
        assert loaded is not None
        assert loaded.hw_type == "afd01"
        assert loaded.channel_table_ver == 5
        assert loaded.get_channel(0).name == "roll"
        assert loaded.get_state(0).enums[1].name == "LOCK"
        assert loaded.get_event(0xFFFF).name == "USER_MARK"
        assert loaded.meta.device_sn == "SN-A"

    def test_load_missing_returns_none(self, tmp_cache: ProfileCache):
        assert tmp_cache.load("not_exist") is None

    def test_store_persists_via_cache(
        self, tmp_path: Path, sample_channels
    ):
        cache = ProfileCache(tmp_path / "profiles")
        store = ProfileStore(cache=cache)
        store.apply_channel_define("afd01", 1, sample_channels)
        # 磁盘文件应存在
        assert (tmp_path / "profiles" / "afd01.json").is_file()

        # 新 store 直接 load_cached 能恢复
        new_store = ProfileStore(cache=cache)
        assert new_store.load_cached("afd01") is True
        assert new_store.get_channel("afd01", 10).unit == "dB"

    def test_delete_cache(self, tmp_cache: ProfileCache, sample_channels):
        p = DeviceProfile(hw_type="afd01", channel_table_ver=1)
        p.channels = {c.channel_id: c for c in sample_channels}
        tmp_cache.save(p)
        assert tmp_cache.delete("afd01") is True
        assert tmp_cache.delete("afd01") is False


# -----------------------------------------------------------------------------
# Export / Import
# -----------------------------------------------------------------------------

class TestExportImport:
    def test_export_import(
        self, tmp_path: Path, sample_channels, sample_states, sample_events
    ):
        store = ProfileStore()
        store.apply_channel_define("afd01", 2, sample_channels)
        store.apply_state_define("afd01", 2, sample_states)
        store.apply_event_define("afd01", 2, sample_events)

        out_path = tmp_path / "afd01_profile.json"
        assert store.export("afd01", out_path) is True
        assert out_path.is_file()

        store2 = ProfileStore()
        hw_type = store2.import_(out_path)
        assert hw_type == "afd01"
        assert store2.get_channel("afd01", 10).unit == "dB"

    def test_export_missing_returns_false(self, tmp_path: Path):
        store = ProfileStore()
        assert store.export("nope", tmp_path / "x.json") is False

    def test_import_invalid_returns_none(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json at all", encoding="utf-8")
        store = ProfileStore()
        assert store.import_(bad) is None
