"""KA256 扫描损失模型测试 — golden 值来自固件 ka256_loss_table.c。"""

import pytest
from satellite_debug_tool.core.simulation.ka256_loss import (
    KA256_BEAM_WIDTH,
    KA256_HARD_LIMIT_DEG,
    KA256_MAX_SCAN_DEG,
    ka256_scan_loss_db,
    _find_nearest_band,
)


class TestKa256ScanLoss:
    """ka256_scan_loss_db 测试。"""

    @pytest.mark.parametrize("theta,expected", [
        (0.0, 0.0077),
        (10.0, 0.1129),
        (30.0, 0.7813),
        (40.78, 1.5733),
        (50.0, 2.6453),
        (65.0, 5.5426),
        (70.0, 6.9304),
        (80.0, 6.9304),  # 与 70° 相等（饱和）
    ])
    def test_golden_values_20ghz(self, theta: float, expected: float):
        """20.2 GHz 频段 golden 值（容差 ±0.001 dB）。"""
        result = ka256_scan_loss_db(20.2, theta)
        assert abs(result - expected) < 0.001, f"theta={theta}: got {result}, expected {expected}"

    def test_zero_loss_at_boresight(self):
        """0° 扫描角损失接近 0。"""
        loss = ka256_scan_loss_db(20.2, 0.0)
        assert loss < 0.01

    def test_monotonic_increase(self):
        """扫描角 0→65° 时损失严格递增。"""
        prev = 0.0
        for theta in range(0, 66, 5):
            loss = ka256_scan_loss_db(20.2, float(theta))
            assert loss >= prev, f"not monotonic at theta={theta}: {loss} < {prev}"
            prev = loss

    def test_saturation_at_70(self):
        """θ>70° 饱和到 70° 值。"""
        loss_70 = ka256_scan_loss_db(20.2, 70.0)
        loss_80 = ka256_scan_loss_db(20.2, 80.0)
        loss_90 = ka256_scan_loss_db(20.2, 90.0)
        assert abs(loss_70 - loss_80) < 0.001
        assert abs(loss_70 - loss_90) < 0.001

    def test_negative_angle_symmetry(self):
        """负角度与正角度相同。"""
        for theta in [10.0, 30.0, 50.0]:
            assert abs(ka256_scan_loss_db(20.2, theta) - ka256_scan_loss_db(20.2, -theta)) < 0.0001

    def test_nearest_band_selection(self):
        """19.5 GHz 应选 19.45 GHz 频段。"""
        a_195 = _find_nearest_band(19.5)
        a_1945 = _find_nearest_band(19.45)
        assert a_195 == a_1945

    def test_all_bands_positive_loss(self):
        """所有频段在 30° 时损失为正。"""
        for freq in [17.7, 18.2, 18.7, 19.2, 19.45, 19.7, 20.2, 20.7, 21.2]:
            loss = ka256_scan_loss_db(freq, 30.0)
            assert loss > 0, f"freq={freq}: loss={loss}"

    def test_constants(self):
        """常量值正确。"""
        assert KA256_BEAM_WIDTH == 6.3
        assert KA256_MAX_SCAN_DEG == 65.0
        assert KA256_HARD_LIMIT_DEG == 70.0
