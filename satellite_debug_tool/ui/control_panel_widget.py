"""
ControlPanelWidget — 简易控制面板：采样率 / 用户标记 / 重置统计。

信号统一由 MainWindow 消费 → 通过 worker.send() 发 CONTROL 子命令。
TRACE_MODE 切换由 DashboardWidget 的模式按钮组提供，此面板不重复。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.ui import styles as S


_SAMPLE_RATES = [5, 10, 25, 50, 100, 200]
_DEFAULT_SAMPLE_RATE = 25


class _ChannelEnableDialog(QDialog):
    """A5: 通道使能位掩码选择对话框（profile 驱动）。

    以网格排列 profile 里所有通道的复选框，用户勾选后点"应用"→
    生成 64 位 bitmask（当前仅用低 16 位）发给 ControlPanel。
    """

    def __init__(
        self,
        profile_store: ProfileStore,
        hw_type: Optional[str],
        current_mask: int,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(tr("Channel enable"))
        self.setMinimumWidth(420)
        self._checks: dict[int, QCheckBox] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        self._hint = QLabel(
            tr(
                "Only selected DATA_REPORT channels are sampled after "
                "CONTROL.CHANNEL_ENABLE_MASK is sent."
            )
        )
        self._hint.setWordWrap(True)
        outer.addWidget(self._hint)

        # 全选 / 反选 按钮
        tool = QHBoxLayout()
        self._btn_all = QPushButton(tr("Select all"))
        self._btn_none = QPushButton(tr("Select none"))
        self._btn_invert = QPushButton(tr("Invert selection"))
        self._btn_all.clicked.connect(lambda: self._set_all(True))
        self._btn_none.clicked.connect(lambda: self._set_all(False))
        self._btn_invert.clicked.connect(self._invert)
        tool.addWidget(self._btn_all)
        tool.addWidget(self._btn_none)
        tool.addWidget(self._btn_invert)
        tool.addStretch(1)
        outer.addLayout(tool)

        # 通道网格（带滚动，防止通道多时超屏）
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)

        channels = []
        if hw_type is not None:
            channels = profile_store.get_channels(hw_type)

        if not channels:
            self._empty_label = QLabel(
                tr("(CHANNEL_DEFINE has not been received)")
            )
            grid.addWidget(self._empty_label, 0, 0)
        else:
            self._empty_label = None
            cols = 2
            for i, ch in enumerate(channels):
                cb = QCheckBox(
                    f"#{ch.channel_id}  {ch.name}"
                    + (f" ({ch.unit})" if ch.unit else "")
                )
                cb.setChecked(bool(current_mask & (1 << ch.channel_id)))
                cb.setToolTip(
                    f"channel_id={ch.channel_id}  group={ch.group_id}\n"
                    f"range=[{ch.display_min}, {ch.display_max}]"
                )
                self._checks[ch.channel_id] = cb
                grid.addWidget(cb, i // cols, i % cols)
        scroll.setWidget(host)
        outer.addWidget(scroll, 1)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, Qt.Horizontal, self
        )
        self._buttons.button(QDialogButtonBox.Ok).setText(tr("Apply"))
        self._buttons.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)
        register_translatable(self)

    def retranslate_ui(self) -> None:
        self.setWindowTitle(tr("Channel enable"))
        self._hint.setText(
            tr(
                "Only selected DATA_REPORT channels are sampled after "
                "CONTROL.CHANNEL_ENABLE_MASK is sent."
            )
        )
        self._btn_all.setText(tr("Select all"))
        self._btn_none.setText(tr("Select none"))
        self._btn_invert.setText(tr("Invert selection"))
        if self._empty_label is not None:
            self._empty_label.setText(
                tr("(CHANNEL_DEFINE has not been received)")
            )
        self._buttons.button(QDialogButtonBox.Ok).setText(tr("Apply"))
        self._buttons.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))

    def _set_all(self, on: bool) -> None:
        for cb in self._checks.values():
            cb.setChecked(on)

    def _invert(self) -> None:
        for cb in self._checks.values():
            cb.setChecked(not cb.isChecked())

    def result_mask(self) -> int:
        m = 0
        for cid, cb in self._checks.items():
            if cb.isChecked():
                m |= (1 << cid)
        return m


class ControlPanelWidget(QFrame):
    sample_rate_changed = Signal(int)                    # Hz
    user_mark_requested = Signal(int, str)               # mark_id, text
    reset_stats_requested = Signal()
    channel_enable_changed = Signal(int)                 # A5: 64bit bitmask

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._mark_counter = 0
        self._enabled = False
        self._is_dark = True
        self._theme = "dark"
        self._scale = "medium"
        # A5
        self._profile_store: Optional[ProfileStore] = None
        self._hw_type: Optional[str] = None
        self._channel_mask = 0xFFFFFFFFFFFFFFFF   # 默认全使能

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        # ---- 采样率 ----
        self._sample_rate_label = QLabel(tr("Sample rate:"))
        row.addWidget(self._sample_rate_label)
        self._rate_combo = QComboBox()
        for hz in _SAMPLE_RATES:
            self._rate_combo.addItem(f"{hz} Hz", hz)
        self._rate_combo.setCurrentIndex(_SAMPLE_RATES.index(_DEFAULT_SAMPLE_RATE))
        self._rate_combo.setFixedWidth(80)
        self._rate_combo.setToolTip(
            tr("Send CONTROL.SET_SAMPLE_RATE to set the device DATA_REPORT rate")
        )
        self._rate_combo.currentIndexChanged.connect(self._on_rate_changed)
        row.addWidget(self._rate_combo)

        # ---- 用户标记 ----
        row.addSpacing(10)
        self._marker_label = QLabel(tr("Marker:"))
        row.addWidget(self._marker_label)
        self._mark_edit = QLineEdit()
        self._mark_edit.setPlaceholderText(tr("Example: passed intersection A"))
        self._mark_edit.setToolTip(
            tr("Enter marker text and press Enter, or leave it blank and use the button")
        )
        self._mark_edit.returnPressed.connect(self._on_mark_clicked)
        row.addWidget(self._mark_edit, 1)

        self._mark_btn = QPushButton(tr("⚑ Add marker"))
        self._mark_btn.setToolTip(
            tr(
                "Send CONTROL.USER_MARK; a marker line appears after the device "
                "returns EVENT(0xFFFF)"
            )
        )
        self._mark_btn.clicked.connect(self._on_mark_clicked)
        row.addWidget(self._mark_btn)

        # ---- 通道使能 (A5) ----
        self._ch_enable_btn = QPushButton(tr("Channel enable..."))
        self._ch_enable_btn.setToolTip(
            tr(
                "Choose which DATA channels the device samples and reports "
                "(CONTROL.CHANNEL_ENABLE_MASK)"
            )
        )
        self._ch_enable_btn.clicked.connect(self._on_channel_enable_clicked)
        row.addWidget(self._ch_enable_btn)

        # ---- 统计复位 ----
        self._reset_btn = QPushButton(tr("Reset statistics"))
        self._reset_btn.setToolTip(
            tr("Send CONTROL.RESET_STATS to clear device counters and accumulators")
        )
        self._reset_btn.clicked.connect(self.reset_stats_requested.emit)
        row.addWidget(self._reset_btn)

        self.set_theme("dark", "medium")
        self.set_enabled(False)
        register_translatable(self)

    def retranslate_ui(self) -> None:
        self._sample_rate_label.setText(tr("Sample rate:"))
        self._rate_combo.setToolTip(
            tr("Send CONTROL.SET_SAMPLE_RATE to set the device DATA_REPORT rate")
        )
        self._marker_label.setText(tr("Marker:"))
        self._mark_edit.setPlaceholderText(tr("Example: passed intersection A"))
        self._mark_edit.setToolTip(
            tr(
                "Enter marker text and press Enter, or leave it blank and use the button"
            )
        )
        self._mark_btn.setText(tr("⚑ Add marker"))
        self._mark_btn.setToolTip(
            tr(
                "Send CONTROL.USER_MARK; a marker line appears after the device "
                "returns EVENT(0xFFFF)"
            )
        )
        self._ch_enable_btn.setText(tr("Channel enable..."))
        self._ch_enable_btn.setToolTip(
            tr(
                "Choose which DATA channels the device samples and reports "
                "(CONTROL.CHANNEL_ENABLE_MASK)"
            )
        )
        self._reset_btn.setText(tr("Reset statistics"))
        self._reset_btn.setToolTip(
            tr("Send CONTROL.RESET_STATS to clear device counters and accumulators")
        )

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light", self._scale)

    def set_theme(self, theme: str = "dark", scale: str = "medium") -> None:
        self._theme = S._normalize_theme(theme)
        self._is_dark = self._theme != "light"
        self._scale = scale
        p = S.palette(self._theme)
        px = S.font_px(12, scale)
        self.setStyleSheet(
            f"ControlPanelWidget {{ background-color: {p['card']}; "
            f"border: 1px solid {p['border']}; border-radius: 4px; }}"
            f"QLabel {{ color: {p['text']}; border: none; background: transparent; "
            f"font-size: {px}px; }}"
            f"QComboBox, QLineEdit {{ background-color: {p['input_bg']}; color: {p['text']}; "
            f"border: 1px solid {p['input_border']}; border-radius: 3px; padding: 3px 6px; "
            f"font-size: {px}px; }}"
            f"QPushButton {{ background-color: {p['input_bg']}; color: {p['text']}; "
            f"border: 1px solid {p['input_border']}; border-radius: 3px; padding: 4px 10px; "
            f"font-size: {px}px; }}"
            f"QPushButton:hover:enabled {{ background-color: {p['card_alt']}; }}"
            f"QPushButton:disabled {{ color: {p['text_faint']}; border-color: {p['border']}; }}"
        )

    # ---- Public ----

    def set_enabled(self, enabled: bool) -> None:
        """连接建立后 enable，断开后 disable。"""
        self._enabled = enabled
        self._rate_combo.setEnabled(enabled)
        self._mark_edit.setEnabled(enabled)
        self._mark_btn.setEnabled(enabled)
        self._reset_btn.setEnabled(enabled)
        self._ch_enable_btn.setEnabled(enabled)

    def current_sample_rate(self) -> int:
        return int(self._rate_combo.currentData())

    # ---- A5: 通道使能 ----

    def set_profile_store(self, store: ProfileStore) -> None:
        """注入 ProfileStore，用来列出可选通道。"""
        self._profile_store = store

    def set_hw_type(self, hw_type: Optional[str]) -> None:
        self._hw_type = hw_type

    def current_channel_mask(self) -> int:
        return self._channel_mask

    def _on_channel_enable_clicked(self) -> None:
        if not self._enabled or self._profile_store is None:
            return
        dlg = _ChannelEnableDialog(
            self._profile_store, self._hw_type, self._channel_mask, self
        )
        if dlg.exec() != QDialog.Accepted:
            return
        new_mask = dlg.result_mask()
        if new_mask == self._channel_mask:
            return
        self._channel_mask = new_mask
        self.channel_enable_changed.emit(new_mask)

    # ---- 内部 ----

    def _on_rate_changed(self) -> None:
        if self._enabled:
            self.sample_rate_changed.emit(self.current_sample_rate())

    def _on_mark_clicked(self) -> None:
        if not self._enabled:
            return
        text = self._mark_edit.text().strip()
        self._mark_counter = (self._mark_counter + 1) & 0xFFFF
        self.user_mark_requested.emit(self._mark_counter, text)
        self._mark_edit.clear()
