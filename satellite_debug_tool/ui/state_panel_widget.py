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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.data import DataStore, StateStore
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    STATE_FLAG_INVERSE,
    ChannelDefEntry,
    StateDefEntry,
    StateType,
)
from satellite_debug_tool.i18n import (
    mark_raw_text,
    register_translatable,
    set_translatable_text,
    tr,
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

# A3: 最近变化 2 秒内边框高亮
_HIGHLIGHT_BORDER = "#FFC107"    # 琥珀
_HIGHLIGHT_MS = 2000


# A4: 子系统分组 — 从 state.name 前缀（下划线前）抽取
# 规则：
# - TRACE_MODE / TRACE_X → "trace"
# - MODEM_X / SNR_LOCK  → "modem"（SNR 视为 modem 子）
# - INS_READY / IMU_X   → "ins"
# - PLL_LOCKED / LO_X   → "rf"
# - LOCK_FLAG           → "trace"（业务上锁星属 trace）
# - 其它                → "general"
_SUBSYSTEM_RULES = [
    # (关键字, 子系统 key) —— 顺序敏感：更特定的关键字排前面，
    # 否则 "SNR_LOCKED" / "PLL_LOCKED" 会先命中 "lock" 被误归 trace。
    (("modem", "snr", "beacon"), "modem"),
    (("pll", "lo", "buc", "lnb", "polar", "pol"), "rf"),
    (("trace", "lock"),          "trace"),
    (("ins", "imu", "gps"),      "ins"),
]
_SUBSYSTEM_ORDER = ["trace", "modem", "rf", "ins", "general"]
# Compatibility export for callers that inspect the classification table.
# Runtime section titles come from _subsystem_label() so they can be translated.
_SUBSYSTEM_LABELS = {
    "trace": "跟踪 (Trace)",
    "modem": "调制解调 (Modem)",
    "ins": "导航 (INS / GPS)",
    "rf": "射频 (RF)",
    "general": "其它 (General)",
}


def _subsystem_label(key: str) -> str:
    return {
        "trace": tr("Tracking"),
        "modem": tr("Modem"),
        "ins": tr("Navigation (INS / GPS)"),
        "rf": tr("RF"),
        "general": tr("Other"),
    }[key]


def _classify_subsystem(name: str) -> str:
    """把 state.name 分到子系统桶。

    匹配 _ 切分的 token；比对规则：
    - 2 字符及以下的短关键字（如 "lo"）走**精确**匹配，避免 "LOCK" 被误归 "LO"
    - 3 字符及以上走**前缀**匹配（覆盖 "PLL_LOCKED" → "pll"）
    规则顺序：modem > rf > trace > ins，避免 "SNR_LOCKED" 被 lock 抢走。
    """
    tokens = [t.lower() for t in name.split("_") if t]
    if not tokens:
        return "general"
    for keys, sub in _SUBSYSTEM_RULES:
        for k in keys:
            for tok in tokens:
                if len(k) <= 2:
                    if tok == k:
                        return sub
                else:
                    if tok.startswith(k):
                        return sub
    return "general"


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
        self._theme = "dark"
        self._scale = "medium"
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self._clear_highlight)
        layout = QGridLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(0)

        self._dot = QLabel()
        self._dot.setStyleSheet(_dot_stylesheet(_BOOL_OFF_COLOR))
        self._name_label = QLabel(entry.name)
        mark_raw_text(self._name_label)
        self._value_label = QLabel("—")
        mark_raw_text(self._value_label)
        self._value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        # M6: tooltip 显示 state_id / 类型 / flags / 枚举说明
        if entry.state_type == int(StateType.BOOL):
            tip = (
                f"[BOOL] state_id={entry.state_id}  {entry.name}\n"
                f"flags=0x{entry.flags:02X}"
                + (
                    tr("\n(INVERSE: 0=normal, 1=alarm)")
                    if entry.flags & STATE_FLAG_INVERSE
                    else ""
                )
            )
        else:
            enum_lines = "\n".join(
                f"  {e.value} = {e.name}  [lv={e.level}]" for e in entry.enums
            )
            tip = (
                f"[ENUM] state_id={entry.state_id}  {entry.name}\n"
                f"flags=0x{entry.flags:02X}\n"
                f"{enum_lines or tr('  (no enum items)')}"
            )
        self.setToolTip(tip)

        layout.addWidget(self._dot, 0, 0)
        layout.addWidget(self._name_label, 0, 1)
        layout.addWidget(self._value_label, 0, 2)
        layout.setColumnStretch(1, 1)

        self.set_theme(self._theme, self._scale)
        self.set_unknown()
        register_translatable(self)

    def retranslate_ui(self) -> None:
        entry = self._entry
        if entry.state_type == int(StateType.BOOL):
            suffix = (
                tr("\n(INVERSE: 0=normal, 1=alarm)")
                if entry.flags & STATE_FLAG_INVERSE
                else ""
            )
            tip = (
                f"[BOOL] state_id={entry.state_id}  {entry.name}\n"
                f"flags=0x{entry.flags:02X}{suffix}"
            )
        else:
            enum_lines = "\n".join(
                f"  {e.value} = {e.name}  [lv={e.level}]" for e in entry.enums
            )
            tip = (
                f"[ENUM] state_id={entry.state_id}  {entry.name}\n"
                f"flags=0x{entry.flags:02X}\n"
                f"{enum_lines or tr('  (no enum items)')}"
            )
        self.setToolTip(tip)

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light", self._scale)

    def _make_style(self, border: str) -> str:
        p = S.palette(self._theme)
        px = S.font_px(12, self._scale)
        return (
            f"QFrame {{ background-color: {p['card_alt']}; "
            f"border: 1px solid {border}; border-radius: 4px; padding: 4px 8px; }}"
            f"QLabel {{ border: none; color: {p['text']}; font-size: {px}px; }}"
        )

    def set_theme(self, theme: str = "dark", scale: str = "medium") -> None:
        self._theme = S._normalize_theme(theme)
        self._is_dark = self._theme != "light"
        self._scale = scale
        p = S.palette(self._theme)
        name_px = S.font_px(12, scale)
        # A3: 默认"无边框"视觉（边与背景同色），flash 时换琥珀色
        self._normal_border = p["card_alt"]
        self.setStyleSheet(self._make_style(self._normal_border))
        self._name_label.setStyleSheet(
            f"font-weight: 500; color: {p['text']}; font-size: {name_px}px;"
        )
        # value_label 颜色由 set_value/set_unknown 设置，不在此覆盖

    # ----- A3: 最近变化 2 秒高亮 -----

    def flash_highlight(self) -> None:
        """值变化时，外框高亮 2 秒后恢复。"""
        self.setStyleSheet(self._make_style(_HIGHLIGHT_BORDER))
        self._highlight_timer.start(_HIGHLIGHT_MS)

    def _clear_highlight(self) -> None:
        self.setStyleSheet(
            self._make_style(getattr(self, "_normal_border", "transparent"))
        )

    # ----- 更新 -----

    def set_unknown(self) -> None:
        """状态未知（未曾上报）。"""
        self._dot.setStyleSheet(_dot_stylesheet(_BOOL_OFF_COLOR))
        self._value_label.setText("—")
        p = S.palette(self._theme)
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


class ChannelItemRow(QFrame):
    """一行 channel 数值显示：左标签名 + 右侧数值（含单位）。

    用途：把 INS 姿态等关键 raw 数据放在状态栏侧边，跟 ENUM/BOOL 状态字一起看，
    供供应商联调使用（无需切换到图表 tab）。
    """

    def __init__(self, channel: ChannelDefEntry, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._channel = channel
        self._theme = "dark"
        self._scale = "medium"

        layout = QGridLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(0)

        # 跟 StateItemRow 视觉对齐：左圆点（恒灰色占位）+ name + value 右对齐
        self._dot = QLabel()
        self._dot.setStyleSheet(_dot_stylesheet("#3A6E66"))  # 青色，区分 state 圆点
        self._name_label = QLabel(channel.name)
        mark_raw_text(self._name_label)
        self._value_label = QLabel("—")
        mark_raw_text(self._value_label)
        self._value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        # tooltip 显示 channel 元信息
        tip = (
            f"[CHANNEL] channel_id={channel.channel_id}  {channel.name}\n"
            f"unit={channel.unit or tr('(none)')}  "
            f"range=[{channel.display_min}, {channel.display_max}]\n"
            f"group_id={channel.group_id}  flags=0x{channel.flags:02X}"
        )
        self.setToolTip(tip)

        layout.addWidget(self._dot, 0, 0)
        layout.addWidget(self._name_label, 0, 1)
        layout.addWidget(self._value_label, 0, 2)
        layout.setColumnStretch(1, 1)

        self.set_theme(self._theme, self._scale)
        register_translatable(self)

    def retranslate_ui(self) -> None:
        channel = self._channel
        self.setToolTip(
            f"[CHANNEL] channel_id={channel.channel_id}  {channel.name}\n"
            f"unit={channel.unit or tr('(none)')}  "
            f"range=[{channel.display_min}, {channel.display_max}]\n"
            f"group_id={channel.group_id}  flags=0x{channel.flags:02X}"
        )

    def set_theme(self, theme: str = "dark", scale: str = "medium") -> None:
        self._theme = S._normalize_theme(theme)
        self._scale = scale
        p = S.palette(self._theme)
        px = S.font_px(12, scale)
        self.setStyleSheet(
            f"QFrame {{ background-color: {p['card_alt']}; "
            f"border: 1px solid {p['card_alt']}; border-radius: 4px; padding: 4px 8px; }}"
            f"QLabel {{ border: none; color: {p['text']}; font-size: {px}px; }}"
        )
        self._name_label.setStyleSheet(
            f"font-weight: 500; color: {p['text']}; font-size: {px}px;"
        )

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light", self._scale)

    def set_value(self, value: Optional[float]) -> None:
        """更新数值显示。value=None → 显示 '—'"""
        p = S.palette(self._theme)
        if value is None:
            self._value_label.setText("—")
            self._value_label.setStyleSheet(f"color: {p['text_faint']};")
            return
        # 精度：标准差/小量级用 2 位小数，其他 1 位小数
        unit = self._channel.unit or ""
        try:
            num = float(value)
        except (TypeError, ValueError):
            self._value_label.setText("—")
            self._value_label.setStyleSheet(f"color: {p['text_faint']};")
            return
        # 智能精度：|val| < 10 用 .2f，否则 .1f
        if abs(num) < 10.0:
            text = f"{num:+.2f}{unit}"
        else:
            text = f"{num:+.1f}{unit}"
        self._value_label.setText(text)
        # 数值非"未知"时用正常前景色
        self._value_label.setStyleSheet(
            f"color: {p['text']}; font-family: monospace; font-weight: 500;"
        )


class _SubsystemSection(QFrame):
    """A4: 一个可折叠的子系统分组。header 带 ▶/▼ 三角 + 名称 + (n 项) 计数。"""

    def __init__(self, key: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._key = key
        self._collapsed = False
        self._count = 0
        self._theme = "dark"
        self._scale = "medium"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        self._header_btn = QPushButton()
        self._header_btn.setCheckable(False)
        self._header_btn.setCursor(Qt.PointingHandCursor)
        self._header_btn.clicked.connect(self._toggle)
        outer.addWidget(self._header_btn)

        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(3)
        outer.addWidget(self._body)
        self.set_count(0)
        register_translatable(self)

    # ----- Public -----

    def add_row(self, row: QWidget) -> None:
        self._body_layout.addWidget(row)

    def clear_rows(self) -> None:
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

    def set_count(self, n: int) -> None:
        self._count = n
        arrow = "▼" if not self._collapsed else "▶"
        self._header_btn.setText(f"{arrow} {_subsystem_label(self._key)}  ({n})")

    def retranslate_ui(self) -> None:
        self.set_count(self._count)

    def _toggle(self) -> None:
        self._collapsed = not self._collapsed
        self._body.setVisible(not self._collapsed)
        # header 文本更新
        self.set_count(self._body_layout.count())

    def set_theme(self, theme: str, scale: str) -> None:
        self._theme = S._normalize_theme(theme)
        self._scale = scale
        p = S.palette(self._theme)
        px = S.font_px(12, scale)
        self._header_btn.setStyleSheet(
            f"QPushButton {{ background-color: {p['panel']}; color: {p['text_muted']}; "
            f"border: none; text-align: left; padding: 4px 6px; "
            f"font-size: {px}px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: {p['card_alt']}; color: {p['text']}; }}"
        )


class StatePanelWidget(QScrollArea):
    """整体状态灯板：跟随 ProfileStore / StateStore 自动刷新。"""

    # 哪些 channel 关键字会被加到 state_panel 侧边一起显示
    #   (跟 _SUBSYSTEM_RULES 同前缀机制；仅 "ins" / "gps" 关键字暴露到 panel，
    #    避免把图表用的 roll/pitch/yaw 全堆到侧边)
    _CHANNEL_SIDEBAR_KEYS = ("ins", "gps", "imu")

    def __init__(
        self,
        profile_store: ProfileStore,
        state_store: StateStore,
        parent: Optional[QWidget] = None,
        data_store: Optional[DataStore] = None,
    ):
        super().__init__(parent)
        self._profile = profile_store
        self._states = state_store
        self._data = data_store
        self._rows: Dict[int, StateItemRow] = {}
        self._channel_rows: Dict[int, ChannelItemRow] = {}  # channel_id → row
        self._sections: Dict[str, _SubsystemSection] = {}   # A4
        self._current_hw: Optional[str] = None
        self._is_dark = True
        self._theme = "dark"
        self._scale = "medium"

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._vlayout = QVBoxLayout(self._container)
        self._vlayout.setContentsMargins(6, 6, 6, 6)
        self._vlayout.setSpacing(4)
        self._vlayout.addStretch(1)
        self.setWidget(self._container)

        self._empty_label = QLabel(tr("Waiting for device handshake..."))
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._vlayout.insertWidget(0, self._empty_label)

        self.set_theme("dark", "medium")

        profile_store.profile_changed.connect(self._on_profile_changed)
        state_store.state_changed.connect(self._on_state_changed)
        state_store.state_cleared.connect(self._on_state_cleared)
        register_translatable(self)

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light", self._scale)

    def set_theme(self, theme: str = "dark", scale: str = "medium") -> None:
        self._theme = S._normalize_theme(theme)
        self._is_dark = self._theme != "light"
        self._scale = scale
        p = S.palette(self._theme)
        self.setStyleSheet(
            f"QScrollArea {{ background-color: {p['bg']}; border: 1px solid {p['border']}; }}"
        )
        self._container.setStyleSheet(f"background-color: {p['bg']};")
        empty_px = S.font_px(12, scale)
        self._empty_label.setStyleSheet(
            f"color: {p['text_faint']}; padding: 16px; font-size: {empty_px}px;"
        )
        for row in self._rows.values():
            row.set_theme(self._theme, scale)
        for ch_row in self._channel_rows.values():
            ch_row.set_theme(self._theme, scale)
        for sec in self._sections.values():
            sec.set_theme(self._theme, scale)

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
        # 幂等：states + channels 表签名未变时跳过（避免上游重复 emit 导致整板闪烁）
        new_sig = (
            tuple(
                (s.state_id, s.name, s.state_type, s.flags,
                 tuple((e.value, e.level, e.name) for e in s.enums))
                for s in self._profile.get_states(hw_type)
            ),
            # §debug: 加入 channel 表签名 — 否则 channel 改了但 state 没变，UI 不会更新
            tuple(
                (c.channel_id, c.name, c.unit, c.flags)
                for c in self._profile.get_channels(hw_type)
            ),
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
            # A3: 最近变化项短暂高亮
            row.flash_highlight()

    def _on_state_cleared(self, hw_type: object) -> None:
        if self._current_hw is None:
            return
        if hw_type is None or hw_type == self._current_hw:
            self._rebuild()

    # ----- 构建 -----

    def _clear_rows(self) -> None:
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        for row in self._channel_rows.values():
            row.setParent(None)
            row.deleteLater()
        self._channel_rows.clear()
        for sec in self._sections.values():
            self._vlayout.removeWidget(sec)
            sec.deleteLater()
        self._sections.clear()

    def _rebuild(self) -> None:
        self._clear_rows()
        if self._current_hw is None:
            set_translatable_text(
                "Waiting for device handshake...", self._empty_label
            )
            self._empty_label.show()
            return

        states = self._profile.get_states(self._current_hw)
        if not states:
            set_translatable_text(
                "[{hardware}] STATE_DEFINE has not been received",
                self._empty_label,
                hardware=self._current_hw,
            )
            self._empty_label.show()
            return

        self._empty_label.hide()

        # A4: 按子系统分桶
        buckets: Dict[str, list[StateDefEntry]] = {k: [] for k in _SUBSYSTEM_ORDER}
        for entry in states:
            buckets[_classify_subsystem(entry.name)].append(entry)

        # §debug: 同样按子系统分桶 channel —— 仅把 ins/gps/imu 关键字的 channel 加到侧边
        # （roll/pitch/yaw 等通用姿态 channel 仍只在图表显示，避免侧边堆积）
        ch_buckets: Dict[str, list[ChannelDefEntry]] = {k: [] for k in _SUBSYSTEM_ORDER}
        all_channels = self._profile.get_channels(self._current_hw) if self._current_hw else []
        for ch in all_channels:
            sub = _classify_subsystem(ch.name)
            # 只把白名单关键字（ins/gps/imu）的 channel 加到侧边显示
            tokens = [t.lower() for t in ch.name.split("_") if t]
            if any(tok.startswith(k) for k in self._CHANNEL_SIDEBAR_KEYS for tok in tokens):
                ch_buckets[sub].append(ch)

        insert_pos = 0
        for sub_key in _SUBSYSTEM_ORDER:
            entries = buckets[sub_key]
            channels = ch_buckets[sub_key]
            if not entries and not channels:
                continue
            section = _SubsystemSection(sub_key)
            section.set_theme(self._theme, self._scale)
            # 先 state（ENUM/BOOL），再 channel（数值）—— 让供应商先看状态字，再看数值细节
            for entry in entries:
                row = StateItemRow(entry)
                row.set_theme(self._theme, self._scale)
                section.add_row(row)
                self._rows[entry.state_id] = row
                current = self._states.get_value(self._current_hw, entry.state_id)
                if current is not None:
                    row.set_value(current)
            for ch in channels:
                ch_row = ChannelItemRow(ch)
                ch_row.set_theme(self._theme, self._scale)
                section.add_row(ch_row)
                self._channel_rows[ch.channel_id] = ch_row
                # 初值回灌（如果 data_store 已有该 channel 数据）
                self._refresh_one_channel(ch_row, ch.channel_id)
            section.set_count(len(entries) + len(channels))
            self._vlayout.insertWidget(insert_pos, section)
            self._sections[sub_key] = section
            insert_pos += 1

    def retranslate_ui(self) -> None:
        if self._current_hw is None:
            set_translatable_text(
                "Waiting for device handshake...", self._empty_label
            )
        elif not self._profile.get_states(self._current_hw):
            set_translatable_text(
                "[{hardware}] STATE_DEFINE has not been received",
                self._empty_label,
                hardware=self._current_hw,
            )

    def _refresh_one_channel(self, row: ChannelItemRow, channel_id: int) -> None:
        """从 data_store 读 channel 最新值并更新一行 UI（保持纯函数性，不抛异常）。"""
        if self._data is None:
            row.set_value(None)
            return
        buf = self._data.get_channel_by_id(channel_id)
        if buf is None:
            row.set_value(None)
            return
        latest = buf.get_latest()
        if latest is None:
            row.set_value(None)
            return
        # (timestamp, value) tuple
        row.set_value(float(latest[1]))

    def refresh_channel_values(self) -> None:
        """供 MainWindow 5Hz timer 调用：刷新所有侧边 channel 的最新数值。
        state 行不刷（state_changed 信号已经实时推送）。"""
        for channel_id, row in self._channel_rows.items():
            self._refresh_one_channel(row, channel_id)
