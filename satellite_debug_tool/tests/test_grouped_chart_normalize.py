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
    def test_simple_minmax_to_01(self, NormFn):
        ys = np.array([0.0, 5.0, 10.0], dtype=np.float64)
        out = NormFn(ys, ys)
        np.testing.assert_allclose(out, [0.0, 0.5, 1.0], atol=1e-6)
        assert out.dtype == np.float32

    def test_negative_range(self, NormFn):
        ys = np.array([-180.0, 0.0, 180.0], dtype=np.float64)
        out = NormFn(ys, ys)
        np.testing.assert_allclose(out, [0.0, 0.5, 1.0], atol=1e-6)

    def test_flat_line_returns_05(self, NormFn):
        ys = np.array([5.0, 5.0, 5.0, 5.0], dtype=np.float64)
        out = NormFn(ys, ys)
        np.testing.assert_allclose(out, [0.5, 0.5, 0.5, 0.5])

    def test_empty(self, NormFn):
        ys = np.array([], dtype=np.float64)
        out = NormFn(ys, ys)
        assert out.size == 0
        assert out.dtype == np.float32

    def test_range_from_subset(self, NormFn):
        """ys_for_range 是 ys 的子集（可视窗口切片场景）。"""
        ys = np.array([-100.0, -50.0, 0.0, 50.0, 100.0], dtype=np.float64)
        # 只用中间 3 个点（-50..50）做范围 → 端点 -100/+100 会被映射到 [-0.5, 1.5]
        ys_for_range = ys[1:4]
        out = NormFn(ys, ys_for_range)
        # 中点 -50→0, 0→0.5, 50→1；端点超出 [0,1]
        np.testing.assert_allclose(out[1:4], [0.0, 0.5, 1.0], atol=1e-6)
        assert out[0] < 0.0    # -100 在 [-50, 50] 范围下被映射到 -0.5
        assert out[-1] > 1.0   # +100 被映射到 1.5

    def test_empty_range_falls_back_to_ys(self, NormFn):
        """ys_for_range 为空时退回用 ys 自身做范围。"""
        ys = np.array([0.0, 10.0], dtype=np.float64)
        empty = np.array([], dtype=np.float64)
        out = NormFn(ys, empty)
        np.testing.assert_allclose(out, [0.0, 1.0], atol=1e-6)


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
