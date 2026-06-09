"""GroupedChartWidget M10 F1 归一化纯函数测试。

只测 `_normalize_ys` 静态方法（无 Qt 依赖），覆盖典型 case + 边界。
"""
from __future__ import annotations

import numpy as np
import pytest


@pytest.fixture(scope="module")
def NormFn():
    """避免每次 import 重复绑定 Qt（_normalize_ys 是 @staticmethod，可独立调用）。"""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget
    return GroupedChartWidget._normalize_ys


class TestNormalize:
    # M12：_normalize_ys 现在返回 (ys_draw, vmin, vmax)，vmin/vmax 供 legend 复用

    def test_simple_minmax_to_01(self, NormFn):
        ys = np.array([0.0, 5.0, 10.0], dtype=np.float64)
        out, vmin, vmax = NormFn(ys, ys)
        np.testing.assert_allclose(out, [0.0, 0.5, 1.0], atol=1e-6)
        assert out.dtype == np.float32
        assert (vmin, vmax) == (0.0, 10.0)

    def test_negative_range(self, NormFn):
        ys = np.array([-180.0, 0.0, 180.0], dtype=np.float64)
        out, vmin, vmax = NormFn(ys, ys)
        np.testing.assert_allclose(out, [0.0, 0.5, 1.0], atol=1e-6)
        assert (vmin, vmax) == (-180.0, 180.0)

    def test_flat_line_returns_05(self, NormFn):
        ys = np.array([5.0, 5.0, 5.0, 5.0], dtype=np.float64)
        out, vmin, vmax = NormFn(ys, ys)
        np.testing.assert_allclose(out, [0.5, 0.5, 0.5, 0.5])
        assert (vmin, vmax) == (5.0, 5.0)

    def test_empty(self, NormFn):
        ys = np.array([], dtype=np.float64)
        out, vmin, vmax = NormFn(ys, ys)
        assert out.size == 0
        assert out.dtype == np.float32

    def test_range_from_subset(self, NormFn):
        """ys_for_range 是 ys 的子集（可视窗口切片场景）。"""
        ys = np.array([-100.0, -50.0, 0.0, 50.0, 100.0], dtype=np.float64)
        # 只用中间 3 个点（-50..50）做范围 → 端点 -100/+100 会被映射到 [-0.5, 1.5]
        ys_for_range = ys[1:4]
        out, vmin, vmax = NormFn(ys, ys_for_range)
        # 中点 -50→0, 0→0.5, 50→1；端点超出 [0,1]
        np.testing.assert_allclose(out[1:4], [0.0, 0.5, 1.0], atol=1e-6)
        assert out[0] < 0.0    # -100 在 [-50, 50] 范围下被映射到 -0.5
        assert out[-1] > 1.0   # +100 被映射到 1.5
        assert (vmin, vmax) == (-50.0, 50.0)   # 范围来自子集

    def test_empty_range_falls_back_to_ys(self, NormFn):
        """ys_for_range 为空时退回用 ys 自身做范围。"""
        ys = np.array([0.0, 10.0], dtype=np.float64)
        empty = np.array([], dtype=np.float64)
        out, vmin, vmax = NormFn(ys, empty)
        np.testing.assert_allclose(out, [0.0, 1.0], atol=1e-6)
        assert (vmin, vmax) == (0.0, 10.0)


class TestToggleIntegration:
    """通过实际构造 widget 验证 toggle 状态 + Y 范围切换。"""

    def test_toggle_changes_internal_flag(self, NormFn):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget
        app = QApplication.instance() or QApplication([])
        w = GroupedChartWidget()
        assert w._normalize is False
        w._btn_normalize.setChecked(True)
        assert w._normalize is True
        w._btn_normalize.setChecked(False)
        assert w._normalize is False
        w.deleteLater()


@pytest.fixture
def stacked_chart_2groups():
    """M12：构造一个 stacked 模式、含 2 个子图（group 0/1）的 chart。"""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget
    from satellite_debug_tool.core.profile import ProfileStore
    from satellite_debug_tool.core.protocol import ChannelDefEntry
    app = QApplication.instance() or QApplication([])
    ps = ProfileStore()
    chans = [
        ChannelDefEntry(channel_id=i, data_type=1, group_id=(i % 2), flags=0,
                        name=f"c{i}", unit="", display_min=-1.0, display_max=1.0)
        for i in range(4)   # group 0: c0,c2 ; group 1: c1,c3
    ]
    ps.apply_channel_define("hw", table_ver=1, entries=chans)
    w = GroupedChartWidget()
    w.set_profile_store(ps)
    w.set_hw_type("hw")
    w.set_mode("stacked")
    yield w
    w.deleteLater()


class TestPerPlotNormalize:
    """M12：按子图独立归一化 + 全局开关同步。"""

    def test_two_subplots_exist(self, stacked_chart_2groups):
        w = stacked_chart_2groups
        assert set(w._plots.keys()) == {0, 1}

    def test_per_plot_toggle_isolated(self, stacked_chart_2groups):
        w = stacked_chart_2groups
        # 只归一化 group 0
        w._on_plot_normalize_toggled(0, True)
        assert w._is_plot_normalized(0) is True
        assert w._is_plot_normalized(1) is False
        # 标题标记只加在 group 0
        assert "归一" in w._plots[0].titleLabel.text
        assert "归一" not in w._plots[1].titleLabel.text
        # 工具栏全局按钮不应被点亮（只有一个子图归一化）
        assert w._btn_normalize.isChecked() is False

    def test_global_button_syncs_all(self, stacked_chart_2groups):
        w = stacked_chart_2groups
        w._btn_normalize.setChecked(True)
        assert w._is_plot_normalized(0) is True
        assert w._is_plot_normalized(1) is True
        # 右键 action 勾选态同步
        assert w._plot_norm_actions[0].isChecked() is True
        assert w._plot_norm_actions[1].isChecked() is True
        w._btn_normalize.setChecked(False)
        assert w._is_plot_normalized(0) is False
        assert w._is_plot_normalized(1) is False

    def test_all_on_lights_global_button(self, stacked_chart_2groups):
        w = stacked_chart_2groups
        # 分别打开两个子图 → 全开后工具栏按钮自动点亮
        w._on_plot_normalize_toggled(0, True)
        assert w._btn_normalize.isChecked() is False
        w._on_plot_normalize_toggled(1, True)
        assert w._btn_normalize.isChecked() is True
