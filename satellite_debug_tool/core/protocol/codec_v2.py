"""
DEBUG 协议 v2 — 帧编解码。

封装函数：
- build_frame(cmd_type, data): 生成完整帧（加帧头、type、cmd、len、CRC、footer）
- build_control_*(...):         H→D 控制帧快捷构造
- decode_*(data):               各 cmd 的 DATA 段解码（不含帧头/CRC/footer）

解码失败时抛出 ``CodecError``。
"""

from __future__ import annotations

import math
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
    ParaEntry,
    ParaTableReport,
    ProfileSemanticCapabilityEntry,
    ProfileSemanticChannelEntry,
    ProfileSemanticStateEntry,
    ProfileSemanticsReport,
    GnssSkySatellite,
    GnssSkyReport,
    GnssCnrObservation,
    GnssCnrReport,
    GnssSatRecord,
    GnssSatReport,
    GnssSignalRecord,
    GnssSignalReport,
    ServiceCapabilities,
    ServiceComponentHealth,
    ServiceComponentValue,
    ServiceControlOp,
    ServiceControlResponse,
    ServiceFastState,
    ServiceIdentity,
    ServiceSlowState,
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


def build_request_profile_semantics() -> bytes:
    return build_control(SubCmd.REQUEST_PROFILE_SEMANTICS)


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


# M9: 参数管理 + OTA 构建函数 ------------------------------------------------

def build_request_para_table() -> bytes:
    return build_control(SubCmd.REQUEST_PARA_TABLE)


def build_para_set(name: str, value: str) -> bytes:
    """PARA_SET: name_len(u8) + name(utf8) + value_len(u8) + value(utf8)。"""
    nb = name.encode("utf-8")
    vb = value.encode("utf-8")
    if len(nb) > 255 or len(vb) > 255:
        raise CodecError("para name/value too long")
    return build_control(SubCmd.PARA_SET, bytes([len(nb)]) + nb + bytes([len(vb)]) + vb)


def build_para_reset() -> bytes:
    return build_control(SubCmd.PARA_RESET)


def build_ota_begin(file_size: int, filename: str) -> bytes:
    """OTA_BEGIN: file_size(u32) + name_len(u8) + filename(utf8)。"""
    fb = filename.encode("utf-8")
    if len(fb) > 255:
        raise CodecError("OTA filename too long")
    return build_control(
        SubCmd.OTA_BEGIN,
        struct.pack("<I", file_size) + bytes([len(fb)]) + fb,
    )


def build_ota_data(seq: int, chunk: bytes) -> bytes:
    """OTA_DATA: seq(u16) + data(≤1021B)。MAX_DATA=1024 减去 sub_cmd(1)+seq(2)=3。"""
    if len(chunk) > 1021:
        raise CodecError("OTA chunk exceeds 1021 bytes")
    return build_control(SubCmd.OTA_DATA, struct.pack("<H", seq) + chunk)


def build_ota_end(crc32: int) -> bytes:
    """OTA_END: crc32(u32)。"""
    return build_control(SubCmd.OTA_END, struct.pack("<I", crc32 & 0xFFFFFFFF))


def build_ota_abort() -> bytes:
    return build_control(SubCmd.OTA_ABORT)


def build_device_reboot() -> bytes:
    return build_control(SubCmd.DEVICE_REBOOT)


# M18: AFD01 product-service controls ----------------------------------------

SERVICE_SCHEMA_VERSION = 1


def _require_service_schema(schema: int, record_name: str) -> None:
    if schema != SERVICE_SCHEMA_VERSION:
        raise CodecError(
            f"{record_name} unsupported schema {schema}; expected {SERVICE_SCHEMA_VERSION}"
        )


def _build_service_control(request_id: int, operation: int, payload: bytes = b"") -> bytes:
    if not (0 <= request_id <= 0xFFFFFFFF):
        raise CodecError("service request id out of u32 range")
    data = struct.pack(
        "<BIB", SERVICE_SCHEMA_VERSION, request_id, int(operation) & 0xFF
    ) + payload
    return build_frame(CmdType.SERVICE_CONTROL_REQUEST, data)


def build_service_subscribe(request_id: int, fast_rate_hz: int = 10) -> bytes:
    if not (1 <= fast_rate_hz <= 20):
        raise CodecError("service fast rate must be 1..20 Hz")
    return _build_service_control(
        request_id, ServiceControlOp.SUBSCRIBE, bytes([fast_rate_hz])
    )


def build_service_set_control_mode(request_id: int, mode: int) -> bytes:
    if int(mode) not in (0, 1):
        raise CodecError("service control mode must be 0 (auto) or 1 (manual)")
    return _build_service_control(
        request_id, ServiceControlOp.SET_CONTROL_MODE, bytes([mode & 0xFF])
    )


def build_service_apply_rf(
    request_id: int,
    rx_frequency_mhz: float,
    tx_frequency_mhz: float,
    rx_polarization: int,
    tx_polarization: int,
) -> bytes:
    if not math.isfinite(rx_frequency_mhz) or not math.isfinite(tx_frequency_mhz):
        raise CodecError("service RF frequencies must be finite")
    if int(rx_polarization) not in range(4) or int(tx_polarization) not in range(4):
        raise CodecError("service polarization must be in range 0..3")
    return _build_service_control(
        request_id,
        ServiceControlOp.APPLY_RF,
        struct.pack(
            "<ffBB",
            float(rx_frequency_mhz),
            float(tx_frequency_mhz),
            rx_polarization & 0xFF,
            tx_polarization & 0xFF,
        ),
    )


def build_service_set_tx_enable(request_id: int, enabled: bool) -> bytes:
    return _build_service_control(
        request_id, ServiceControlOp.SET_TX_ENABLE, bytes([1 if enabled else 0])
    )


def build_service_set_capture_profile(request_id: int, support_full: bool) -> bytes:
    return _build_service_control(
        request_id,
        ServiceControlOp.SET_CAPTURE_PROFILE,
        bytes([1 if support_full else 0]),
    )


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


def decode_service_identity(data: bytes) -> ServiceIdentity:
    if len(data) < 10:
        raise CodecError("SERVICE_IDENTITY too short")
    schema, timestamp, valid_mask = struct.unpack_from("<BII", data, 0)
    _require_service_schema(schema, "SERVICE_IDENTITY")
    offset = 9
    model, offset = _read_u8_prefixed_utf8(data, offset)
    serial_number, offset = _read_u8_prefixed_utf8(data, offset)
    main_firmware, offset = _read_u8_prefixed_utf8(data, offset)
    boot_firmware, offset = _read_u8_prefixed_utf8(data, offset)
    if offset + 1 != len(data):
        raise CodecError("SERVICE_IDENTITY invalid length")
    return ServiceIdentity(
        schema,
        timestamp,
        valid_mask,
        model,
        serial_number,
        main_firmware,
        boot_firmware,
        data[offset],
    )


def decode_service_fast_state(data: bytes) -> ServiceFastState:
    fmt = "<BII6B6f"
    if len(data) != struct.calcsize(fmt):
        raise CodecError("SERVICE_FAST_STATE invalid length")
    values = struct.unpack(fmt, data)
    _require_service_schema(values[0], "SERVICE_FAST_STATE")
    return ServiceFastState(
        schema=values[0],
        timestamp=values[1],
        valid_mask=values[2],
        control_mode=values[3],
        tracking_phase=values[4],
        locked=bool(values[5]),
        navigation_state=values[6],
        gnss_fix=values[7],
        tx_enabled=bool(values[8]),
        roll_deg=values[9],
        pitch_deg=values[10],
        yaw_deg=values[11],
        beam_az_deg=values[12],
        beam_el_deg=values[13],
        snr_db=values[14],
    )


def decode_service_slow_state(data: bytes) -> ServiceSlowState:
    fmt = "<BII5f3B"
    if len(data) != struct.calcsize(fmt):
        raise CodecError("SERVICE_SLOW_STATE invalid length")
    values = struct.unpack(fmt, data)
    _require_service_schema(values[0], "SERVICE_SLOW_STATE")
    return ServiceSlowState(
        schema=values[0],
        timestamp=values[1],
        valid_mask=values[2],
        latitude_deg=values[3],
        longitude_deg=values[4],
        altitude_m=values[5],
        rx_frequency_mhz=values[6],
        tx_frequency_mhz=values[7],
        rx_polarization=values[8],
        tx_polarization=values[9],
        tx_enabled=bool(values[10]),
    )


def decode_service_component_health(data: bytes) -> ServiceComponentHealth:
    header_fmt = "<BI"
    item_fmt = "<BBffI"
    expected = struct.calcsize(header_fmt) + 3 * struct.calcsize(item_fmt)
    if len(data) != expected:
        raise CodecError("SERVICE_COMPONENT_HEALTH invalid length")
    schema, timestamp = struct.unpack_from(header_fmt, data, 0)
    _require_service_schema(schema, "SERVICE_COMPONENT_HEALTH")
    offset = struct.calcsize(header_fmt)
    components = []
    for _ in range(3):
        valid_mask, online, temperature, voltage, version = struct.unpack_from(
            item_fmt, data, offset
        )
        components.append(
            ServiceComponentValue(
                valid_mask, bool(online), temperature, voltage, version
            )
        )
        offset += struct.calcsize(item_fmt)
    return ServiceComponentHealth(schema, timestamp, *components)


def decode_service_capabilities(data: bytes) -> ServiceCapabilities:
    fmt = "<BII4fBBB"
    if len(data) != struct.calcsize(fmt):
        raise CodecError("SERVICE_CAPABILITIES invalid length")
    values = struct.unpack(fmt, data)
    _require_service_schema(values[0], "SERVICE_CAPABILITIES")
    return ServiceCapabilities(*values)


def decode_service_control_response(data: bytes) -> ServiceControlResponse:
    fmt = "<BIBBIBffBBB"
    if len(data) != struct.calcsize(fmt):
        raise CodecError("SERVICE_CONTROL_RESPONSE invalid length")
    values = struct.unpack(fmt, data)
    _require_service_schema(values[0], "SERVICE_CONTROL_RESPONSE")
    return ServiceControlResponse(
        schema=values[0],
        request_id=values[1],
        operation=values[2],
        result_code=values[3],
        applied_mask=values[4],
        control_mode=values[5],
        rx_frequency_mhz=values[6],
        tx_frequency_mhz=values[7],
        rx_polarization=values[8],
        tx_polarization=values[9],
        tx_enabled=bool(values[10]),
    )


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


def decode_gnss_sky_report(data: bytes) -> GnssSkyReport:
    """解码 0x0D GSV 完整快照。"""
    if len(data) < 10:
        raise CodecError("GNSS_SKY_REPORT too short")
    version = data[0]
    if version != 1:
        raise CodecError(f"GNSS_SKY_REPORT unsupported version {version}")
    timestamp = int.from_bytes(data[1:5], "little")
    try:
        talker = data[5:7].decode("ascii")
    except UnicodeDecodeError as exc:
        raise CodecError("GNSS_SKY_REPORT invalid talker") from exc
    total_visible = data[7]
    count = data[8]
    flags = data[9]
    if count > 36:
        raise CodecError(f"GNSS_SKY_REPORT satellite_count {count} exceeds 36")
    expected = 10 + count * 7
    if len(data) != expected:
        raise CodecError(f"GNSS_SKY_REPORT expects {expected}B got {len(data)}B")
    satellites: List[GnssSkySatellite] = []
    off = 10
    for _ in range(count):
        prn, elevation, azimuth, snr, valid_flags = struct.unpack_from("<HbHBB", data, off)
        satellites.append(GnssSkySatellite(
            prn=prn,
            elevation_deg=elevation,
            azimuth_deg=azimuth,
            snr=snr,
            valid_flags=valid_flags,
        ))
        off += 7
    return GnssSkyReport(
        version=version,
        timestamp=timestamp,
        talker=talker,
        total_visible=total_visible,
        flags=flags,
        satellites=satellites,
    )


def decode_gnss_cnr_report(data: bytes) -> GnssCnrReport:
    """解码 0x0E RANGECMPB C/N₀ 分片。"""
    if len(data) < 13:
        raise CodecError("GNSS_CNR_REPORT too short")
    version, timestamp, report_id, chunk_index, chunk_count, total, count, flags = struct.unpack_from(
        "<BIHBBHBB", data, 0,
    )
    if version != 1:
        raise CodecError(f"GNSS_CNR_REPORT unsupported version {version}")
    if chunk_count == 0 or chunk_count > 2 or chunk_index >= chunk_count:
        raise CodecError("GNSS_CNR_REPORT invalid chunk metadata")
    if total > 256 or count > 128:
        raise CodecError("GNSS_CNR_REPORT observation limit exceeded")
    expected = 13 + count * 7
    if len(data) != expected:
        raise CodecError(f"GNSS_CNR_REPORT expects {expected}B got {len(data)}B")
    observations: List[GnssCnrObservation] = []
    off = 13
    for _ in range(count):
        system, prn, signal_type, cn0, tracking, lock_flags, glo_channel = struct.unpack_from(
            "<BBBBBBB", data, off,
        )
        observations.append(GnssCnrObservation(
            system=system,
            prn=prn,
            signal_type=signal_type,
            cn0_dbhz=cn0,
            tracking_state=tracking,
            lock_flags=lock_flags,
            glo_freq_channel=glo_channel,
        ))
        off += 7
    return GnssCnrReport(
        version=version,
        timestamp=timestamp,
        report_id=report_id,
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        total_observations=total,
        flags=flags,
        observations=observations,
    )


def _decode_gnss_fragment_header(data: bytes, name: str) -> Tuple[int, int, int, int, int, int, int, int, int]:
    """解码 0x0F/0x10 共用的 14 字节分片头。"""
    if len(data) < 14:
        raise CodecError(f"{name} too short")
    version, source, timestamp, report_id, chunk_index, chunk_count, total, count, flags = struct.unpack_from(
        "<BBIHBBHBB", data, 0,
    )
    if version != 1:
        raise CodecError(f"{name} unsupported version {version}")
    if source > 4:
        raise CodecError(f"{name} unsupported source {source}")
    if total > 92:
        raise CodecError(f"{name} record limit exceeded")

    # 固件固定按 64 条切片：0..64 条只能是一片，65..92 条必须是两片；
    # 除最后一片外均恰好 64 条。只检查“上限”会让缺片、空尾片或重叠片
    # 进入 Store，无法再可靠判定报告边界。
    expected_chunk_count = max(1, (total + 63) // 64)
    if chunk_count != expected_chunk_count or chunk_index >= chunk_count:
        raise CodecError(f"{name} non-canonical chunk metadata")
    expected_count = min(64, total - chunk_index * 64)
    if count != expected_count:
        raise CodecError(
            f"{name} non-canonical record_count {count}, expected {expected_count}"
        )
    return version, source, timestamp, report_id, chunk_index, chunk_count, total, count, flags


def decode_gnss_sat_report(data: bytes) -> GnssSatReport:
    """解码 0x0F MG902 NAV-SAT 分片。"""
    header = _decode_gnss_fragment_header(data, "GNSS_SAT_REPORT")
    version, source, timestamp, report_id, chunk_index, chunk_count, total, count, flags = header
    expected = 14 + count * 10
    if len(data) != expected:
        raise CodecError(f"GNSS_SAT_REPORT expects {expected}B got {len(data)}B")
    records: List[GnssSatRecord] = []
    offset = 14
    for _ in range(count):
        system, sv_id, cn0, elevation, azimuth, raw_flags = struct.unpack_from("<BBBbHI", data, offset)
        records.append(GnssSatRecord(system, sv_id, cn0, elevation, azimuth, raw_flags))
        offset += 10
    return GnssSatReport(
        version, source, timestamp, report_id, chunk_index, chunk_count, total, flags, records,
    )


def decode_gnss_signal_report(data: bytes) -> GnssSignalReport:
    """解码 0x10 MG902 NAV-SIG 分片。"""
    header = _decode_gnss_fragment_header(data, "GNSS_SIGNAL_REPORT")
    version, source, timestamp, report_id, chunk_index, chunk_count, total, count, flags = header
    expected = 14 + count * 12
    if len(data) != expected:
        raise CodecError(f"GNSS_SIGNAL_REPORT expects {expected}B got {len(data)}B")
    records: List[GnssSignalRecord] = []
    offset = 14
    for _ in range(count):
        values = struct.unpack_from("<BBBbBBBBhH", data, offset)
        records.append(GnssSignalRecord(*values))
        offset += 12
    return GnssSignalReport(
        version, source, timestamp, report_id, chunk_index, chunk_count, total, flags, records,
    )


def decode_para_table_report(data: bytes) -> ParaTableReport:
    """PARA_TABLE_REPORT(0x0B): table_ver(u8) + count(u8) + N * 参数条目。"""
    if len(data) < 2:
        raise CodecError("PARA_TABLE_REPORT too short")
    table_ver = data[0]
    count = data[1]
    off = 2
    params: List[ParaEntry] = []
    for _ in range(count):
        name, off = _read_u8_prefixed_utf8(data, off)
        if off + 2 > len(data):
            raise CodecError("PARA_TABLE_REPORT entry truncated")
        para_type = data[off]
        flags = data[off + 1]
        off += 2
        value, off = _read_u8_prefixed_utf8(data, off)
        params.append(ParaEntry(name=name, para_type=para_type, flags=flags, value=value))
    return ParaTableReport(table_ver=table_ver, params=params)


def decode_profile_semantics(data: bytes) -> ProfileSemanticsReport:
    """PROFILE_SEMANTICS(0x0C): semantic roles/capabilities extension."""
    if len(data) < 2:
        raise CodecError("PROFILE_SEMANTICS too short")
    table_ver = data[0]
    channel_count = data[1]
    off = 2
    channels: List[ProfileSemanticChannelEntry] = []
    for _ in range(channel_count):
        if off + 2 > len(data):
            raise CodecError("PROFILE_SEMANTICS channel header overflow")
        channel_id = data[off]
        role_count = data[off + 1]
        off += 2
        roles: List[str] = []
        for _r in range(role_count):
            role, off = _read_u8_prefixed_utf8(data, off)
            roles.append(role)
        channels.append(ProfileSemanticChannelEntry(channel_id=channel_id, roles=roles))

    if off >= len(data):
        raise CodecError("PROFILE_SEMANTICS state_count missing")
    state_count = data[off]
    off += 1
    states: List[ProfileSemanticStateEntry] = []
    for _ in range(state_count):
        if off >= len(data):
            raise CodecError("PROFILE_SEMANTICS state_id missing")
        state_id = data[off]
        off += 1
        role, off = _read_u8_prefixed_utf8(data, off)
        if off + 2 > len(data):
            raise CodecError("PROFILE_SEMANTICS state control overflow")
        control_subcmd = data[off]
        control_value_from = data[off + 1]
        off += 2
        states.append(ProfileSemanticStateEntry(
            state_id=state_id,
            role=role,
            control_subcmd=control_subcmd,
            control_value_from=control_value_from,
        ))

    if off >= len(data):
        raise CodecError("PROFILE_SEMANTICS capability_count missing")
    capability_count = data[off]
    off += 1
    capabilities: List[ProfileSemanticCapabilityEntry] = []
    for _ in range(capability_count):
        name, off = _read_u8_prefixed_utf8(data, off)
        if off >= len(data):
            raise CodecError("PROFILE_SEMANTICS capability value missing")
        supported = data[off] != 0
        off += 1
        capabilities.append(ProfileSemanticCapabilityEntry(name=name, supported=supported))
    return ProfileSemanticsReport(
        table_ver=table_ver,
        channels=channels,
        states=states,
        capabilities=capabilities,
    )


__all__ = [
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
    "build_service_subscribe", "build_service_set_control_mode",
    "build_service_apply_rf", "build_service_set_tx_enable",
    "build_service_set_capture_profile",
    "decode_meta_info", "decode_channel_define", "decode_state_define",
    "decode_event_define", "decode_data_report", "decode_state_report",
    "decode_event_report", "decode_heartbeat", "decode_command_response",
    "decode_para_table_report", "decode_profile_semantics",
    "decode_gnss_sky_report", "decode_gnss_cnr_report",
    "decode_gnss_sat_report", "decode_gnss_signal_report",
    "decode_service_identity", "decode_service_fast_state",
    "decode_service_slow_state", "decode_service_component_health",
    "decode_service_capabilities", "decode_service_control_response",
]
