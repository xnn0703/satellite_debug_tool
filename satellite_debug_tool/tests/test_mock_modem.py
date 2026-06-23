"""MockModem 单元测试 — IOT503 编解码 + SNR 计算 + 线程安全。"""

import os
import struct
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from satellite_debug_tool.core.simulation.mock_modem import (
    MockModem,
    RealTimeReport,
    encode_beam_config,
    encode_snr_report,
    iot503_build_frame,
    iot503_parse_frame,
    iot503_checksum,
    decode_real_time_report,
    Iot503Cmd,
    SNR_CLEAR_BORESIGHT,
    SNR_FLOOR,
    KA256_BEAM_WIDTH,
)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class TestIot503Codec:
    """IOT503 帧编解码测试。"""

    def test_snr_roundtrip(self):
        """SNR_REPORT 编解码往返。"""
        payload = encode_snr_report(14.43, indicator=0x03, power=1, reboot=0)
        frame = iot503_build_frame(Iot503Cmd.SNR_REPORT, payload)
        result = iot503_parse_frame(frame)
        assert result is not None
        cmd, parsed = result
        assert cmd == Iot503Cmd.SNR_REPORT
        snr = struct.unpack(">f", parsed[:4])[0]
        assert abs(snr - 14.43) < 0.01
        assert parsed[4] == 0x03  # indicator
        assert parsed[5] == 1     # power
        assert parsed[6] == 0     # reboot

    def test_beam_config_roundtrip(self):
        """BEAM_CONFIG 编解码往返。"""
        payload = encode_beam_config(134.0, 0, 19450.0, 29250.0)
        frame = iot503_build_frame(Iot503Cmd.BEAM_CONFIG, payload)
        result = iot503_parse_frame(frame)
        assert result is not None
        cmd, parsed = result
        assert cmd == Iot503Cmd.BEAM_CONFIG
        lon = struct.unpack(">h", parsed[0:2])[0] / 100.0
        assert abs(lon - 134.0) < 0.01

    def test_negative_longitude(self):
        """负经度不崩溃且 round-trip 正确。"""
        payload = encode_beam_config(-100.0, 0, 19450.0, 29250.0)
        frame = iot503_build_frame(Iot503Cmd.BEAM_CONFIG, payload)
        result = iot503_parse_frame(frame)
        assert result is not None
        lon = struct.unpack(">h", result[1][0:2])[0] / 100.0
        assert abs(lon - (-100.0)) < 0.01

    def test_rtr_decode(self):
        """REAL_TIME_REPORT 解码。"""
        payload = struct.pack(">BhhHffffBBhhhhhBBBI",
            1, 11880, 3206, 25, 19450.0, 29250.0, 19250.0, 29050.0,
            1, 0, 0, 0, 0, 4078, 29713, 0, 0, 0x21, 100)
        frame = iot503_build_frame(Iot503Cmd.REAL_TIME_REPORT, payload)
        result = iot503_parse_frame(frame)
        assert result is not None
        report = decode_real_time_report(result[1])
        assert abs(report.lat - 32.06) < 0.01
        assert abs(report.lon - 118.8) < 0.01
        assert abs(report.theta - 40.78) < 0.01
        assert abs(report.phi - 297.13) < 0.01

    def test_checksum_valid(self):
        """校验和验证。"""
        frame = iot503_build_frame(0x01, encode_snr_report(10.0))
        result = iot503_parse_frame(frame)
        assert result is not None

    def test_checksum_invalid(self):
        """错误校验和 → 解析失败。"""
        frame = iot503_build_frame(0x01, encode_snr_report(10.0))
        corrupted = bytearray(frame)
        corrupted[-1] ^= 0xFF  # 破坏校验和
        result = iot503_parse_frame(bytes(corrupted))
        assert result is None

    def test_hb_frame(self):
        """HB_CHECK/HB_ACK 帧。"""
        for cmd in [Iot503Cmd.HB_CHECK, Iot503Cmd.HB_ACK]:
            frame = iot503_build_frame(cmd, b"")
            result = iot503_parse_frame(frame)
            assert result is not None
            assert result[0] == cmd
            assert len(result[1]) == 0


class TestMockModem:
    """MockModem 集成测试。"""

    def test_start_stop(self, qapp):
        """启动/停止不崩溃。"""
        modem = MockModem()
        modem.start(port=40201, remote_addr=("127.0.0.1", 40200))
        assert modem.is_running()
        modem.stop()
        assert not modem.is_running()

    def test_snr_floor_when_no_report(self, qapp):
        """无 RTR 时 SNR = 地板。"""
        modem = MockModem()
        snr_vals = []
        modem.snr_updated.connect(lambda s: snr_vals.append(s))
        modem.start(port=40211, remote_addr=("127.0.0.1", 40210))
        time.sleep(0.5)
        modem.stop()
        assert all(s == SNR_FLOOR for s in snr_vals)

    def test_inject_blockage(self, qapp):
        """遮挡后 SNR = 地板。"""
        modem = MockModem()
        snr_vals = []
        modem.snr_updated.connect(lambda s: snr_vals.append(s))
        modem.start(port=40221, remote_addr=("127.0.0.1", 40220))
        time.sleep(0.3)
        modem.inject_blockage(0.5)
        time.sleep(0.3)
        recent = snr_vals[-5:]
        assert all(s <= SNR_FLOOR + 1.0 for s in recent)
        modem.stop()

    def test_set_snr_baseline(self, qapp):
        """SNR 基准可调。"""
        modem = MockModem()
        modem.start(port=40231, remote_addr=("127.0.0.1", 40230))
        modem.set_snr_baseline(20.0)
        time.sleep(0.1)  # 等命令被消费
        if modem._worker:
            assert modem._worker._snr_clear_boresight == 20.0
        modem.stop()
