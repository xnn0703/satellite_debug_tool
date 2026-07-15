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

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
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
from satellite_debug_tool.ui.flow_layout import FlowLayout

# Mission Console 语义色标（与 StatePanel 同源）
_ENUM_LEVEL_COLORS = {
    0: "#34D399",   # ok 绿
    1: "#FBBF24",   # warn 黄
    2: "#FB7185",   # err 红
    3: "#586976",   # idle 灰
}
_BOOL_ON = "#34D399"
_BOOL_OFF = "#586976"
_LINK_OK = "#34D399"
_LINK_BAD = "#FB7185"
_LINK_IDLE = "#586976"


def _dot(color: str, size: int = 8) -> str:
    """状态圆点：Mission Console 点阵风（8px + 同色辉光）。"""
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
        self._theme = "dark"
        self._scale = "medium"
        self.apply_theme(self._theme, self._scale)

    def apply_theme(self, theme="dark", scale="medium") -> None:
        """theme: "dark"/"dark_hc"/"light"（也接受 bool）；scale: 字号档位。"""
        self._theme = S._normalize_theme(theme)
        self._scale = scale
        p = S.palette(self._theme)
        self.setStyleSheet(
            f"QFrame {{ background-color: {p['card_2']}; border: 1px solid {p['border']}; "
            f"border-radius: 11px; padding: 2px 7px; }}"
            f"QLabel {{ border: none; color: {p['text_2']}; "
            f"font-size: {S.font_px(11, scale)}px; background: transparent; }}"
        )

    def set_dot(self, color: str) -> None:
        self._dot_label.setStyleSheet(_dot(color))

    def set_text(self, text: str) -> None:
        self._text.setText(text)


class StatusStripWidget(QFrame):
    """自适应换行的状态条：用 FlowLayout 替代横向滚动条。"""

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

        self._is_dark = True
        self._theme = "dark"
        self._scale = "medium"

        # FlowLayout 直接挂在 QFrame 上，宽度变化时 chip 自动换行
        self._flow = FlowLayout(self, margin=4, h_spacing=6, v_spacing=4)

        # 高度跟随内容（行数 × chip 高 + spacing），不再固定 36px
        sp = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

        # 固定 chips
        self._link_chip = _Chip("LINK")
        self._link_chip.setToolTip("协议级心跳（HEARTBEAT）健康状态：OK=链路通，-- =3s 未收到心跳")
        self._recording_chip = _Chip("REC")
        self._recording_chip.setToolTip("本机 .sdb 录制状态：红灯闪=正在录，灰=空闲")
        self._beat_chip = _Chip("BEAT")
        self._beat_chip.set_dot(_LINK_IDLE)
        self._beat_chip.setToolTip("每收到一帧 HEARTBEAT 闪一下绿灯（可验证下位机存活）")
        self._flow.addWidget(self._link_chip)
        self._flow.addWidget(self._recording_chip)
        self._flow.addWidget(self._beat_chip)

        # 分隔符（在 flow 中作为普通 item，换行时会留在自然位置）
        self._sep = QLabel("|")
        self._flow.addWidget(self._sep)

        # Beat 灭灯定时器
        self._beat_timer = QTimer(self)
        self._beat_timer.setSingleShot(True)
        self._beat_timer.setInterval(200)
        self._beat_timer.timeout.connect(lambda: self._beat_chip.set_dot(_LINK_IDLE))

        profile_store.profile_changed.connect(self._on_profile_changed)
        state_store.state_changed.connect(self._on_state_changed)
        state_store.state_cleared.connect(self._on_state_cleared)

        self.set_theme("dark", "medium")
        self.set_recording(False)
        self.set_link_state(connected=False)

    # ---- 让父布局正确分配高度（FlowLayout 是高度跟随宽度的布局） ----

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt API)
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._flow.heightForWidth(width)

    def set_dark_theme(self, is_dark: bool) -> None:
        """兼容老接口：bool → 三档主题中的 dark/light。"""
        self.set_theme("dark" if is_dark else "light", self._scale)

    def set_theme(self, theme: str = "dark", scale: str = "medium") -> None:
        self._theme = S._normalize_theme(theme)
        self._is_dark = self._theme != "light"
        self._scale = scale
        p = S.palette(self._theme)
        # 高度由 FlowLayout heightForWidth 控制，不再 setFixedHeight
        self.setStyleSheet(
            f"StatusStripWidget {{ background-color: {p['bg']}; "
            f"border-top: 1px solid {p['border']}; border-bottom: 1px solid {p['border']}; }}"
        )
        self._sep.setStyleSheet(f"color: {p['text_faint']}; padding: 0 4px;")
        for chip in (self._link_chip, self._recording_chip, self._beat_chip, *self._state_chips.values()):
            chip.apply_theme(self._theme, self._scale)
        # 主题切换后让 FlowLayout 重新计算（updateGeometry 触发 heightForWidth 重算）
        self.updateGeometry()

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

    def _on_state_cleared(self, hw_type: object) -> None:
        if self._current_hw is None:
            return
        if hw_type is None or hw_type == self._current_hw:
            self._rebuild_dynamic()

    # ---- 重建动态 chips ----

    def _clear_dynamic(self) -> None:
        # 从 FlowLayout 移除动态 chip（固定 chip + 分隔符之后的所有 item）
        for chip in self._state_chips.values():
            self._flow.removeWidget(chip)
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
            chip.apply_theme(self._theme, self._scale)
            if entry.state_type == int(StateType.BOOL):
                tip = f"[BOOL] state_id={entry.state_id}  {entry.name}"
                if entry.flags & STATE_FLAG_INVERSE:
                    tip += "\n(INVERSE: 0=正常/绿，1=告警/灰)"
            else:
                enum_lines = "\n".join(
                    f"  {e.value} = {e.name} [lv={e.level}]" for e in entry.enums
                )
                tip = (
                    f"[ENUM] state_id={entry.state_id}  {entry.name}\n"
                    f"{enum_lines or '  (无枚举项)'}"
                )
            chip.setToolTip(tip)
            self._flow.addWidget(chip)
            self._state_chips[entry.state_id] = chip
            self._state_entries[entry.state_id] = entry
            # 回灌当前值
            current = self._states.get_value(self._current_hw, entry.state_id)
            if current is not None:
                self._apply_state(chip, entry, current)
        self.updateGeometry()

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
