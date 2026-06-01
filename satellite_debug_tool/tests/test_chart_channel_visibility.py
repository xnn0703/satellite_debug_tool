"""D6 P0 修复：GroupedChartWidget.set_channel_visible 控制单曲线显隐。"""
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
def chart_with_channels(qapp):
    from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget
    from satellite_debug_tool.core.profile import ProfileStore
    from satellite_debug_tool.core.protocol import ChannelDefEntry
    ps = ProfileStore()
    chans = [
        ChannelDefEntry(channel_id=i, data_type=1, group_id=0, flags=0,
                        name=f"c{i}", unit="", display_min=-1.0, display_max=1.0)
        for i in range(3)
    ]
    ps.apply_channel_define("hw", table_ver=1, entries=chans)
    c = GroupedChartWidget()
    c.set_profile_store(ps)
    c.set_hw_type("hw")
    yield c
    c.deleteLater()


def _curve_for(chart, name):
    from satellite_debug_tool.core.data.data_store import channel_key
    for cid, (_g, curve, _e) in chart._curves.items():
        if channel_key(cid) == name:
            return curve
    return None


class TestChannelVisibility:
    def test_all_curves_visible_initially(self, chart_with_channels):
        from satellite_debug_tool.core.data.data_store import channel_key
        for i in range(3):
            cv = _curve_for(chart_with_channels, channel_key(i))
            assert cv is not None and cv.isVisible()

    def test_hide_one_channel(self, chart_with_channels):
        from satellite_debug_tool.core.data.data_store import channel_key
        chart_with_channels.set_channel_visible(channel_key(1), False)
        assert _curve_for(chart_with_channels, channel_key(1)).isVisible() is False
        # 其它两条不受影响
        assert _curve_for(chart_with_channels, channel_key(0)).isVisible() is True
        assert _curve_for(chart_with_channels, channel_key(2)).isVisible() is True

    def test_show_again(self, chart_with_channels):
        from satellite_debug_tool.core.data.data_store import channel_key
        chart_with_channels.set_channel_visible(channel_key(2), False)
        chart_with_channels.set_channel_visible(channel_key(2), True)
        assert _curve_for(chart_with_channels, channel_key(2)).isVisible() is True

    def test_unknown_channel_noop(self, chart_with_channels):
        # 不存在的通道名不应抛
        chart_with_channels.set_channel_visible("ch_99", False)
