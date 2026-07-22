"""
DEBUG 协议 v2 — 帧接收状态机。

与 v1 FrameReceiver 相互独立：v2 envelope 与 v1 完全一致（帧头/CRC/footer），
不同点是命令集（0x01–0x0A）与 DATA 段语义。本类根据 cmd_type 分发到
`codec_v2.decode_*`，对未识别的 cmd 返回 ``RawFrame``（不报错，保持链路前向兼容）。

使用方式::

    receiver = FrameReceiverV2()
    records = receiver.feed(bytes_from_worker)
    for rec in records:
        if isinstance(rec, DataReport): ...
        elif isinstance(rec, MetaInfo): ...
        ...
"""

from __future__ import annotations

from enum import IntEnum
from typing import List

from satellite_debug_tool.core.link_trace import FrameTraceLogger, describe_frame

from .codec_v2 import (
    CodecError,
    decode_channel_define,
    decode_command_response,
    decode_data_report,
    decode_event_define,
    decode_event_report,
    decode_heartbeat,
    decode_meta_info,
    decode_para_table_report,
    decode_profile_semantics,
    decode_gnss_sky_report,
    decode_gnss_cnr_report,
    decode_state_define,
    decode_state_report,
)
from .crc16 import Crc16
from .frame_v2 import (
    CmdType,
    DEVICE_TYPE,
    FRAME_FOOTER,
    FRAME_HEADER_0,
    FRAME_HEADER_1,
    FrameV2Record,
    MAX_DATA_LENGTH,
    RawFrame,
)


class _State(IntEnum):
    WAIT_HEADER_1 = 0
    WAIT_HEADER_2 = 1
    READ_TYPE = 2
    READ_CMD = 3
    READ_LEN_LO = 4
    READ_LEN_HI = 5
    READ_DATA = 6
    READ_CRC_LO = 7
    READ_CRC_HI = 8
    READ_FOOTER = 9


# cmd → decoder 分发表。未列出的 cmd 回退到 RawFrame。
_DECODERS = {
    int(CmdType.DATA_REPORT): decode_data_report,
    int(CmdType.COMMAND_RESPONSE): decode_command_response,
    int(CmdType.META_INFO): decode_meta_info,
    int(CmdType.CHANNEL_DEFINE): decode_channel_define,
    int(CmdType.STATE_DEFINE): decode_state_define,
    int(CmdType.EVENT_DEFINE): decode_event_define,
    int(CmdType.STATE_REPORT): decode_state_report,
    int(CmdType.EVENT_REPORT): decode_event_report,
    int(CmdType.HEARTBEAT): decode_heartbeat,
    int(CmdType.PARA_TABLE_REPORT): decode_para_table_report,
    int(CmdType.PROFILE_SEMANTICS): decode_profile_semantics,
    int(CmdType.GNSS_SKY_REPORT): decode_gnss_sky_report,
    int(CmdType.GNSS_CNR_REPORT): decode_gnss_cnr_report,
}


class FrameReceiverV2:
    """v2 字节流帧解析器。"""

    def __init__(self) -> None:
        self._state = _State.WAIT_HEADER_1
        self._cmd_type = 0
        self._data_len = 0
        self._data_buf = bytearray()
        self._crc_bytes = bytearray()

        # 统计
        self._framing_errors = 0   # header/footer/device_type 错
        self._crc_errors = 0
        self._decode_errors = 0    # DATA 段解码异常
        self._frames_ok = 0
        self._trace = FrameTraceLogger("DBG_FRAME")

    # ----- 公共接口 -----

    def reset(self) -> None:
        self._state = _State.WAIT_HEADER_1
        self._cmd_type = 0
        self._data_len = 0
        self._data_buf.clear()
        self._crc_bytes.clear()

    def feed(self, data: bytes) -> List[FrameV2Record]:
        """喂入字节流，返回本次解析出的全部 record。"""
        out: List[FrameV2Record] = []
        for byte in data:
            rec = self._feed_byte(byte)
            if rec is not None:
                out.append(rec)
        return out

    # ----- 统计（只读属性） -----

    @property
    def framing_errors(self) -> int:
        return self._framing_errors

    @property
    def crc_errors(self) -> int:
        return self._crc_errors

    @property
    def decode_errors(self) -> int:
        return self._decode_errors

    @property
    def frames_ok(self) -> int:
        return self._frames_ok

    @property
    def error_count(self) -> int:
        """保持与 v1 兼容的聚合错误计数。"""
        return self._framing_errors + self._crc_errors + self._decode_errors

    # ----- 内部：逐字节消费 -----

    def _feed_byte(self, byte: int):
        st = self._state

        if st == _State.WAIT_HEADER_1:
            if byte == FRAME_HEADER_0:
                self._state = _State.WAIT_HEADER_2
            # 非帧头字节丢弃，不计错误（保持稳定）
            return None

        if st == _State.WAIT_HEADER_2:
            if byte == FRAME_HEADER_1:
                self._state = _State.READ_TYPE
            else:
                # 'AA xx' 非 55 → 放弃本次 header
                self._framing_errors += 1
                self._trace.message(f"DECODER_INVALID reason=header2 byte=0x{byte:02X}")
                self._state = _State.WAIT_HEADER_1
            return None

        if st == _State.READ_TYPE:
            if byte != DEVICE_TYPE:
                self._framing_errors += 1
                self._trace.message(f"DECODER_INVALID reason=device_type byte=0x{byte:02X}")
                self.reset()
                return None
            self._state = _State.READ_CMD
            return None

        if st == _State.READ_CMD:
            self._cmd_type = byte
            self._state = _State.READ_LEN_LO
            return None

        if st == _State.READ_LEN_LO:
            self._data_len = byte
            self._state = _State.READ_LEN_HI
            return None

        if st == _State.READ_LEN_HI:
            self._data_len |= byte << 8
            if self._data_len > MAX_DATA_LENGTH:
                self._framing_errors += 1
                self._trace.message(
                    f"DECODER_INVALID reason=data_len value={self._data_len} "
                    f"max={MAX_DATA_LENGTH}"
                )
                self.reset()
                return None
            self._data_buf.clear()
            if self._data_len == 0:
                self._state = _State.READ_CRC_LO
            else:
                self._state = _State.READ_DATA
            return None

        if st == _State.READ_DATA:
            self._data_buf.append(byte)
            if len(self._data_buf) >= self._data_len:
                self._state = _State.READ_CRC_LO
            return None

        if st == _State.READ_CRC_LO:
            self._crc_bytes.clear()
            self._crc_bytes.append(byte)
            self._state = _State.READ_CRC_HI
            return None

        if st == _State.READ_CRC_HI:
            self._crc_bytes.append(byte)
            self._state = _State.READ_FOOTER
            return None

        if st == _State.READ_FOOTER:
            if byte != FRAME_FOOTER:
                self._framing_errors += 1
                self._trace.message(f"DECODER_INVALID reason=footer byte=0x{byte:02X}")
                self.reset()
                return None
            return self._finalize()

        # 不应到达
        self._framing_errors += 1
        self.reset()
        return None

    def _finalize(self):
        # 先校验 CRC
        envelope = bytes([FRAME_HEADER_0, FRAME_HEADER_1, DEVICE_TYPE, self._cmd_type]) \
            + self._data_len.to_bytes(2, "little") + bytes(self._data_buf)
        raw_frame = envelope + bytes(self._crc_bytes) + bytes([FRAME_FOOTER])
        expected = Crc16.from_le(bytes(self._crc_bytes))
        if not Crc16.verify(envelope, expected):
            self._crc_errors += 1
            self._trace.message(
                f"DECODER_INVALID {describe_frame(raw_frame)} reason=crc"
            )
            self.reset()
            return None

        # 按 cmd 分发
        decoder = _DECODERS.get(self._cmd_type)
        record = None
        if decoder is None:
            record = RawFrame(cmd_type=self._cmd_type, data=bytes(self._data_buf))
            self._frames_ok += 1
        else:
            try:
                record = decoder(bytes(self._data_buf))
                self._frames_ok += 1
            except CodecError:
                self._decode_errors += 1
                self._trace.message(
                    f"DECODER_INVALID {describe_frame(raw_frame)} reason=payload"
                )
                record = None

        if record is not None:
            self._trace.frame(
                "DECODER_OUT", raw_frame, f"record={type(record).__name__}",
            )

        self.reset()
        return record


__all__ = ["FrameReceiverV2"]
