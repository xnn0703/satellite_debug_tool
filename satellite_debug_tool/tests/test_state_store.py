"""StateStore 单元测试。"""

import satellite_debug_tool.core.data.state_store as state_store_module

from satellite_debug_tool.core.data import StateStore
from satellite_debug_tool.core.protocol import StateReport, StateSample


def _report(ts, *states):
    return StateReport(timestamp=ts, states=[StateSample(sid, v) for sid, v in states])


class TestStateStore:
    def test_empty_initial(self):
        s = StateStore()
        assert s.get_value("afd01", 0) is None
        assert s.get_all("afd01") == {}

    def test_update_sets_snapshot(self):
        s = StateStore()
        s.update("afd01", _report(100, (0, 3), (1, 1)))
        assert s.get_value("afd01", 0) == 3
        assert s.get_value("afd01", 1) == 1
        snap = s.get("afd01", 0)
        assert snap.last_change_ms == 100

    def test_no_signal_on_unchanged(self):
        s = StateStore()
        fires = []
        s.state_changed.connect(lambda hw, sid, v, old: fires.append((hw, sid, v, old)))

        s.update("afd01", _report(100, (0, 3)))
        s.update("afd01", _report(200, (0, 3)))       # 同值
        s.update("afd01", _report(300, (0, 4)))       # 变化

        assert fires == [("afd01", 0, 3, -1), ("afd01", 0, 4, 3)]

    def test_received_and_changed_wallclock_are_independent(self, monkeypatch):
        clock = [10.0]
        monkeypatch.setattr(state_store_module.time, "time", lambda: clock[0])
        s = StateStore()
        s.update("afd01", _report(100, (0, 2)))
        clock[0] = 11.0
        s.update("afd01", _report(200, (0, 2)))
        snap = s.get("afd01", 0)
        assert snap.last_received_wallclock == 11.0
        assert snap.last_changed_wallclock == 10.0
        assert snap.last_change_ms == 100

    def test_expire_stale_clears_bucket_after_report_timeout(self):
        s = StateStore()
        s.update("afd01", _report(100, (0, 2)))
        snap = s.get("afd01", 0)
        assert snap is not None
        assert not s.expire_stale("afd01", 3.5, now=snap.last_received_wallclock + 3.5)
        assert s.expire_stale("afd01", 3.5, now=snap.last_received_wallclock + 3.51)
        assert s.get_value("afd01", 0) is None

    def test_any_state_report_keeps_the_bucket_known(self, monkeypatch):
        clock = [20.0]
        monkeypatch.setattr(state_store_module.time, "time", lambda: clock[0])
        s = StateStore()
        s.update("afd01", _report(100, (0, 2)))
        clock[0] = 23.0
        s.update("afd01", _report(200, (1, 1)))
        assert not s.expire_stale("afd01", 3.5, now=24.0)
        assert s.get_value("afd01", 0) == 2

    def test_multi_hw_isolation(self):
        s = StateStore()
        s.update("afd01", _report(100, (0, 3)))
        s.update("ufd45", _report(100, (0, 1)))
        assert s.get_value("afd01", 0) == 3
        assert s.get_value("ufd45", 0) == 1

    def test_clear(self):
        s = StateStore()
        s.update("afd01", _report(100, (0, 3)))
        s.update("ufd45", _report(100, (0, 1)))
        s.clear("afd01")
        assert s.get_value("afd01", 0) is None
        assert s.get_value("ufd45", 0) == 1
        s.clear()
        assert s.get_value("ufd45", 0) is None

    def test_clear_emits_scope(self):
        s = StateStore()
        fires = []
        s.state_cleared.connect(fires.append)

        s.clear("afd01")
        s.clear()

        assert fires == ["afd01", None]
