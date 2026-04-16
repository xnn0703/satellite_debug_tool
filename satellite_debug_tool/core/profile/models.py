"""DeviceProfile — 单个 hw_type 的 channel/state/event 元数据聚合。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    EventDefEntry,
    MetaInfo,
    StateDefEntry,
)


@dataclass
class DeviceProfile:
    """
    一个设备型号（hw_type）的完整 profile。

    三张表的版本号相互独立（协议允许 DEFINE 只更新某一张）。
    table_ver 为 None 表示对应表尚未收到过。
    """

    hw_type: str
    channel_table_ver: Optional[int] = None
    state_table_ver: Optional[int] = None
    event_table_ver: Optional[int] = None

    channels: Dict[int, ChannelDefEntry] = field(default_factory=dict)
    states: Dict[int, StateDefEntry] = field(default_factory=dict)
    events: Dict[int, EventDefEntry] = field(default_factory=dict)

    meta: Optional[MetaInfo] = None

    # ---- 读取辅助 ----

    def get_channel(self, channel_id: int) -> Optional[ChannelDefEntry]:
        return self.channels.get(channel_id)

    def get_state(self, state_id: int) -> Optional[StateDefEntry]:
        return self.states.get(state_id)

    def get_event(self, event_id: int) -> Optional[EventDefEntry]:
        return self.events.get(event_id)

    def channel_list(self) -> List[ChannelDefEntry]:
        return [self.channels[k] for k in sorted(self.channels)]

    def state_list(self) -> List[StateDefEntry]:
        return [self.states[k] for k in sorted(self.states)]

    def event_list(self) -> List[EventDefEntry]:
        return [self.events[k] for k in sorted(self.events)]

    # ---- 状态判断 ----

    def is_ready(self) -> bool:
        """三张表都已收到至少一次（任一为空也算 ready，只要 table_ver 被设过）。"""
        return (
            self.channel_table_ver is not None
            and self.state_table_ver is not None
            and self.event_table_ver is not None
        )
