"""
DataStore — v2 数据通道缓冲管理。

接收 `DataReport`，按 `channel_id` 入对应 ChannelBuffer。
内部 key 采用稳定的 ``ch_{id:02d}``，即便尚未收到 CHANNEL_DEFINE 也可工作。

M3 阶段 UI 切换到 Profile 驱动后，Dashboard/Chart 会通过 ProfileStore
用 ``channel_id`` 查询真实名称/单位/分组，本层只保留"ID → 数据"映射。
"""

import time
from typing import Dict, List, Optional

from .channel_buffer import ChannelBuffer
from satellite_debug_tool.core.protocol import DataReport


def channel_key(channel_id: int) -> str:
    """由 channel_id 生成内部稳定 key。"""
    return f"ch_{int(channel_id):02d}"


class DataStore:
    """按 channel_id 组织的缓冲集合。

    M7：`buffer_capacity = None` 进入无界模式，给 PlaybackView / LogView
    一次性灌入历史数据用；Live Tab 仍用默认 30000 环形缓冲。
    """

    def __init__(
        self,
        max_channels: int = 16,
        buffer_capacity: Optional[int] = 30000,
    ) -> None:
        self._max_channels = max_channels
        self._buffer_capacity = buffer_capacity
        self._buffers: Dict[str, ChannelBuffer] = {}
        self._last_received_monotonic: Dict[str, float] = {}
        self._frame_count = 0

    # ---- 写入 ----

    def update(self, report: DataReport) -> None:
        """将一帧 DATA_REPORT 的采样写入对应 channel buffer。"""
        self._frame_count += 1
        now = time.monotonic()
        for sample in report.samples[: self._max_channels]:
            key = channel_key(sample.channel_id)
            buf = self._buffers.get(key)
            if buf is None:
                buf = ChannelBuffer(key, self._buffer_capacity)
                self._buffers[key] = buf
            buf.append(report.timestamp, sample.value)
            self._last_received_monotonic[key] = now

    # ---- 读取 ----

    def get_channel(self, name: str) -> ChannelBuffer | None:
        """按内部 key 取 ChannelBuffer；name 不存在返回 None。"""
        return self._buffers.get(name)

    def get_channel_by_id(self, channel_id: int) -> ChannelBuffer | None:
        return self._buffers.get(channel_key(channel_id))

    def last_received_monotonic(self, channel_id: int) -> Optional[float]:
        """返回该通道最近一次由主机接收的单调时钟秒数。"""
        return self._last_received_monotonic.get(channel_key(channel_id))

    def get_all_channels(self) -> List[str]:
        """返回当前所有 channel 的内部 key 列表（已按加入顺序）。"""
        return list(self._buffers.keys())

    # ---- 其它 ----

    def clear(self) -> None:
        self._buffers.clear()
        self._last_received_monotonic.clear()
        self._frame_count = 0

    @property
    def frame_count(self) -> int:
        return self._frame_count
