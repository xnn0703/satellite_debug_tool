"""
ControlPanelWidget — 简易控制面板：采样率 / 用户标记 / 重置统计。

信号统一由 MainWindow 消费 → 通过 worker.send() 发 CONTROL 子命令。
TRACE_MODE 切换由 DashboardWidget 的模式按钮组提供，此面板不重复。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)


_SAMPLE_RATES = [5, 10, 25, 50, 100, 200]


class ControlPanelWidget(QFrame):
    sample_rate_changed = Signal(int)         # Hz
    user_mark_requested = Signal(int, str)    # mark_id, text
    reset_stats_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._mark_counter = 0
        self._enabled = False

        self.setStyleSheet(
            "ControlPanelWidget { background-color: #252526; border: 1px solid #3C3C3C; "
            "border-radius: 4px; }"
            "QLabel { color: #CCCCCC; border: none; }"
            "QComboBox, QLineEdit { background-color: #333333; color: #CCCCCC; "
            "border: 1px solid #555555; border-radius: 3px; padding: 3px 6px; }"
            "QPushButton { background-color: #333333; color: #CCCCCC; "
            "border: 1px solid #555555; border-radius: 3px; padding: 4px 10px; }"
            "QPushButton:hover:enabled { background-color: #3E3E3E; }"
            "QPushButton:disabled { color: #666666; border-color: #333333; }"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        # ---- 采样率 ----
        row.addWidget(QLabel("采样率:"))
        self._rate_combo = QComboBox()
        for hz in _SAMPLE_RATES:
            self._rate_combo.addItem(f"{hz} Hz", hz)
        self._rate_combo.setCurrentIndex(_SAMPLE_RATES.index(100))
        self._rate_combo.setFixedWidth(80)
        self._rate_combo.currentIndexChanged.connect(self._on_rate_changed)
        row.addWidget(self._rate_combo)

        # ---- 用户标记 ----
        row.addSpacing(10)
        row.addWidget(QLabel("标记:"))
        self._mark_edit = QLineEdit()
        self._mark_edit.setPlaceholderText("例如：经过路口 A")
        self._mark_edit.returnPressed.connect(self._on_mark_clicked)
        row.addWidget(self._mark_edit, 1)

        self._mark_btn = QPushButton("⚑ 打标记")
        self._mark_btn.clicked.connect(self._on_mark_clicked)
        row.addWidget(self._mark_btn)

        # ---- 统计复位 ----
        self._reset_btn = QPushButton("复位统计")
        self._reset_btn.clicked.connect(self.reset_stats_requested.emit)
        row.addWidget(self._reset_btn)

        self.set_enabled(False)

    # ---- Public ----

    def set_enabled(self, enabled: bool) -> None:
        """连接建立后 enable，断开后 disable。"""
        self._enabled = enabled
        self._rate_combo.setEnabled(enabled)
        self._mark_edit.setEnabled(enabled)
        self._mark_btn.setEnabled(enabled)
        self._reset_btn.setEnabled(enabled)

    def current_sample_rate(self) -> int:
        return int(self._rate_combo.currentData())

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
