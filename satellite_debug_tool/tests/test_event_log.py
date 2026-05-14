"""EventLog 单元测试。"""

from satellite_debug_tool.core.data import EventLog, EventRecord
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    EVENT_ID_USER_MARK,
    EventDefEntry,
    EventReport,
)


def _ev(ts, eid, payload=b""):
    return EventReport(timestamp=ts, event_id=eid, payload=payload)


class TestEventLog:
    def _store(self):
        ps = ProfileStore()
        ps.apply_event_define("afd01", 1, [
            EventDefEntry(event_id=0x0003, level=1, name="LOCK_ACQUIRED"),
            EventDefEntry(event_id=0x0004, level=2, name="LOCK_LOST"),
        ])
        return ps

    def test_add_known_event(self):
        log = EventLog()
        ps = self._store()
        rec = log.add("afd01", _ev(100, 0x0003), ps)
        assert rec.name == "LOCK_ACQUIRED"
        assert rec.level == 1
        assert rec.hw_type == "afd01"
        assert rec.timestamp_ms == 100
        assert len(log) == 1

    def test_add_unknown_event_falls_back(self):
        log = EventLog()
        ps = ProfileStore()
        rec = log.add("afd01", _ev(100, 0x1234), ps)
        assert rec.name == "EVENT_1234"
        assert rec.level == 1   # 默认 INFO

    def test_user_mark_recognized(self):
        log = EventLog()
        ps = ProfileStore()
        rec = log.add("afd01", _ev(10, EVENT_ID_USER_MARK, b"point A"), ps)
        assert rec.name == "USER_MARK"
        assert rec.is_user_mark is True
        assert rec.payload == b"point A"

    def test_event_added_signal(self):
        log = EventLog()
        ps = self._store()
        fires: list[EventRecord] = []
        log.event_added.connect(fires.append)
        log.add("afd01", _ev(100, 0x0003), ps)
        assert len(fires) == 1
        assert fires[0].event_id == 0x0003

    def test_capacity_overflow_drops_oldest(self):
        log = EventLog(capacity=3)
        ps = self._store()
        for i in range(5):
            log.add("afd01", _ev(i, 0x0003), ps)
        assert len(log) == 3
        assert [r.timestamp_ms for r in log.all()] == [2, 3, 4]

    def test_filter_by_level(self):
        log = EventLog()
        ps = self._store()
        log.add("afd01", _ev(1, 0x0003), ps)      # INFO
        log.add("afd01", _ev(2, 0x0004), ps)      # WARN
        warns = log.filter(level_min=2)
        assert len(warns) == 1
        assert warns[0].event_id == 0x0004

    def test_filter_by_hw(self):
        log = EventLog()
        ps = self._store()
        log.add("afd01", _ev(1, 0x0003), ps)
        log.add("ufd45", _ev(2, 0x0003), ps)
        assert len(log.filter(hw_type="afd01")) == 1
        assert len(log.filter(hw_type="ufd45")) == 1

    def test_filter_by_keyword(self):
        log = EventLog()
        ps = self._store()
        log.add("afd01", _ev(1, 0x0003), ps)      # LOCK_ACQUIRED
        log.add("afd01", _ev(2, 0x0004), ps)      # LOCK_LOST
        assert len(log.filter(keyword="ACQ")) == 1
        assert len(log.filter(keyword="lock")) == 2    # case-insensitive

    def test_recent_limit(self):
        log = EventLog()
        ps = self._store()
        for i in range(10):
            log.add("afd01", _ev(i, 0x0003), ps)
        recent = log.recent(3)
        assert len(recent) == 3
        assert [r.timestamp_ms for r in recent] == [7, 8, 9]

    def test_clear(self):
        log = EventLog()
        ps = self._store()
        log.add("afd01", _ev(1, 0x0003), ps)
        log.clear()
        assert len(log) == 0
