"""IOT503 Mock Modem — 通过 UDP 与设备端 simulate_modem 通信。

替代真实 IOT503 modem，接收设备 REAL_TIME_REPORT (0xA0)，
用 B0 几何模型（ka256 扫描损失 + beampointing 失指）计算 SNR，
回复 SNR_REPORT (0x01)。
"""

from __future__ import annotations

import math
import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from .beampointing import geo_to_phased_array, pointing_error_deg, sat_view
from .ka256_loss import KA256_BEAM_WIDTH, ka256_scan_loss_db
from .presets import BAND_PRESETS, SATELLITE_PRESETS

# ============================================================
# 常量
# ============================================================

SNR_CLEAR_BORESIGHT = 16.0  # dB，晴空/对准/扫描0 上限
SNR_FLOOR = -10.0           # dB，遮挡/不可见 地板

IOT503_MAGIC = 0x55


# ============================================================
# IOT503 协议
# ============================================================

class Iot503Cmd(IntEnum):
    SNR_REPORT = 0x01
    BEAM_CONFIG = 0x02
    TRANSMIT_SWITCH = 0x03
    HEADING_SCAN_ANGLE = 0x04
    TRACE_MODE = 0x05
    HEADING_ALIGN_ANGLE = 0x06
    BEAM_ANGLE_CTRL_TX = 0x07
    TLE_CONFIG = 0x08
    BEAM_ANGLE_CTRL_RX = 0x09
    BEAM_ANGLE_CTRL = 0x0A
    REAL_TIME_REPORT = 0xA0
    HB_CHECK = 0xA2
    HB_ACK = 0xA3
    ERR_ACK = 0xFF


def iot503_checksum(data: bytes) -> int:
    """校验和：data[1:] 累加和取低 16 位。"""
    s = 0
    for b in data[1:]:
        s = (s + b) & 0xFFFF
    return s


def iot503_build_frame(cmd: int, payload: bytes) -> bytes:
    """构建 IOT503 帧。"""
    length = len(payload)
    header = bytes([IOT503_MAGIC, cmd & 0xFF]) + struct.pack(">H", length)
    frame = header + payload
    ck = iot503_checksum(frame)
    return frame + struct.pack(">H", ck)


def iot503_parse_frame(data: bytes) -> Optional[tuple[int, bytes]]:
    """解析 IOT503 帧，返回 (cmd, payload) 或 None。"""
    if len(data) < 6:
        return None
    if data[0] != IOT503_MAGIC:
        return None
    length = struct.unpack_from(">H", data, 2)[0]
    if len(data) < 4 + length + 2:
        return None
    payload = data[4:4 + length]
    ck_received = struct.unpack_from(">H", data, 4 + length)[0]
    ck_calc = iot503_checksum(data[:4 + length])
    if ck_received != ck_calc:
        return None
    return (data[1], payload)


# ============================================================
# REAL_TIME_REPORT (0xA0) 编解码
# ============================================================

@dataclass
class RealTimeReport:
    """设备发来的实时报告。"""
    gps_lock: int = 0
    lon: float = 0.0
    lat: float = 0.0
    alt: float = 0.0
    rx_freq: float = 0.0
    tx_freq: float = 0.0
    rx_lo: float = 0.0
    tx_lo: float = 0.0
    power: int = 0
    polar: int = 0
    pitch: float = 0.0
    roll: float = 0.0
    heading: float = 0.0
    theta: float = 0.0       # 阵面 EL = 天顶距（扫描角）
    phi: float = 0.0         # 阵面 AZ
    mode: int = 0
    tle_mode: int = 0
    status: int = 0
    time_s: int = 0


def decode_real_time_report(payload: bytes) -> RealTimeReport:
    """解码 REAL_TIME_REPORT (0xA0) 载荷。"""
    r = RealTimeReport()
    if len(payload) < 36:
        return r
    r.gps_lock = payload[0]
    r.lon = struct.unpack_from(">h", payload, 1)[0] / 100.0
    r.lat = struct.unpack_from(">h", payload, 3)[0] / 100.0
    r.alt = struct.unpack_from(">H", payload, 5)[0]
    r.rx_freq = struct.unpack_from(">f", payload, 7)[0]
    r.tx_freq = struct.unpack_from(">f", payload, 11)[0]
    r.rx_lo = struct.unpack_from(">f", payload, 15)[0]
    r.tx_lo = struct.unpack_from(">f", payload, 19)[0]
    r.power = payload[23]
    r.polar = payload[24]
    r.pitch = struct.unpack_from(">h", payload, 25)[0] / 100.0
    r.roll = struct.unpack_from(">h", payload, 27)[0] / 100.0
    r.heading = struct.unpack_from(">h", payload, 29)[0] / 100.0
    r.theta = struct.unpack_from(">h", payload, 31)[0] / 100.0
    r.phi = struct.unpack_from(">h", payload, 33)[0] / 100.0
    r.mode = payload[35]
    if len(payload) > 36:
        r.tle_mode = payload[36]
    if len(payload) > 37:
        r.status = payload[37]
    if len(payload) > 41:
        r.time_s = struct.unpack_from(">I", payload, 38)[0]
    return r


# ============================================================
# SNR_REPORT (0x01) / BEAM_CONFIG (0x02) 编码
# ============================================================

def encode_snr_report(snr_db: float, indicator: int = 0x03,
                       power: int = 0, reboot: int = 0) -> bytes:
    return struct.pack(">f", snr_db) + bytes([indicator, power, reboot])


def encode_beam_config(lon_deg: float, polar: int = 0,
                        rx_freq_mhz: float = 19450.0,
                        tx_freq_mhz: float = 29250.0) -> bytes:
    """编码 BEAM_CONFIG。修复：负经度直接用 >h（不 mask）。"""
    return struct.pack(">h", int(round(lon_deg * 100))) + bytes([polar]) + struct.pack(">ff", rx_freq_mhz, tx_freq_mhz)


# ============================================================
# _ModemWorker — 后台线程
# ============================================================

class _ModemWorker(QThread):
    """后台线程：UDP 收发 + SNR 定时发送。所有 socket 操作仅在此线程。"""

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._port = 45679
        self._remote_addr: tuple[str, int] = ("127.0.0.1", 5004)
        self._sock: socket.socket | None = None
        self._stop_event = threading.Event()
        # 命令队列（GUI 线程 push，worker 线程消费）
        self._cmd_q: queue.Queue = queue.Queue()
        # 卫星参数
        self._sat_lon = 134.0
        self._polar = 0
        self._rx_freq = 19450.0
        self._tx_freq = 29250.0
        self._freq_ghz = 20.2
        # SNR 参数
        self._snr_clear_boresight = SNR_CLEAR_BORESIGHT
        self._rain_fade_db = 0.0
        self._blockage_until = 0.0  # monotonic 截止时刻
        self._preset_heading: float | None = None  # 预设航向（None=用设备上报值）
        # 设备数据
        self._last_report: RealTimeReport | None = None
        self._last_snr = 0.0
        self._last_scan_angle = 0.0
        self._last_pointing_err = 0.0
        # 回调（只在 worker 线程调用，由 Qt 信号桥接）
        self.on_snr_updated: object = None
        self.on_report_received: object = None
        self.on_metrics_updated: object = None

    def configure(self, port: int, remote_addr: tuple[str, int],
                  sat_lon: float, band: str) -> None:
        """初始配置（仅在 start 前调用）。"""
        self._port = port
        self._remote_addr = remote_addr
        self._sat_lon = sat_lon
        preset = BAND_PRESETS.get(band, BAND_PRESETS["Ka"])
        self._freq_ghz = preset["freq_ghz"]
        self._rx_freq = preset["rx_freq"]
        self._tx_freq = preset["tx_freq"]

    def _apply_satellite(self, sat_lon: float, band: str) -> None:
        """在 worker 线程内应用卫星参数。"""
        self._sat_lon = sat_lon
        preset = BAND_PRESETS.get(band, BAND_PRESETS["Ka"])
        self._freq_ghz = preset["freq_ghz"]
        self._rx_freq = preset["rx_freq"]
        self._tx_freq = preset["tx_freq"]

    def run(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self._port))
        self._sock.settimeout(0.005)

        # 发送初始 BEAM_CONFIG
        self._send_beam_config()

        next_snr = time.monotonic()
        try:
            while not self._stop_event.is_set():
                # 消费命令队列
                self._drain_cmd_queue()
                # 接收
                try:
                    data, addr = self._sock.recvfrom(1024)
                    self._handle_rx(data, addr)
                except (socket.timeout, BlockingIOError, OSError):
                    pass
                # 定时发送 SNR_REPORT @50Hz
                now = time.monotonic()
                if now >= next_snr:
                    self._send_snr_report()
                    next_snr = now + 0.02
        finally:
            if self._sock:
                self._sock.close()
                self._sock = None

    def stop(self) -> None:
        self._stop_event.set()
        self.wait(2000)

    def _drain_cmd_queue(self) -> None:
        """排空 GUI 线程 push 的命令。"""
        while not self._cmd_q.empty():
            try:
                name, val = self._cmd_q.get_nowait()
            except queue.Empty:
                break
            if name == "satellite":
                self._apply_satellite(*val)
                self._send_beam_config()
            elif name == "rain":
                self._rain_fade_db = val
            elif name == "blockage":
                self._blockage_until = time.monotonic() + val
            elif name == "baseline":
                self._snr_clear_boresight = val
            elif name == "heading":
                self._preset_heading = val

    def _handle_rx(self, data: bytes, addr: tuple[str, int]) -> None:
        result = iot503_parse_frame(data)
        if result is None:
            return
        cmd, payload = result

        if cmd == Iot503Cmd.REAL_TIME_REPORT:
            report = decode_real_time_report(payload)
            self._last_report = report
            if self.on_report_received:
                self.on_report_received(report)

        elif cmd == Iot503Cmd.HB_CHECK:
            ack_frame = iot503_build_frame(Iot503Cmd.HB_ACK, b"")
            if self._sock:
                self._sock.sendto(ack_frame, addr)

    def _compute_snr(self) -> float:
        """用 B0 模型计算 SNR：基准 − 扫描损失 − 失指损失 − 雨衰。"""
        if self._last_report is None:
            return SNR_FLOOR

        # 遮挡检查
        if time.monotonic() < self._blockage_until:
            return SNR_FLOOR

        r = self._last_report
        lat, lon, alt = r.lat, r.lon, r.alt

        # 卫星可见性
        sv = sat_view(self._sat_lon, lat, lon, alt)
        if not sv.visible:
            return SNR_FLOOR

        # "真·应指"（PC 独立解算，用预设航向或设备上报航向）
        true_heading = self._preset_heading if self._preset_heading is not None else r.heading
        true_az, true_el = geo_to_phased_array(
            self._sat_lon, lat, lon, alt,
            true_heading, r.pitch, r.roll,
        )

        # 设备实际上报的波束指向
        dev_theta = abs(r.theta)  # 阵面 EL = 天顶距 = 扫描角
        dev_phi = r.phi           # 阵面 AZ

        # 扫描损失
        scan_loss = ka256_scan_loss_db(self._freq_ghz, dev_theta)

        # 失指误差
        pointing_err = pointing_error_deg(dev_theta, dev_phi, true_el, true_az)

        # SNR = 基准 − 扫描损失 − 失指损失 − 雨衰
        mispoint_loss = 12.0 * (pointing_err / KA256_BEAM_WIDTH) ** 2
        snr = self._snr_clear_boresight - scan_loss - mispoint_loss - self._rain_fade_db
        snr = max(snr, SNR_FLOOR)

        # 记录 metrics
        self._last_scan_angle = dev_theta
        self._last_pointing_err = pointing_err

        return snr

    def _send_snr_report(self) -> None:
        if self._sock is None:
            return
        snr = self._compute_snr()
        self._last_snr = snr
        payload = encode_snr_report(snr)
        frame = iot503_build_frame(Iot503Cmd.SNR_REPORT, payload)
        try:
            self._sock.sendto(frame, self._remote_addr)
        except OSError:
            pass
        if self.on_snr_updated:
            self.on_snr_updated(snr)
        if self.on_metrics_updated:
            self.on_metrics_updated({
                "snr": snr,
                "scan_angle": self._last_scan_angle,
                "pointing_err": self._last_pointing_err,
            })

    def _send_beam_config(self) -> None:
        if self._sock is None:
            return
        payload = encode_beam_config(
            self._sat_lon, self._polar, self._rx_freq, self._tx_freq,
        )
        frame = iot503_build_frame(Iot503Cmd.BEAM_CONFIG, payload)
        try:
            self._sock.sendto(frame, self._remote_addr)
        except OSError:
            pass


# ============================================================
# MockModem — 嵌入 GUI 的 IOT503 模拟 modem
# ============================================================

class MockModem(QObject):
    """IOT503 Mock Modem — 嵌入 LiveView，通过 UDP 与设备端 simulate_modem 通信。

    Signals:
        snr_updated(float): 当前 SNR
        report_received(RealTimeReport): 设备实时报告
        metrics_updated(dict): {"snr", "scan_angle", "pointing_err"}
    """

    snr_updated = Signal(float)
    report_received = Signal(object)
    metrics_updated = Signal(object)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._worker: _ModemWorker | None = None

    def start(self, port: int = 45679, remote_addr: tuple[str, int] = ("127.0.0.1", 5004),
              sat_lon: float = 134.0, band: str = "Ka") -> None:
        if self._worker and self._worker.isRunning():
            return
        self._worker = _ModemWorker(self)
        self._worker.configure(port, remote_addr, sat_lon, band)
        self._worker.on_snr_updated = lambda snr: self.snr_updated.emit(snr)
        self._worker.on_report_received = lambda r: self.report_received.emit(r)
        self._worker.on_metrics_updated = lambda m: self.metrics_updated.emit(m)
        self._worker.start()

    def stop(self) -> None:
        if self._worker:
            self._worker.stop()
            self._worker = None

    def is_running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def set_satellite(self, lon: float, band: str) -> None:
        """线程安全：push 到命令队列。"""
        if self._worker:
            self._worker._cmd_q.put(("satellite", (lon, band)))

    def set_rain_fade(self, db: float) -> None:
        if self._worker:
            self._worker._cmd_q.put(("rain", db))

    def inject_blockage(self, duration_s: float) -> None:
        if self._worker:
            self._worker._cmd_q.put(("blockage", duration_s))

    def set_snr_baseline(self, db: float) -> None:
        if self._worker:
            self._worker._cmd_q.put(("baseline", db))

    def set_heading(self, heading_deg: float | None) -> None:
        """设置预设航向（None=用设备上报值）。"""
        if self._worker:
            self._worker._cmd_q.put(("heading", heading_deg))

    @property
    def last_snr(self) -> float:
        if self._worker:
            return self._worker._last_snr
        return 0.0

    @property
    def last_report(self) -> RealTimeReport | None:
        if self._worker:
            return self._worker._last_report
        return None
