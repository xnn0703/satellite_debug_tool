"""TimeRangeControl 单测（M7-S3）。"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------------------


class TestInitialState:
    def test_default_preset_all(self, qapp):
        from satellite_debug_tool.ui.time_range_control import TimeRangeControl
        w = TimeRangeControl()
        assert w._preset_combo.currentText() == "全部"
        # 未 set_total 时总时长 0，range = (0, 0)
        assert w.current_range() == (0.0, 0.0)


class TestPresets:
    def test_set_total_updates_full_range(self, qapp):
        """预设 = '全部' 时，set_total(7200) 后 current_range 应等于 (0, 7200)。"""
        from satellite_debug_tool.ui.time_range_control import TimeRangeControl
        w = TimeRangeControl()
        w.set_total(7200.0)
        assert w.current_range() == (0.0, 7200.0)

    def test_preset_last_30s_emits(self, qapp):
        from satellite_debug_tool.ui.time_range_control import TimeRangeControl
        w = TimeRangeControl()
        w.set_total(3600.0)

        captured: list[tuple[float, float]] = []
        w.range_changed.connect(lambda s, e: captured.append((s, e)))

        # 选 "最近 30 秒" 索引 1
        w._preset_combo.setCurrentIndex(1)
        # 应得 (3570, 3600)
        assert w.current_range() == pytest.approx((3570.0, 3600.0))
        assert captured == [pytest.approx((3570.0, 3600.0))]

    def test_preset_window_larger_than_total(self, qapp):
        """总时长 < 预设窗口时，应回退为 (0, total)。"""
        from satellite_debug_tool.ui.time_range_control import TimeRangeControl
        w = TimeRangeControl()
        w.set_total(20.0)   # 只有 20 秒
        w._preset_combo.setCurrentIndex(1)   # 最近 30 秒
        assert w.current_range() == (0.0, 20.0)

    def test_preset_5min(self, qapp):
        from satellite_debug_tool.ui.time_range_control import TimeRangeControl
        w = TimeRangeControl()
        w.set_total(3600.0)
        w._preset_combo.setCurrentIndex(3)   # 最近 5 分钟
        assert w.current_range() == pytest.approx((3300.0, 3600.0))


class TestCustomMode:
    def test_custom_enables_spinbox(self, qapp):
        from satellite_debug_tool.ui.time_range_control import TimeRangeControl
        w = TimeRangeControl()
        w.set_total(1000.0)
        # 切到 "自定义"
        w._preset_combo.setCurrentIndex(w._preset_combo.count() - 1)
        assert w._spin_start.isEnabled() is True
        assert w._spin_end.isEnabled() is True
        assert w._btn_apply.isEnabled() is True

    def test_custom_apply_emits(self, qapp):
        from satellite_debug_tool.ui.time_range_control import TimeRangeControl
        w = TimeRangeControl()
        w.set_total(1000.0)
        w._preset_combo.setCurrentIndex(w._preset_combo.count() - 1)   # 自定义

        captured: list[tuple[float, float]] = []
        w.range_changed.connect(lambda s, e: captured.append((s, e)))

        w._spin_start.setValue(100.0)
        w._spin_end.setValue(500.0)
        # 设 SpinBox 本身不该 emit（避免每改一次都触发）
        assert captured == []

        w._btn_apply.click()
        assert captured == [pytest.approx((100.0, 500.0))]

    def test_custom_inverted_range_self_corrects(self, qapp):
        """终 ≤ 起 时，应用按钮把终自动挪到 起 + 1（不超 total）。"""
        from satellite_debug_tool.ui.time_range_control import TimeRangeControl
        w = TimeRangeControl()
        w.set_total(1000.0)
        w._preset_combo.setCurrentIndex(w._preset_combo.count() - 1)
        w._spin_start.setValue(500.0)
        w._spin_end.setValue(300.0)   # 反了

        captured: list[tuple[float, float]] = []
        w.range_changed.connect(lambda s, e: captured.append((s, e)))
        w._btn_apply.click()
        # 终被纠正到 501.0
        assert w.current_range() == pytest.approx((500.0, 501.0))
        assert captured == [pytest.approx((500.0, 501.0))]


class TestProgrammaticSet:
    def test_set_range_no_emit(self, qapp):
        from satellite_debug_tool.ui.time_range_control import TimeRangeControl
        w = TimeRangeControl()
        w.set_total(1000.0)

        captured: list = []
        w.range_changed.connect(lambda s, e: captured.append((s, e)))

        w.set_range(100.0, 800.0)
        assert w.current_range() == pytest.approx((100.0, 800.0))
        assert w._preset_combo.currentText() == "自定义"
        assert captured == []   # 程序化设置不 emit
