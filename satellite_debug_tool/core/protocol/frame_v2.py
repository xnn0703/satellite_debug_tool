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
# 2026-04-24：esa01 注册 27 路 channel，CHANNEL_DEFINE 序列化约 785B，上限从 512 扩到 1024。
# 2026-08-23：AFD01 的 21-state STATE_DEFINE 序列化为 1170B，上限保持 1536。
# 旧设备发送的 ≤512/1024B 帧仍能被本上位机正确接收（向后兼容）。
MAX_DATA_LENGTH = 1536
# 设备端保守帧缓冲合同；实际 wire 固定开销为 9B，此处额外预留 3B。
MAX_FRAME_LENGTH = MAX_DATA_LENGTH + 12

# 通道/状态/事件 ID 上界（与下位机 DEBUG_MAX_CHANNELS 等保持同步）
# 2026-07-30：控制协议的 enable mask 原本已预留 64 位；AFD01 为追加内部 INS
# 诊断通道把注册容量扩为 64，channel id 0..63 合法。
CHANNEL_ID_MAX = 63          # 0..63
STATE_ID_MAX = 63            # 0..63
EVENT_ID_MIN = 0x0001        # 0x0000 保留/禁用
EVENT_ID_MAX = 0xFFFE
EVENT_ID_USER_MARK = 0xFFFF  # 规范 §8.1：上位机 USER_MARK 保留事件

# DATA_REPORT 单帧最大通道数
# 2026-04-24：esa01 一帧上报 27 个 channel，超原 16 上限会被 codec_v2 直接丢帧，
# 表象是"通道列表显示但数据全 0、Frames=0"。
# 2026-07-30：随固件注册表和 64-bit enable mask 扩至 64；最大 DATA 段仅 325B，
# 仍明显低于 MAX_DATA_LENGTH=1536，旧设备的小帧保持兼容。
DATA_REPORT_MAX_CHANNELS = 64
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
    PROFILE_SEMANTICS = 0x0C
    GNSS_SKY_REPORT = 0x0D
    GNSS_CNR_REPORT = 0x0E
    GNSS_SAT_REPORT = 0x0F
    GNSS_SIGNAL_REPORT = 0x10
    # M18: stable AFD01 product-service records. The envelope stays compatible
    # with Debug v2 while customer UI no longer depends on dynamic channel names.
    SERVICE_IDENTITY = 0x20
    SERVICE_FAST_STATE = 0x21
    SERVICE_SLOW_STATE = 0x22
    SERVICE_COMPONENT_HEALTH = 0x23
    SERVICE_CAPABILITIES = 0x24
    SERVICE_CONTROL_REQUEST = 0x25
    SERVICE_CONTROL_RESPONSE = 0x26
    SERVICE_LINK_DETAIL = 0x27
    SERVICE_RF_LOCK_STATUS = 0x28
    SERVICE_HARDWARE_IDENTITY = 0x29
    SERVICE_NAV_SOURCE_INFO = 0x2A
    SERVICE_EXTERNAL_INS_DIAGNOSTICS = 0x2B
    # XESA01 Orbit/TLE service. It uses the Debug v2 envelope but remains
    # independent from continuous engineering telemetry and product service.
    ORBIT_REQUEST = 0x30
    ORBIT_REPORT = 0x31


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
    REQUEST_PROFILE_SEMANTICS = 0x13


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


class ServiceControlOp(IntEnum):
    """AFD01 product-service control operations."""

    SUBSCRIBE = 0
    SET_CONTROL_MODE = 1
    APPLY_RF = 2
    SET_TX_ENABLE = 3
    SET_CAPTURE_PROFILE = 4


class ServiceResultCode(IntEnum):
    SUCCESS = 0
    INVALID_REQUEST = 1
    OUT_OF_RANGE = 2
    STATE_NOT_ALLOWED = 3
    NOT_SUPPORTED = 4
    BUSY = 5
    INTERNAL_ERROR = 6


class OrbitOperation(IntEnum):
    """XESA01 Orbit/TLE request/report operation values."""

    CAPABILITIES = 1
    SCAN = 2
    CATALOG = 3
    CURRENT = 4
    PREDICT_SUBMIT = 5
    PREDICT_PAGE = 6
    SELECT = 7
    UPLOAD_BEGIN = 8
    UPLOAD_CHUNK = 9
    UPLOAD_END = 10
    UPLOAD_ABORT = 11
    SKY_SNAPSHOT = 12


class OrbitStatus(IntEnum):
    """Orbit report status values."""

    OK = 0
    INVALID_REQUEST = 1
    UNAVAILABLE = 2
    BUSY = 3
    INTERNAL_ERROR = 4
    CRC_ERROR = 5


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


@dataclass
class ProfileSemanticChannelEntry:
    """PROFILE_SEMANTICS 中单个 channel 的 role 声明。"""

    channel_id: int
    roles: List[str] = field(default_factory=list)


@dataclass
class ProfileSemanticStateEntry:
    """PROFILE_SEMANTICS 中单个 state 的 role/control 声明。"""

    state_id: int
    role: str = ""
    control_subcmd: int = 0
    control_value_from: int = 0


@dataclass
class ProfileSemanticCapabilityEntry:
    """PROFILE_SEMANTICS 中单个 capability 声明。"""

    name: str
    supported: bool


@dataclass
class ProfileSemanticsReport:
    """0x0C PROFILE_SEMANTICS 载荷。"""

    table_ver: int
    channels: List[ProfileSemanticChannelEntry] = field(default_factory=list)
    states: List[ProfileSemanticStateEntry] = field(default_factory=list)
    capabilities: List[ProfileSemanticCapabilityEntry] = field(default_factory=list)


@dataclass(frozen=True)
class OrbitReportHeader:
    version: int
    operation: OrbitOperation
    status: OrbitStatus
    request_id: int


@dataclass(frozen=True)
class OrbitStatusReport(OrbitReportHeader):
    """无附加载荷的成功确认或失败响应。"""


@dataclass(frozen=True)
class OrbitCapabilitiesReport(OrbitReportHeader):
    feature_flags: int
    max_catalog_entries: int
    max_file_size: int
    max_horizon_s: int
    min_step_s: int
    max_output_points: int
    stale_days: float


@dataclass(frozen=True)
class OrbitCatalogEntry:
    norad_id: int
    epoch_unix_s: int
    name: str
    source: str


@dataclass(frozen=True)
class OrbitCatalogReport(OrbitReportHeader):
    generation: int
    total_entries: int
    page: int
    scan_pending: bool
    scan_running: bool
    last_scan_success: bool
    invalid_records: int
    duplicate_records: int
    capacity_rejections: int
    entries: tuple[OrbitCatalogEntry, ...]


@dataclass(frozen=True)
class OrbitCurrentSample:
    norad_id: int
    utc_unix_ms: int
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    azimuth_deg: float
    elevation_deg: float
    slant_range_m: float
    tle_age_days: float
    stale: bool
    visible: bool


@dataclass(frozen=True)
class OrbitCurrentReport(OrbitReportHeader):
    generation: int
    total_entries: int
    page: int
    samples: tuple[OrbitCurrentSample, ...]


@dataclass(frozen=True)
class OrbitSkySample:
    norad_id: int
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    azimuth_deg: float
    elevation_deg: float
    slant_range_m: float
    array_azimuth_deg: float
    array_offaxis_deg: float
    tle_age_days: float
    stale: bool
    geographic_visible: bool
    front_hemisphere: bool
    in_hard_envelope: bool
    active_target: bool


@dataclass(frozen=True)
class OrbitSkyReport(OrbitReportHeader):
    snapshot_id: int
    generation: int
    utc_unix_ms: int
    total_entries: int
    page: int
    hard_offaxis_limit_deg: float
    recommended_offaxis_limit_deg: float
    mount_yaw_deg: float
    mount_pitch_deg: float
    mount_roll_deg: float
    azimuth_zero_offset_deg: float
    azimuth_direction: int
    second_angle_type: int
    active_target_id: int
    profile_characterized: bool
    samples: tuple[OrbitSkySample, ...]


@dataclass(frozen=True)
class OrbitPredictionAccepted(OrbitReportHeader):
    job_id: int
    generation: int
    norad_id: int
    start_utc_ms: int
    horizon_s: int
    step_s: int
    minimum_elevation_deg: float
    station_latitude_deg: float
    station_longitude_deg: float
    station_altitude_m: float
    assumption_flags: int


@dataclass(frozen=True)
class OrbitPredictionSample:
    utc_unix_ms: int
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    azimuth_deg: float
    elevation_deg: float
    slant_range_m: float
    tle_age_days: float
    stale: bool
    visible: bool


@dataclass(frozen=True)
class OrbitPredictionPage(OrbitReportHeader):
    job_id: int
    page: int
    generation: int
    total_samples: int
    samples: tuple[OrbitPredictionSample, ...]


@dataclass(frozen=True)
class OrbitPassSummary:
    norad_id: int
    aos_utc_ms: int
    los_utc_ms: int
    maximum_elevation_utc_ms: int
    maximum_elevation_deg: float
    has_pass: bool
    stale: bool
    open_at_start: bool
    open_at_end: bool


@dataclass(frozen=True)
class OrbitPassPage(OrbitReportHeader):
    job_id: int
    page: int
    generation: int
    total_satellites: int
    passes: tuple[OrbitPassSummary, ...]


@dataclass(frozen=True)
class OrbitUploadProgress(OrbitReportHeader):
    acknowledged_size: int


@dataclass(frozen=True)
class ServiceIdentity:
    schema: int
    timestamp: int
    valid_mask: int
    model: str
    serial_number: str
    main_firmware: str
    boot_firmware: str
    protocol_version: int


@dataclass(frozen=True)
class ServiceHardwareIdentity:
    schema: int
    timestamp: int
    valid_mask: int
    uid_words: tuple[int, int, int]
    mac_address: bytes
    mac_source: int

    @property
    def device_uid(self) -> str:
        return "".join(f"{word:08X}" for word in self.uid_words)

    @property
    def mac_text(self) -> str:
        return ":".join(f"{octet:02X}" for octet in self.mac_address)


@dataclass(frozen=True)
class ServiceNavigationSourceInfo:
    schema: int
    timestamp: int
    valid_mask: int
    gnss_source: int
    imu_source: int
    attitude_source: int
    external_ins_source: int
    external_role_mask: int
    capability_flags: int
    imu_mount_rotation: int


@dataclass(frozen=True)
class ServiceExternalInsDiagnostics:
    schema: int
    timestamp: int
    valid_mask: int
    source: int
    role_mask: int
    online: bool
    state: int
    aligned: bool
    raw_ins_status: int
    raw_position_type: int
    gnss_position_type: int
    num_svs: int
    inspvax_count: int
    rawimuxa_count: int
    bestpvt_count: int
    inspvax_hz: float
    rawimuxa_hz: float
    bestpvt_hz: float
    ascii_crc_errors: int
    binary_crc_errors: int
    binary_format_errors: int
    rx_overflow_bytes: int
    yaw_deg: float
    pitch_deg: float
    roll_deg: float
    yaw_std_deg: float
    pitch_std_deg: float
    roll_std_deg: float
    latitude_std_m: float
    longitude_std_m: float
    height_std_m: float
    velocity_north_std_mps: float
    velocity_east_std_mps: float
    velocity_up_std_mps: float
    solution_age_s: float
    differential_age_s: float


@dataclass(frozen=True)
class ServiceFastState:
    schema: int
    timestamp: int
    valid_mask: int
    control_mode: int
    tracking_phase: int
    locked: bool
    navigation_state: int
    gnss_fix: int
    tx_enabled: bool
    roll_deg: float
    pitch_deg: float
    yaw_deg: float
    beam_az_deg: float
    beam_el_deg: float
    snr_db: float


@dataclass(frozen=True)
class ServiceSlowState:
    schema: int
    timestamp: int
    valid_mask: int
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    rx_frequency_mhz: float
    tx_frequency_mhz: float
    rx_polarization: int
    tx_polarization: int
    tx_enabled: bool


@dataclass(frozen=True)
class ServiceLinkDetail:
    schema: int
    timestamp: int
    valid_mask: int
    modem_online: bool
    rx_lo_mhz: float
    tx_lo_mhz: float
    satellite_mode: int
    satellite_longitude_deg: float
    satellite_id: int
    satellite_name: str


@dataclass(frozen=True)
class ServiceRfLockStatus:
    schema: int
    timestamp: int
    valid_mask: int
    lock_mask: int


@dataclass(frozen=True)
class ServiceComponentValue:
    valid_mask: int
    online: bool
    temperature_c: float
    voltage_v: float
    version: int


@dataclass(frozen=True)
class ServiceComponentHealth:
    schema: int
    timestamp: int
    converter: ServiceComponentValue
    tx_array: ServiceComponentValue
    rx_array: ServiceComponentValue


@dataclass(frozen=True)
class ServiceCapabilities:
    schema: int
    timestamp: int
    valid_mask: int
    rx_frequency_min_mhz: float
    rx_frequency_max_mhz: float
    tx_frequency_min_mhz: float
    tx_frequency_max_mhz: float
    polarization_mask: int
    feature_flags: int
    capture_profile_mask: int


@dataclass(frozen=True)
class ServiceControlResponse:
    schema: int
    request_id: int
    operation: int
    result_code: int
    applied_mask: int
    control_mode: int
    rx_frequency_mhz: float
    tx_frequency_mhz: float
    rx_polarization: int
    tx_polarization: int
    tx_enabled: bool


@dataclass(frozen=True)
class GnssSkySatellite:
    """GSV 天空图中的单颗卫星。"""

    prn: int
    elevation_deg: int
    azimuth_deg: int
    snr: int
    valid_flags: int


@dataclass
class GnssSkyReport:
    """0x0D GNSS_SKY_REPORT 完整 talker 快照。"""

    version: int
    timestamp: int
    talker: str
    total_visible: int
    flags: int
    satellites: List[GnssSkySatellite] = field(default_factory=list)


@dataclass(frozen=True)
class GnssCnrObservation:
    """RANGECMPB 中一条精确信号观测。"""

    system: int
    prn: int
    signal_type: int
    cn0_dbhz: int
    tracking_state: int
    lock_flags: int
    glo_freq_channel: int


@dataclass
class GnssCnrReport:
    """0x0E GNSS_CNR_REPORT 的一个分片。"""

    version: int
    timestamp: int
    report_id: int
    chunk_index: int
    chunk_count: int
    total_observations: int
    flags: int
    observations: List[GnssCnrObservation] = field(default_factory=list)


@dataclass(frozen=True)
class GnssSatRecord:
    """MG902 UBX-NAV-SAT 中的一颗卫星。"""

    system: int
    sv_id: int
    cn0_dbhz: int
    elevation_deg: int
    azimuth_deg: int
    raw_sat_flags: int


@dataclass
class GnssSatReport:
    """0x0F GNSS_SAT_REPORT 的一个分片。"""

    version: int
    source: int
    timestamp: int
    report_id: int
    chunk_index: int
    chunk_count: int
    total_records: int
    flags: int
    records: List[GnssSatRecord] = field(default_factory=list)


@dataclass(frozen=True)
class GnssSignalRecord:
    """MG902 UBX-NAV-SIG 中的一条原始信号记录。"""

    system: int
    sv_id: int
    raw_signal_id: int
    freq_id: int
    cn0_dbhz: int
    quality_ind: int
    corr_source: int
    iono_model: int
    pr_res_0p1m: int
    raw_sig_flags: int


@dataclass
class GnssSignalReport:
    """0x10 GNSS_SIGNAL_REPORT 的一个分片。"""

    version: int
    source: int
    timestamp: int
    report_id: int
    chunk_index: int
    chunk_count: int
    total_records: int
    flags: int
    records: List[GnssSignalRecord] = field(default_factory=list)


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
    ProfileSemanticsReport,
    GnssSkyReport,
    GnssCnrReport,
    GnssSatReport,
    GnssSignalReport,
    ServiceIdentity,
    ServiceHardwareIdentity,
    ServiceNavigationSourceInfo,
    ServiceExternalInsDiagnostics,
    ServiceFastState,
    ServiceSlowState,
    ServiceLinkDetail,
    ServiceRfLockStatus,
    ServiceComponentHealth,
    ServiceCapabilities,
    ServiceControlResponse,
    OrbitStatusReport,
    OrbitCapabilitiesReport,
    OrbitCatalogReport,
    OrbitCurrentReport,
    OrbitSkyReport,
    OrbitPredictionAccepted,
    OrbitPredictionPage,
    OrbitPassPage,
    OrbitUploadProgress,
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
    "ServiceControlOp", "ServiceResultCode", "OrbitOperation", "OrbitStatus",
    # dataclasses
    "MetaInfo",
    "ChannelDefEntry", "ChannelDefineTable",
    "StateEnumItem", "StateDefEntry", "StateDefineTable",
    "EventDefEntry", "EventDefineTable",
    "ChannelSample", "DataReport",
    "StateSample", "StateReport",
    "EventReport", "Heartbeat", "CommandResponse",
    "ParaEntry", "ParaTableReport",
    "ProfileSemanticChannelEntry", "ProfileSemanticStateEntry",
    "ProfileSemanticCapabilityEntry", "ProfileSemanticsReport",
    "GnssSkySatellite", "GnssSkyReport",
    "GnssCnrObservation", "GnssCnrReport",
    "GnssSatRecord", "GnssSatReport",
    "GnssSignalRecord", "GnssSignalReport",
    "ServiceIdentity", "ServiceHardwareIdentity",
    "ServiceNavigationSourceInfo", "ServiceExternalInsDiagnostics",
    "ServiceFastState", "ServiceSlowState", "ServiceLinkDetail",
    "ServiceRfLockStatus",
    "ServiceComponentValue", "ServiceComponentHealth",
    "ServiceCapabilities", "ServiceControlResponse",
    "OrbitReportHeader", "OrbitStatusReport", "OrbitCapabilitiesReport",
    "OrbitCatalogEntry", "OrbitCatalogReport",
    "OrbitCurrentSample", "OrbitCurrentReport", "OrbitSkySample", "OrbitSkyReport",
    "OrbitPredictionAccepted", "OrbitPredictionSample", "OrbitPredictionPage",
    "OrbitPassSummary", "OrbitPassPage", "OrbitUploadProgress",
    "RawFrame", "FrameV2Record",
]
