"""_downsample_peak 单测（M9.1 性能优化）。

验证峰值保留降采样：压点数 + 不丢尖峰 + 边界安全。
"""

from __future__ import annotations

import numpy as np
import pytest

from satellite_debug_tool.ui.grouped_chart_widget import _downsample_peak


class TestNoDownsampleNeeded:
    def test_below_target_returns_original(self):
        xs = np.arange(100, dtype=np.float64)
        ys = np.arange(100, dtype=np.float32)
        ox, oy = _downsample_peak(xs, ys, 3000)
        assert ox is xs and oy is ys   # 原样返回，不 copy

    def test_tiny_target_returns_original(self):
        xs = np.arange(1000, dtype=np.float64)
        ys = np.arange(1000, dtype=np.float32)
        ox, oy = _downsample_peak(xs, ys, 2)   # target < 4
        assert ox is xs and oy is ys


class TestDownsampleReducesPoints:
    def test_point_count_reduced(self):
        n = 18000
        xs = np.arange(n, dtype=np.float64)
        ys = np.sin(xs / 100.0).astype(np.float32)
        ox, oy = _downsample_peak(xs, ys, 3000)
        # 降到约 target 量级（min/max 双点 + 尾部残点）
        assert ox.size < n
        assert ox.size <= 3000 + 100   # 容忍尾部残点
        assert ox.size == oy.size

    def test_dtype_preserved(self):
        xs = np.arange(18000, dtype=np.float64)
        ys = np.arange(18000, dtype=np.float32)
        ox, oy = _downsample_peak(xs, ys, 3000)
        assert ox.dtype == np.float64
        assert oy.dtype == np.float32


class TestPeakPreserved:
    def test_spike_not_lost(self):
        """中间插一个尖峰，降采样后该尖峰值仍在结果里（普通 stride 会丢）。"""
        n = 18000
        xs = np.arange(n, dtype=np.float64)
        ys = np.zeros(n, dtype=np.float32)
        ys[9000] = 999.0   # 单点尖峰
        ox, oy = _downsample_peak(xs, ys, 3000)
        assert oy.max() == pytest.approx(999.0), "尖峰被降采样丢掉了"

    def test_negative_spike_preserved(self):
        n = 18000
        xs = np.arange(n, dtype=np.float64)
        ys = np.zeros(n, dtype=np.float32)
        ys[5000] = -888.0
        ox, oy = _downsample_peak(xs, ys, 3000)
        assert oy.min() == pytest.approx(-888.0)

    def test_tail_point_kept(self):
        """尾部残点补回 —— 曲线最右端应是最新值，不被 bin 截断。"""
        n = 18001   # 故意非整除
        xs = np.arange(n, dtype=np.float64)
        ys = xs.astype(np.float32)
        ox, oy = _downsample_peak(xs, ys, 3000)
        assert ox[-1] == pytest.approx(float(n - 1))
        assert oy[-1] == pytest.approx(float(n - 1))


class TestMonotonicData:
    def test_ramp_endpoints(self):
        """单调上升数据：降采样后首尾值不变（首 bin min、末尾残点）。"""
        n = 12000
        xs = np.arange(n, dtype=np.float64)
        ys = xs.astype(np.float32)
        ox, oy = _downsample_peak(xs, ys, 3000)
        assert oy[0] == pytest.approx(0.0)            # 第一个 bin 的 min
        assert oy.max() == pytest.approx(float(n - 1))
