"""ChannelPanel 单测：add/remove/value/checked、信号、全选清空。

不真正显示 widget，所有断言走 API + 内部状态。
"""
from __future__ import annotations

import os
import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def panel(qapp):
    from satellite_debug_tool.ui.channel_panel import ChannelPanel
    p = ChannelPanel()
    yield p
    p.deleteLater()


class TestAddRemove:
    def test_starts_empty(self, panel):
        assert panel.channel_names() == []

    def test_add_one(self, panel):
        panel.add_channel("ch_00", "#FF0000", "roll")
        assert panel.channel_names() == ["ch_00"]
        assert panel.is_checked("ch_00") is True   # 默认勾选

    def test_add_multiple_preserves_order(self, panel):
        for n, c in [("a", "#111"), ("b", "#222"), ("c", "#333")]:
            panel.add_channel(n, c)
        assert panel.channel_names() == ["a", "b", "c"]

    def test_add_existing_is_idempotent(self, panel):
        panel.add_channel("ch_00", "#FF0000", "roll")
        panel.add_channel("ch_00", "#00FF00", "ROLL")  # 二次 add 不重复
        assert panel.channel_names() == ["ch_00"]

    def test_add_existing_updates_label(self, panel):
        panel.add_channel("ch_00", "#FF0000", "roll")
        panel.add_channel("ch_00", "#FF0000", "ROLL_NEW")
        row = panel._rows["ch_00"]
        assert row._name_label.text() == "ROLL_NEW"

    def test_remove(self, panel):
        panel.add_channel("a", "#111")
        panel.add_channel("b", "#222")
        panel.remove_channel("a")
        assert panel.channel_names() == ["b"]

    def test_remove_nonexistent_is_noop(self, panel):
        panel.add_channel("a", "#111")
        panel.remove_channel("zzz")  # 不存在
        assert panel.channel_names() == ["a"]


class TestValueAndLabel:
    def test_update_value_changes_display(self, panel):
        panel.add_channel("ch_00", "#FF0000")
        panel.update_value("ch_00", "42.50")
        assert panel._rows["ch_00"]._value_label.text() == "42.50"

    def test_update_value_unknown_channel_noop(self, panel):
        panel.update_value("not_added", "999")  # 不抛异常

    def test_set_label(self, panel):
        panel.add_channel("ch_00", "#FF0000", "roll")
        panel.set_label("ch_00", "ROLL (deg)")
        assert panel._rows["ch_00"]._name_label.text() == "ROLL (deg)"


class TestSelection:
    def test_default_checked(self, panel):
        panel.add_channel("ch_00", "#FF0000")
        assert panel.is_checked("ch_00") is True

    def test_uncheck_emits_signal(self, panel):
        panel.add_channel("ch_00", "#FF0000")
        received = []
        panel.selection_changed.connect(lambda n, c: received.append((n, c)))
        panel._rows["ch_00"]._checkbox.setChecked(False)
        assert received == [("ch_00", False)]

    def test_check_emits_signal(self, panel):
        panel.add_channel("ch_00", "#FF0000")
        panel._rows["ch_00"]._checkbox.setChecked(False)
        received = []
        panel.selection_changed.connect(lambda n, c: received.append((n, c)))
        panel._rows["ch_00"]._checkbox.setChecked(True)
        assert received == [("ch_00", True)]

    def test_is_checked_unknown_returns_false(self, panel):
        assert panel.is_checked("not_added") is False


class TestSelectAllClearAll:
    def test_clear_all_unchecks_everything(self, panel):
        for n in ("a", "b", "c"):
            panel.add_channel(n, "#111")
        panel._on_clear_all()
        for n in ("a", "b", "c"):
            assert panel.is_checked(n) is False

    def test_clear_all_emits_per_channel(self, panel):
        for n in ("a", "b", "c"):
            panel.add_channel(n, "#111")
        received = []
        panel.selection_changed.connect(lambda n, c: received.append((n, c)))
        panel._on_clear_all()
        # 3 个原本 True 的 → 3 条信号
        assert sorted(received) == sorted([("a", False), ("b", False), ("c", False)])

    def test_select_all_checks_everything(self, panel):
        for n in ("a", "b", "c"):
            panel.add_channel(n, "#111")
        panel._on_clear_all()    # 先全清
        panel._on_select_all()
        for n in ("a", "b", "c"):
            assert panel.is_checked(n) is True

    def test_select_all_skips_already_checked(self, panel):
        """已经勾选的不重复 emit signal（避免噪声）。"""
        panel.add_channel("a", "#111")  # 默认 True
        received = []
        panel.selection_changed.connect(lambda n, c: received.append((n, c)))
        panel._on_select_all()
        assert received == []   # a 本来就 True


class TestThemes:
    def test_apply_dark_theme_does_not_crash(self, panel):
        panel.add_channel("a", "#111")
        panel.apply_theme("dark", "medium")

    def test_apply_light_theme(self, panel):
        panel.add_channel("a", "#111")
        panel.apply_theme("light", "medium")

    def test_apply_dark_hc_theme(self, panel):
        panel.add_channel("a", "#111")
        panel.apply_theme("dark_hc", "small")
