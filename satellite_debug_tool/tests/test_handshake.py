"""Handshake 状态机测试。"""

from __future__ import annotations

import pytest

from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    ChannelDefineTable,
    EventDefEntry,
    EventDefineTable,
    Heartbeat,
    MetaInfo,
    StateDefEntry,
    StateDefineTable,
    SubCmd,
)
from satellite_debug_tool.core.protocol.handshake import Handshake


@pytest.fixture
def sender_sink():
    sent: list[bytes] = []
    return sent, sent.append


def _subcmd(frame: bytes) -> int:
    # frame = AA 55 0D 03 <len_lo> <len_hi> <sub_cmd> ...
    return frame[6]


class TestStartSendsRequests:
    def test_start_sends_four_requests(self, sender_sink):
        sent, send = sender_sink
        store = ProfileStore()
        hs = Handshake(store, send)
        hs.start()
        assert len(sent) == 4
        sub_cmds = [_subcmd(f) for f in sent]
        assert sub_cmds == [
            SubCmd.REQUEST_META_INFO,
            SubCmd.REQUEST_CHANNEL_DEFINE,
            SubCmd.REQUEST_STATE_DEFINE,
            SubCmd.REQUEST_EVENT_DEFINE,
        ]

    def test_stop_disables(self, sender_sink):
        sent, send = sender_sink
        store = ProfileStore()
        hs = Handshake(store, send)
        hs.start()
        sent.clear()
        hs.stop()
        hs.tick(10_000)
        assert sent == []


class TestReadyTransition:
    def _build_records(self):
        meta = MetaInfo(2, "afd01-1.0", "afd01", "SN-A")
        channel_table = ChannelDefineTable(
            table_ver=1,
            channels=[ChannelDefEntry(0, 1, 0, 0x02, "roll", "°", -180, 180)],
        )
        state_table = StateDefineTable(
            table_ver=1,
            states=[StateDefEntry(state_id=0, state_type=0, flags=0x01, name="LOCK_FLAG")],
        )
        event_table = EventDefineTable(
            table_ver=1,
            events=[EventDefEntry(event_id=0x0003, level=1, name="LOCK_ACQUIRED")],
        )
        return meta, channel_table, state_table, event_table

    def test_ready_after_all_defines(self, sender_sink):
        sent, send = sender_sink
        store = ProfileStore()
        hs = Handshake(store, send)

        ready_fires: list[str] = []
        hs.ready.connect(ready_fires.append)

        hs.start()
        meta, ch, st, ev = self._build_records()

        hs.feed(meta)
        assert not hs.is_ready
        hs.feed(ch)
        assert not hs.is_ready
        hs.feed(st)
        assert not hs.is_ready
        hs.feed(ev)

        assert hs.is_ready
        assert ready_fires == ["afd01"]

    def test_define_before_meta_is_ignored(self, sender_sink):
        """如果设备先发 DEFINE 再发 META，握手应该先忽略 DEFINE 并等 META。"""
        sent, send = sender_sink
        store = ProfileStore()
        hs = Handshake(store, send)
        hs.start()
        _, ch, st, ev = self._build_records()
        hs.feed(ch)
        hs.feed(st)
        hs.feed(ev)
        assert not hs.is_ready
        # 此时 META 尚未到，store 里没 hw_type
        assert store.current_hw_type() is None


class TestResendOnTimeout:
    def test_resend_only_missing(self, sender_sink):
        sent, send = sender_sink
        store = ProfileStore()
        hs = Handshake(store, send, define_timeout_ms=100)
        hs.start()
        sent.clear()

        # 接收 META
        hs.feed(MetaInfo(2, "fw", "afd01", "sn"))
        # 还没收到三张 DEFINE，tick 100ms 后应重发 3 条
        hs.tick(100)
        assert len(sent) == 3
        sub_cmds = sorted(_subcmd(f) for f in sent)
        assert sub_cmds == sorted([
            SubCmd.REQUEST_CHANNEL_DEFINE,
            SubCmd.REQUEST_STATE_DEFINE,
            SubCmd.REQUEST_EVENT_DEFINE,
        ])

    def test_no_resend_when_ready(self, sender_sink):
        sent, send = sender_sink
        store = ProfileStore()
        hs = Handshake(store, send, define_timeout_ms=50)
        hs.start()

        hs.feed(MetaInfo(2, "fw", "afd01", "sn"))
        hs.feed(ChannelDefineTable(1, [ChannelDefEntry(0, 1, 0, 0, "a", "", 0, 1)]))
        hs.feed(StateDefineTable(1, [StateDefEntry(0, 0, 0, "B")]))
        hs.feed(EventDefineTable(1, [EventDefEntry(1, 1, "E")]))
        assert hs.is_ready
        sent.clear()

        hs.tick(1000)
        assert sent == []

    def test_define_timeout_signal(self, sender_sink):
        sent, send = sender_sink
        store = ProfileStore()
        hs = Handshake(store, send, define_timeout_ms=10)
        fires: list[str] = []
        hs.define_timeout.connect(fires.append)
        hs.start()
        hs.tick(10)
        # 所有都缺 → 4 类都 emit
        assert sorted(fires) == sorted(["meta", "channel", "state", "event"])


class TestHeartbeat:
    def _hb(self):
        return Heartbeat(uptime_ms=100, cpu_load=10, free_heap=1024,
                         rx_frame_rate=50, tx_frame_rate=50)

    def test_heartbeat_sets_alive(self, sender_sink):
        sent, send = sender_sink
        store = ProfileStore()
        hs = Handshake(store, send)
        hs.start()
        assert not hs.heartbeat_alive
        hs.feed(self._hb())
        assert hs.heartbeat_alive

    def test_link_lost_after_timeout(self, sender_sink):
        sent, send = sender_sink
        store = ProfileStore()
        hs = Handshake(store, send, heartbeat_timeout_ms=1000)
        hs.start()
        hs.feed(self._hb())
        lost_fires: list[None] = []
        hs.link_lost.connect(lambda: lost_fires.append(None))
        hs.tick(1000)
        assert not hs.heartbeat_alive
        assert len(lost_fires) == 1

    def test_link_restored_signal(self, sender_sink):
        sent, send = sender_sink
        store = ProfileStore()
        hs = Handshake(store, send, heartbeat_timeout_ms=500)
        hs.start()
        restored_fires: list[None] = []
        hs.link_restored.connect(lambda: restored_fires.append(None))

        hs.feed(self._hb())                 # 第 1 次：alive
        assert len(restored_fires) == 1
        hs.tick(500)                         # 超时
        assert not hs.heartbeat_alive
        hs.feed(self._hb())                  # 再次收到
        assert hs.heartbeat_alive
        assert len(restored_fires) == 2
