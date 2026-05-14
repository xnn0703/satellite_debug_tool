"""ChannelBuffer 单测（M7-S2）—— 环形 + 无界两种模式。"""

from __future__ import annotations

import numpy as np
import pytest

from satellite_debug_tool.core.data.channel_buffer import ChannelBuffer


# ---------------------------------------------------------------------------
# 环形模式（默认）—— 保持 M6 之前的行为不变
# ---------------------------------------------------------------------------


class TestBoundedMode:
    def test_default_capacity_is_30000(self):
        buf = ChannelBuffer("ch_00")
        assert buf.capacity == 30000

    def test_append_and_get_below_capacity(self):
        buf = ChannelBuffer("ch_00", capacity=10)
        for i in range(5):
            buf.append(i * 100.0, float(i))
        assert len(buf) == 5
        assert np.array_equal(buf.get_times(), np.array([0, 100, 200, 300, 400], dtype=np.float64))
        assert np.array_equal(buf.get_values(), np.array([0, 1, 2, 3, 4], dtype=np.float32))

    def test_ring_wraparound(self):
        """超过容量后旧数据被覆盖，get_* 返回顺序应该是 oldest→newest。"""
        buf = ChannelBuffer("ch_00", capacity=3)
        for i in range(5):
            buf.append(i * 100.0, float(i))
        # 期望保留最后 3 条：100, 200, 300 (idx=1,2)... 实际上是 200, 300, 400
        times = buf.get_times()
        values = buf.get_values()
        assert times.tolist() == [200.0, 300.0, 400.0]
        assert values.tolist() == [2.0, 3.0, 4.0]

    def test_get_latest(self):
        buf = ChannelBuffer("ch_00", capacity=5)
        assert buf.get_latest() is None
        buf.append(1000.0, 3.14)
        assert buf.get_latest() == pytest.approx((1000.0, 3.14), abs=1e-5)
        buf.append(2000.0, 2.71)
        assert buf.get_latest() == pytest.approx((2000.0, 2.71), abs=1e-5)

    def test_dtype(self):
        buf = ChannelBuffer("ch_00", capacity=5)
        buf.append(0.0, 1.0)
        assert buf.get_times().dtype == np.float64
        assert buf.get_values().dtype == np.float32


# ---------------------------------------------------------------------------
# 无界模式（M7 新增，PlaybackView / LogView 用）
# ---------------------------------------------------------------------------


class TestUnboundedMode:
    def test_capacity_none(self):
        buf = ChannelBuffer("ch_log", capacity=None)
        assert buf.capacity is None

    def test_empty_returns_empty_arrays(self):
        buf = ChannelBuffer("ch_log", capacity=None)
        assert buf.get_times().size == 0
        assert buf.get_values().size == 0
        assert buf.get_latest() is None
        assert len(buf) == 0

    def test_append_no_wraparound(self):
        """无界模式：append 10万次，全部保留，不环回。"""
        buf = ChannelBuffer("ch_log", capacity=None)
        n = 100_000
        for i in range(n):
            buf.append(float(i), float(i % 1000))
        assert len(buf) == n
        times = buf.get_times()
        values = buf.get_values()
        assert times.shape == (n,)
        assert values.shape == (n,)
        # 抽样校验头/尾
        assert times[0] == 0.0
        assert times[-1] == float(n - 1)
        assert values[0] == 0.0
        assert values[-1] == float((n - 1) % 1000)

    def test_dtype_unbounded(self):
        buf = ChannelBuffer("ch_log", capacity=None)
        buf.append(1.5, 2.5)
        assert buf.get_times().dtype == np.float64
        assert buf.get_values().dtype == np.float32

    def test_get_latest_unbounded(self):
        buf = ChannelBuffer("ch_log", capacity=None)
        buf.append(10.0, 1.0)
        buf.append(20.0, 2.0)
        buf.append(30.0, 3.0)
        latest = buf.get_latest()
        assert latest == pytest.approx((30.0, 3.0), abs=1e-5)

    def test_cache_invalidates_on_append(self):
        """get_* 之后再 append，下次 get_* 必须包含新值。"""
        buf = ChannelBuffer("ch_log", capacity=None)
        for i in range(10):
            buf.append(float(i), float(i))
        _ = buf.get_values()   # 触发缓存
        buf.append(100.0, 99.0)
        values = buf.get_values()
        assert values.shape == (11,)
        assert values[-1] == 99.0
