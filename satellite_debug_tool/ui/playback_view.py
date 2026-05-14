"""PlaybackView —— 离线 .sdb 回放 Tab（M7-S5 占位，S5 阶段填充功能）。

S4 阶段先做占位，让 MainWindow Tab 容器能挂上来；
S5 阶段补完：独立 DataStore + ProfileStore + 接 TimeRangeControl + 完整导入流程。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from satellite_debug_tool.ui import styles as S


class PlaybackView(QWidget):
    status_message = Signal(str, int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme = "dark"
        self._placeholder = QLabel("回放 Tab — S5 阶段实现（独立 DataStore + 时间范围选择）")
        self._placeholder.setAlignment(Qt.AlignCenter)
        layout = QVBoxLayout(self)
        layout.addWidget(self._placeholder)
        self._apply_theme()

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
        self._apply_theme()

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light")

    def _apply_theme(self):
        p = S.palette(self._theme)
        self.setStyleSheet(f"background-color: {p['bg']}; color: {p['text_muted']};")
        self._placeholder.setStyleSheet(f"color: {p['text_faint']}; font-size: 14px;")
