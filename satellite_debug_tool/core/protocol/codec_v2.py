"""
DEBUG 协议 v2 — 帧编解码。

封装函数：
- build_frame(cmd_type, data): 生成完整帧（加帧头、type、cmd、len、CRC、footer）
- build_control_*(...):         H→D 控制帧快捷构造
- decode_*(data):               各 cmd 的 DATA 段解码（不含帧头/CRC/footer）

解码失败时抛出 ``CodecError``。
"""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple

from .crc16 import Crc16
from .frame_v2 import (
    ChannelDefEntry,
    ChannelDefineTable,
    ChannelSample,
    CmdType,
    CommandResponse,
    DEVICE_TYPE,
    DataReport,
    DATA_REPORT_MAX_CHANNELS,
    EVENT_PAYLOAD_MAX,
    EventDefEntry,
    EventDefineTable,
    EventReport,
    FRAME_FOOTER,
    FRAME_HEADER_0,
    FRAME_HEADER_1,
    Heartbeat,
    MAX_DATA_LENGTH,
    MetaInfo,
    PROTOCOL_VERSION,
    StateDefEntry,
    StateDefineTable,
    StateEnumItem,
    StateReport,
    StateSample,
    SubCmd,
)


class CodecError(ValueError):
    """v2 解码错误。"""


# -----------------------------------------------------------------------------
# Frame encoding (all messages share the same envelope)
# -----------------------------------------------------------------------------

def build_frame(cmd_type: int, data: bytes = b"") -> bytes:
    """
    组装一个完整的 v2 帧。

    Envelope: AA 55 0D <cmd> <len_lo> <len_hi> <data...> <crc_lo> <crc_hi> EE
    """
    if len(data) > MAX_DATA_LENGTH:
        raise CodecError(f"data length {len(data)} exceeds max {MAX_DATA_LENGTH}")

    header = bytes([FRAME_HEADER_0, FRAME_HEADER_1, DEVICE_TYPE, cmd_type & 0xFF])
    length = len(data).to_bytes(2, "little")
    payload = header + length + data
    crc = Crc16.calculate(payload)
    crc_bytes = bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    return payload + crc_bytes + bytes([FRAME_FOOTER])


# -----------------------------------------------------------------------------
# CONTROL (H→D) helpers
# -----------------------------------------------------------------------------

def build_control(sub_cmd: int, payload: bytes = b"") -> bytes:
    """构造 CONTROL(0x03) 帧，data = sub_cmd(1B) + payload。"""
    return build_frame(CmdType.CONTROL, bytes([sub_cmd & 0xFF]) + payload)


def build_debug_enable_v2(enabled: bool) -> bytes:
    return build_control(SubCmd.DEBUG_ENABLE, bytes([1 if enabled else 0]))


def build_request_meta_info() -> bytes:
    return build_control(SubCmd.REQUEST_META_INFO)


def build_request_channel_define() -> bytes:
    return build_control(SubCmd.REQUEST_CHANNEL_DEFINE)


def build_request_state_define() -> bytes:
    return build_control(SubCmd.REQUEST_STATE_DEFINE)


def build_request_event_define() -> bytes:
    return build_control(SubCmd.REQUEST_EVENT_DEFINE)


def build_user_mark(mark_id: int, text: str = "") -> bytes:
    """
    USER_MARK 子命令：u16 mark_id + u8 len + utf8 text (≤255)。
    """
    text_bytes = text.encode("utf-8")
    if len(text_bytes) > 255:
        raise CodecError("USER_MARK text exceeds 255 bytes")
    payload = struct.pack("<HB", mark_id & 0xFFFF, len(text_bytes)) + text_bytes
    return build_control(SubCmd.USER_MARK, payload)


def build_set_sample_rate(hz: int) -> bytes:
    if not (0 <= hz <= 0xFFFF):
        raise CodecError("sample rate out of u16 range")
    return build_control(SubCmd.SET_SAMPLE_RATE, struct.pack("<H", hz))


def build_set_trace_mode(mode: int) -> bytes:
    return build_control(SubCmd.SET_TRACE_MODE, bytes([mode & 0xFF]))


def build_channel_enable_mask(mask: int) -> bytes:
    """
    CHANNEL_ENABLE_MASK：u32 bitmask_lo + u32 bitmask_hi（协议预留 64 通道，当前仅用低 16 位）。
    """
    lo = mask & 0xFFFFFFFF
    hi = (mask >> 32) & 0xFFFFFFFF
    return build_control(SubCmd.CHANNEL_ENABLE_MASK, struct.pack("<II", lo, hi))


def build_reset_stats() -> bytes:
    return build_control(SubCmd.RESET_STATS)


# -----------------------------------------------------------------------------
# Small helpers for variable-length utf8 reads
# -----------------------------------------------------------------------------

def _read_u8_prefixed_utf8(data: bytes, offset: int) -> Tuple[str, int]:
    """读取 `len_u8 + utf8_bytes`，返回 (字符串, 下一个偏移)。"""
    if offset >= len(data):
        raise CodecError(f"unexpected EOF at offset {offset}")
    n = data[offset]
    offset += 1
    if offset + n > len(data):
        raise CodecError(f"utf8 chunk ({n}B) overflows at offset {offset}")
    try:
        text = data[offset:offset + n].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodecError(f"invalid utf-8 at offset {offset}: {exc}")
    return text, offset + n


# -----------------------------------------------------------------------------
# Decoders — DATA 段解析（已去除帧头/CRC/footer）
# -----------------------------------------------------------------------------

def decode_meta_info(data: bytes) -> MetaInfo:
    """
    META_INFO(0x04):
        protocol_ver u8, fw_ver(len+utf8), hw_type(len+utf8), device_sn(len+utf8).
    """
    if len(data) < 1:
        raise CodecError("META_INFO too short")
    protocol_ver = data[0]
    off = 1
    fw_ver, off = _read_u8_prefixed_utf8(data, off)
    hw_type, off = _read_u8_prefixed_utf8(data, off)
    device_sn, off = _read_u8_prefixed_utf8(data, off)
    # 允许尾部有多余字节（固件 padding 容错）
    return MetaInfo(protocol_ver=protocol_ver, fw_ver=fw_ver, hw_type=hw_type, device_sn=device_sn)


def decode_channel_define(data: bytes) -> ChannelDefineTable:
    """
    CHANNEL_DEFINE(0x05):
        table_ver u8, count u8,
        entry[N]: channel_id u8, data_type u8, group_id u8, flags u8,
                  name(len+utf8), unit(len+utf8), display_min f32, display_max f32.
    """
    if len(data) < 2:
        raise CodecError("CHANNEL_DEFINE too short")
    table_ver = data[0]
    count = data[1]
    off = 2
    entries: List[ChannelDefEntry] = []
    for _ in range(count):
        if off + 4 > len(data):
            raise CodecError("CHANNEL_DEFINE entry header overflow")
        channel_id, data_type, group_id, flags = data[off:off + 4]
        off += 4
        name, off = _read_u8_prefixed_utf8(data, off)
        unit, off = _read_u8_prefixed_utf8(data, off)
        if off + 8 > len(data):
            raise CodecError("CHANNEL_DEFINE display range overflow")
        display_min, display_max = struct.unpack_from("<ff", data, off)
        off += 8
        entries.append(ChannelDefEntry(
            channel_id=channel_id, data_type=data_type, group_id=group_id,
            flags=flags, name=name, unit=unit,
            display_min=display_min, display_max=display_max,
        ))
    return ChannelDefineTable(table_ver=table_ver, channels=entries)


def decode_state_define(data: bytes) -> StateDefineTable:
    """
    STATE_DEFINE(0x06):
        table_ver u8, state_count u8,
        entry[N]: state_id u8, state_type u8, flags u8, name(len+utf8),
                  enum_count u8,
                  [if ENUM] enum[M]: value u8, level u8, name(len+utf8).
    """
    if len(data) < 2:
        raise CodecError("STATE_DEFINE too short")
    table_ver = data[0]
    count = data[1]
    off = 2
    states: List[StateDefEntry] = []
    for _ in range(count):
        if off + 3 > len(data):
            raise CodecError("STATE_DEFINE entry header overflow")
        state_id, state_type, flags = data[off:off + 3]
        off += 3
        name, off = _read_u8_prefixed_utf8(data, off)
        if off >= len(data):
            raise CodecError("STATE_DEFINE enum_count missing")
        enum_count = data[off]
        off += 1
        enums: List[StateEnumItem] = []
        for _e in range(enum_count):
            if off + 2 > len(data):
                raise CodecError("STATE_DEFINE enum item header overflow")
            value, level = data[off:off + 2]
            off += 2
            enum_name, off = _read_u8_prefixed_utf8(data, off)
            enums.append(StateEnumItem(value=value, level=level, name=enum_name))
        states.append(StateDefEntry(
            state_id=state_id, state_type=state_type, flags=flags,
            name=name, enums=enums,
        ))
    return StateDefineTable(table_ver=table_ver, states=states)


def decode_event_define(data: bytes) -> EventDefineTable:
    """
    EVENT_DEFINE(0x07):
        table_ver u8, event_count u8,
        entry[N]: event_id u16, level u8, name(len+utf8).
    """
    if len(data) < 2:
        raise CodecError("EVENT_DEFINE too short")
    table_ver = data[0]
    count = data[1]
    off = 2
    events: List[EventDefEntry] = []
    for _ in range(count):
        if off + 3 > len(data):
            raise CodecError("EVENT_DEFINE entry header overflow")
        event_id = int.from_bytes(data[off:off + 2], "little")
        level = data[off + 2]
        off += 3
        name, off = _read_u8_prefixed_utf8(data, off)
        events.append(EventDefEntry(event_id=event_id, level=level, name=name))
    return EventDefineTable(table_ver=table_ver, events=events)


def decode_data_report(data: bytes) -> DataReport:
    """
    DATA_REPORT(0x01):
        timestamp u32, channel_count u8, entry[N]: channel_id u8, value f32.
    """
    if len(data) < 5:
        raise CodecError("DATA_REPORT too short")
    timestamp = int.from_bytes(data[0:4], "little")
    count = data[4]
    if count > DATA_REPORT_MAX_CHANNELS:
        raise CodecError(f"DATA_REPORT channel_count {count} exceeds max {DATA_REPORT_MAX_CHANNELS}")
    expected = 5 + count * 5
    if len(data) < expected:
        raise CodecError(f"DATA_REPORT expects {expected}B got {len(data)}B")
    samples: List[ChannelSample] = []
    off = 5
    for _ in range(count):
        channel_id = data[off]
        value = struct.unpack_from("<f", data, off + 1)[0]
        samples.append(ChannelSample(channel_id=channel_id, value=value))
        off += 5
    return DataReport(timestamp=timestamp, samples=samples)


def decode_state_report(data: bytes) -> StateReport:
    """
    STATE_REPORT(0x08):
        timestamp u32, state_count u8, entry[N]: state_id u8, value u8.
    """
    if len(data) < 5:
        raise CodecError("STATE_REPORT too short")
    timestamp = int.from_bytes(data[0:4], "little")
    count = data[4]
    expected = 5 + count * 2
    if len(data) < expected:
        raise CodecError(f"STATE_REPORT expects {expected}B got {len(data)}B")
    states: List[StateSample] = []
    off = 5
    for _ in range(count):
        states.append(StateSample(state_id=data[off], value=data[off + 1]))
        off += 2
    return StateReport(timestamp=timestamp, states=states)


def decode_event_report(data: bytes) -> EventReport:
    """
    EVENT_REPORT(0x09):
        timestamp u32, event_id u16, payload_len u8, payload bytes.
    """
    if len(data) < 7:
        raise CodecError("EVENT_REPORT too short")
    timestamp = int.from_bytes(data[0:4], "little")
    event_id = int.from_bytes(data[4:6], "little")
    payload_len = data[6]
    if payload_len > EVENT_PAYLOAD_MAX:
        raise CodecError(f"EVENT_REPORT payload_len {payload_len} exceeds max {EVENT_PAYLOAD_MAX}")
    if len(data) < 7 + payload_len:
        raise CodecError(f"EVENT_REPORT expects {7 + payload_len}B got {len(data)}B")
    payload = bytes(data[7:7 + payload_len])
    return EventReport(timestamp=timestamp, event_id=event_id, payload=payload)


def decode_heartbeat(data: bytes) -> Heartbeat:
    """
    HEARTBEAT(0x0A):
        uptime_ms u32, cpu_load u8, free_heap u32, rx_frame_rate u16,
        tx_frame_rate u16, reserved u32.
    """
    expected = 4 + 1 + 4 + 2 + 2 + 4  # 17 bytes
    if len(data) < expected:
        raise CodecError(f"HEARTBEAT expects {expected}B got {len(data)}B")
    uptime, cpu, heap, rx_rate, tx_rate, reserved = struct.unpack("<IBIHHI", data[:expected])
    return Heartbeat(
        uptime_ms=uptime, cpu_load=cpu, free_heap=heap,
        rx_frame_rate=rx_rate, tx_frame_rate=tx_rate, reserved=reserved,
    )


def decode_command_response(data: bytes) -> CommandResponse:
    """COMMAND_RESPONSE(0x02): code u8, msg utf8（剩余所有字节）。"""
    if len(data) < 1:
        raise CodecError("COMMAND_RESPONSE too short")
    code = data[0]
    raw = bytes(data[1:])
    # 允许设备在 msg 末尾补 0x00 作为 C 字符串终止符
    raw = raw.rstrip(b"\x00")
    try:
        msg = raw.decode("utf-8")
    except UnicodeDecodeError:
        msg = raw.decode("utf-8", errors="replace")
    return CommandResponse(code=code, msg=msg)


__all__ = [
    "CodecError",
    "build_frame", "build_control",
    "build_debug_enable_v2",
    "build_request_meta_info", "build_request_channel_define",
    "build_request_state_define", "build_request_event_define",
    "build_user_mark", "build_set_sample_rate", "build_set_trace_mode",
    "build_channel_enable_mask", "build_reset_stats",
    "decode_meta_info", "decode_channel_define", "decode_state_define",
    "decode_event_define", "decode_data_report", "decode_state_report",
    "decode_event_report", "decode_heartbeat", "decode_command_response",
]
