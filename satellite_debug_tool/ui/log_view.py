"""LogView —— WindTerm Log 导入解析 Tab（M7-S6 占位）。

S4 阶段先做占位；S6 阶段补完：WindTermLogParser + 虚拟 ProfileStore + 接
GroupedChartWidget + TimeRangeControl。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from satellite_debug_tool.ui import styles as S


class LogView(QWidget):
    status_message = Signal(str, int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme = "dark"
        self._placeholder = QLabel(
            "Log Tab — S6 阶段实现（WindTerm log 自动表头分列 + 绘曲线）"
        )
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
