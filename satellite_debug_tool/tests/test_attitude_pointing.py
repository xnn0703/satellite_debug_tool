"""AttitudeWidget 几何反向解链单测（2026-05-25 重写）。

`_ant_to_world_nwu` 把设备上报的阵面驱动角 (ant_az, ant_el) + 载体姿态 DCM
反向解到 widget 世界系 (NWU) 单位向量。重点验证：

1. 单位向量性质（任何输入返回长度 1）
2. 零姿态特殊点（zenith / 机头 / 右翼方向）
3. **GEO 稳定性**：同一空间方向，载体不同 yaw 时设备会发不同 ant_az，
   但反向解出的 v_world 应保持不变 —— 这是本次重写的核心动机。
4. mesh 变换零姿态 = identity（机头 +X = 北 / 左翼 +Y = 西 / 顶 +Z = 上）

不启动 Qt GUI —— 只测纯函数 + 矩阵代数。
"""
from __future__ import annotations

import math
import numpy as np
import pytest

from satellite_debug_tool.ui.attitude_widget import (
    P_NED_NWU,
    _ant_to_world_nwu,
)


def _R_body2geo(yaw_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    """与 AttitudeWidget.update_attitude 完全一致的 DCM 构造。"""
    r = math.radians(roll_deg)
    p = math.radians(pitch_deg)
    y = math.radians(yaw_deg)
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return np.array([
        [cy*cp, cy*sp*sr - sy*cr, cy*sp*cr + sy*sr],
        [sy*cp, sy*sp*sr + cy*cr, sy*sp*cr - cy*sr],
        [-sp,   cp*sr,            cp*cr           ],
    ], dtype=np.float64)


class TestReturnType:
    def test_shape_and_dtype(self):
        v = _ant_to_world_nwu(0.0, 45.0, np.eye(3))
        assert v.shape == (3,)
        assert v.dtype == np.float32

    def test_unit_length(self):
        """任何 (ant_az, ant_el, attitude) 输入返回都是单位矢量。"""
        for yaw in (-90, 0, 30, 180):
            for pitch in (-30, 0, 30):
                for roll in (-30, 0, 30):
                    R = _R_body2geo(yaw, pitch, roll)
                    for az in range(0, 360, 45):
                        for el in (0, 15, 45, 75, 90):
                            v = _ant_to_world_nwu(az, el, R)
                            n = float(np.linalg.norm(v))
                            assert abs(n - 1.0) < 1e-5, (
                                f"yaw={yaw} az={az} el={el} |v|={n}"
                            )


class TestZeroAttitudeCases:
    """零姿态（R=I）下，ant_az/ant_el 几个典型点对应的世界方向。

    afd01 mount: az 硬件 CCW+，az=0 起点 = body +Y(右翼)；
    el 是天顶距（0=朝天，90=水平）。
    零姿态时 body 与 NED 对齐，NED 再映射到 NWU。
    """

    R0 = np.eye(3, dtype=np.float64)

    @pytest.mark.parametrize("ant_az, ant_el, expected", [
        # ant_el=0 朝天：az 无意义，结果应为 (0,0,1) 即 NWU 的 +Z 上
        (0,   0, (0.0,  0.0,  1.0)),
        (45,  0, (0.0,  0.0,  1.0)),
        (180, 0, (0.0,  0.0,  1.0)),
        # ant_el=90 水平面：
        # az=0 → 机头右翼方向，零姿态 yaw=0 时机头朝北 → 右翼朝东 → NWU 的 (0,-1,0)
        (0,   90, (0.0, -1.0, 0.0)),
        # az=90 （硬件 CCW 走 90°）→ 从右翼朝向机头 → 北 = NWU +X
        (90,  90, (1.0,  0.0, 0.0)),
        # az=180 → 左翼方向 → 西 = NWU +Y
        (180, 90, (0.0,  1.0, 0.0)),
        # az=270 → 机尾方向 → 南 = NWU -X
        (270, 90, (-1.0, 0.0, 0.0)),
    ])
    def test_typical_directions(self, ant_az, ant_el, expected):
        v = _ant_to_world_nwu(ant_az, ant_el, self.R0)
        for got, want in zip(v.tolist(), expected):
            assert abs(got - want) < 1e-5, (
                f"ant_az={ant_az} ant_el={ant_el}: got {v.tolist()}, want {expected}"
            )


class TestGeoStability:
    """核心动机：GEO 卫星方向锁定时，载体姿态变化，设备会算出不同的 ant_az/ant_el，
    但反向解出的 v_world 应保持稳定（=原始世界方向）。

    构造方式：取一个固定的世界方向 (NWU) v_world*，
    用与设备端 trace_antenna_adapter.c 完全一致的正向链
    计算出在某个姿态下应当上报的 (ant_az, ant_el)，
    再用 widget 的反向函数还原，验证两者一致。
    """

    @staticmethod
    def _forward_solve(v_world_nwu: np.ndarray, R_body2geo: np.ndarray) -> tuple[float, float]:
        """正向：NWU 世界方向 + 姿态 → ant_az/ant_el（复刻设备 Layer 3）。"""
        # NWU → NED
        v_geo = P_NED_NWU @ v_world_nwu
        # NED → FRD body：v_body = R^T · v_geo
        v_body = R_body2geo.T @ v_geo
        # body → array：v_array = R_array2body^T · v_body
        # R_array2body = Rz(+90°) = [[0,-1,0],[1,0,0],[0,0,1]]
        # R_array2body^T = [[0,1,0],[-1,0,0],[0,0,1]]
        v_array = np.array([v_body[1], -v_body[0], v_body[2]])
        # 阵面 az/el
        horiz = math.hypot(v_array[0], v_array[1])
        az_math_deg = math.degrees(math.atan2(v_array[1], v_array[0]))
        el_up_deg = -math.degrees(math.atan2(v_array[2], horiz))
        # az_ccw_positive=false → az_out = -az_math
        az_out = (-az_math_deg) % 360.0
        # el_is_zenith=true → el_out = 90 - el_up
        el_out = max(0.0, min(90.0, 90.0 - el_up_deg))
        return az_out, el_out

    @pytest.mark.parametrize("v_world_target", [
        # 各种 GEO 卫星可能的世界方向
        (math.cos(math.radians(30)), 0.0, math.sin(math.radians(30))),  # 北偏天顶 30°
        (0.0, math.cos(math.radians(45)), math.sin(math.radians(45))),  # 西偏天顶 45°
        (0.5, -0.5, math.sqrt(0.5)),                                     # 任意点
    ])
    @pytest.mark.parametrize("attitude", [
        (0, 0, 0),
        (45, 0, 0),
        (90, 0, 0),
        (180, 0, 0),
        (-45, 10, -5),
        (30, -10, 15),
        (15, 15, 15),   # 用户场景：三轴各 ±15°
        (-15, -15, 15),
    ])
    def test_target_recovered(self, v_world_target, attitude):
        v_target = np.array(v_world_target, dtype=np.float64)
        v_target /= np.linalg.norm(v_target)
        yaw, pitch, roll = attitude
        R = _R_body2geo(yaw, pitch, roll)

        ant_az, ant_el = self._forward_solve(v_target, R)
        v_back = _ant_to_world_nwu(ant_az, ant_el, R).astype(np.float64)

        # 反向解应当还原成原 world 方向（精度 1e-5）
        for got, want in zip(v_back.tolist(), v_target.tolist()):
            assert abs(got - want) < 1e-5, (
                f"target={v_target.tolist()} att={attitude} "
                f"ant=({ant_az:.3f},{ant_el:.3f}) back={v_back.tolist()}"
            )

    def test_swaying_keeps_world_direction_stable(self):
        """模拟设备 ±15° 三轴摇摆，跟踪同一 GEO 方向，
        widget 端 v_world 散布应当极小（< 1e-4）。这是用户报告的 bug 场景。"""
        v_target = np.array([0.866, 0.0, 0.5], dtype=np.float64)   # 北、抬 30°
        worlds = []
        rng = np.random.default_rng(seed=42)
        for _ in range(20):
            yaw   = rng.uniform(-15, 15)
            pitch = rng.uniform(-15, 15)
            roll  = rng.uniform(-15, 15)
            R = _R_body2geo(yaw, pitch, roll)
            az, el = self._forward_solve(v_target, R)
            v_back = _ant_to_world_nwu(az, el, R).astype(np.float64)
            worlds.append(v_back)
        arr = np.asarray(worlds)
        # 每个维度的标准差应当 ~0
        for axis_std in arr.std(axis=0):
            assert axis_std < 1e-4, f"散布过大: std={arr.std(axis=0)}"


class TestMeshTransformZeroAttitude:
    """mesh 变换 M = P·R·P，零姿态时应 = identity，机头 (mesh +X) 朝 NWU 北。"""

    def test_identity_at_zero(self):
        R = np.eye(3)
        M = P_NED_NWU @ R @ P_NED_NWU
        np.testing.assert_array_almost_equal(M, np.eye(3))

    def test_mesh_nose_maps_to_north_at_zero(self):
        """机头在 mesh 坐标系 = (+1, 0, 0)；零姿态 → 世界 (+1, 0, 0) = NWU 北。"""
        M = P_NED_NWU @ np.eye(3) @ P_NED_NWU
        nose_mesh = np.array([1.0, 0.0, 0.0])
        nose_world = M @ nose_mesh
        np.testing.assert_array_almost_equal(nose_world, [1.0, 0.0, 0.0])

    def test_yaw_90_rotates_nose_east(self):
        """yaw=90° 时机头朝东 = NWU (0, -1, 0)。"""
        R = _R_body2geo(90, 0, 0)
        M = P_NED_NWU @ R @ P_NED_NWU
        nose_world = M @ np.array([1.0, 0.0, 0.0])
        np.testing.assert_array_almost_equal(nose_world, [0.0, -1.0, 0.0])

    def test_yaw_180_rotates_nose_south(self):
        """yaw=180° 机头朝南 = NWU (-1, 0, 0)。"""
        R = _R_body2geo(180, 0, 0)
        M = P_NED_NWU @ R @ P_NED_NWU
        nose_world = M @ np.array([1.0, 0.0, 0.0])
        np.testing.assert_array_almost_equal(nose_world, [-1.0, 0.0, 0.0])

    def test_left_wing_maps_west_at_zero(self):
        """mesh 左翼 = (0, +1, 0)，零姿态时 → NWU 西 (0, +1, 0)。"""
        M = P_NED_NWU @ np.eye(3) @ P_NED_NWU
        left_world = M @ np.array([0.0, 1.0, 0.0])
        np.testing.assert_array_almost_equal(left_world, [0.0, 1.0, 0.0])

    def test_top_maps_up_at_zero(self):
        """mesh 顶面 = (0, 0, +1)，零姿态时 → NWU 上 (0, 0, +1)。"""
        M = P_NED_NWU @ np.eye(3) @ P_NED_NWU
        top_world = M @ np.array([0.0, 0.0, 1.0])
        np.testing.assert_array_almost_equal(top_world, [0.0, 0.0, 1.0])
