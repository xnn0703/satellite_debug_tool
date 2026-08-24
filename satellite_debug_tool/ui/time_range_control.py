"""TimeRangeControl —— PlaybackView / LogView 共用的时间范围选择控件。

用户场景：导入 2 小时录制后，只想看其中某一段；或者快速跳到"最近 5 分钟"。

布局：
    [范围: ▼全部] 起 [0.0  ] s 终 [7200.0] s [应用]

- 预设：全部 / 最近 30 秒 / 最近 1 分钟 / 最近 5 分钟 / 最近 30 分钟 / 自定义
- 选预设 → 自动算 (start, end)，SpinBox 跟随更新，立刻 emit range_changed
- 选"自定义" → SpinBox 可编辑，要点"应用"才 emit；预设之外的预设按钮置灰

外部接口：
    set_total(total_sec)          数据总时长（一旦解析完成立刻调）
    set_range(start, end)         程序化设置（不 emit）
    current_range() -> (s, e)
    range_changed(start, end)     用户触发后才 emit
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.ui import styles as S


# (stable id, seconds-from-end);
# None = 显示全部；"custom" = 进入自定义模式。
_PRESETS: list[tuple[str, object]] = [
    ("all", None),
    ("last_30_seconds", 30.0),
    ("last_1_minute", 60.0),
    ("last_5_minutes", 300.0),
    ("last_30_minutes", 1800.0),
    ("custom", "custom"),
]


def _preset_label(preset_id: str) -> str:
    """返回可被 lupdate 静态提取的预设显示文案。"""
    return {
        "all": tr("All"),
        "last_30_seconds": tr("Last 30 seconds"),
        "last_1_minute": tr("Last 1 minute"),
        "last_5_minutes": tr("Last 5 minutes"),
        "last_30_minutes": tr("Last 30 minutes"),
        "custom": tr("Custom"),
    }[preset_id]


class TimeRangeControl(QWidget):
    range_changed = Signal(float, float)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._total_sec: float = 0.0
        self._theme = "dark"
        self._suppress_emit = False   # 程序化更新 SpinBox 时阻止 valueChanged 触发

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        self._lbl_prefix = QLabel(tr("Range:"))
        layout.addWidget(self._lbl_prefix)

        self._preset_combo = QComboBox()
        for preset_id, _ in _PRESETS:
            self._preset_combo.addItem(_preset_label(preset_id), preset_id)
        self._preset_combo.setCurrentIndex(0)   # 默认"全部"
        self._preset_combo.setFixedWidth(110)
        self._preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        layout.addWidget(self._preset_combo)

        self._lbl_start = QLabel(tr("Start"))
        layout.addWidget(self._lbl_start)
        self._spin_start = QDoubleSpinBox()
        self._spin_start.setDecimals(1)
        self._spin_start.setRange(0.0, 0.0)
        self._spin_start.setSingleStep(1.0)
        self._spin_start.setSuffix(" s")
        self._spin_start.setFixedWidth(90)
        self._spin_start.setEnabled(False)
        layout.addWidget(self._spin_start)

        self._lbl_end = QLabel(tr("End"))
        layout.addWidget(self._lbl_end)
        self._spin_end = QDoubleSpinBox()
        self._spin_end.setDecimals(1)
        self._spin_end.setRange(0.0, 0.0)
        self._spin_end.setSingleStep(1.0)
        self._spin_end.setSuffix(" s")
        self._spin_end.setFixedWidth(90)
        self._spin_end.setEnabled(False)
        layout.addWidget(self._spin_end)

        self._btn_apply = QPushButton(tr("Apply"))
        self._btn_apply.setFixedWidth(60)
        self._btn_apply.setEnabled(False)
        self._btn_apply.clicked.connect(self._on_apply_clicked)
        layout.addWidget(self._btn_apply)

        layout.addStretch(1)

        self._apply_theme()
        register_translatable(self)

    def retranslate_ui(self) -> None:
        self._lbl_prefix.setText(tr("Range:"))
        current = self._preset_combo.currentData()
        for index, (preset_id, _value) in enumerate(_PRESETS):
            self._preset_combo.setItemText(index, _preset_label(preset_id))
        selected = self._preset_combo.findData(current)
        if selected >= 0:
            self._preset_combo.setCurrentIndex(selected)
        self._lbl_start.setText(tr("Start"))
        self._lbl_end.setText(tr("End"))
        self._btn_apply.setText(tr("Apply"))

    # ------------------------------------------------------------------ API

    def set_total(self, total_sec: float) -> None:
        """设置数据总时长（来自解析后的真实时间跨度）。"""
        self._total_sec = max(0.0, float(total_sec))
        self._spin_start.setRange(0.0, self._total_sec)
        self._spin_end.setRange(0.0, self._total_sec)
        # 默认"全部"会立刻反映新 total
        self._apply_preset_silently(self._preset_combo.currentIndex())

    def set_range(self, start: float, end: float) -> None:
        """程序化设置范围（不 emit range_changed）。会自动切到"自定义"模式。"""
        self._suppress_emit = True
        try:
            self._preset_combo.setCurrentIndex(len(_PRESETS) - 1)   # 自定义
            self._spin_start.setValue(max(0.0, float(start)))
            self._spin_end.setValue(min(self._total_sec, float(end)))
            self._spin_start.setEnabled(True)
            self._spin_end.setEnabled(True)
            self._btn_apply.setEnabled(True)
        finally:
            self._suppress_emit = False

    def current_range(self) -> tuple[float, float]:
        return (self._spin_start.value(), self._spin_end.value())

    def set_theme(self, theme: str = "dark", scale: str = "small") -> None:
        self._theme = S._normalize_theme(theme)
        self._apply_theme()

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light")

    # --------------------------------------------------------------- signals

    def _on_preset_changed(self, idx: int) -> None:
        if self._suppress_emit:
            return
        self._apply_preset_silently(idx, emit=True)

    def _apply_preset_silently(self, idx: int, emit: bool = False) -> None:
        if idx < 0 or idx >= len(_PRESETS):
            return
        _preset_id, value = _PRESETS[idx]
        custom_mode = value == "custom"
        self._spin_start.setEnabled(custom_mode)
        self._spin_end.setEnabled(custom_mode)
        self._btn_apply.setEnabled(custom_mode)

        if custom_mode:
            # 自定义：保持 SpinBox 当前值，等用户点应用
            return

        # 预设：算出 (start, end)
        total = self._total_sec
        if value is None:
            start, end = 0.0, total
        else:
            window = float(value)
            if total <= window:
                start, end = 0.0, total
            else:
                start, end = total - window, total

        self._suppress_emit = True
        try:
            self._spin_start.setValue(start)
            self._spin_end.setValue(end)
        finally:
            self._suppress_emit = False

        if emit and total > 0:
            self.range_changed.emit(start, end)

    def _on_apply_clicked(self) -> None:
        start = self._spin_start.value()
        end = self._spin_end.value()
        if end <= start:
            # 反向 / 相等：终往后挪 1s 兜底
            end = min(self._total_sec, start + 1.0)
            self._suppress_emit = True
            try:
                self._spin_end.setValue(end)
            finally:
                self._suppress_emit = False
        self.range_changed.emit(start, end)

    # ----------------------------------------------------------------- theme

    def _apply_theme(self) -> None:
        p = S.palette(self._theme)
        text = p["text"]
        input_bg = p["input_bg"]
        primary = p["primary"]
        for lbl in (self._lbl_prefix, self._lbl_start, self._lbl_end):
            lbl.setStyleSheet(f"color: {text}; background: transparent;")
        combo_css = (
            f"background-color: {input_bg}; color: {text}; "
            f"border: 1px solid {p['input_border']}; padding: 2px 6px; border-radius: 2px;"
        )
        self._preset_combo.setStyleSheet(combo_css)
        spin_css = (
            f"background-color: {input_bg}; color: {text}; "
            f"border: 1px solid {p['input_border']}; padding: 2px 4px; border-radius: 2px;"
        )
        self._spin_start.setStyleSheet(spin_css)
        self._spin_end.setStyleSheet(spin_css)
        self._btn_apply.setStyleSheet(
            f"background-color: {primary}; color: white; border: none; "
            f"padding: 2px 8px; border-radius: 2px;"
        )
