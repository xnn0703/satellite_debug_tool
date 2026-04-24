"""M4: AttitudeWidget 坐标公式 + 误差扇面几何 单元测试。

不启动 Qt GUI —— 只测纯函数（公式/几何）。
"""
from __future__ import annotations

import math
import numpy as np
import pytest

from satellite_debug_tool.ui.attitude_widget import (
    AZ_PHI_OFFSET_DEG,
    AZ_SIGN,
    _build_error_fan,
    _pointing_unit_vec,
)


# ===== 约定验证 =====
#
# 坐标系：+X 机头 / +Y 左翼 / +Z 天顶
# az=0 指机头右（-Y），逆时针增大；el=0 天顶，90 水平面。
#
# 关键典型点：
#   az, el  →  (x, y, z)
#   any, 0  →  (0, 0, 1)    天顶
#   0,  90  →  (0, -1, 0)   机头右
#   90, 90  →  (1, 0, 0)    机头
#   180, 90 →  (0, 1, 0)    机头左
#   270, 90 →  (-1, 0, 0)   机尾

_CASES = [
    # (az, el, expected xyz)
    (0,    0, (0.0,  0.0,  1.0)),
    (45,   0, (0.0,  0.0,  1.0)),   # el=0 时 az 无所谓
    (123,  0, (0.0,  0.0,  1.0)),
    (0,    90, (0.0, -1.0, 0.0)),
    (90,   90, (1.0,  0.0, 0.0)),
    (180,  90, (0.0,  1.0, 0.0)),
    (270,  90, (-1.0, 0.0, 0.0)),
    # 中间角：el=60 → cos60=0.5（Z），sin60≈0.866（XY 平面）
    (0,    60, (0.0, -math.sin(math.radians(60)), math.cos(math.radians(60)))),
    (90,   60, (math.sin(math.radians(60)), 0.0, math.cos(math.radians(60)))),
]


class TestPointingUnitVec:
    @pytest.mark.parametrize("az, el, expected", _CASES)
    def test_typical_cases(self, az, el, expected):
        v = _pointing_unit_vec(az, el)
        assert v.shape == (3,) and v.dtype == np.float32
        for a, b in zip(v.tolist(), expected):
            assert abs(a - b) < 1e-5, f"az={az} el={el}: got {v.tolist()}, want {expected}"

    def test_unit_length(self):
        """任何 az/el 结果都是单位矢量。"""
        for az in range(-180, 181, 30):
            for el in range(0, 181, 20):
                v = _pointing_unit_vec(az, el)
                n = float(np.linalg.norm(v))
                assert abs(n - 1.0) < 1e-5, f"az={az} el={el} |v|={n}"

    def test_constants_documented(self):
        """约定常数必须在模块里存在且有默认值，便于实际设备微调时改。"""
        assert AZ_PHI_OFFSET_DEG == 270.0
        assert AZ_SIGN in (+1, -1)


class TestErrorFan:
    def test_basic_shape(self):
        a = _pointing_unit_vec(60, 70) * 4.0
        b = _pointing_unit_vec(80, 70) * 4.0
        verts, faces = _build_error_fan(a, b, segments=12)
        assert verts.shape == (14, 3)     # 原点 + 13 个弧点
        assert faces.shape == (12, 3)
        assert verts.dtype == np.float32
        assert faces.dtype == np.uint32
        # 第一个顶点是原点
        np.testing.assert_array_almost_equal(verts[0], [0, 0, 0])
        # 面索引全部在合法范围
        assert faces.max() < verts.shape[0]

    def test_degenerate_zero_vector(self):
        """零矢量 → 空扇面。"""
        v, f = _build_error_fan(np.zeros(3), np.ones(3))
        assert v.shape == (0, 3)
        assert f.shape == (0, 3)

    def test_degenerate_same_direction(self):
        """两矢量完全重合 → 空扇面。"""
        a = _pointing_unit_vec(45, 45)
        b = _pointing_unit_vec(45, 45)
        v, f = _build_error_fan(a, b)
        assert v.shape == (0, 3)

    def test_arc_passes_through_endpoints(self):
        """弧的首尾应该接近两矢量各自方向。"""
        a = _pointing_unit_vec(30, 60) * 4.0
        b = _pointing_unit_vec(150, 60) * 4.0
        verts, _ = _build_error_fan(a, b, segments=20)
        # verts[1] 对应 t=0，应接近 a 方向；verts[-1] 对应 t=1，应接近 b
        # 半径在 _build_error_fan 里取两矢量平均 → 这里两者都是 4，所以结果长度也 ~4
        def cos(u, v):
            return float(np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v)))
        assert cos(verts[1], a) > 0.9999
        assert cos(verts[-1], b) > 0.9999

    def test_variable_segments(self):
        a = _pointing_unit_vec(0, 60) * 3
        b = _pointing_unit_vec(40, 60) * 3
        for seg in (2, 4, 8, 16, 32):
            v, f = _build_error_fan(a, b, seg)
            assert f.shape == (seg, 3)
            assert v.shape == (seg + 2, 3)
