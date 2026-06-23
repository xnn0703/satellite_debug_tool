"""卫星指向几何测试 — golden 值来自固件 beampointing 实测。"""

import pytest
from satellite_debug_tool.core.simulation.beampointing import (
    SatView,
    sat_view,
    sat_view_to_body,
    antenna_phased_solve,
    geo_to_phased_array,
    pointing_error_deg,
)


class TestSatView:
    """sat_view 卫星地理视线测试。"""

    def test_nanjing_to_apstar6(self):
        """南京(32.0603,118.7969) → 亚太6号(134°E)：az≈152.87, el≈49.22, range≈37123km"""
        sv = sat_view(134.0, 32.0603, 118.7969, 25.0)
        assert isinstance(sv, SatView)
        assert sv.visible is True
        assert abs(sv.az_geo_deg - 152.869) < 0.01, f"az={sv.az_geo_deg}"
        assert abs(sv.el_geo_deg - 49.221) < 0.01, f"el={sv.el_geo_deg}"
        assert abs(sv.slant_range_m / 1000 - 37123.1) < 1.0, f"range={sv.slant_range_m/1000}"

    def test_equator_visible(self):
        """赤道正下方：el 接近 90°。"""
        sv = sat_view(100.0, 0.0, 100.0, 0.0)
        assert sv.visible is True
        assert sv.el_geo_deg > 80.0

    def test_opposite_side_invisible(self):
        """地球对面：不可见。"""
        sv = sat_view(0.0, 0.0, 180.0, 0.0)
        assert sv.visible is False


class TestSatViewToBody:
    """sat_view_to_body 测试。"""

    def test_zero_attitude_identity(self):
        """零姿态 body ≡ geo。"""
        az, el = sat_view_to_body(152.869, 49.221, 0.0, 0.0, 0.0)
        assert abs(az - 152.869) < 0.01
        assert abs(el - 49.221) < 0.01


class TestAntennaPhasedSolve:
    """antenna_phased_solve 阵面解算测试。"""

    def test_az0_el30(self):
        """AZ=0, EL=30° → 阵面指向（固件锚点：mount_yaw=90° 使 body AZ=0 → array AZ=90°）。"""
        az, el = antenna_phased_solve(0.0, 30.0)
        # mount_yaw=90°：body AZ=0 → array AZ=90°；EL=30° → 天顶距=60°
        assert abs(az - 90.0) < 0.1, f"az={az}"
        assert abs(el - 60.0) < 0.1, f"el={el}"


class TestGeoToPhasedArray:
    """geo_to_phased_array 一步到位测试。"""

    def test_nanjing_apstar6(self):
        """南京→亚太6号，零姿态：array_az≈297.13, array_el(天顶距)≈40.78"""
        az, el = geo_to_phased_array(134.0, 32.0603, 118.7969, 25.0, 0, 0, 0)
        assert abs(az - 297.131) < 0.01, f"az={az}"
        assert abs(el - 40.779) < 0.01, f"el={el}"

    def test_latitude_scan_angle_relation(self):
        """纬度↑ → 扫描角↑。"""
        cases = [
            (0.0, 17.86),
            (20.0, 29.14),
            (32.0603, 40.78),
            (45.0, 53.97),
        ]
        for lat, expected_el in cases:
            _, el = geo_to_phased_array(134.0, lat, 118.7969, 25.0, 0, 0, 0)
            assert abs(el - expected_el) < 0.5, f"lat={lat}: el={el}, expected≈{expected_el}"


class TestPointingError:
    """pointing_error_deg 测试。"""

    def test_same_pointing_zero(self):
        """同一指向：失指 = 0。"""
        err = pointing_error_deg(40.0, 100.0, 40.0, 100.0)
        assert abs(err) < 0.001

    def test_el_offset_3deg(self):
        """纯 EL 偏 3°：失指 ≈ 3°。"""
        err = pointing_error_deg(40.0, 100.0, 43.0, 100.0)
        assert abs(err - 3.0) < 0.01

    def test_az_offset_compressed(self):
        """纯 AZ 偏 5°，el=40°：失指被 sin(el) 收缩。"""
        err = pointing_error_deg(40.0, 100.0, 40.0, 105.0)
        assert abs(err - 3.2133) < 0.01

    def test_symmetry(self):
        """球面距离对称。"""
        e1 = pointing_error_deg(40.0, 100.0, 43.0, 105.0)
        e2 = pointing_error_deg(43.0, 105.0, 40.0, 100.0)
        assert abs(e1 - e2) < 0.0001
