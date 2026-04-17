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
    build_user_mark,
    build_set_sample_rate,
    build_set_trace_mode,
    build_channel_enable_mask,
    build_reset_stats,
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
    CmdType, SubCmd, RespCode, DataType, StateType, Level,
    MetaInfo,
    ChannelDefEntry, ChannelDefineTable,
    StateEnumItem, StateDefEntry, StateDefineTable,
    EventDefEntry, EventDefineTable,
    ChannelSample, DataReport,
    StateSample, StateReport,
    EventReport, Heartbeat, CommandResponse,
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
    "build_user_mark", "build_set_sample_rate", "build_set_trace_mode",
    "build_channel_enable_mask", "build_reset_stats",
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
    # enums
    "CmdType", "SubCmd", "RespCode", "DataType", "StateType", "Level",
    # dataclasses
    "MetaInfo",
    "ChannelDefEntry", "ChannelDefineTable",
    "StateEnumItem", "StateDefEntry", "StateDefineTable",
    "EventDefEntry", "EventDefineTable",
    "ChannelSample", "DataReport",
    "StateSample", "StateReport",
    "EventReport", "Heartbeat", "CommandResponse",
    "RawFrame", "FrameV2Record",
]
