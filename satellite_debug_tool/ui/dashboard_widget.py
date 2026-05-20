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
    QScrollArea,
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

    # 基准字号（scale=1.0 时的像素值）
    _VALUE_BASE_PX = 22
    _NAME_BASE_PX = 10
    _UNIT_BASE_PX = 11

    def __init__(self, entry: ChannelDefEntry, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._entry = entry
        self._out_of_range = False
        self._is_dark = True
        self._theme = "dark"
        self._scale = "medium"

        # 固定高度 72px 与 ModeButtonGroup 对齐；宽度按字号放大（由 _apply_font 调整）
        self.setFixedHeight(72)
        self.setMinimumWidth(110)
        # 用 Preferred 而不是 Expanding：装得下按自然宽度排，装不下由外层 QScrollArea 横滚
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        # M6: tooltip 悬停显示通道全信息
        self.setToolTip(
            f"通道 #{entry.channel_id}  {entry.name}\n"
            f"单位: {entry.unit or '(无)'}\n"
            f"量程: [{entry.display_min:.2f}, {entry.display_max:.2f}]\n"
            f"group_id: {entry.group_id}  flags: 0x{entry.flags:02X}\n"
            f"来源: DEFINE 表"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        self._name_label = QLabel(entry.name.upper())
        self._value_label = QLabel("—")
        self._value_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._unit_label = QLabel(entry.unit or "")

        self._apply_font(self._scale)

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

    # ---------- 主题/字号 ----------

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light", self._scale)

    def set_theme(self, theme: str = "dark", scale: str = "medium") -> None:
        self._theme = S._normalize_theme(theme)
        self._is_dark = self._theme != "light"
        self._scale = scale
        self._apply_font(scale)
        self._apply_style(normal=not self._out_of_range)

    def _apply_font(self, scale) -> None:
        # Dashboard KPI 数字字体：锁定等宽族，字号随档位缩放
        value_px = S.font_px(self._VALUE_BASE_PX, scale)
        mono = QFont(S.monospace_family(), 0, QFont.Bold)
        mono.setStyleHint(QFont.Monospace)
        mono.setPixelSize(value_px)
        self._value_label.setFont(mono)
        # 超大档下给卡片留足高度（默认 72 在 xlarge 会截断）
        min_h = max(72, value_px + 34)
        self.setFixedHeight(min_h)
        # 最小宽度：按 value 字号估算 "−000.00°" 需要的像素（6 字符 × 0.6 字宽 + 单位 + padding）
        min_w = max(110, int(value_px * 4.0) + 40)
        self.setMinimumWidth(min_w)

    def _apply_style(self, normal: bool) -> None:
        p = S.palette(self._theme)
        if normal:
            card_bg = p["card"]
            border = p["border"]
        else:
            # 告警色：三档主题下分别挑一档
            if self._theme == "dark_hc":
                card_bg = "#2A0000"
            elif self._theme == "light":
                card_bg = "#FFE8E8"
            else:
                card_bg = "#3B1F1F"
            border = p["error"]
        name_px = S.font_px(self._NAME_BASE_PX, self._scale)
        unit_px = S.font_px(self._UNIT_BASE_PX, self._scale)
        self.setStyleSheet(
            f"KpiCard {{ background-color: {card_bg}; border: 1px solid {border}; "
            f"border-radius: 4px; }}"
        )
        self._name_label.setStyleSheet(
            f"color: {p['text_muted']}; font-size: {name_px}px; font-weight: 600; "
            f"background: transparent;"
        )
        self._value_label.setStyleSheet(
            f"color: {p['value_number']}; background: transparent;"
        )
        self._unit_label.setStyleSheet(
            f"color: {p['text_muted']}; font-size: {unit_px}px; background: transparent;"
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
# 只读枚举状态紧凑标签（不可控的 ENUM 状态字）
# ============================================================

# 与 state_panel_widget 同源色标
_ENUM_LEVEL_COLORS = {
    0: "#4EC9B0",   # INFO (绿)
    1: "#DCDCAA",   # WARN (黄)
    2: "#F14C4C",   # ERROR (红)
    3: "#808080",   # NEUTRAL (灰)
}


class EnumStatusChip(QFrame):
    """只读 ENUM 状态字的紧凑显示：状态名 + 圆点 + 当前值。与 KpiCard 等高对齐。"""

    def __init__(self, state: StateDefEntry, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._state = state
        self._current_value: Optional[int] = None
        self._theme = "dark"
        self._scale = "medium"

        self.setFixedHeight(72)
        self.setMinimumWidth(90)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        enum_lines = "\n".join(
            f"  {e.value} = {e.name} [lv={e.level}]" for e in state.enums
        )
        self.setToolTip(
            f"[ENUM] state_id={state.state_id}  {state.name}\n"
            f"{enum_lines or '  (无枚举项)'}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        self._name_label = QLabel(state.name)
        layout.addWidget(self._name_label)

        val_row = QHBoxLayout()
        val_row.setContentsMargins(0, 0, 0, 0)
        val_row.setSpacing(6)
        self._dot = QLabel()
        self._dot.setFixedSize(12, 12)
        self._value_label = QLabel("—")
        val_font = QFont(S.monospace_family(), 0, QFont.Bold)
        val_font.setStyleHint(QFont.Monospace)
        self._value_label.setFont(val_font)
        val_row.addWidget(self._dot, 0, Qt.AlignVCenter)
        val_row.addWidget(self._value_label, 1, Qt.AlignVCenter)
        layout.addLayout(val_row)

        self.set_theme(self._theme, self._scale)

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light", self._scale)

    def set_theme(self, theme: str = "dark", scale: str = "medium") -> None:
        self._theme = S._normalize_theme(theme)
        self._scale = scale
        p = S.palette(self._theme)
        name_px = S.font_px(11, scale)
        value_px = S.font_px(16, scale)
        min_h = max(72, value_px + name_px + 28)
        self.setFixedHeight(min_h)
        self.setStyleSheet(
            f"EnumStatusChip {{ background-color: {p['card']}; "
            f"border: 1px solid {p['border']}; border-radius: 4px; }}"
        )
        self._name_label.setStyleSheet(
            f"color: {p['text_muted']}; font-size: {name_px}px; font-weight: 600; "
            f"background: transparent;"
        )
        val_font = self._value_label.font()
        val_font.setPixelSize(value_px)
        self._value_label.setFont(val_font)
        # 重新应用当前值的颜色
        self.update_value(self._current_value)

    def update_value(self, value: Optional[int]) -> None:
        self._current_value = value
        p = S.palette(self._theme)
        if value is None:
            self._dot.setStyleSheet(
                "background-color: #555555; border-radius: 6px;"
            )
            self._value_label.setText("—")
            self._value_label.setStyleSheet(
                f"color: {p['text_faint']}; background: transparent;"
            )
            return
        match = next((e for e in self._state.enums if e.value == value), None)
        if match is None:
            color = "#888888"
            text = str(value)
        else:
            color = _ENUM_LEVEL_COLORS.get(match.level, "#888888")
            text = match.name
        self._dot.setStyleSheet(
            f"background-color: {color}; border-radius: 6px;"
        )
        self._value_label.setText(text)
        self._value_label.setStyleSheet(
            f"color: {color}; font-weight: bold; background: transparent;"
        )


# ============================================================
# 可控枚举模式按钮组（如 TRACE_MODE，点击可发送控制帧）
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
        self._theme = "dark"
        self._scale = "medium"

        # 与 KpiCard 高度 72 对齐
        self.setFixedHeight(72)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(2)

        self._title = QLabel(state.name)
        self._title.setToolTip(
            f"[ENUM] state_id={state.state_id}  {state.name}\n"
            f"点击按钮发送 CONTROL.SET_TRACE_MODE（或等价子命令）"
        )
        outer.addWidget(self._title)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for item in state.enums:
            btn = QPushButton(item.name)
            btn.setCheckable(True)
            btn.setFixedHeight(26)
            btn.setToolTip(f"→ {state.name} = {item.name} (value={item.value}, level={item.level})")
            btn.clicked.connect(lambda _c=False, v=item.value: self._on_clicked(v))
            row.addWidget(btn)
            self._buttons[item.value] = btn
        row.addStretch(1)
        outer.addLayout(row)

        self.set_theme(self._theme, self._scale)

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light", self._scale)

    def set_theme(self, theme: str = "dark", scale: str = "medium") -> None:
        self._theme = S._normalize_theme(theme)
        self._is_dark = self._theme != "light"
        self._scale = scale
        p = S.palette(self._theme)
        title_px = S.font_px(11, scale)
        btn_px = S.font_px(11, scale)
        # 超大档按钮容易被裁：撑一下整体高度
        min_h = max(72, title_px + btn_px + 36)
        self.setFixedHeight(min_h)
        self.setStyleSheet(
            f"ModeButtonGroup {{ background-color: {p['card']}; "
            f"border: 1px solid {p['border']}; border-radius: 4px; }}"
        )
        self._title.setStyleSheet(
            f"color: {p['text_muted']}; font-size: {title_px}px; font-weight: 600; "
            f"background: transparent;"
        )
        btn_style = (
            f"QPushButton {{ background-color: {p['input_bg']}; color: {p['text']}; "
            f"border: 1px solid {p['input_border']}; border-radius: 3px; "
            f"padding: 4px 10px; font-size: {btn_px}px; }}"
            f"QPushButton:hover {{ background-color: {p['card_alt']}; }}"
            f"QPushButton:checked {{ background-color: {p['primary']}; color: white; "
            f"border-color: {p['primary']}; font-weight: 600; }}"
        )
        btn_h = btn_px + 14   # 字号 + padding
        for btn in self._buttons.values():
            btn.setFixedHeight(btn_h)
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

    # 用户点击模式按钮：(state_id, target_value)  — 保留信号兼容外部连接
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
        self._status_chips: Dict[int, EnumStatusChip] = {}
        self._is_dark = True
        self._theme = "dark"
        self._scale = "medium"

        self.setStyleSheet("background-color: transparent;")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(4)

        # M6: main_row 放进横向滚动区；超大字号/超多卡片时底部出现横滚条
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._inner = QWidget()
        self._scroll.setWidget(self._inner)
        outer.addWidget(self._scroll, 1)

        # 单行布局：KPI 卡片 + 模式按钮组并排，高度一致避免两行参差
        self._main_row = QHBoxLayout(self._inner)
        self._main_row.setContentsMargins(0, 0, 0, 0)
        self._main_row.setSpacing(6)

        # 兼容旧调用点：保留两个子 layout 引用（实际都放进 _main_row）
        self._cards_row = QHBoxLayout()
        self._cards_row.setSpacing(6)
        self._modes_row = QHBoxLayout()
        self._modes_row.setSpacing(6)
        self._main_row.addLayout(self._cards_row, 1)
        self._main_row.addLayout(self._modes_row, 0)

        self._empty_label = QLabel("等待设备握手…")
        self._empty_label.setAlignment(Qt.AlignCenter)
        outer.addWidget(self._empty_label)

        self.set_theme("dark", "medium")

        profile_store.profile_changed.connect(self._on_profile_changed)
        state_store.state_changed.connect(self._on_state_changed)

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light", self._scale)

    def set_theme(self, theme: str = "dark", scale: str = "medium") -> None:
        self._theme = S._normalize_theme(theme)
        self._is_dark = self._theme != "light"
        self._scale = scale
        p = S.palette(self._theme)
        empty_px = S.font_px(12, scale)
        self._empty_label.setStyleSheet(
            f"color: {p['text_faint']}; padding: 12px; font-size: {empty_px}px;"
        )
        # 滚动区背景、Dashboard 高度随档位放大（给卡片 + 横滚条留位）
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background-color: transparent; border: none; }}"
        )
        self._inner.setStyleSheet("background-color: transparent;")
        # KpiCard 在 xlarge 档会把自己撑到 ~100px 高，外框留 ~20px scrollbar + margin
        card_h = S.font_px(KpiCard._VALUE_BASE_PX, scale) + 34  # 与 KpiCard._apply_font 同步
        self.setMinimumHeight(card_h + 20)
        for card in self._cards.values():
            card.set_theme(self._theme, scale)
        for group in self._mode_groups.values():
            group.set_theme(self._theme, scale)
        for chip in self._status_chips.values():
            chip.set_theme(self._theme, scale)

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
        if hw_type != self._current_hw:
            return
        # 幂等：关键 profile 子集（critical channels）未变时跳过
        channels = [c for c in self._profile.get_channels(hw_type) if c.critical]
        new_sig = tuple(
            (c.channel_id, c.name, c.unit, c.display_min, c.display_max) for c in channels
        )
        if getattr(self, "_dash_signature", None) == new_sig and self._cards:
            return
        self._dash_signature = new_sig
        self._rebuild()

    def _on_state_changed(self, hw_type: str, state_id: int, value: int, _old: int) -> None:
        if hw_type != self._current_hw:
            return
        group = self._mode_groups.get(state_id)
        if group is not None:
            group.update_value(value)
        chip = self._status_chips.get(state_id)
        if chip is not None:
            chip.update_value(value)

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
        for chip in self._status_chips.values():
            self._modes_row.removeWidget(chip)
            chip.deleteLater()
        self._status_chips.clear()

    def _rebuild(self) -> None:
        self._clear()
        if self._current_hw is None:
            self._empty_label.setText("等待设备握手…")
            self._empty_label.show()
            return

        channels = [c for c in self._profile.get_channels(self._current_hw) if c.critical]

        if not channels:
            self._empty_label.setText(
                f"[{self._current_hw}] profile 无 critical channel"
            )
            self._empty_label.show()
            return
        self._empty_label.hide()

        # ---- KPI 卡片 ----
        for ch in channels:
            card = KpiCard(ch)
            card.set_theme(self._theme, self._scale)
            self._cards_row.addWidget(card)
            self._cards[ch.channel_id] = card
        self._cards_row.addStretch(1)
