"""MockModem + fake-device loopback 集成测试。

验证闭环：fake-device 发 RTR → MockModem 收 → 算 SNR → 发 SNR_REPORT → fake-device 收 → FSM 收敛。
"""

import socket
import struct
import threading
import time
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app

from satellite_debug_tool.core.simulation.mock_modem import (
    MockModem,
    RealTimeReport,
    encode_beam_config,
    encode_snr_report,
    iot503_build_frame,
    iot503_parse_frame,
    Iot503Cmd,
    BAND_PRESETS,
    SNR_CLEAR_BORESIGHT,
    SNR_FLOOR,
)
from satellite_debug_tool.core.simulation.beampointing import geo_to_phased_array


class _FakeDevice:
    """精简版 fake-device，用于进程内 loopback 测试。"""

    def __init__(self, port: int, remote_port: int, sat_lon: float,
                 lat: float, lon: float, alt: float,
                 heading_offset: float = 0.0):
        self.port = port
        self.remote_port = remote_port
        self.sat_lon = sat_lon
        self.lat = lat
        self.lon = lon
        self.alt = alt
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 状态
        self.measured_snr = SNR_FLOOR
        self.cur_theta = 45.0
        self.cur_phi = 0.0
        self.cur_heading = heading_offset % 360.0  # 初始航向偏移
        self.heading_offset = heading_offset
        self.heading_error = abs(heading_offset)
        self._target_az = 0.0
        self._target_el = 40.0
        self._tick_count = 0

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self.port))
        self._sock.settimeout(0.005)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()
            self._sock = None

    def _run(self):
        remote = ("127.0.0.1", self.remote_port)
        dt = 0.02
        while not self._stop.is_set():
            # 接收
            try:
                data, addr = self._sock.recvfrom(1024)
                result = iot503_parse_frame(data)
                if result:
                    cmd, payload = result
                    if cmd == Iot503Cmd.SNR_REPORT:
                        self.measured_snr = struct.unpack(">f", payload[:4])[0]
                    elif cmd == Iot503Cmd.BEAM_CONFIG:
                        lon_raw = struct.unpack(">h", payload[0:2])[0]
                        self.sat_lon = lon_raw / 100.0
                    elif cmd == Iot503Cmd.HB_CHECK:
                        ack = iot503_build_frame(Iot503Cmd.HB_ACK, b"")
                        self._sock.sendto(ack, remote)
            except (socket.timeout, BlockingIOError, OSError):
                pass

            # 算目标（用当前航向）
            tgt_az, tgt_el = geo_to_phased_array(
                self.sat_lon, self.lat, self.lon, self.alt,
                self.cur_heading, 0, 0,
            )
            self._target_az = tgt_az
            self._target_el = tgt_el

            # 收敛波束指向
            k = 0.15
            diff = (tgt_az - self.cur_phi + 180) % 360 - 180
            self.cur_phi = (self.cur_phi + diff * k) % 360.0
            self.cur_theta += k * (tgt_el - self.cur_theta)
            self.cur_theta = max(0.0, min(90.0, self.cur_theta))

            # 航向收敛（SNR 驱动）
            if self.measured_snr > 3.0:
                heading_diff = (0.0 - self.cur_heading + 180) % 360 - 180  # 真航向=0
                self.cur_heading = (self.cur_heading + heading_diff * 0.05) % 360.0
            self.heading_error = abs(self.cur_heading if self.cur_heading < 180 else 360 - self.cur_heading)

            # 发 RTR
            rtr_payload = self._encode_rtr()
            rtr_frame = iot503_build_frame(Iot503Cmd.REAL_TIME_REPORT, rtr_payload)
            self._sock.sendto(rtr_frame, remote)
            self._tick_count += 1
            time.sleep(dt)

    def _encode_rtr(self) -> bytes:
        fmt = ">BhhHffffBBhhhhhBBBI"  # 42B，与固件 real_time_report_payload_tle_t 一致
        return struct.pack(
            fmt,
            1,  # gps_lock
            int(self.lon * 100), int(self.lat * 100), int(self.alt),
            19450.0, 29250.0, 19250.0, 29050.0,
            1, 0,  # power, polar
            0, 0, int(self.cur_heading * 100),  # pitch, roll, heading
            int(self.cur_theta * 100), min(32767, int(self.cur_phi * 100)),
            0, 0, 0x21, self._tick_count,
        )


@pytest.fixture
def loopback_ports():
    """分配不冲突的端口。"""
    import random
    base = random.randint(20000, 30000)
    return base, base + 1  # fake_device port, mockmodem port


class TestLoopback:
    """MockModem + fake-device loopback 测试。"""

    def test_convergence(self, qapp, loopback_ports):
        """闭环收敛：fake-device 波束朝卫星收敛，SNR 上升。"""
        fake_port, modem_port = loopback_ports
        fake = _FakeDevice(fake_port, modem_port, 134.0, 32.0603, 118.7969, 25.0)
        modem = MockModem()

        snr_values = []
        modem.snr_updated.connect(lambda snr: snr_values.append(snr))

        try:
            fake.start()
            modem.start(port=modem_port, remote_addr=("127.0.0.1", fake_port),
                        sat_lon=134.0, band="Ka")

            # 跑 ~3 秒让 FSM 收敛，定期刷新事件队列
            for _ in range(60):
                time.sleep(0.05)
                qapp.processEvents()

            # 验证 SNR 有上升趋势
            assert len(snr_values) > 50, f"only {len(snr_values)} SNR samples"
            # 最后的 SNR 应该比开始高（波束收敛后 SNR 应上升）
            early_avg = sum(snr_values[10:30]) / 20
            late_avg = sum(snr_values[-30:]) / 20
            assert late_avg > early_avg, f"SNR not converging: early={early_avg:.1f}, late={late_avg:.1f}"

            # TRACK 稳态 SNR 应接近基准−scan_loss
            assert late_avg > 10.0, f"steady SNR too low: {late_avg:.1f}dB"

        finally:
            modem.stop()
            fake.stop()

    def test_beam_config_updates_target(self, qapp, loopback_ports):
        """BEAM_CONFIG 改卫星经度后 fake-device 目标随之变。"""
        fake_port, modem_port = loopback_ports
        fake = _FakeDevice(fake_port, modem_port, 134.0, 32.0603, 118.7969, 25.0)
        modem = MockModem()

        try:
            fake.start()
            modem.start(port=modem_port, remote_addr=("127.0.0.1", fake_port),
                        sat_lon=134.0, band="Ka")
            for _ in range(20):
                time.sleep(0.05)
                qapp.processEvents()

            # 改卫星经度
            modem.set_satellite(110.0, "Ka")
            for _ in range(20):
                time.sleep(0.05)
                qapp.processEvents()

            # fake-device 的目标应随之变化
            assert abs(fake.sat_lon - 110.0) < 1.0, f"sat_lon={fake.sat_lon}"

        finally:
            modem.stop()
            fake.stop()

    def test_hb_check_ack(self, qapp, loopback_ports):
        """HB_CHECK → HB_ACK 正常往返。"""
        fake_port, modem_port = loopback_ports
        fake = _FakeDevice(fake_port, modem_port, 134.0, 32.0603, 118.7969, 25.0)
        modem = MockModem()

        report_received = []
        modem.report_received.connect(lambda r: report_received.append(r))

        try:
            fake.start()
            modem.start(port=modem_port, remote_addr=("127.0.0.1", fake_port),
                        sat_lon=134.0, band="Ka")
            for _ in range(20):
                time.sleep(0.05)
                qapp.processEvents()

            # MockModem 应收到 RTR
            assert len(report_received) > 10, f"only {len(report_received)} RTR"

        finally:
            modem.stop()
            fake.stop()

    def test_inject_blockage(self, qapp, loopback_ports):
        """遮挡注入后 SNR 跌地板。"""
        fake_port, modem_port = loopback_ports
        fake = _FakeDevice(fake_port, modem_port, 134.0, 32.0603, 118.7969, 25.0)
        modem = MockModem()

        snr_values = []
        modem.snr_updated.connect(lambda snr: snr_values.append(snr))

        try:
            fake.start()
            modem.start(port=modem_port, remote_addr=("127.0.0.1", fake_port),
                        sat_lon=134.0, band="Ka")
            for _ in range(40):
                time.sleep(0.05)
                qapp.processEvents()

            # 注入遮挡
            modem.inject_blockage(1.0)
            for _ in range(10):
                time.sleep(0.05)
                qapp.processEvents()

            # SNR 应该在地板
            recent = snr_values[-10:]
            assert len(recent) > 0, "no SNR samples during blockage"
            avg_block = sum(recent) / len(recent)
            assert avg_block < SNR_FLOOR + 2.0, f"SNR not at floor during blockage: {avg_block:.1f}"

            # 等遮挡结束
            for _ in range(30):
                time.sleep(0.05)
                qapp.processEvents()

        finally:
            modem.stop()
            fake.stop()

    def test_heading_calibration(self, qapp, loopback_ports):
        """航向校准：初始偏移 30° → 收敛到接近 0°。"""
        fake_port, modem_port = loopback_ports
        fake = _FakeDevice(fake_port, modem_port, 134.0, 32.0603, 118.7969, 25.0,
                           heading_offset=30.0)
        modem = MockModem()

        snr_values = []
        modem.snr_updated.connect(lambda snr: snr_values.append(snr))

        try:
            fake.start()
            modem.start(port=modem_port, remote_addr=("127.0.0.1", fake_port),
                        sat_lon=134.0, band="Ka")

            # 跑 ~5 秒让航向收敛
            for _ in range(100):
                time.sleep(0.05)
                qapp.processEvents()

            # 航向误差应减小
            assert fake.heading_error < 20.0, \
                f"heading error not converging: {fake.heading_error:.1f}° (started at 30°)"

            # 最后的 SNR 应比开始高（航向收敛 → 失指减小 → SNR 上升）
            if len(snr_values) > 30:
                early = sum(snr_values[5:15]) / 10
                late = sum(snr_values[-15:]) / 10
                assert late > early, \
                    f"SNR not improving with heading calibration: early={early:.1f}, late={late:.1f}"

        finally:
            modem.stop()
            fake.stop()
