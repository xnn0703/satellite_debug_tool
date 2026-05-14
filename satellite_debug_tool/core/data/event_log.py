"""
EventLog — 跨 hw_type 的统一事件时间线（环形缓冲）。

消费 `EventReport`，结合 `ProfileStore` 查出事件名 / level（未知则降级为
`EVENT_<hex>` / INFO）。UI 订阅 `event_added` 信号即可增量渲染。

容量默认 5000 条；溢出时丢弃最早。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Iterable, List, Optional

from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import EventReport, EVENT_ID_USER_MARK


@dataclass
class EventRecord:
    """UI 与录制层共享的事件视图。"""

    hw_type: str
    event_id: int
    level: int                 # 0=DEBUG 1=INFO 2=WARN 3=ERROR
    name: str                  # profile 里的名字，未知 → "EVENT_<HEX>"
    timestamp_ms: int          # 设备时间戳
    wallclock: float = field(default_factory=time.time)
    payload: bytes = b""

    @property
    def is_user_mark(self) -> bool:
        return self.event_id == EVENT_ID_USER_MARK


class EventLog(QObject):
    """环形事件缓冲 + 查询接口。"""

    event_added = Signal(object)   # EventRecord（用 object 避免 Qt 类型擦除）

    def __init__(self, capacity: int = 5000, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._capacity = capacity
        self._buf: Deque[EventRecord] = deque(maxlen=capacity)

    # ---- 写入 ----

    def add(self, hw_type: str, report: EventReport, profile_store: ProfileStore) -> EventRecord:
        """消费一条 EventReport 并返回落库后的 EventRecord。"""
        name = None
        level = 1   # 默认 INFO
        if report.event_id == EVENT_ID_USER_MARK:
            name = "USER_MARK"
            level = 1
        else:
            ev = profile_store.get_event(hw_type, report.event_id)
            if ev is not None:
                name = ev.name
                level = ev.level
        if name is None:
            name = f"EVENT_{report.event_id:04X}"

        record = EventRecord(
            hw_type=hw_type,
            event_id=report.event_id,
            level=level,
            name=name,
            timestamp_ms=report.timestamp,
            payload=report.payload,
        )
        self._buf.append(record)
        self.event_added.emit(record)
        return record

    # ---- 读取 ----

    def __len__(self) -> int:
        return len(self._buf)

    def all(self) -> List[EventRecord]:
        return list(self._buf)

    def recent(self, n: int) -> List[EventRecord]:
        """最近 n 条（按到达顺序，不含时间倒序）。"""
        if n <= 0:
            return []
        if n >= len(self._buf):
            return list(self._buf)
        return list(self._buf)[-n:]

    def filter(
        self,
        *,
        hw_type: Optional[str] = None,
        level_min: Optional[int] = None,
        keyword: Optional[str] = None,
        event_id: Optional[int] = None,
    ) -> List[EventRecord]:
        """按条件过滤，返回按时间排列的 list（仍是 FIFO 顺序）。"""
        def match(rec: EventRecord) -> bool:
            if hw_type is not None and rec.hw_type != hw_type:
                return False
            if level_min is not None and rec.level < level_min:
                return False
            if event_id is not None and rec.event_id != event_id:
                return False
            if keyword is not None and keyword.lower() not in rec.name.lower():
                return False
            return True
        return [r for r in self._buf if match(r)]

    # ---- 其它 ----

    def clear(self) -> None:
        self._buf.clear()

    @property
    def capacity(self) -> int:
        return self._capacity
