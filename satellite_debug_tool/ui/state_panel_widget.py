"""
StatePanelWidget — profile 驱动的状态字指示灯板。

- Profile 任一 STATE_DEFINE 变化时整体重建 UI（订阅 ProfileStore.profile_changed）
- StateStore.state_changed 触发对应项刷新（灯 + 文本）
- BOOL: 绿灯 / 灰灯（inverse 时反色）
- ENUM: 按 enum item level 选色（INFO 绿 / WARN 黄 / ERROR 红 / NEUTRAL 灰），
  显示当前枚举项名
- 未知 state_id 不显示
"""

from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
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


# ---- 色标（与 DEBUG_ENUM_LEVEL_* 对齐） ----

_ENUM_LEVEL_COLORS = {
    0: "#4EC9B0",   # INFO (绿)
    1: "#DCDCAA",   # WARN (黄)
    2: "#F14C4C",   # ERROR (红)
    3: "#808080",   # NEUTRAL (灰)
}

_BOOL_ON_COLOR = "#4EC9B0"     # 绿
_BOOL_OFF_COLOR = "#555555"    # 深灰


def _dot_stylesheet(color: str) -> str:
    return (
        f"background-color: {color}; border-radius: 7px; min-width: 14px; "
        f"max-width: 14px; min-height: 14px; max-height: 14px;"
    )


class StateItemRow(QFrame):
    """一行：左圆点 + 状态字名 + 当前值文本。"""

    def __init__(self, entry: StateDefEntry, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._entry = entry
        self._is_dark = True
        layout = QGridLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(0)

        self._dot = QLabel()
        self._dot.setStyleSheet(_dot_stylesheet(_BOOL_OFF_COLOR))
        self._name_label = QLabel(entry.name)
        self._value_label = QLabel("—")
        self._value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        layout.addWidget(self._dot, 0, 0)
        layout.addWidget(self._name_label, 0, 1)
        layout.addWidget(self._value_label, 0, 2)
        layout.setColumnStretch(1, 1)

        self.set_dark_theme(True)
        self.set_unknown()

    def set_dark_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        p = S.palette(is_dark)
        self.setStyleSheet(
            f"QFrame {{ background-color: {p['card_alt']}; border-radius: 4px; padding: 4px 8px; }}"
            f"QLabel {{ border: none; color: {p['text']}; }}"
        )
        self._name_label.setStyleSheet(f"font-weight: 500; color: {p['text']};")
        # value_label 颜色由 set_value/set_unknown 设置，不在此覆盖

    # ----- 更新 -----

    def set_unknown(self) -> None:
        """状态未知（未曾上报）。"""
        self._dot.setStyleSheet(_dot_stylesheet(_BOOL_OFF_COLOR))
        self._value_label.setText("—")
        p = S.palette(self._is_dark)
        self._value_label.setStyleSheet(f"color: {p['text_faint']};")

    def set_value(self, value: int) -> None:
        if self._entry.state_type == int(StateType.BOOL):
            self._render_bool(value)
        else:
            self._render_enum(value)

    def _render_bool(self, value: int) -> None:
        inverse = bool(self._entry.flags & STATE_FLAG_INVERSE)
        # inverse: 0=on(绿), 1=off(灰/红) 的含义反了；这里简化：inverse 时颜色取反
        on = (value != 0) ^ inverse
        color = _BOOL_ON_COLOR if on else _BOOL_OFF_COLOR
        self._dot.setStyleSheet(_dot_stylesheet(color))
        self._value_label.setText("ON" if value != 0 else "OFF")
        self._value_label.setStyleSheet(f"color: {color};")

    def _render_enum(self, value: int) -> None:
        match = next((e for e in self._entry.enums if e.value == value), None)
        if match is None:
            # 未知枚举值：显示原始整数
            self._dot.setStyleSheet(_dot_stylesheet(_BOOL_OFF_COLOR))
            self._value_label.setText(str(value))
            self._value_label.setStyleSheet("color: #888888;")
            return
        color = _ENUM_LEVEL_COLORS.get(match.level, "#888888")
        self._dot.setStyleSheet(_dot_stylesheet(color))
        self._value_label.setText(match.name)
        self._value_label.setStyleSheet(f"color: {color}; font-weight: 500;")


class StatePanelWidget(QScrollArea):
    """整体状态灯板：跟随 ProfileStore / StateStore 自动刷新。"""

    def __init__(
        self,
        profile_store: ProfileStore,
        state_store: StateStore,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._profile = profile_store
        self._states = state_store
        self._rows: Dict[int, StateItemRow] = {}
        self._current_hw: Optional[str] = None
        self._is_dark = True

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._vlayout = QVBoxLayout(self._container)
        self._vlayout.setContentsMargins(6, 6, 6, 6)
        self._vlayout.setSpacing(4)
        self._vlayout.addStretch(1)
        self.setWidget(self._container)

        self._empty_label = QLabel("等待设备握手…")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._vlayout.insertWidget(0, self._empty_label)

        self.set_dark_theme(True)

        profile_store.profile_changed.connect(self._on_profile_changed)
        state_store.state_changed.connect(self._on_state_changed)

    def set_dark_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        p = S.palette(is_dark)
        self.setStyleSheet(
            f"QScrollArea {{ background-color: {p['bg']}; border: 1px solid {p['border']}; }}"
        )
        self._container.setStyleSheet(f"background-color: {p['bg']};")
        self._empty_label.setStyleSheet(f"color: {p['text_faint']}; padding: 16px;")
        for row in self._rows.values():
            row.set_dark_theme(is_dark)

    # ----- Public -----

    def set_hw_type(self, hw_type: Optional[str]) -> None:
        """切换显示的 hw_type（一般由外部调用，比如 Handshake ready 时）。"""
        if hw_type == self._current_hw:
            return
        self._current_hw = hw_type
        self._rebuild()

    # ----- 响应 -----

    def _on_profile_changed(self, hw_type: str) -> None:
        """当前 hw_type 的 profile 变化了 → 重建行。"""
        if self._current_hw is None:
            self._current_hw = hw_type   # 首次同步
        if hw_type != self._current_hw:
            return
        # 幂等：states 表签名未变时跳过（避免上游重复 emit 导致整板闪烁）
        new_sig = tuple(
            (s.state_id, s.name, s.state_type, s.flags, tuple((e.value, e.level, e.name) for e in s.enums))
            for s in self._profile.get_states(hw_type)
        )
        if getattr(self, "_states_signature", None) == new_sig and self._rows:
            return
        self._states_signature = new_sig
        self._rebuild()

    def _on_state_changed(self, hw_type: str, state_id: int, value: int, _old: int) -> None:
        if hw_type != self._current_hw:
            return
        row = self._rows.get(state_id)
        if row is not None:
            row.set_value(value)

    # ----- 构建 -----

    def _clear_rows(self) -> None:
        for row in self._rows.values():
            self._vlayout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()

    def _rebuild(self) -> None:
        self._clear_rows()
        if self._current_hw is None:
            self._empty_label.setText("等待设备握手…")
            self._empty_label.show()
            return

        states = self._profile.get_states(self._current_hw)
        if not states:
            self._empty_label.setText(f"[{self._current_hw}] 尚未收到 STATE_DEFINE")
            self._empty_label.show()
            return

        self._empty_label.hide()
        insert_pos = 0
        for entry in states:
            row = StateItemRow(entry)
            row.set_dark_theme(self._is_dark)
            self._vlayout.insertWidget(insert_pos, row)
            self._rows[entry.state_id] = row
            insert_pos += 1
            # 回灌当前值
            current = self._states.get_value(self._current_hw, entry.state_id)
            if current is not None:
                row.set_value(current)
