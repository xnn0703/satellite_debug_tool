"""
DEBUG 协议 v2 — 常量与数据类。

参考 `doc/DEBUG设备协议接口规范_v2.md`。协议与具体设备解耦：
本模块只定义协议编码格式，不涉及任何 hw_type 特定的 ID 含义。
"""

from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional, Union


# ---------- 协议常量 ----------

PROTOCOL_VERSION = 0x02

FRAME_HEADER_0 = 0xAA
FRAME_HEADER_1 = 0x55
FRAME_FOOTER = 0xEE
DEVICE_TYPE = 0x0D

# 每帧 DATA 段最大长度
# 2026-04-24：esa01 注册 27 路 channel，CHANNEL_DEFINE 序列化约 785B 超原 512 上限，
# 整张表会被下位机直接丢弃。上调到 1024 同时容纳 27 路；afd01/ufd45 端仍按 512 发，
# 旧设备的 ≤512 帧仍能被本上位机正确接收（向后兼容）。
MAX_DATA_LENGTH = 1024
# 完整帧最大长度（协议总量 + 9B 帧头/固定 + 2B CRC + 1B footer）
MAX_FRAME_LENGTH = MAX_DATA_LENGTH + 12

# 通道/状态/事件 ID 上界（与下位机 DEBUG_MAX_CHANNELS 等保持同步）
# 2026-04-24：esa01 端 DEBUG_MAX_CHANNELS=32，channel id 0..31 合法
CHANNEL_ID_MAX = 31          # 0..31
STATE_ID_MAX = 63            # 0..63
EVENT_ID_MIN = 0x0001        # 0x0000 保留/禁用
EVENT_ID_MAX = 0xFFFE
EVENT_ID_USER_MARK = 0xFFFF  # 规范 §8.1：上位机 USER_MARK 保留事件

# DATA_REPORT 单帧最大通道数
# 2026-04-24：esa01 一帧上报 27 个 channel，超原 16 上限会被 codec_v2 直接丢帧，
# 表象是"通道列表显示但数据全 0、Frames=0"。上调到 32 与下位机
# DEBUG_MAX_CHANNELS=32 对齐；afd01/ufd45 仍 ≤ 11 channel/帧，向后兼容。
DATA_REPORT_MAX_CHANNELS = 32
# EVENT_REPORT payload 上限（§5.9）
EVENT_PAYLOAD_MAX = 200


class CmdType(IntEnum):
    """顶层命令类型，见规范 §4。"""

    DATA_REPORT = 0x01
    COMMAND_RESPONSE = 0x02
    CONTROL = 0x03
    META_INFO = 0x04
    CHANNEL_DEFINE = 0x05
    STATE_DEFINE = 0x06
    EVENT_DEFINE = 0x07
    STATE_REPORT = 0x08
    EVENT_REPORT = 0x09
    HEARTBEAT = 0x0A
    PARA_TABLE_REPORT = 0x0B


class SubCmd(IntEnum):
    """CONTROL(0x03) 子命令，见规范 §5.3。"""

    DEBUG_ENABLE = 0x01
    REQUEST_META_INFO = 0x02
    REQUEST_CHANNEL_DEFINE = 0x03
    REQUEST_STATE_DEFINE = 0x04
    REQUEST_EVENT_DEFINE = 0x05
    USER_MARK = 0x06
    SET_SAMPLE_RATE = 0x07
    SET_TRACE_MODE = 0x08
    CHANNEL_ENABLE_MASK = 0x09
    RESET_STATS = 0x0A
    # M9: 参数管理 + OTA
    REQUEST_PARA_TABLE = 0x0B
    PARA_SET = 0x0C
    PARA_RESET = 0x0D
    OTA_BEGIN = 0x0E
    OTA_DATA = 0x0F
    OTA_END = 0x10
    OTA_ABORT = 0x11
    DEVICE_REBOOT = 0x12


class RespCode(IntEnum):
    """COMMAND_RESPONSE 响应码，见规范 §6。"""

    SUCCESS = 0
    INVALID_COMMAND = 1
    PARAM_ERROR = 2
    BUSY = 3
    INTERNAL_ERROR = 4
    NOT_SUPPORTED = 5
    STATE_NOT_ALLOWED = 6
    OUT_OF_RANGE = 7


class DataType(IntEnum):
    """CHANNEL_DEFINE.data_type（规范 §8.1）。"""

    FLOAT32 = 0x01


class StateType(IntEnum):
    """STATE_DEFINE.state_type（规范 §8.1）。"""

    BOOL = 0
    ENUM = 1


class Level(IntEnum):
    """事件/枚举级别（规范 §8.1）。"""

    DEBUG = 0
    INFO = 1
    WARN = 2
    ERROR = 3
    # 仅 STATE enum_item 使用，事件层不使用
    NEUTRAL = 0xFF  # 规范里 enum level=3 是 NEUTRAL，此处用 0xFF 避免与 EVENT level 混淆


class ParaType(IntEnum):
    """参数类型，与设备端 para_manage.h ParaType 对齐。"""

    INT = 0
    FLOAT = 1
    STRING = 2
    IP = 3
    UINT8 = 4
    INT8 = 5
    UINT16 = 6
    INT16 = 7


# 参数 flags
PARA_FLAG_REQUIRES_REBOOT = 0x01
PARA_FLAG_READ_ONLY = 0x02

# channel flags (CHANNEL_DEFINE.flags)
CHANNEL_FLAG_DEFAULT_VISIBLE = 0x01
CHANNEL_FLAG_CRITICAL = 0x02

# state flags (STATE_DEFINE.flags)
STATE_FLAG_CRITICAL = 0x01
STATE_FLAG_INVERSE = 0x02


# ---------- 数据类（解析结果） ----------


@dataclass
class MetaInfo:
    """0x04 META_INFO 载荷。"""

    protocol_ver: int
    fw_ver: str
    hw_type: str
    device_sn: str


@dataclass
class ChannelDefEntry:
    """CHANNEL_DEFINE 中单个通道定义。"""

    channel_id: int
    data_type: int
    group_id: int
    flags: int
    name: str
    unit: str
    display_min: float
    display_max: float

    @property
    def critical(self) -> bool:
        return bool(self.flags & CHANNEL_FLAG_CRITICAL)

    @property
    def default_visible(self) -> bool:
        return bool(self.flags & CHANNEL_FLAG_DEFAULT_VISIBLE)


@dataclass
class ChannelDefineTable:
    """0x05 CHANNEL_DEFINE 完整表。"""

    table_ver: int
    channels: List[ChannelDefEntry] = field(default_factory=list)


@dataclass
class StateEnumItem:
    """STATE_DEFINE ENUM 的单个枚举项。"""

    value: int
    level: int     # 参见规范 §5.6：0=INFO(绿) 1=WARN(黄) 2=ERROR(红) 3=NEUTRAL(灰)
    name: str


@dataclass
class StateDefEntry:
    """STATE_DEFINE 中单个状态字。"""

    state_id: int
    state_type: int              # 0=BOOL, 1=ENUM
    flags: int
    name: str
    enums: List[StateEnumItem] = field(default_factory=list)

    @property
    def critical(self) -> bool:
        return bool(self.flags & STATE_FLAG_CRITICAL)

    @property
    def inverse(self) -> bool:
        return bool(self.flags & STATE_FLAG_INVERSE)


@dataclass
class StateDefineTable:
    """0x06 STATE_DEFINE 完整表。"""

    table_ver: int
    states: List[StateDefEntry] = field(default_factory=list)


@dataclass
class EventDefEntry:
    """EVENT_DEFINE 中单个事件。"""

    event_id: int
    level: int        # 0=DEBUG 1=INFO 2=WARN 3=ERROR
    name: str


@dataclass
class EventDefineTable:
    """0x07 EVENT_DEFINE 完整表。"""

    table_ver: int
    events: List[EventDefEntry] = field(default_factory=list)


@dataclass
class ChannelSample:
    """DATA_REPORT 内单个通道采样。"""

    channel_id: int
    value: float


@dataclass
class DataReport:
    """0x01 DATA_REPORT 载荷。"""

    timestamp: int                               # ms since boot
    samples: List[ChannelSample] = field(default_factory=list)


@dataclass
class StateSample:
    """STATE_REPORT 单条状态。"""

    state_id: int
    value: int


@dataclass
class StateReport:
    """0x08 STATE_REPORT 载荷。"""

    timestamp: int
    states: List[StateSample] = field(default_factory=list)


@dataclass
class EventReport:
    """0x09 EVENT_REPORT 载荷。"""

    timestamp: int
    event_id: int
    payload: bytes = b""


@dataclass
class Heartbeat:
    """0x0A HEARTBEAT 载荷。"""

    uptime_ms: int
    cpu_load: int
    free_heap: int
    rx_frame_rate: int
    tx_frame_rate: int
    reserved: int = 0


@dataclass
class CommandResponse:
    """0x02 COMMAND_RESPONSE 载荷。"""

    code: int
    msg: str = ""


@dataclass
class ParaEntry:
    """参数表中的单个参数。"""

    name: str
    para_type: int       # ParaType 枚举值
    flags: int           # bit0=requires_reboot, bit1=read_only
    value: str           # 字符串表示的当前值


@dataclass
class ParaTableReport:
    """0x0B PARA_TABLE_REPORT 载荷。"""

    table_ver: int
    params: List[ParaEntry] = field(default_factory=list)


# 解析后返回的 union 类型
FrameV2Record = Union[
    DataReport,
    CommandResponse,
    MetaInfo,
    ChannelDefineTable,
    StateDefineTable,
    EventDefineTable,
    StateReport,
    EventReport,
    Heartbeat,
    ParaTableReport,
    # 未识别/未解码的控制帧等，保留原始 cmd+data
    "RawFrame",
]


@dataclass
class RawFrame:
    """未识别的命令帧，保留原始 cmd_type 与 data。"""

    cmd_type: int
    data: bytes


__all__ = [
    # constants
    "PROTOCOL_VERSION",
    "FRAME_HEADER_0", "FRAME_HEADER_1", "FRAME_FOOTER", "DEVICE_TYPE",
    "MAX_DATA_LENGTH", "MAX_FRAME_LENGTH",
    "CHANNEL_ID_MAX", "STATE_ID_MAX",
    "EVENT_ID_MIN", "EVENT_ID_MAX", "EVENT_ID_USER_MARK",
    "DATA_REPORT_MAX_CHANNELS", "EVENT_PAYLOAD_MAX",
    "CHANNEL_FLAG_DEFAULT_VISIBLE", "CHANNEL_FLAG_CRITICAL",
    "STATE_FLAG_CRITICAL", "STATE_FLAG_INVERSE",
    "PARA_FLAG_REQUIRES_REBOOT", "PARA_FLAG_READ_ONLY",
    # enums
    "CmdType", "SubCmd", "RespCode", "DataType", "StateType", "Level", "ParaType",
    # dataclasses
    "MetaInfo",
    "ChannelDefEntry", "ChannelDefineTable",
    "StateEnumItem", "StateDefEntry", "StateDefineTable",
    "EventDefEntry", "EventDefineTable",
    "ChannelSample", "DataReport",
    "StateSample", "StateReport",
    "EventReport", "Heartbeat", "CommandResponse",
    "ParaEntry", "ParaTableReport",
    "RawFrame", "FrameV2Record",
]
