from typing import Dict, List
from .channel_buffer import ChannelBuffer
from satellite_debug_tool.core.protocol import DataFrame


class DataStore:
    def __init__(self, max_channels: int = 16, buffer_capacity: int = 2000):
        self._max_channels = max_channels
        self._buffers: Dict[str, ChannelBuffer] = {}
        self._buffer_capacity = buffer_capacity
        self._frame_count = 0

    def update(self, frame: DataFrame) -> None:
        self._frame_count += 1
        for channel in frame.channels[: self._max_channels]:
            name = channel.name
            if name not in self._buffers:
                self._buffers[name] = ChannelBuffer(name, self._buffer_capacity)
            self._buffers[name].append(frame.timestamp, channel.value)

    def get_channel(self, name: str) -> ChannelBuffer | None:
        return self._buffers.get(name)

    def get_all_channels(self) -> List[str]:
        return list(self._buffers.keys())

    def clear(self) -> None:
        self._buffers.clear()
        self._frame_count = 0

    @property
    def frame_count(self) -> int:
        return self._frame_count
