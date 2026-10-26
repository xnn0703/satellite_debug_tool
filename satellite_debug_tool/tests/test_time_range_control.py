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


@pytest.mark.parametrize("theme", ("dark", "dark_hc", "light"))
@pytest.mark.parametrize("locale", ("zh_CN", "en_US"))
@pytest.mark.parametrize("host_kind", ("standalone", "playback", "log"))
def test_preset_text_fits_control_and_popup(qapp, tmp_path, monkeypatch, theme, locale, host_kind):
    """回归：中英文最长选项完整显示，框体和弹出列表按同一字体布局。"""
    from pathlib import Path
    from PySide6.QtGui import QFontInfo
    from PySide6.QtWidgets import QStyle, QStyleOptionComboBox
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.i18n import initialize_translation_manager
    from satellite_debug_tool.ui import styles as S
    from satellite_debug_tool.ui.qss import build
    from satellite_debug_tool.ui.time_range_control import TimeRangeControl

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("SATELLITE_DEBUG_LOCALE", raising=False)
    manager = initialize_translation_manager(qapp, Settings())
    previous_style, previous_font = qapp.styleSheet(), qapp.font()
    widget = None
    try:
        S.apply_global_font(qapp, scale="small", base_px=13)
        qapp.setStyleSheet(build(theme))
        manager.set_preference(locale)
        if host_kind == "standalone":
            widget = TimeRangeControl()
            control = widget
            widget.resize(700, 60)
        else:
            from satellite_debug_tool.ui.log_view import LogView
            from satellite_debug_tool.ui.playback_view import PlaybackView
            view_type = PlaybackView if host_kind == "playback" else LogView
            widget = view_type(settings=Settings())
            control = widget._range_ctl
            widget.resize(1024, 600)
        widget.set_theme(theme)
        control.set_total(3600)
        widget.show()
        qapp.processEvents()
        combo = control._preset_combo
        combo.setCurrentIndex(4)
        current = control.current_range()
        emitted = []
        control.range_changed.connect(lambda *values: emitted.append(values))
        # 同一实例往返切换语言，验证宽度失效与重算，同时保持选择和时间事实。
        for language in (locale, "en_US" if locale == "zh_CN" else "zh_CN", locale):
            manager.set_preference(language)
            qapp.processEvents()
            option = QStyleOptionComboBox()
            combo.initStyleOption(option)
            content = combo.style().subControlRect(
                QStyle.ComplexControl.CC_ComboBox, option,
                QStyle.SubControl.SC_ComboBoxEditField, combo,
            )
            required = max(combo.fontMetrics().horizontalAdvance(combo.itemText(i))
                           for i in range(combo.count()))
            assert content.width() >= required
            combo.showPopup()
            qapp.processEvents()
            view = combo.view()
            assert QFontInfo(combo.font()).family() == QFontInfo(view.font()).family()
            assert combo.font().pixelSize() == view.font().pixelSize()
            assert view.sizeHintForColumn(0) <= view.viewport().width()
            for index in range(combo.count()):
                row = view.visualRect(combo.model().index(index, 0))
                assert row.right() <= view.viewport().rect().right()
            combo.hidePopup()
            assert combo.currentData() == "last_30_minutes"
            assert control.current_range() == current
        assert emitted == []
    finally:
        if widget is not None:
            control._preset_combo.hidePopup()
            widget.close()
        manager.set_preference("zh_CN")
        qapp.setStyleSheet(previous_style)
        qapp.setFont(previous_font)
        qapp.processEvents()


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
