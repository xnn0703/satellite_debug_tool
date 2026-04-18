"""
EventTimelineWidget — 事件时间线面板。

- 顶部：过滤栏（level 下拉 + 关键字输入 + 清空按钮）
- 主体：QListWidget 反序（最新在顶部），每行色标 + 时间 + 名字 + payload 预览
- 订阅 EventLog.event_added；过滤变更时全量重绘
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.data import EventLog, EventRecord
from satellite_debug_tool.ui import styles as S


_LEVEL_COLORS = {
    0: "#808080",   # DEBUG 灰
    1: "#4EC9B0",   # INFO 绿
    2: "#DCDCAA",   # WARN 黄
    3: "#F14C4C",   # ERROR 红
}
_LEVEL_NAMES = {0: "DBG", 1: "INF", 2: "WRN", 3: "ERR"}


class EventTimelineWidget(QWidget):
    def __init__(self, log: EventLog, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._log = log
        self._min_level = 0
        self._keyword = ""
        self._is_dark = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        # ---- 过滤栏 ----
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(6)

        self._level_combo = QComboBox()
        self._level_combo.addItem("All", 0)
        self._level_combo.addItem("INFO+", 1)
        self._level_combo.addItem("WARN+", 2)
        self._level_combo.addItem("ERROR", 3)
        self._level_combo.setFixedWidth(80)
        self._level_combo.currentIndexChanged.connect(self._on_filter_changed)

        self._keyword_edit = QLineEdit()
        self._keyword_edit.setPlaceholderText("筛选事件名…")
        self._keyword_edit.textChanged.connect(self._on_keyword_changed)

        self._clear_btn = QPushButton("清空")
        self._clear_btn.setFixedWidth(60)
        self._clear_btn.clicked.connect(self._on_clear_clicked)

        filter_bar.addWidget(QLabel("级别:"))
        filter_bar.addWidget(self._level_combo)
        filter_bar.addWidget(self._keyword_edit, 1)
        filter_bar.addWidget(self._clear_btn)
        outer.addLayout(filter_bar)

        # ---- 列表 ----
        self._list = QListWidget()
        self._list.setAlternatingRowColors(False)
        outer.addWidget(self._list, 1)

        # ---- 底部计数 ----
        self._count_label = QLabel("0 events")
        outer.addWidget(self._count_label)

        self.set_dark_theme(True)

        # 订阅
        log.event_added.connect(self._on_event_added)

    def set_dark_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        p = S.palette(is_dark)
        item_border = "#333333" if is_dark else "#E8E8E8"
        self.setStyleSheet(
            f"QWidget {{ background-color: {p['bg']}; color: {p['text']}; }}"
            f"QListWidget {{ background-color: {p['card']}; border: 1px solid {p['border']}; }}"
            f"QListWidget::item {{ padding: 4px; border-bottom: 1px solid {item_border}; }}"
            f"QComboBox, QLineEdit {{ background-color: {p['input_bg']}; color: {p['text']}; "
            f"border: 1px solid {p['input_border']}; border-radius: 3px; padding: 2px 4px; }}"
            f"QPushButton {{ background-color: {p['input_bg']}; color: {p['text']}; "
            f"border: 1px solid {p['input_border']}; border-radius: 3px; padding: 2px 8px; }}"
            f"QPushButton:hover {{ background-color: {p['card_alt']}; }}"
        )
        self._count_label.setStyleSheet(f"color: {p['text_muted']}; padding: 2px 4px;")

    # ----- 事件 -----

    def _on_event_added(self, record: EventRecord) -> None:
        if self._passes_filter(record):
            self._insert_record_top(record)
        self._update_count()

    def _on_filter_changed(self) -> None:
        self._min_level = int(self._level_combo.currentData())
        self._rebuild()

    def _on_keyword_changed(self, text: str) -> None:
        self._keyword = text.strip().lower()
        self._rebuild()

    def _on_clear_clicked(self) -> None:
        self._log.clear()
        self._list.clear()
        self._update_count()

    # ----- 过滤 / 渲染 -----

    def _passes_filter(self, rec: EventRecord) -> bool:
        if rec.level < self._min_level:
            return False
        if self._keyword and self._keyword not in rec.name.lower():
            return False
        return True

    def _format_row(self, rec: EventRecord) -> str:
        t = datetime.fromtimestamp(rec.wallclock).strftime("%H:%M:%S.%f")[:-3]
        tag = _LEVEL_NAMES.get(rec.level, "?")
        hw = rec.hw_type
        payload_preview = ""
        if rec.payload:
            try:
                payload_preview = " · " + rec.payload.decode("utf-8")
            except UnicodeDecodeError:
                payload_preview = f" · <{len(rec.payload)}B>"
        return f"{t}  [{tag}] {hw}  {rec.name}{payload_preview}"

    def _insert_record_top(self, rec: EventRecord) -> None:
        item = QListWidgetItem(self._format_row(rec))
        color = QColor(_LEVEL_COLORS.get(rec.level, "#CCCCCC"))
        item.setForeground(color)
        self._list.insertItem(0, item)

    def _rebuild(self) -> None:
        """用当前过滤条件重刷列表；按新→旧顺序。"""
        self._list.clear()
        for rec in reversed(list(self._log.all())):
            if not self._passes_filter(rec):
                continue
            item = QListWidgetItem(self._format_row(rec))
            item.setForeground(QColor(_LEVEL_COLORS.get(rec.level, "#CCCCCC")))
            self._list.addItem(item)
        self._update_count()

    def _update_count(self) -> None:
        total = len(self._log)
        shown = self._list.count()
        if total == shown:
            self._count_label.setText(f"{total} events")
        else:
            self._count_label.setText(f"{shown}/{total} events (filtered)")
