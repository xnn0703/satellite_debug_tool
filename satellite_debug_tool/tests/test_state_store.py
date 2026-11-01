"""StateStore 单元测试。"""

from types import SimpleNamespace

import pytest

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

    def test_received_and_changed_monotonic_times_are_independent(self, monkeypatch):
        clock = [10.0]
        monkeypatch.setattr(state_store_module.time, "monotonic", lambda: clock[0])
        s = StateStore()
        s.update("afd01", _report(100, (0, 2)))
        clock[0] = 11.0
        s.update("afd01", _report(200, (0, 2)))
        snap = s.get("afd01", 0)
        assert snap.last_received_monotonic == 11.0
        assert snap.last_changed_monotonic == 10.0
        assert snap.last_change_ms == 100

    def test_expire_stale_clears_bucket_after_report_timeout(self):
        s = StateStore()
        s.update("afd01", _report(100, (0, 2)))
        snap = s.get("afd01", 0)
        assert snap is not None
        assert not s.expire_stale(
            "afd01", 3.5, now_monotonic=snap.last_received_monotonic + 3.5
        )
        assert s.expire_stale(
            "afd01", 3.5, now_monotonic=snap.last_received_monotonic + 3.51
        )
        assert s.get_value("afd01", 0) is None

    def test_any_state_report_keeps_the_bucket_known(self, monkeypatch):
        clock = [20.0]
        monkeypatch.setattr(state_store_module.time, "monotonic", lambda: clock[0])
        s = StateStore()
        s.update("afd01", _report(100, (0, 2)))
        clock[0] = 23.0
        s.update("afd01", _report(200, (1, 1)))
        assert not s.expire_stale("afd01", 3.5, now_monotonic=24.0)
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


def test_received_revision_orders_reports_at_one_clock_tick(monkeypatch) -> None:
    """报告接收顺序独立于时间分辨率；同值回读保留变化时间且更新接收序列。"""
    monkeypatch.setattr(state_store_module, "time", SimpleNamespace(monotonic=lambda: 10.0))
    store = StateStore()
    changes = []
    store.state_changed.connect(lambda *args: changes.append(args))
    store.update("afd01", _report(100, (0, 1), (1, 2)))
    first = store.get("afd01", 0)
    assert first.received_revision == store.get("afd01", 1).received_revision == 1
    store.update("afd01", _report(200, (0, 1)))
    repeated = store.get("afd01", 0)
    assert repeated.received_revision == 2
    assert store.get("afd01", 1).received_revision == 1
    assert repeated.last_received_monotonic == repeated.last_changed_monotonic == 10.0
    assert repeated.last_change_ms == 100
    assert len(changes) == 2
    store.update("afd01", _report(300, (0, 0)))
    assert store.get("afd01", 0).received_revision == 3
    assert len(changes) == 3


@pytest.mark.parametrize("clear_scope", ["model", "all", "expire"])
def test_received_revision_advances_after_clearing(clear_scope: str) -> None:
    """清空与过期清除快照，整个Store接收序列保持递增。"""
    store = StateStore()
    store.update("afd01", _report(100, (0, 1)))
    store.update("afd01c", _report(100, (0, 1)))
    previous_revision = store.get("afd01c", 0).received_revision
    if clear_scope == "model":
        store.clear("afd01")
    elif clear_scope == "all":
        store.clear()
    else:
        received = store.get("afd01", 0).last_received_monotonic
        assert store.expire_stale("afd01", now_monotonic=received + 4.0)
    assert store.get("afd01", 0) is None
    store.update("afd01", _report(1, (0, 0)))
    assert store.get("afd01", 0).received_revision > previous_revision
