import logging
from PySide6.QtCore import QThread, Signal

from satellite_debug_tool.core.protocol.frame_v2 import (
    FRAME_HEADER_0,
    FRAME_HEADER_1,
    MAX_DATA_LENGTH,
)


class BaseWorker(QThread):
    connected = Signal()
    disconnected = Signal()
    error = Signal(str)
    data_received = Signal(bytes)

    def __init__(self):
        super().__init__()
        self._running = False

    def connect(self, config: dict) -> bool:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def is_connected(self) -> bool:
        return self._running

    def send(self, data: bytes) -> bool:
        raise NotImplementedError

    @staticmethod
    def _drain_debug_frames(buffer: bytearray) -> list[bytes]:
        """从共享接收缓冲中取出完整 DEBUG v2 帧，并丢弃超长坏头。"""
        frames: list[bytes] = []
        header = bytes((FRAME_HEADER_0, FRAME_HEADER_1))
        while buffer:
            header_index = buffer.find(header)
            if header_index < 0:
                # 保留跨 read/datagram 边界的首个帧头字节。
                if buffer[-1] == FRAME_HEADER_0:
                    buffer[:] = bytes((FRAME_HEADER_0,))
                else:
                    buffer.clear()
                break
            if header_index > 0:
                del buffer[:header_index]
            if len(buffer) < 6:
                break

            data_len = int.from_bytes(buffer[4:6], "little")
            if data_len > MAX_DATA_LENGTH:
                # 只丢弃当前候选帧头的首字节，下一轮重新搜索 AA 55；这样坏头
                # 后紧随的合法帧不会被当作超长 payload 吞掉。
                del buffer[0]
                continue

            frame_len = 9 + data_len
            if len(buffer) < frame_len:
                break
            frames.append(bytes(buffer[:frame_len]))
            del buffer[:frame_len]
        return frames
