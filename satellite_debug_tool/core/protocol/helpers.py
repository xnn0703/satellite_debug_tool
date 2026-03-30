from satellite_debug_tool.core.protocol.crc16 import Crc16


def build_debug_control_frame(enable: bool) -> bytes:
    header = bytes([0xAA, 0x55, 0x0D, 0x03])
    data = bytes([1 if enable else 0])
    data_len = len(data).to_bytes(2, "little")
    payload = header + data_len + data
    crc = Crc16.calculate(payload)
    crc_bytes = bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    footer = bytes([0xEE])
    return payload + crc_bytes + footer
