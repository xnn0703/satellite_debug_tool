"""卫星指向几何 — 从固件 beampointing(Layer1/2) + trace_antenna_adapter(Layer3) 1:1 移植。

坐标系：geo=NED(+X北/+Y东/+Z下)，body=FRD(+X前/+Y右/+Z下)，array 跟随 body。
afd01 阵面 mount：mount_yaw=90°，az 俯视 CCW 为正(代码翻号)，EL=天顶距(0=正对法线)。
对外角度约定：az 北/机头起 CW+ [0,360)；geo/body el 朝上+ [-90,90]；array el=天顶距 [0,90]。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

_DEG = math.pi / 180.0
_RAD = 180.0 / math.pi
_WGS84_A = 6378137.0
_WGS84_E2 = 0.00669437999014
_GEO_RADIUS = _WGS84_A + 35786000.0
_VIS_MIN_DEG = -0.5
_REFRACT_TH_DEG = 15.0


@dataclass(frozen=True)
class SatView:
    """终端→卫星 地理系视线。"""
    az_geo_deg: float       # 北=0 CW+
    el_geo_deg: float       # 朝上+
    slant_range_m: float
    visible: bool


def _lla_to_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> tuple:
    la, lo = lat_deg * _DEG, lon_deg * _DEG
    sl, cl, so, co = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)
    n = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sl * sl)
    return ((n + alt_m) * cl * co, (n + alt_m) * cl * so, (n * (1.0 - _WGS84_E2) + alt_m) * sl)


def _geo_sat_ecef(sat_lon_deg: float) -> tuple:
    lo = sat_lon_deg * _DEG
    return (_GEO_RADIUS * math.cos(lo), _GEO_RADIUS * math.sin(lo), 0.0)


def _refraction_correct(el_deg: float) -> float:
    """Saastamoinen 简化折射，仅 el<15° 启用（固件 bp_refraction_correct）。"""
    if el_deg >= _REFRACT_TH_DEG:
        return el_deg
    arg = el_deg + 7.31 / (el_deg + 4.4)
    if arg <= 0.0 or arg >= 90.0:
        return el_deg
    return el_deg + (1.0 / math.tan(arg * _DEG)) / 60.0


def sat_view(sat_lon_deg: float, lat_deg: float, lon_deg: float, alt_m: float = 25.0) -> SatView:
    """GEO 卫星地理视线（固件 bp_geo_sat_view → bp_sat_view_from_ecef）。"""
    sx, sy, sz = _geo_sat_ecef(sat_lon_deg)
    tx, ty, tz = _lla_to_ecef(lat_deg, lon_deg, alt_m)
    dx, dy, dz = sx - tx, sy - ty, sz - tz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    la, lo = lat_deg * _DEG, lon_deg * _DEG
    sl, cl, so, co = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)
    n = -sl * co * dx - sl * so * dy + cl * dz   # North
    e = -so * dx + co * dy                       # East
    d = -cl * co * dx - cl * so * dy - sl * dz   # Down
    az = math.atan2(e, n) * _RAD % 360.0
    el = -math.asin(max(-1.0, min(1.0, d / rng))) * _RAD
    el = _refraction_correct(el)
    return SatView(az, el, rng, el > _VIS_MIN_DEG)


def _dcm_body_to_geo(yaw_deg: float, pitch_deg: float, roll_deg: float) -> list:
    """R_body2geo = Rz(yaw)·Ry(pitch)·Rx(roll)（固件 bp_dcm_body_to_geo，无负号）。"""
    y, p, r = yaw_deg * _DEG, pitch_deg * _DEG, roll_deg * _DEG
    cy, sy = math.cos(y), math.sin(y)
    cp, sp = math.cos(p), math.sin(p)
    cr, sr = math.cos(r), math.sin(r)
    return [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr,
            sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr,
            -sp,     cp * sr,                cp * cr]


def _azel_to_vec_ned(az_deg: float, el_deg: float) -> tuple:
    a, e = az_deg * _DEG, el_deg * _DEG
    ce = math.cos(e)
    return (ce * math.cos(a), ce * math.sin(a), -math.sin(e))  # z=Down → -sin(el)


def sat_view_to_body(az_geo_deg: float, el_geo_deg: float,
                     yaw_deg: float, pitch_deg: float, roll_deg: float) -> tuple:
    """geo 视线 → body az/el（固件 bp_sat_view_to_body，v_body = R^T·v_geo）。返回 (az_body, el_body)。"""
    vg = _azel_to_vec_ned(az_geo_deg, el_geo_deg)
    r = _dcm_body_to_geo(yaw_deg, pitch_deg, roll_deg)
    vb = (r[0] * vg[0] + r[3] * vg[1] + r[6] * vg[2],
          r[1] * vg[0] + r[4] * vg[1] + r[7] * vg[2],
          r[2] * vg[0] + r[5] * vg[1] + r[8] * vg[2])
    horiz = math.sqrt(vb[0] * vb[0] + vb[1] * vb[1])
    el = max(-90.0, min(90.0, -math.atan2(vb[2], horiz) * _RAD))
    az = math.atan2(vb[1], vb[0]) * _RAD % 360.0
    return az, el


def antenna_phased_solve(az_body_deg: float, el_body_deg: float) -> tuple:
    """body az/el → afd01 阵面 az/el（固件 bp_antenna_phased_solve，mount_yaw=90/az_ccw=False/el_zenith=True）。
    返回 (az_array_deg[0,360), el_array_deg[0,90]=天顶距=扫描角)。"""
    ab, eb = az_body_deg * _DEG, el_body_deg * _DEG
    ce = math.cos(eb)
    vbody = (ce * math.cos(ab), ce * math.sin(ab), -math.sin(eb))
    # mount: R_array2body = Rz(90°)；v_array = R^T·v_body。仅 yaw=90，pitch=roll=0。
    cy, sy = math.cos(math.pi / 2), math.sin(math.pi / 2)
    va = (cy * vbody[0] + sy * vbody[1],
          -sy * vbody[0] + cy * vbody[1],
          vbody[2])
    horiz = math.sqrt(va[0] * va[0] + va[1] * va[1])
    az_math = math.atan2(va[1], va[0]) * _RAD
    el_up = max(-90.0, min(90.0, -math.atan2(va[2], horiz) * _RAD))
    az_out = (-az_math) % 360.0           # az_ccw_positive=False → 翻号
    el_out = max(0.0, min(90.0, 90.0 - el_up))  # el_is_zenith=True
    return az_out, el_out


def geo_to_phased_array(sat_lon_deg: float, lat_deg: float, lon_deg: float, alt_m: float,
                        yaw_deg: float, pitch_deg: float, roll_deg: float) -> tuple:
    """一步到位：卫星经度+终端LLA+姿态 → 阵面"真·应指" (az_array, el_array=天顶距=扫描角)。
    （固件 bp_geo_to_phased_array）"""
    sv = sat_view(sat_lon_deg, lat_deg, lon_deg, alt_m)
    az_b, el_b = sat_view_to_body(sv.az_geo_deg, sv.el_geo_deg, yaw_deg, pitch_deg, roll_deg)
    return antenna_phased_solve(az_b, el_b)


def pointing_error_deg(el1_deg: float, az1_deg: float, el2_deg: float, az2_deg: float) -> float:
    """两个阵面指向的球面夹角 = 失指误差。el 为天顶距(从阵面法线起的极角)。"""
    e1, a1, e2, a2 = el1_deg * _DEG, az1_deg * _DEG, el2_deg * _DEG, az2_deg * _DEG
    c = math.cos(e1) * math.cos(e2) + math.sin(e1) * math.sin(e2) * math.cos(a1 - a2)
    return math.acos(max(-1.0, min(1.0, c))) * _RAD
