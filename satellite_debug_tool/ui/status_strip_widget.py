"""
StatusStripWidget — 窄条状态带，顶部持续可见。

目的：即便曲线/Dashboard 被切换或遮挡，用户一眼也能确认链路、录制、
核心状态是否正常。

条目：
- **LINK**：协议级心跳健康（Handshake.link_lost/restored 驱动）
- **RECORDING**：本机录制状态（由 MainWindow 外部刷新）
- **BEAT**：HEARTBEAT 节拍小指示（心跳到达时闪一下）
- 前 ≤ 6 个 `flags.critical` 的 BOOL / ENUM 状态字（profile 驱动）
"""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QWidget,
)

from satellite_debug_tool.core.data import StateStore
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    STATE_FLAG_INVERSE,
    StateDefEntry,
    StateType,
)
from satellite_debug_tool.ui import styles as S

# 与 StatePanel 同源的色标
_ENUM_LEVEL_COLORS = {
    0: "#4EC9B0",
    1: "#DCDCAA",
    2: "#F14C4C",
    3: "#808080",
}
_BOOL_ON = "#4EC9B0"
_BOOL_OFF = "#555555"
_LINK_OK = "#4EC9B0"
_LINK_BAD = "#F14C4C"
_LINK_IDLE = "#808080"


def _dot(color: str, size: int = 12) -> str:
    return (
        f"background-color: {color}; border-radius: {size // 2}px; "
        f"min-width: {size}px; max-width: {size}px; "
        f"min-height: {size}px; max-height: {size}px;"
    )


class _Chip(QFrame):
    """一块胶囊：[圆点] 标签。"""

    def __init__(self, label: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 2, 8, 2)
        row.setSpacing(6)
        self._dot_label = QLabel()
        self._dot_label.setStyleSheet(_dot(_LINK_IDLE))
        self._text = QLabel(label)
        row.addWidget(self._dot_label)
        row.addWidget(self._text)
        self.apply_theme(True)

    def apply_theme(self, is_dark: bool) -> None:
        p = S.palette(is_dark)
        self.setStyleSheet(
            f"QFrame {{ background-color: {p['card']}; border: 1px solid {p['border']}; "
            f"border-radius: 10px; padding: 2px 6px; }}"
            f"QLabel {{ border: none; color: {p['text']}; font-size: 11px; }}"
        )

    def set_dot(self, color: str) -> None:
        self._dot_label.setStyleSheet(_dot(color))

    def set_text(self, text: str) -> None:
        self._text.setText(text)


class StatusStripWidget(QFrame):
    """32px 高的横条状态带。"""

    MAX_CRITICAL_STATES = 6

    def __init__(
        self,
        profile_store: ProfileStore,
        state_store: StateStore,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._profile = profile_store
        self._states = state_store
        self._current_hw: Optional[str] = None
        self._state_chips: Dict[int, _Chip] = {}
        self._state_entries: Dict[int, StateDefEntry] = {}

        self.setFixedHeight(36)
        self._is_dark = True

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 3, 8, 3)
        row.setSpacing(6)

        # 固定 chips
        self._link_chip = _Chip("LINK")
        self._recording_chip = _Chip("REC")
        self._beat_chip = _Chip("BEAT")
        self._beat_chip.set_dot(_LINK_IDLE)
        row.addWidget(self._link_chip)
        row.addWidget(self._recording_chip)
        row.addWidget(self._beat_chip)

        # 分隔符
        self._sep = QLabel("|")
        row.addWidget(self._sep)

        # 动态 chips 区域
        self._dynamic_host = QWidget()
        self._dynamic_layout = QHBoxLayout(self._dynamic_host)
        self._dynamic_layout.setContentsMargins(0, 0, 0, 0)
        self._dynamic_layout.setSpacing(6)
        row.addWidget(self._dynamic_host, 1)

        row.addStretch(1)

        # Beat 灭灯定时器
        self._beat_timer = QTimer(self)
        self._beat_timer.setSingleShot(True)
        self._beat_timer.setInterval(200)
        self._beat_timer.timeout.connect(lambda: self._beat_chip.set_dot(_LINK_IDLE))

        profile_store.profile_changed.connect(self._on_profile_changed)
        state_store.state_changed.connect(self._on_state_changed)

        self.set_dark_theme(True)
        self.set_recording(False)
        self.set_link_state(connected=False)

    def set_dark_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        p = S.palette(is_dark)
        self.setStyleSheet(
            f"StatusStripWidget {{ background-color: {p['bg']}; "
            f"border-top: 1px solid {p['border']}; border-bottom: 1px solid {p['border']}; }}"
        )
        self._sep.setStyleSheet(f"color: {p['text_faint']}; padding: 0 4px;")
        for chip in (self._link_chip, self._recording_chip, self._beat_chip, *self._state_chips.values()):
            chip.apply_theme(is_dark)

    # ---- Public ----

    def set_hw_type(self, hw_type: Optional[str]) -> None:
        if hw_type == self._current_hw:
            return
        self._current_hw = hw_type
        self._rebuild_dynamic()

    def set_link_state(self, *, connected: bool) -> None:
        if connected:
            self._link_chip.set_dot(_LINK_OK)
            self._link_chip.set_text("LINK OK")
        else:
            self._link_chip.set_dot(_LINK_BAD)
            self._link_chip.set_text("LINK --")

    def set_recording(self, active: bool) -> None:
        self._recording_chip.set_dot("#F14C4C" if active else _LINK_IDLE)
        self._recording_chip.set_text("REC ●" if active else "REC")

    def pulse_heartbeat(self) -> None:
        """由 MainWindow 在收到 HEARTBEAT 帧时调用，闪一下 BEAT 灯。"""
        self._beat_chip.set_dot(_LINK_OK)
        self._beat_timer.start()

    # ---- 响应 ----

    def _on_profile_changed(self, hw_type: str) -> None:
        if self._current_hw is None:
            self._current_hw = hw_type
        if hw_type == self._current_hw:
            self._rebuild_dynamic()

    def _on_state_changed(self, hw_type: str, state_id: int, value: int, _old: int) -> None:
        if hw_type != self._current_hw:
            return
        chip = self._state_chips.get(state_id)
        entry = self._state_entries.get(state_id)
        if chip is not None and entry is not None:
            self._apply_state(chip, entry, value)

    # ---- 重建动态 chips ----

    def _clear_dynamic(self) -> None:
        for chip in self._state_chips.values():
            self._dynamic_layout.removeWidget(chip)
            chip.deleteLater()
        self._state_chips.clear()
        self._state_entries.clear()

    def _rebuild_dynamic(self) -> None:
        self._clear_dynamic()
        if self._current_hw is None:
            return

        states = self._profile.get_states(self._current_hw)
        critical = [s for s in states if s.critical][: self.MAX_CRITICAL_STATES]
        for entry in critical:
            chip = _Chip(entry.name)
            chip.apply_theme(self._is_dark)
            self._dynamic_layout.addWidget(chip)
            self._state_chips[entry.state_id] = chip
            self._state_entries[entry.state_id] = entry
            # 回灌当前值
            current = self._states.get_value(self._current_hw, entry.state_id)
            if current is not None:
                self._apply_state(chip, entry, current)

    def _apply_state(self, chip: _Chip, entry: StateDefEntry, value: int) -> None:
        if entry.state_type == int(StateType.BOOL):
            inverse = bool(entry.flags & STATE_FLAG_INVERSE)
            on = (value != 0) ^ inverse
            chip.set_dot(_BOOL_ON if on else _BOOL_OFF)
            chip.set_text(entry.name)
            return
        # ENUM
        match = next((e for e in entry.enums if e.value == value), None)
        if match is None:
            chip.set_dot(_BOOL_OFF)
            chip.set_text(f"{entry.name}=?")
            return
        chip.set_dot(_ENUM_LEVEL_COLORS.get(match.level, _BOOL_OFF))
        chip.set_text(f"{entry.name}: {match.name}")
