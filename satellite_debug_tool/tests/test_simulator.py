"""
模拟器 smoke 测试：验证 tools/device_simulator.py 生成的帧能被上位机
FrameReceiverV2 正确解析，且 profile 数据能通过 ProfileStore 和 Handshake
完整消费。

不开 UDP socket（run loop），只测 encode → build_frame → decode 对称性。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


# ---- 动态加载 tools/device_simulator.py（非包内模块） ----

_SIM_PATH = Path(__file__).resolve().parents[2] / "tools" / "device_simulator.py"


def _load_sim_module():
    spec = importlib.util.spec_from_file_location("device_simulator_mod", _SIM_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["device_simulator_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


SIM = _load_sim_module()


from satellite_debug_tool.core.profile import ProfileStore  # noqa: E402
from satellite_debug_tool.core.protocol import (  # noqa: E402
    ChannelDefineTable,
    CmdType,
    DataReport,
    EventDefineTable,
    FrameReceiverV2,
    Heartbeat,
    MetaInfo,
    PROTOCOL_VERSION,
    StateDefineTable,
    StateReport,
    build_frame,
)
from satellite_debug_tool.core.protocol.handshake import Handshake  # noqa: E402


@pytest.fixture(params=["afd01", "ufd45"])
def profile(request):
    return SIM.PROFILES[request.param]()


# ============================================================
# encode → decode 对称性
# ============================================================

class TestEncodeRoundTrip:
    def _decode_one(self, cmd: int, data: bytes):
        """把单帧 feed 给 FrameReceiverV2 并返回解码对象。"""
        rx = FrameReceiverV2()
        frame = build_frame(cmd, data)
        out = rx.feed(frame)
        assert len(out) == 1
        return out[0]

    def test_meta_info(self, profile):
        rec = self._decode_one(CmdType.META_INFO, SIM.encode_meta_info(profile))
        assert isinstance(rec, MetaInfo)
        assert rec.protocol_ver == PROTOCOL_VERSION
        assert rec.hw_type == profile.hw_type
        assert rec.fw_ver == profile.fw_ver
        assert rec.device_sn == profile.device_sn

    def test_channel_define(self, profile):
        rec = self._decode_one(CmdType.CHANNEL_DEFINE, SIM.encode_channel_define(profile))
        assert isinstance(rec, ChannelDefineTable)
        assert rec.table_ver == profile.channel_table_ver
        assert len(rec.channels) == len(profile.channels)
        ids_spec = [c.id for c in profile.channels]
        ids_wire = [c.channel_id for c in rec.channels]
        assert ids_spec == ids_wire
        # 单位/名称保真
        names_spec = [c.name for c in profile.channels]
        names_wire = [c.name for c in rec.channels]
        assert names_spec == names_wire

    def test_state_define(self, profile):
        rec = self._decode_one(CmdType.STATE_DEFINE, SIM.encode_state_define(profile))
        assert isinstance(rec, StateDefineTable)
        assert len(rec.states) == len(profile.states)
        # ENUM 项数量保真
        for spec, wire in zip(profile.states, rec.states):
            assert spec.id == wire.state_id
            assert spec.name == wire.name
            assert len(spec.enums) == len(wire.enums)

    def test_event_define(self, profile):
        rec = self._decode_one(CmdType.EVENT_DEFINE, SIM.encode_event_define(profile))
        assert isinstance(rec, EventDefineTable)
        assert len(rec.events) == len(profile.events)
        for spec, wire in zip(profile.events, rec.events):
            assert spec.id == wire.event_id
            assert spec.name == wire.name
            assert spec.level == wire.level

    def test_data_report(self):
        samples = [(0, 1.5), (3, -0.7), (10, 25.25)]
        rec = self._decode_one(CmdType.DATA_REPORT, SIM.encode_data_report(123, samples))
        assert isinstance(rec, DataReport)
        assert rec.timestamp == 123
        got = [(s.channel_id, round(s.value, 3)) for s in rec.samples]
        assert got == [(0, 1.5), (3, -0.7), (10, 25.25)]

    def test_state_report(self):
        rec = self._decode_one(
            CmdType.STATE_REPORT, SIM.encode_state_report(100, [(0, 3), (1, 1)]),
        )
        assert isinstance(rec, StateReport)
        assert rec.timestamp == 100
        assert [(s.state_id, s.value) for s in rec.states] == [(0, 3), (1, 1)]

    def test_heartbeat(self):
        rec = self._decode_one(
            CmdType.HEARTBEAT, SIM.encode_heartbeat(12345, 30, 65536, 50, 50),
        )
        assert isinstance(rec, Heartbeat)
        assert rec.uptime_ms == 12345
        assert rec.cpu_load == 30
        assert rec.free_heap == 65536


# ============================================================
# 端到端：simulator 编码 → receiver → Handshake → ProfileStore
# ============================================================

class TestEndToEndHandshake:
    def test_full_handshake_drives_ready(self, profile):
        store = ProfileStore()
        sent: list[bytes] = []
        hs = Handshake(store, sent.append)
        ready: list[str] = []
        hs.ready.connect(ready.append)
        hs.start()

        # 模拟 simulator 的广播顺序
        rx = FrameReceiverV2()

        frames = [
            build_frame(CmdType.META_INFO,      SIM.encode_meta_info(profile)),
            build_frame(CmdType.CHANNEL_DEFINE, SIM.encode_channel_define(profile)),
            build_frame(CmdType.STATE_DEFINE,   SIM.encode_state_define(profile)),
            build_frame(CmdType.EVENT_DEFINE,   SIM.encode_event_define(profile)),
        ]
        for f in frames:
            for rec in rx.feed(f):
                hs.feed(rec)

        assert hs.is_ready, "握手未完成"
        assert ready == [profile.hw_type]

        # ProfileStore 应该已经拥有完整 profile
        assert store.is_ready(profile.hw_type)
        assert len(store.get_channels(profile.hw_type)) == len(profile.channels)
        assert len(store.get_states(profile.hw_type)) == len(profile.states)
        assert len(store.get_events(profile.hw_type)) == len(profile.events)
