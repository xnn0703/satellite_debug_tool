"""PC fake-device — 桌面替身，模拟 afd01 终端的 IOT503 通信侧。

用法：
    python -m tools.fake_device
    python -m tools.fake_device --sat-lon 134 --lat 32.0603 --lon 118.7969 --duration 30

功能：
- 绑 UDP 5004，收 MockModem 发来的 SNR_REPORT / BEAM_CONFIG
- @50Hz 发 REAL_TIME_REPORT（含合成 GPS/姿态 + 当前波束角）
- 自带 FSM（SEARCH→LOCK→TRACK→BLOCKED），波束朝卫星方向收敛
- 1Hz 发 HB_CHECK，收 HB_ACK
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import sys
import time
from pathlib import Path

# 让 import 能找到项目包
_PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from satellite_debug_tool.core.simulation.beampointing import geo_to_phased_array, sat_view
from satellite_debug_tool.core.simulation.ka256_loss import KA256_BEAM_WIDTH, ka256_scan_loss_db
from satellite_debug_tool.core.simulation.mock_modem import (
    IOT503_MAGIC,
    Iot503Cmd,
    encode_snr_report,
    iot503_build_frame,
    iot503_parse_frame,
)

# ============================================================
# IOT503 REAL_TIME_REPORT 编码（MockModem 只有 decode，这里补 encode）
# ============================================================

_RTR_FMT = ">BhhHffffBBhhhhhBBBI"  # 42 bytes, big-endian, 与固件一致


def encode_real_time_report(
    gps_lock: int, lon: float, lat: float, alt: float,
    rx_freq: float, tx_freq: float, rx_lo: float, tx_lo: float,
    power: int, polar: int,
    pitch: float, roll: float, heading: float,
    theta: float, phi: float,
    mode: int, tle_mode: int, status: int, time_s: int,
) -> bytes:
    return struct.pack(
        _RTR_FMT,
        gps_lock,
        int(round(lon * 100)), int(round(lat * 100)), int(alt),
        rx_freq, tx_freq, rx_lo, tx_lo,
        power, polar,
        int(round(pitch * 100)), int(round(roll * 100)), int(round(heading * 100)),
        int(round(theta * 100)), min(32767, int(round(phi * 100))),
        mode, tle_mode, status, time_s,
    )


# ============================================================
# FSM 状态
# ============================================================

class FSM:
    """波束收敛状态机：SEARCH→LOCK→TRACK→BLOCKED。

    航向校准仿真：
    - 初始航向 = 真航向 + 偏移量（模拟上电航向不准）
    - SEARCH 阶段：周扫，航向偏移导致失指大、SNR 低
    - LOCK 阶段：航向开始收敛（SNR 反馈驱动）
    - TRACK 阶段：航向进一步收敛到真航向，SNR 趋于稳定
    """

    SEARCH = 0
    LOCK = 1
    TRACK = 2
    BLOCKED = 3

    SNR_LOSS_TH = 3.0       # dB，低于此视为失锁
    SNR_LOSS_HOLD = 0.5     # 连续低于阈值多久退回 SEARCH
    HEADING_CONV_K = 0.08   # 航向收敛速率（LOCK 阶段）
    HEADING_TRACK_K = 0.03  # 航向收敛速率（TRACK 阶段）

    def __init__(self, sat_lon: float, lat: float, lon: float, alt: float,
                 true_heading: float = 0.0, heading_offset: float = 0.0):
        self.sat_lon = sat_lon
        self.lat = lat
        self.lon = lon
        self.alt = alt

        self.state = self.SEARCH
        self._state_time = 0.0

        # 航向
        self.true_heading = true_heading          # 真航向（0~360）
        self.cur_heading = (true_heading + heading_offset) % 360.0  # 当前航向（初始偏移）
        self.heading_offset = heading_offset       # 初始偏移量

        # 当前波束指向（阵面 AZ/EL 天顶距）
        self.cur_az = 0.0
        self.cur_el = 45.0

        # 搜星扫描参数
        self._scan_az = 0.0
        self._scan_el = 15.0

        # measured SNR
        self.measured_snr = -10.0
        self._snr_low_time = 0.0

        # 航向校准历史（用于显示）
        self.heading_error = abs(heading_offset)

    def set_sat_lon(self, lon: float) -> None:
        self.sat_lon = lon

    def update(self, dt_s: float, snr_report: float | None) -> tuple[float, float, float]:
        """推进 FSM，返回 (theta, phi, heading) 填入 REAL_TIME_REPORT。"""
        self._state_time += dt_s

        # 更新 measured SNR
        if snr_report is not None:
            self.measured_snr = snr_report

        # 用当前航向算目标指向（设备认为的"应指"）
        tgt_az, tgt_el = geo_to_phased_array(
            self.sat_lon, self.lat, self.lon, self.alt,
            self.cur_heading, 0, 0,
        )

        if self.state == self.SEARCH:
            # 周扫：大范围扫描（模拟设备搜星过程）
            self._scan_az += 30.0 * dt_s
            if self._scan_az > 360.0:
                self._scan_az -= 360.0
                self._scan_el += 5.0
                if self._scan_el > 70.0:
                    self._scan_el = 15.0
            self.cur_az = self._scan_az
            self.cur_el = self._scan_el

            # 航向在 SEARCH 阶段不收敛（设备还没找到卫星）
            if self._state_time > 8.0:
                self.state = self.LOCK
                self._state_time = 0.0

        elif self.state == self.LOCK:
            # 快速收敛波束指向
            k = 0.3
            self.cur_az = self._wrap_toward(self.cur_az, tgt_az, k, dt_s)
            self.cur_el += k * (tgt_el - self.cur_el) * min(dt_s * 10, 1.0)

            # 航向开始收敛（SNR 反馈驱动）
            if self.measured_snr > self.SNR_LOSS_TH:
                self._concur_heading(dt_s, self.HEADING_CONV_K)

            if self._state_time > 2.0:
                self.state = self.TRACK
                self._state_time = 0.0
                self._snr_low_time = 0.0

        elif self.state == self.TRACK:
            # 跟踪：平滑 + 小抖动
            k = 0.15
            jitter_az = 0.2 * math.sin(self._state_time * 1.5)
            jitter_el = 0.15 * math.cos(self._state_time * 1.2)
            self.cur_az = self._wrap_toward(self.cur_az, tgt_az + jitter_az, k, dt_s)
            self.cur_el += k * (tgt_el + jitter_el - self.cur_el) * min(dt_s * 10, 1.0)

            # 航向持续收敛（更慢的速率，精细校准）
            if self.measured_snr > self.SNR_LOSS_TH:
                self._concur_heading(dt_s, self.HEADING_TRACK_K)

            # 监测失锁
            if self.measured_snr < self.SNR_LOSS_TH:
                self._snr_low_time += dt_s
                if self._snr_low_time > self.SNR_LOSS_HOLD:
                    self.state = self.SEARCH
                    self._state_time = 0.0
                    self._snr_low_time = 0.0
            else:
                self._snr_low_time = 0.0

        elif self.state == self.BLOCKED:
            pass  # 保持原位，由外部解除

        self.cur_el = max(0.0, min(90.0, self.cur_el))
        self.cur_az = self.cur_az % 360.0
        self.heading_error = abs(self._heading_diff(self.cur_heading, self.true_heading))
        return self.cur_el, self.cur_az, self.cur_heading

    def _concur_heading(self, dt_s: float, k: float) -> None:
        """航向朝真航向收敛（一阶滞后）。"""
        diff = self._heading_diff(self.true_heading, self.cur_heading)
        self.cur_heading = (self.cur_heading + diff * k * min(dt_s * 10, 1.0)) % 360.0

    @staticmethod
    def _heading_diff(target: float, current: float) -> float:
        """两个航向的最短角度差（-180~+180）。"""
        return (target - current + 180) % 360 - 180

    @staticmethod
    def _wrap_toward(cur: float, tgt: float, k: float, dt: float) -> float:
        diff = (tgt - cur + 180) % 360 - 180
        return (cur + diff * k * min(dt * 10, 1.0)) % 360.0


# ============================================================
# Main loop
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="IOT503 fake-device for loopback testing")
    parser.add_argument("--sat-lon", type=float, default=134.0, help="卫星经度 (°E)")
    parser.add_argument("--lat", type=float, default=32.0603, help="终端纬度 (°N)")
    parser.add_argument("--lon", type=float, default=118.7969, help="终端经度 (°E)")
    parser.add_argument("--alt", type=float, default=25.0, help="终端海拔 (m)")
    parser.add_argument("--heading-offset", type=float, default=30.0,
                        help="初始航向偏移 (°)，模拟上电航向不准 (默认 30°)")
    parser.add_argument("--duration", type=float, default=0, help="运行时长 (秒，0=无限)")
    parser.add_argument("--local-port", type=int, default=5004, help="本地端口")
    parser.add_argument("--remote-port", type=int, default=45679, help="MockModem 端口")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.local_port))
    sock.settimeout(0.005)

    remote = ("127.0.0.1", args.remote_port)
    fsm = FSM(args.sat_lon, args.lat, args.lon, args.alt,
              true_heading=0.0, heading_offset=args.heading_offset)

    t_start = time.monotonic()
    t_rtr = t_start
    t_hb = t_start
    tick_hz = 50.0
    dt = 1.0 / tick_hz
    last_snr: float | None = None
    acu_time = 0

    print(f"fake-device: sat_lon={args.sat_lon}°, pos=({args.lat},{args.lon}), "
          f"local={args.local_port}, remote={args.remote_port}")
    print("Press Ctrl-C to stop.")

    try:
        while True:
            now = time.monotonic()
            if args.duration > 0 and (now - t_start) > args.duration:
                break

            # 接收
            try:
                data, addr = sock.recvfrom(1024)
                result = iot503_parse_frame(data)
                if result:
                    cmd, payload = result
                    if cmd == Iot503Cmd.SNR_REPORT:
                        last_snr = struct.unpack(">f", payload[:4])[0]
                    elif cmd == Iot503Cmd.BEAM_CONFIG:
                        lon_raw = struct.unpack(">h", payload[0:2])[0]
                        fsm.set_sat_lon(lon_raw / 100.0)
                    elif cmd == Iot503Cmd.HB_ACK:
                        pass
            except (socket.timeout, BlockingIOError, OSError):
                pass

            # FSM tick
            theta, phi, heading = fsm.update(dt, last_snr)

            # 发 REAL_TIME_REPORT @50Hz
            if now - t_rtr >= dt:
                acu_time += 1
                rtr_payload = encode_real_time_report(
                    gps_lock=1, lon=args.lon, lat=args.lat, alt=args.alt,
                    rx_freq=19450.0, tx_freq=29250.0, rx_lo=19250.0, tx_lo=29050.0,
                    power=1 if fsm.state == FSM.TRACK else 0, polar=0,
                    pitch=0.0, roll=0.0, heading=heading,
                    theta=theta, phi=phi,
                    mode=0, tle_mode=0,
                    status=(0x21 if fsm.state == FSM.TRACK else 0x01),
                    time_s=acu_time,
                )
                frame = iot503_build_frame(Iot503Cmd.REAL_TIME_REPORT, rtr_payload)
                sock.sendto(frame, remote)
                t_rtr = now

            # 发 HB_CHECK @1Hz
            if now - t_hb >= 1.0:
                hb_frame = iot503_build_frame(Iot503Cmd.HB_CHECK, b"")
                sock.sendto(hb_frame, remote)
                t_hb = now

            # 状态输出
            if acu_time % 50 == 0 and acu_time > 0:
                state_names = ["SEARCH", "LOCK", "TRACK", "BLOCKED"]
                sv = sat_view(fsm.sat_lon, args.lat, args.lon, args.alt)
                print(f"  t={acu_time:4d} state={state_names[fsm.state]:8s} "
                      f"theta={theta:6.1f}° phi={phi:6.1f}° "
                      f"heading={heading:6.1f}° err={fsm.heading_error:5.1f}° "
                      f"snr={fsm.measured_snr:6.1f}dB "
                      f"sat_az={sv.az_geo_deg:.1f}° el={sv.el_geo_deg:.1f}°")

            time.sleep(max(0, dt - (time.monotonic() - now)))

    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        print("fake-device stopped.")


if __name__ == "__main__":
    main()
