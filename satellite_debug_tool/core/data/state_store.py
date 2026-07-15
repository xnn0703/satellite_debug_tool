"""
StateStore — 维护所有 state_id 的当前值 + 最近变化时间。

与 DataStore 并列，由 MainWindow 在收到 `StateReport` 时调用 `update()`。
UI 组件（StatePanel）订阅 `state_changed` 信号即可刷新指示灯。

按 hw_type 分桶：不同型号的同一 state_id 语义可能完全不同，必须隔离。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.protocol import StateReport


@dataclass
class StateSnapshot:
    """单个 state 的当前运行时信息。"""

    value: int
    last_change_ms: int       # device timestamp (ms since boot)
    last_change_wallclock: float = field(default_factory=time.time)  # PC 端 time.time()


class StateStore(QObject):
    """按 hw_type 分桶存储 state_id → StateSnapshot。"""

    # (hw_type, state_id, new_value, old_value_or_-1)
    state_changed = Signal(str, int, int, int)
    # hw_type or None；用于通知 UI 把已显示的状态恢复为未知态
    state_cleared = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._buckets: Dict[str, Dict[int, StateSnapshot]] = {}

    # ---- 写入 ----

    def update(self, hw_type: str, report: StateReport) -> None:
        """消费一条 StateReport；对真正变化的 state 发射信号。"""
        bucket = self._buckets.setdefault(hw_type, {})
        now_wall = time.time()
        for sample in report.states:
            prev = bucket.get(sample.state_id)
            if prev is not None and prev.value == sample.value:
                # 无变化：刷新时间戳，但不发信号
                prev.last_change_ms = report.timestamp
                continue
            old = -1 if prev is None else prev.value
            bucket[sample.state_id] = StateSnapshot(
                value=sample.value,
                last_change_ms=report.timestamp,
                last_change_wallclock=now_wall,
            )
            self.state_changed.emit(hw_type, sample.state_id, sample.value, old)

    # ---- 读取 ----

    def get(self, hw_type: str, state_id: int) -> Optional[StateSnapshot]:
        bucket = self._buckets.get(hw_type)
        return None if bucket is None else bucket.get(state_id)

    def get_value(self, hw_type: str, state_id: int) -> Optional[int]:
        snap = self.get(hw_type, state_id)
        return None if snap is None else snap.value

    def get_all(self, hw_type: str) -> Dict[int, StateSnapshot]:
        return dict(self._buckets.get(hw_type, {}))

    # ---- 其它 ----

    def clear(self, hw_type: Optional[str] = None) -> None:
        if hw_type is None:
            self._buckets.clear()
        else:
            self._buckets.pop(hw_type, None)
        self.state_cleared.emit(hw_type)
