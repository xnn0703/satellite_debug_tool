"""DEBUG 协议 v2 公开接口。"""

from .crc16 import Crc16
from .codec_v2 import (
    CodecError,
    build_frame,
    build_control,
    build_debug_enable_v2,
    build_request_meta_info,
    build_request_channel_define,
    build_request_state_define,
    build_request_event_define,
    build_request_profile_semantics,
    build_user_mark,
    build_set_sample_rate,
    build_set_trace_mode,
    build_channel_enable_mask,
    build_reset_stats,
    # M9: 参数管理 + OTA
    build_request_para_table,
    build_para_set,
    build_para_reset,
    build_ota_begin,
    build_ota_data,
    build_ota_end,
    build_ota_abort,
    build_device_reboot,
    decode_para_table_report,
    decode_profile_semantics,
)
from .frame_receiver_v2 import FrameReceiverV2
from .frame_v2 import (
    PROTOCOL_VERSION,
    FRAME_HEADER_0, FRAME_HEADER_1, FRAME_FOOTER, DEVICE_TYPE,
    MAX_DATA_LENGTH, MAX_FRAME_LENGTH,
    CHANNEL_ID_MAX, STATE_ID_MAX,
    EVENT_ID_MIN, EVENT_ID_MAX, EVENT_ID_USER_MARK,
    DATA_REPORT_MAX_CHANNELS, EVENT_PAYLOAD_MAX,
    CHANNEL_FLAG_DEFAULT_VISIBLE, CHANNEL_FLAG_CRITICAL,
    STATE_FLAG_CRITICAL, STATE_FLAG_INVERSE,
    PARA_FLAG_REQUIRES_REBOOT, PARA_FLAG_READ_ONLY,
    CmdType, SubCmd, RespCode, DataType, StateType, Level, ParaType,
    MetaInfo,
    ChannelDefEntry, ChannelDefineTable,
    StateEnumItem, StateDefEntry, StateDefineTable,
    EventDefEntry, EventDefineTable,
    ChannelSample, DataReport,
    StateSample, StateReport,
    EventReport, Heartbeat, CommandResponse,
    ParaEntry, ParaTableReport,
    ProfileSemanticChannelEntry, ProfileSemanticStateEntry,
    ProfileSemanticCapabilityEntry, ProfileSemanticsReport,
    RawFrame, FrameV2Record,
)


__all__ = [
    # crc
    "Crc16",
    # envelope & builders
    "CodecError",
    "build_frame", "build_control",
    "build_debug_enable_v2",
    "build_request_meta_info", "build_request_channel_define",
    "build_request_state_define", "build_request_event_define",
    "build_request_profile_semantics",
    "build_user_mark", "build_set_sample_rate", "build_set_trace_mode",
    "build_channel_enable_mask", "build_reset_stats",
    "build_request_para_table", "build_para_set", "build_para_reset",
    "build_ota_begin", "build_ota_data", "build_ota_end",
    "build_ota_abort", "build_device_reboot",
    "decode_para_table_report", "decode_profile_semantics",
    # receiver
    "FrameReceiverV2",
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
    "ProfileSemanticChannelEntry", "ProfileSemanticStateEntry",
    "ProfileSemanticCapabilityEntry", "ProfileSemanticsReport",
    "RawFrame", "FrameV2Record",
]
