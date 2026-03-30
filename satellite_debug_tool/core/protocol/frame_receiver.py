"""
Frame receiver with state machine for debug protocol.

Protocol format:
    Header (2) + DeviceType (1) + CmdType (1) + DataLen (2) + Data (N) + CRC (2) + Footer (1)
    0xAA 0x55      0x0D          0x01/02/03    LE         N        LE       0xEE
"""

import struct
from enum import IntEnum
from typing import List, Optional

from .crc16 import Crc16
from .data_frame import DataFrame, ChannelData


class FrameReceiver:
    """State machine based frame receiver for debug protocol."""

    class State(IntEnum):
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

    FRAME_HEADER = b"\xaa\x55"
    DEVICE_TYPE = 0x0D
    FOOTER = 0xEE

    def __init__(self):
        self.reset()
        self._frames: List[DataFrame] = []
        self._error_count = 0

    def reset(self) -> None:
        self._state = self.State.WAIT_HEADER_1
        self._device_type: Optional[int] = None
        self._cmd_type: Optional[int] = None
        self._data_len: int = 0
        self._data: bytearray = bytearray()
        self._crc: int = 0
        self._raw_frame: bytearray = bytearray()

    def feed(self, data: bytes) -> List[DataFrame]:
        """
        Feed raw bytes to receiver.

        Args:
            data: Raw byte data from serial/UDP

        Returns:
            List of successfully parsed DataFrame objects
        """
        self._frames.clear()

        for byte in data:
            if not self._process_byte(byte):
                if self._state == self.State.WAIT_HEADER_1:
                    self._raw_frame.clear()

        return list(self._frames)

    def _process_byte(self, byte: int) -> bool:
        """
        Process single byte through state machine.

        Returns:
            True if still in valid frame, False if error/frame complete
        """
        state_handlers = {
            self.State.WAIT_HEADER_1: self._handle_wait_header_1,
            self.State.WAIT_HEADER_2: self._handle_wait_header_2,
            self.State.READ_TYPE: self._handle_read_type,
            self.State.READ_CMD: self._handle_read_cmd,
            self.State.READ_LEN_LO: self._handle_read_len_lo,
            self.State.READ_LEN_HI: self._handle_read_len_hi,
            self.State.READ_DATA: self._handle_read_data,
            self.State.READ_CRC_LO: self._handle_read_crc_lo,
            self.State.READ_CRC_HI: self._handle_read_crc_hi,
            self.State.READ_FOOTER: self._handle_read_footer,
        }

        return state_handlers[self._state](byte)

    def _handle_wait_header_1(self, byte: int) -> bool:
        self._raw_frame.clear()
        if byte == 0xAA:
            self._raw_frame.append(byte)
            self._state = self.State.WAIT_HEADER_2
        return True

    def _handle_wait_header_2(self, byte: int) -> bool:
        self._raw_frame.append(byte)
        if byte == 0x55:
            self._state = self.State.READ_TYPE
        else:
            self._state = self.State.WAIT_HEADER_1
            self._raw_frame.clear()
        return True

    def _handle_read_type(self, byte: int) -> bool:
        """Read device type, must be 0x0D."""
        self._raw_frame.append(byte)
        if byte != self.DEVICE_TYPE:
            self._error_count += 1
            self._state = self.State.WAIT_HEADER_1
            return False
        self._device_type = byte
        self._state = self.State.READ_CMD
        return True

    def _handle_read_cmd(self, byte: int) -> bool:
        """Read command type."""
        self._raw_frame.append(byte)
        self._cmd_type = byte
        self._state = self.State.READ_LEN_LO
        return True

    def _handle_read_len_lo(self, byte: int) -> bool:
        """Read data length low byte."""
        self._raw_frame.append(byte)
        self._data_len = byte
        self._state = self.State.READ_LEN_HI
        return True

    def _handle_read_len_hi(self, byte: int) -> bool:
        """Read data length high byte."""
        self._raw_frame.append(byte)
        self._data_len |= byte << 8
        self._state = self.State.READ_DATA
        self._data.clear()
        if self._data_len == 0:
            self._state = self.State.READ_CRC_LO
        return True

    def _handle_read_data(self, byte: int) -> bool:
        """Read data payload."""
        self._raw_frame.append(byte)
        self._data.append(byte)
        if len(self._data) >= self._data_len:
            self._state = self.State.READ_CRC_LO
        return True

    def _handle_read_crc_lo(self, byte: int) -> bool:
        """Read CRC low byte."""
        self._raw_frame.append(byte)
        self._crc = byte
        self._state = self.State.READ_CRC_HI
        return True

    def _handle_read_crc_hi(self, byte: int) -> bool:
        """Read CRC high byte."""
        self._raw_frame.append(byte)
        self._crc |= byte << 8
        self._state = self.State.READ_FOOTER
        return True

    def _handle_read_footer(self, byte: int) -> bool:
        self._raw_frame.append(byte)

        crc_data = bytes(self._raw_frame[:-3])
        expected_crc = Crc16.calculate(crc_data)

        if byte == self.FOOTER and self._crc == expected_crc:
            self._parse_data_frame()
            self._state = self.State.WAIT_HEADER_1
            self._raw_frame.clear()
            return False
        else:
            self._error_count += 1
            self._state = self.State.WAIT_HEADER_1
            return False

    def _parse_data_frame(self) -> None:
        """Parse data section into DataFrame object."""
        if self._cmd_type == 0x01:
            df = self._parse_data_report()
            if df:
                self._frames.append(df)

    def _parse_data_report(self) -> Optional[DataFrame]:
        """Parse CMD_DATA_REPORT (0x01) payload."""
        if len(self._data) < 5:
            return None

        offset = 0
        timestamp = int.from_bytes(self._data[offset : offset + 4], byteorder="little")
        offset += 4

        channel_count = self._data[offset]
        offset += 1

        channels = []
        for _ in range(channel_count):
            if offset + 36 > len(self._data):
                break

            name_bytes = self._data[offset : offset + 32]
            name = name_bytes.rstrip(b"\x00").decode("utf-8", errors="ignore")
            offset += 32

            value = struct.unpack("<f", bytes(self._data[offset : offset + 4]))[0]
            offset += 4

            channels.append(ChannelData(name=name, value=value))

        return DataFrame(
            cmd_type=self._cmd_type, timestamp=timestamp, channels=channels
        )

    @property
    def error_count(self) -> int:
        """Get number of framing errors encountered."""
        return self._error_count
