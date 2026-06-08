"""
下位机模拟器 —— 协议 v2 端到端联调工具。

用途：在没有真实 afd01/ufd45 硬件的情况下，用 Python 模拟下位机行为：
- 发送 META_INFO / CHANNEL_DEFINE / STATE_DEFINE / EVENT_DEFINE
- 周期发送 DATA_REPORT（100Hz）、STATE_REPORT（5Hz）、HEARTBEAT（1Hz）、
  DEFINE 重广播（0.2Hz）
- 随机注入事件（如 LOCK_ACQUIRED / LOCK_LOST / BEACON_LOST）
- 响应上位机 CONTROL 子命令（DEBUG_ENABLE / REQUEST_* / USER_MARK / SET_SAMPLE_RATE / RESET_STATS）

运行（在仓库根目录）::

    python tools/device_simulator.py --profile afd01
    # 或
    python tools/device_simulator.py --profile ufd45

上位机设置：
  - UDP 远端 127.0.0.1:4004，本地端口 45678
  - 连接后 1-2 秒内应自动完成握手、开始滚动曲线

Ctrl-C 退出。
"""

from __future__ import annotations

import argparse
import logging
import math
import random
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ---- 让模拟器能 import 项目包（脚本放在 tools/ 下） ----
_PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from satellite_debug_tool.core.protocol import (  # noqa: E402
    CmdType,
    FrameReceiverV2,
    MetaInfo,
    PROTOCOL_VERSION,
    RawFrame,
    RespCode,
    SubCmd,
    build_frame,
)


# ============================================================
# Profile dataclasses（独立定义，避免依赖上位机 ProfileStore）
# ============================================================

@dataclass
class ChannelSpec:
    id: int
    name: str
    unit: str
    group: int
    critical: bool
    display_min: float
    display_max: float
    # 运行时数据生成器：returns float
    generator: Callable[[float], float] = field(default=lambda t: 0.0)


@dataclass
class EnumItem:
    value: int
    level: int   # 0=INFO/绿 1=WARN/黄 2=ERROR/红 3=NEUTRAL/灰
    name: str


@dataclass
class StateSpec:
    id: int
    name: str
    is_enum: bool
    critical: bool = True
    inverse: bool = False
    enums: List[EnumItem] = field(default_factory=list)
    initial_value: int = 0


@dataclass
class EventSpec:
    id: int
    level: int
    name: str


@dataclass
class ProfileSpec:
    hw_type: str
    fw_ver: str
    device_sn: str
    channels: List[ChannelSpec]
    states: List[StateSpec]
    events: List[EventSpec]
    channel_table_ver: int = 1
    state_table_ver: int = 1
    event_table_ver: int = 1


# ============================================================
# 编码函数（与下位机 debug_registry.c 对称）
# ============================================================

def _encode_u8_str(s: str, max_len: int) -> bytes:
    b = s.encode("utf-8")[:max_len]
    return bytes([len(b)]) + b


def encode_meta_info(profile: ProfileSpec) -> bytes:
    return (
        bytes([PROTOCOL_VERSION])
        + _encode_u8_str(profile.fw_ver, 31)
        + _encode_u8_str(profile.hw_type, 31)
        + _encode_u8_str(profile.device_sn, 31)
    )


def encode_channel_define(profile: ProfileSpec) -> bytes:
    out = bytearray([profile.channel_table_ver, len(profile.channels)])
    for c in profile.channels:
        flags = 0
        if c.critical:
            flags |= 0x02
        # default_visible 默认置位，方便上位机显示
        flags |= 0x01
        out.extend([c.id, 0x01, c.group, flags])
        out.extend(_encode_u8_str(c.name, 31))
        out.extend(_encode_u8_str(c.unit, 15))
        out.extend(struct.pack("<ff", c.display_min, c.display_max))
    return bytes(out)


def encode_state_define(profile: ProfileSpec) -> bytes:
    out = bytearray([profile.state_table_ver, len(profile.states)])
    for s in profile.states:
        flags = (0x01 if s.critical else 0) | (0x02 if s.inverse else 0)
        out.extend([s.id, 1 if s.is_enum else 0, flags])
        out.extend(_encode_u8_str(s.name, 31))
        out.append(len(s.enums))
        for e in s.enums:
            out.extend([e.value, e.level])
            out.extend(_encode_u8_str(e.name, 31))
    return bytes(out)


def encode_event_define(profile: ProfileSpec) -> bytes:
    out = bytearray([profile.event_table_ver, len(profile.events)])
    for e in profile.events:
        out.extend(struct.pack("<H", e.id))
        out.append(e.level)
        out.extend(_encode_u8_str(e.name, 31))
    return bytes(out)


def encode_data_report(ts_ms: int, samples: List[Tuple[int, float]]) -> bytes:
    buf = bytearray(struct.pack("<I", ts_ms) + bytes([len(samples)]))
    for cid, val in samples:
        buf.append(cid)
        buf.extend(struct.pack("<f", val))
    return bytes(buf)


def encode_state_report(ts_ms: int, states: List[Tuple[int, int]]) -> bytes:
    buf = bytearray(struct.pack("<I", ts_ms) + bytes([len(states)]))
    for sid, val in states:
        buf.extend([sid, val & 0xFF])
    return bytes(buf)


def encode_event_report(ts_ms: int, event_id: int, payload: bytes = b"") -> bytes:
    return (
        struct.pack("<I", ts_ms)
        + struct.pack("<H", event_id)
        + bytes([len(payload)])
        + payload
    )


def encode_heartbeat(uptime_ms: int, cpu_load: int, free_heap: int,
                     rx_rate: int, tx_rate: int) -> bytes:
    return struct.pack("<IBIHHI", uptime_ms, cpu_load, free_heap, rx_rate, tx_rate, 0)


def encode_cmd_response(code: int, msg: str = "OK") -> bytes:
    return bytes([code]) + msg.encode("utf-8")[:63]


# ============================================================
# 内置 profile（afd01 / ufd45）
# ============================================================

def _afd01_profile() -> ProfileSpec:
    # 数据生成器：模拟跟星过程中的姿态抖动 + 锁星后 SNR 上升
    def roll(t: float)  -> float: return 5.0 * math.sin(t * 0.3)
    def pitch(t: float) -> float: return 3.0 * math.sin(t * 0.5)
    def yaw(t: float)   -> float: return (30 * t) % 360 - 180
    def az(t: float)    -> float: return 125 + 2 * math.sin(t * 0.8)
    def el(t: float)    -> float: return 42 + 0.5 * math.sin(t * 0.9)
    def snr(t: float)   -> float:
        base = 25 + 10 * math.tanh(t * 0.2)   # 渐强到 ~35 dB
        return max(0.0, base + random.uniform(-1.5, 1.5))
    def cpu(t: float)   -> float: return 25 + 10 * math.sin(t * 0.1)

    # GPS 轨迹：南京新街口附近，模拟车载终端缓慢游走（椭圆 + 前向漂移），
    # 录制几分钟后回放可在离线地图上看到一段清晰轨迹 + 起点(绿)/终点(红)。
    _LAT0, _LON0 = 32.0603, 118.7969
    def gps_lat(t: float) -> float: return _LAT0 + 0.0050 * math.sin(t * 0.020) + 0.00008 * t
    def gps_lon(t: float) -> float: return _LON0 + 0.0065 * math.cos(t * 0.020) + 0.00010 * t
    def gps_alt(t: float) -> float: return 25.0 + 8.0 * math.sin(t * 0.06)

    return ProfileSpec(
        hw_type="afd01", fw_ver="afd01-sim-2.0", device_sn="SIM-AFD01-001",
        channels=[
            ChannelSpec(0, "roll",     "°",  0, True,  -180, 180, roll),
            ChannelSpec(1, "pitch",    "°",  0, True,  -90,  90,  pitch),
            ChannelSpec(2, "yaw",      "°",  0, True,  -180, 180, yaw),
            ChannelSpec(3, "ant_az",   "°",  1, True,  0,    360, az),
            ChannelSpec(4, "ant_el",   "°",  1, True,  0,    90,  el),
            ChannelSpec(5, "ant_skew", "°",  1, False, -90,  90,  lambda t: 0.0),
            ChannelSpec(6, "tgt_az",   "°",  1, True,  0,    360, lambda t: 125.5),
            ChannelSpec(7, "tgt_el",   "°",  1, True,  0,    90,  lambda t: 42.3),
            ChannelSpec(8, "err_az",   "°",  3, False, -10,  10,  lambda t: 0.3 * math.sin(t)),
            ChannelSpec(9, "err_el",   "°",  3, False, -10,  10,  lambda t: 0.2 * math.cos(t)),
            ChannelSpec(10, "snr",     "dB", 2, True,  0,    60,  snr),
            # 位置（group 4）—— 离线地图按 gps_lat / gps_lon 通道名自动启用
            ChannelSpec(11, "gps_lat", "°",  4, True,  31.5, 32.5,  gps_lat),
            ChannelSpec(12, "gps_lon", "°",  4, True,  118.5, 119.5, gps_lon),
            ChannelSpec(13, "gps_alt", "m",  4, False, 0,    200,   gps_alt),
            ChannelSpec(15, "cpu_load","%",  5, False, 0,    100, cpu),
        ],
        states=[
            StateSpec(0, "TRACE_MODE", True, True, enums=[
                EnumItem(0, 3, "STANDBY"),
                EnumItem(1, 1, "SCAN_GLOBAL"),
                EnumItem(2, 1, "SCAN_WIDE"),
                EnumItem(3, 0, "LOCK"),
                EnumItem(4, 3, "MANUAL"),
            ], initial_value=1),
            StateSpec(1, "LOCK_FLAG", False, True, initial_value=0),
            StateSpec(2, "GPS_FIX", True, True, enums=[
                EnumItem(0, 2, "NO_FIX"),
                EnumItem(1, 1, "2D"),
                EnumItem(2, 0, "3D"),
                EnumItem(3, 0, "RTK"),
            ], initial_value=2),
            StateSpec(3, "INS_READY",  False, True, initial_value=1),
            StateSpec(4, "PLL_LOCKED", False, True, initial_value=1),
            StateSpec(7, "WIZNET_LINK", False, False, initial_value=1),
            StateSpec(8, "MODEM_CONNECTED", False, True, initial_value=1),
        ],
        events=[
            EventSpec(0x0003, 1, "LOCK_ACQUIRED"),
            EventSpec(0x0004, 2, "LOCK_LOST"),
            EventSpec(0x0005, 1, "TRACE_MODE_CHANGED"),
            EventSpec(0x0006, 2, "SNR_BELOW_THRESHOLD"),
            EventSpec(0x0201, 1, "INS_ALIGN_DONE"),
        ],
    )


def _ufd45_profile() -> ProfileSpec:
    # ufd45: Ku 频段特色（buc_temp / lnb_current / bcn_rssi 等）
    def roll(t: float)  -> float: return 2.5 * math.sin(t * 0.25)
    def pitch(t: float) -> float: return 1.5 * math.sin(t * 0.4)
    def yaw(t: float)   -> float: return (20 * t) % 360 - 180
    def az(t: float)    -> float: return 210 + 1.5 * math.sin(t * 0.6)
    def el(t: float)    -> float: return 55 + 0.3 * math.sin(t * 0.9)
    def bcn_rssi(t: float) -> float: return -65 + 8 * math.tanh(t * 0.2) + random.uniform(-1, 1)
    def bcn_off(t: float)  -> float: return 2.0 * math.sin(t * 0.3)
    def buc_temp(t: float) -> float: return 45 + 5 * math.sin(t * 0.05)
    def tx_power(t: float) -> float: return 28 + 0.2 * math.sin(t * 0.3)

    return ProfileSpec(
        hw_type="ufd45", fw_ver="ufd45-sim-2.0", device_sn="SIM-UFD45-001",
        channels=[
            ChannelSpec(0, "roll",             "°",   0, True,  -180, 180,  roll),
            ChannelSpec(1, "pitch",            "°",   0, True,  -90,  90,   pitch),
            ChannelSpec(2, "yaw",              "°",   0, True,  -180, 180,  yaw),
            ChannelSpec(3, "ant_az",           "°",   1, True,  0,    360,  az),
            ChannelSpec(4, "ant_el",           "°",   1, True,  0,    90,   el),
            ChannelSpec(6, "bcn_rssi",         "dBm", 2, True,  -100, -40,  bcn_rssi),
            ChannelSpec(7, "bcn_freq_offset",  "kHz", 2, False, -50,  50,   bcn_off),
            ChannelSpec(10, "ku_lo",           "MHz", 4, False, 9000, 12000, lambda t: 10950.0),
            ChannelSpec(11, "buc_temp",        "°C",  4, True,  -20,  85,   buc_temp),
            ChannelSpec(12, "lnb_current",     "mA",  4, False, 0,    500,  lambda t: 180.0),
            ChannelSpec(13, "tx_power",        "dBm", 4, True,  0,    40,   tx_power),
            ChannelSpec(14, "rx_agc",          "dB",  2, False, 0,    80,   lambda t: 45.0),
        ],
        states=[
            StateSpec(0, "TRACE_MODE", True, True, enums=[
                EnumItem(0, 3, "STANDBY"),
                EnumItem(1, 1, "SCAN_GLOBAL"),
                EnumItem(3, 0, "LOCK"),
                EnumItem(4, 3, "MANUAL"),
            ], initial_value=1),
            StateSpec(1, "LOCK_FLAG",    False, True, initial_value=0),
            StateSpec(3, "BUC_READY",    False, True, initial_value=1),
            StateSpec(4, "LNB_OK",       False, True, initial_value=1),
            StateSpec(5, "BEACON_LOCKED", False, True, initial_value=0),
            StateSpec(8, "POLARIZATION", True, True, enums=[
                EnumItem(0, 0, "H"),
                EnumItem(1, 0, "V"),
                EnumItem(2, 0, "RHCP"),
                EnumItem(3, 0, "LHCP"),
            ], initial_value=0),
        ],
        events=[
            EventSpec(0x0003, 1, "LOCK_ACQUIRED"),
            EventSpec(0x0004, 2, "LOCK_LOST"),
            EventSpec(0x0410, 1, "BEACON_ACQUIRED"),
            EventSpec(0x0411, 2, "BEACON_LOST"),
            EventSpec(0x0420, 3, "BUC_OVERTEMP"),
            EventSpec(0x0430, 1, "POLARIZATION_SWITCH"),
        ],
    )


PROFILES = {"afd01": _afd01_profile, "ufd45": _ufd45_profile}


# ============================================================
# Simulator
# ============================================================

class Simulator:
    def __init__(
        self,
        profile: ProfileSpec,
        local_port: int = 4004,
        remote_addr: Tuple[str, int] = ("127.0.0.1", 45678),
        sample_rate_hz: int = 100,
    ):
        self.profile = profile
        self.local_port = local_port
        self.remote_addr = remote_addr
        self.sample_rate_hz = sample_rate_hz

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", local_port))
        self.sock.settimeout(0.02)
        self.receiver = FrameReceiverV2()

        self.t_start = time.monotonic()
        self._stop = threading.Event()

        # 状态字当前值
        self._state_values: Dict[int, int] = {s.id: s.initial_value for s in profile.states}
        self._state_changed: Dict[int, bool] = {s.id: True for s in profile.states}  # 首次全量

        # 事件注入计划（按仿真时间触发）
        self._next_lock_toggle = 5.0
        self._next_random_event = 8.0

        # 统计
        self._tx_count = 0
        self._rx_count = 0

        self.log = logging.getLogger("sim")

    # -------- 工具 --------

    def uptime_ms(self) -> int:
        return int((time.monotonic() - self.t_start) * 1000)

    def send(self, cmd: int, data: bytes) -> None:
        frame = build_frame(cmd, data)
        self.sock.sendto(frame, self.remote_addr)
        self._tx_count += 1

    def broadcast_meta_and_defines(self) -> None:
        self.send(CmdType.META_INFO,      encode_meta_info(self.profile))
        self.send(CmdType.CHANNEL_DEFINE, encode_channel_define(self.profile))
        self.send(CmdType.STATE_DEFINE,   encode_state_define(self.profile))
        self.send(CmdType.EVENT_DEFINE,   encode_event_define(self.profile))
        self.log.info("broadcast META + 3× DEFINE")

    # -------- 周期任务 --------

    def tick_data_report(self, now: float) -> None:
        samples = [(c.id, float(c.generator(now))) for c in self.profile.channels]
        self.send(CmdType.DATA_REPORT, encode_data_report(self.uptime_ms(), samples))

    def tick_state_report(self, full: bool) -> None:
        if full:
            items = [(sid, v) for sid, v in self._state_values.items()]
        else:
            items = [(sid, v) for sid, v in self._state_values.items()
                     if self._state_changed.get(sid, False)]
        if not items:
            return
        self.send(CmdType.STATE_REPORT, encode_state_report(self.uptime_ms(), items))
        for sid, _ in items:
            self._state_changed[sid] = False

    def tick_heartbeat(self) -> None:
        free_heap = 50000 + random.randint(-500, 500)
        self.send(CmdType.HEARTBEAT, encode_heartbeat(
            self.uptime_ms(),
            cpu_load=random.randint(20, 35),
            free_heap=free_heap,
            rx_rate=self._rx_count,
            tx_rate=self._tx_count,
        ))
        self._rx_count = 0
        self._tx_count = 0

    def inject_events(self, now: float) -> None:
        # 每 ~6 秒切换一次 LOCK_FLAG 并触发事件
        if now >= self._next_lock_toggle:
            lock_id = 1   # LOCK_FLAG
            new_val = 0 if self._state_values.get(lock_id, 0) else 1
            self._set_state(lock_id, new_val)
            ev_id = 0x0003 if new_val else 0x0004
            self.send(CmdType.EVENT_REPORT, encode_event_report(self.uptime_ms(), ev_id))
            # 同步切换 TRACE_MODE（0 → 3 LOCK 或 1 SCAN_GLOBAL）
            self._set_state(0, 3 if new_val else 1)
            self._next_lock_toggle = now + 6.0 + random.uniform(-1, 1)

        # 每 ~10 秒随机注入一条事件
        if now >= self._next_random_event and self.profile.events:
            ev = random.choice(self.profile.events)
            self.send(CmdType.EVENT_REPORT, encode_event_report(self.uptime_ms(), ev.id))
            self._next_random_event = now + 10.0 + random.uniform(-2, 2)

    def _set_state(self, state_id: int, value: int) -> None:
        if self._state_values.get(state_id) != value:
            self._state_values[state_id] = value
            self._state_changed[state_id] = True

    # -------- 下行 CONTROL 处理 --------

    def poll_rx(self) -> None:
        try:
            data, _addr = self.sock.recvfrom(2048)
        except (socket.timeout, BlockingIOError):
            return
        except OSError:
            return
        if not data:
            return
        self._rx_count += 1
        for rec in self.receiver.feed(data):
            if isinstance(rec, RawFrame) and rec.cmd_type == CmdType.CONTROL:
                self.handle_control(rec.data)

    def handle_control(self, data: bytes) -> None:
        if not data:
            self.send(CmdType.COMMAND_RESPONSE,
                      encode_cmd_response(RespCode.PARAM_ERROR, "empty"))
            return
        sub = data[0]
        payload = data[1:]
        self.log.info("CONTROL sub_cmd=0x%02X payload=%dB", sub, len(payload))

        if sub == SubCmd.DEBUG_ENABLE:
            en = bool(payload[0]) if payload else False
            self.log.info("debug_enable=%s", en)
            self._respond(RespCode.SUCCESS, "OK")
        elif sub == SubCmd.REQUEST_META_INFO:
            self.send(CmdType.META_INFO, encode_meta_info(self.profile))
            self._respond(RespCode.SUCCESS)
        elif sub == SubCmd.REQUEST_CHANNEL_DEFINE:
            self.send(CmdType.CHANNEL_DEFINE, encode_channel_define(self.profile))
            self._respond(RespCode.SUCCESS)
        elif sub == SubCmd.REQUEST_STATE_DEFINE:
            self.send(CmdType.STATE_DEFINE, encode_state_define(self.profile))
            self._respond(RespCode.SUCCESS)
        elif sub == SubCmd.REQUEST_EVENT_DEFINE:
            self.send(CmdType.EVENT_DEFINE, encode_event_define(self.profile))
            self._respond(RespCode.SUCCESS)
        elif sub == SubCmd.USER_MARK:
            if len(payload) >= 3:
                mark_id = payload[0] | (payload[1] << 8)
                text_len = payload[2]
                text = payload[3:3 + text_len].decode("utf-8", errors="replace")
                self.log.info("USER_MARK #%d: %r", mark_id, text)
                # 协议要求：以 event_id=0xFFFF 回灌
                self.send(CmdType.EVENT_REPORT,
                          encode_event_report(self.uptime_ms(), 0xFFFF, payload[3:3 + text_len]))
            self._respond(RespCode.SUCCESS)
        elif sub == SubCmd.SET_SAMPLE_RATE:
            if len(payload) >= 2:
                hz = payload[0] | (payload[1] << 8)
                if 5 <= hz <= 200:
                    self.sample_rate_hz = hz
                    self.log.info("sample_rate set to %d Hz", hz)
                    self._respond(RespCode.SUCCESS)
                else:
                    self._respond(RespCode.OUT_OF_RANGE, "5..200")
            else:
                self._respond(RespCode.PARAM_ERROR)
        elif sub == SubCmd.SET_TRACE_MODE:
            if payload:
                mode = payload[0]
                self._set_state(0, mode)
                self._respond(RespCode.SUCCESS)
            else:
                self._respond(RespCode.PARAM_ERROR)
        elif sub == SubCmd.RESET_STATS:
            self._tx_count = 0
            self._rx_count = 0
            self._respond(RespCode.SUCCESS)
        else:
            self._respond(RespCode.NOT_SUPPORTED, "TODO")

    def _respond(self, code: int, msg: str = "OK") -> None:
        self.send(CmdType.COMMAND_RESPONSE, encode_cmd_response(code, msg))

    # -------- 主循环 --------

    def run(self) -> None:
        self.log.info("Simulator %s → %s", self.profile.hw_type, self.remote_addr)
        self.broadcast_meta_and_defines()

        next_data = 0.0
        next_state_changed = 0.2
        next_hb = 1.0
        next_define = 5.0
        next_full_state = 1.0

        try:
            while not self._stop.is_set():
                now = time.monotonic() - self.t_start

                # 接收下行
                self.poll_rx()

                # DATA
                period_data = 1.0 / max(5, self.sample_rate_hz)
                if now >= next_data:
                    self.tick_data_report(now)
                    next_data = now + period_data

                # 变化 STATE
                if now >= next_state_changed:
                    self.tick_state_report(full=False)
                    next_state_changed = now + 0.2

                # 全量 STATE + HEARTBEAT
                if now >= next_hb:
                    self.tick_heartbeat()
                    next_hb = now + 1.0
                if now >= next_full_state:
                    self.tick_state_report(full=True)
                    next_full_state = now + 1.0

                # 5s DEFINE 广播
                if now >= next_define:
                    self.broadcast_meta_and_defines()
                    next_define = now + 5.0

                # 事件注入
                self.inject_events(now)

                # 避免 busy loop
                time.sleep(0.005)
        except KeyboardInterrupt:
            pass
        finally:
            self.log.info("simulator stopped after %.1fs", time.monotonic() - self.t_start)
            self.sock.close()


# ============================================================
# CLI
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="DEBUG v2 device simulator")
    parser.add_argument(
        "--profile", choices=list(PROFILES.keys()), default="afd01",
        help="模拟的设备型号（默认 afd01）",
    )
    parser.add_argument(
        "--local-port", type=int, default=4004,
        help="模拟器绑定的本地 UDP 端口（下位机侧，默认 4004）",
    )
    parser.add_argument(
        "--remote-ip", default="127.0.0.1",
        help="上位机 IP（默认 127.0.0.1）",
    )
    parser.add_argument(
        "--remote-port", type=int, default=45678,
        help="上位机本地 UDP 端口（默认 45678）",
    )
    parser.add_argument(
        "--rate", type=int, default=100,
        help="DATA_REPORT 默认采样率 Hz（默认 100）",
    )
    parser.add_argument(
        "--verbose", "-v", action="count", default=0,
        help="日志级别：-v=INFO（默认），-vv=DEBUG",
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose >= 2 else (logging.INFO if args.verbose >= 1 else logging.WARNING)
    logging.basicConfig(format="%(asctime)s [%(name)s] %(levelname)s %(message)s", level=level)

    profile = PROFILES[args.profile]()
    sim = Simulator(
        profile=profile,
        local_port=args.local_port,
        remote_addr=(args.remote_ip, args.remote_port),
        sample_rate_hz=args.rate,
    )
    sim.run()


if __name__ == "__main__":
    main()
