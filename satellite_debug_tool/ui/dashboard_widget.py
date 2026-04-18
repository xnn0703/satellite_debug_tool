"""
DashboardWidget — profile 驱动的 KPI 卡片 + 模式切换按钮组。

**完全元数据驱动**：Dashboard 不认识任何具体通道名；它从 ProfileStore
读取 `flags.critical == True` 的通道生成 KPI 卡片，从 critical ENUM 状态字
生成按钮组。afd01 上显示 SNR/AZ/EL 等卡片，ufd45 上可能是另一套，
代码无需改动。

刷新节奏：由 MainWindow 的统一定时器调用 ``refresh(data_store, state_store)``，
无内部 QTimer，避免多处心跳。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.data import DataStore, StateStore
from satellite_debug_tool.core.data.data_store import channel_key
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    StateDefEntry,
    StateType,
)
from satellite_debug_tool.ui import styles as S


# ============================================================
# KPI 卡片
# ============================================================

class KpiCard(QFrame):
    """一张 KPI 卡片：大号数字 + 单位 + 通道名。"""

    def __init__(self, entry: ChannelDefEntry, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._entry = entry
        self._out_of_range = False
        self._is_dark = True

        self.setMinimumSize(120, 80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        self._name_label = QLabel(entry.name.upper())
        mono = QFont("JetBrains Mono", 22, QFont.Bold)
        mono.setStyleHint(QFont.Monospace)
        self._value_label = QLabel("—")
        self._value_label.setFont(mono)
        self._value_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._unit_label = QLabel(entry.unit or "")

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self._name_label, 1, Qt.AlignLeft)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addWidget(self._value_label, 1)
        bottom.addWidget(self._unit_label, 0, Qt.AlignBottom)

        layout.addLayout(top)
        layout.addLayout(bottom)
        self._apply_style(normal=True)

    def set_dark_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self._apply_style(normal=not self._out_of_range)

    def _apply_style(self, normal: bool) -> None:
        p = S.palette(self._is_dark)
        if normal:
            card_bg = p["card"]
            border = p["border"]
        else:
            # 告警色：保留高对比红
            card_bg = "#3B1F1F" if self._is_dark else "#FFE8E8"
            border = "#F14C4C"
        self.setStyleSheet(
            f"KpiCard {{ background-color: {card_bg}; border: 1px solid {border}; "
            f"border-radius: 4px; }}"
        )
        self._name_label.setStyleSheet(
            f"color: {p['text_muted']}; font-size: 10px; font-weight: 600; background: transparent;"
        )
        self._value_label.setStyleSheet(
            f"color: {p['value_number']}; background: transparent;"
        )
        self._unit_label.setStyleSheet(
            f"color: {p['text_muted']}; font-size: 11px; background: transparent;"
        )

    def update_value(self, value: Optional[float]) -> None:
        if value is None:
            self._value_label.setText("—")
            if self._out_of_range:
                self._out_of_range = False
                self._apply_style(normal=True)
            return
        self._value_label.setText(f"{value:.2f}")
        out = value < self._entry.display_min or value > self._entry.display_max
        if out != self._out_of_range:
            self._out_of_range = out
            self._apply_style(normal=not out)


# ============================================================
# 枚举模式按钮组（单个 ENUM 状态字对应一组按钮）
# ============================================================

class ModeButtonGroup(QFrame):
    """基于某个 ENUM state 生成按钮；当前值高亮。"""

    mode_requested = Signal(int, int)   # (state_id, target_value)

    def __init__(self, state: StateDefEntry, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._state = state
        self._buttons: Dict[int, QPushButton] = {}
        self._current_value: Optional[int] = None
        self._is_dark = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        self._title = QLabel(state.name)
        outer.addWidget(self._title)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for item in state.enums:
            btn = QPushButton(item.name)
            btn.setCheckable(True)
            btn.setMinimumHeight(28)
            btn.clicked.connect(lambda _c=False, v=item.value: self._on_clicked(v))
            row.addWidget(btn)
            self._buttons[item.value] = btn
        row.addStretch(1)
        outer.addLayout(row)

        self.set_dark_theme(True)

    def set_dark_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        p = S.palette(is_dark)
        self.setStyleSheet(
            f"ModeButtonGroup {{ background-color: {p['card']}; "
            f"border: 1px solid {p['border']}; border-radius: 4px; }}"
        )
        self._title.setStyleSheet(
            f"color: {p['text_muted']}; font-size: 11px; font-weight: 600; background: transparent;"
        )
        btn_style = (
            f"QPushButton {{ background-color: {p['input_bg']}; color: {p['text']}; "
            f"border: 1px solid {p['input_border']}; border-radius: 3px; padding: 4px 10px; }}"
            f"QPushButton:hover {{ background-color: {p['card_alt']}; }}"
            f"QPushButton:checked {{ background-color: #0E639C; color: white; "
            f"border-color: #0E639C; font-weight: 600; }}"
        )
        for btn in self._buttons.values():
            btn.setStyleSheet(btn_style)

    def _on_clicked(self, target_value: int) -> None:
        self.mode_requested.emit(self._state.state_id, target_value)
        # 不自动切换 checked 状态；等下位机回传后 update_value 会刷新
        self.update_value(self._current_value)

    def update_value(self, value: Optional[int]) -> None:
        self._current_value = value
        for v, btn in self._buttons.items():
            btn.setChecked(v == value)


# ============================================================
# Dashboard 主体
# ============================================================

class DashboardWidget(QWidget):
    """完全元数据驱动的顶部仪表盘。"""

    # 用户点击模式按钮：(state_id, target_value)
    mode_requested = Signal(int, int)

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
        self._cards: Dict[int, KpiCard] = {}
        self._mode_groups: Dict[int, ModeButtonGroup] = {}
        self._is_dark = True

        self.setStyleSheet("background-color: transparent;")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(6)

        self._cards_row = QHBoxLayout()
        self._cards_row.setSpacing(6)
        outer.addLayout(self._cards_row)

        self._modes_row = QHBoxLayout()
        self._modes_row.setSpacing(6)
        outer.addLayout(self._modes_row)

        self._empty_label = QLabel("等待设备握手…")
        self._empty_label.setAlignment(Qt.AlignCenter)
        outer.addWidget(self._empty_label)

        self.set_dark_theme(True)

        profile_store.profile_changed.connect(self._on_profile_changed)
        state_store.state_changed.connect(self._on_state_changed)

    def set_dark_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        p = S.palette(is_dark)
        self._empty_label.setStyleSheet(f"color: {p['text_faint']}; padding: 12px;")
        for card in self._cards.values():
            card.set_dark_theme(is_dark)
        for group in self._mode_groups.values():
            group.set_dark_theme(is_dark)

    # ---- Public ----

    def set_hw_type(self, hw_type: Optional[str]) -> None:
        if hw_type == self._current_hw:
            return
        self._current_hw = hw_type
        self._rebuild()

    def refresh(self, data_store: DataStore) -> None:
        """每帧刷新 KPI 数值（模式按钮由 state_changed 信号驱动，不在此处理）。"""
        if self._current_hw is None:
            return
        for channel_id, card in self._cards.items():
            buf = data_store.get_channel(channel_key(channel_id))
            if buf is None:
                card.update_value(None)
                continue
            latest = buf.get_latest()
            card.update_value(latest[1] if latest is not None else None)

    # ---- 信号响应 ----

    def _on_profile_changed(self, hw_type: str) -> None:
        if self._current_hw is None:
            self._current_hw = hw_type
        if hw_type == self._current_hw:
            self._rebuild()

    def _on_state_changed(self, hw_type: str, state_id: int, value: int, _old: int) -> None:
        if hw_type != self._current_hw:
            return
        group = self._mode_groups.get(state_id)
        if group is not None:
            group.update_value(value)

    # ---- 重建 ----

    def _clear(self) -> None:
        for c in self._cards.values():
            self._cards_row.removeWidget(c)
            c.deleteLater()
        self._cards.clear()
        for g in self._mode_groups.values():
            self._modes_row.removeWidget(g)
            g.deleteLater()
        self._mode_groups.clear()

    def _rebuild(self) -> None:
        self._clear()
        if self._current_hw is None:
            self._empty_label.setText("等待设备握手…")
            self._empty_label.show()
            return

        channels = [c for c in self._profile.get_channels(self._current_hw) if c.critical]
        states = self._profile.get_states(self._current_hw)
        critical_enums = [
            s for s in states if s.critical and s.state_type == int(StateType.ENUM)
        ]

        if not channels and not critical_enums:
            self._empty_label.setText(
                f"[{self._current_hw}] profile 无 critical channel/state"
            )
            self._empty_label.show()
            return
        self._empty_label.hide()

        # ---- 卡片 ----
        for ch in channels:
            card = KpiCard(ch)
            card.set_dark_theme(self._is_dark)
            self._cards_row.addWidget(card)
            self._cards[ch.channel_id] = card
        if channels:
            self._cards_row.addStretch(1)

        # ---- 模式按钮组 ----
        for st in critical_enums:
            group = ModeButtonGroup(st)
            group.set_dark_theme(self._is_dark)
            group.mode_requested.connect(self.mode_requested.emit)
            self._modes_row.addWidget(group)
            self._mode_groups[st.state_id] = group
            # 回灌当前值
            current = self._states.get_value(self._current_hw, st.state_id)
            if current is not None:
                group.update_value(current)
        if critical_enums:
            self._modes_row.addStretch(1)
