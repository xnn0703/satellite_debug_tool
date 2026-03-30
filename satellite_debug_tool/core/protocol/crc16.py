"""
CRC16-CCITT checksum implementation.

Polynomial: 0x1021
Initial value: 0xFFFF
"""

from typing import Union


class Crc16:
    """CRC16-CCITT calculator."""

    POLY = 0x1021
    INIT_VAL = 0xFFFF

    @staticmethod
    def calculate(data: bytes) -> int:
        """
        Calculate CRC16-CCITT checksum for data.

        Args:
            data: Input bytes to calculate CRC for

        Returns:
            CRC16 value as integer
        """
        crc = Crc16.INIT_VAL

        for byte in data:
            crc ^= byte << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ Crc16.POLY
                else:
                    crc <<= 1
                crc &= 0xFFFF

        return crc

    @staticmethod
    def verify(data: bytes, expected: int) -> bool:
        """
        Verify CRC16-CCITT checksum.

        Args:
            data: Input bytes
            expected: Expected CRC value

        Returns:
            True if CRC matches, False otherwise
        """
        return Crc16.calculate(data) == expected

    @staticmethod
    def calculate_le(data: bytes) -> bytes:
        """
        Calculate CRC16-CCITT and return as little-endian bytes.

        Args:
            data: Input bytes

        Returns:
            2-byte little-endian CRC
        """
        crc = Crc16.calculate(data)
        return bytes([crc & 0xFF, (crc >> 8) & 0xFF])

    @staticmethod
    def from_le(crc_bytes: bytes) -> int:
        """
        Convert little-endian bytes to CRC16 integer.

        Args:
            crc_bytes: 2-byte CRC in little-endian format

        Returns:
            CRC value as integer
        """
        return int.from_bytes(crc_bytes, byteorder="little")
