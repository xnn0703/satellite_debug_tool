"""ChannelPanel — 通道选择面板（M10 F3b）。

位置：LiveView 主体 QSplitter 左侧。
每条通道一行：[☐] ● name 当前值
顶部 "全选 / 清空" 按钮。

用户勾选 / 取消勾选时 emit `selection_changed(name, checked)`，
由 LiveView 决定是否在 chart 上画这条曲线。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.ui import styles as S


_DOT_SIZE = 10
_ROW_HEIGHT = 26


class _ChannelRow(QWidget):
    """一行通道项：[☑] ● name 当前值。

    点击行内空白处也能切换 checkbox（与点 checkbox 行为一致）。
    """

    toggled = Signal(str, bool)   # (name, checked)

    def __init__(
        self,
        name: str,
        color: str,
        display_label: str,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._name = name
        self._color = color
        self.setFixedHeight(_ROW_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 6, 0)
        layout.setSpacing(6)

        self._checkbox = QCheckBox()
        self._checkbox.setChecked(True)
        self._checkbox.toggled.connect(self._on_checkbox_toggled)

        self._dot = QLabel()
        self._dot.setFixedSize(_DOT_SIZE, _DOT_SIZE)
        self._apply_dot_color()

        self._name_label = QLabel(display_label or name)
        self._name_label.setSizePolicy(self._name_label.sizePolicy().horizontalPolicy(),
                                      self._name_label.sizePolicy().verticalPolicy())

        self._value_label = QLabel("--")
        self._value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._value_label.setMinimumWidth(60)

        layout.addWidget(self._checkbox)
        layout.addWidget(self._dot)
        layout.addWidget(self._name_label, 1)
        layout.addWidget(self._value_label)

    def _on_checkbox_toggled(self, checked: bool) -> None:
        self.toggled.emit(self._name, checked)

    def _apply_dot_color(self) -> None:
        self._dot.setStyleSheet(
            f"background-color: {self._color}; border-radius: {_DOT_SIZE // 2}px;"
        )

    def mousePressEvent(self, event):  # noqa: N802 (Qt API)
        # 点击行内空白处也切换勾选（点 checkbox 本身不会冒泡到这里）
        if event.button() == Qt.MouseButton.LeftButton:
            self._checkbox.toggle()
            event.accept()
            return
        super().mousePressEvent(event)

    # ---- API ----

    def set_color(self, color: str) -> None:
        self._color = color
        self._apply_dot_color()

    def set_label(self, label: str) -> None:
        self._name_label.setText(label)

    def set_value(self, value_text: str) -> None:
        self._value_label.setText(value_text)

    def is_checked(self) -> bool:
        return self._checkbox.isChecked()

    def set_checked(self, checked: bool) -> None:
        # 用 blockSignals 避免 set_checked 也 emit toggled（避免循环）
        self._checkbox.blockSignals(True)
        self._checkbox.setChecked(checked)
        self._checkbox.blockSignals(False)

    def apply_theme_styles(self, p: dict, value_px: int, name_px: int) -> None:
        # 整行：默认透明 + hover 微高亮
        self.setStyleSheet(
            f"_ChannelRow {{ background-color: transparent; border-radius: 3px; }}"
            f"_ChannelRow:hover {{ background-color: {p['card_alt']}; }}"
        )
        self._name_label.setStyleSheet(
            f"color: {p['text']}; font-size: {name_px}px; background: transparent;"
        )
        self._value_label.setStyleSheet(
            f"color: {p['text_muted']}; font-size: {value_px}px; "
            f"font-family: Menlo, Consolas, monospace; background: transparent;"
        )
        self._checkbox.setStyleSheet(
            f"QCheckBox {{ background: transparent; }}"
        )


class ChannelPanel(QWidget):
    """垂直堆叠的通道选择面板。"""

    selection_changed = Signal(str, bool)   # (channel_name, checked)
    select_all_clicked = Signal()
    clear_all_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._rows: Dict[str, _ChannelRow] = {}
        self._theme = "dark"
        self._scale = "medium"

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # 标题
        self._title = QLabel("通道")
        root.addWidget(self._title)

        # 全选 / 清空
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self._btn_all = QPushButton("全选")
        self._btn_none = QPushButton("清空")
        self._btn_all.clicked.connect(self._on_select_all)
        self._btn_none.clicked.connect(self._on_clear_all)
        btn_row.addWidget(self._btn_all)
        btn_row.addWidget(self._btn_none)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        # 通道列表（垂直堆叠 in scroll area）
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._list_host = QWidget()
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(2)
        self._list_layout.addStretch(1)   # 行加到 stretch 之前，保证从顶部开始排
        self._scroll.setWidget(self._list_host)
        root.addWidget(self._scroll, 1)

        self.apply_theme(self._theme, self._scale)

    # ---- public API ----

    def add_channel(self, name: str, color: str, display_label: str = "") -> None:
        """添加一行；name 已存在则更新颜色 + label，不重复创建。"""
        if name in self._rows:
            row = self._rows[name]
            row.set_color(color)
            if display_label:
                row.set_label(display_label)
            return
        row = _ChannelRow(name, color, display_label or name)
        row.toggled.connect(self.selection_changed)
        # 插到 stretch（最后一个）之前
        self._list_layout.insertWidget(self._list_layout.count() - 1, row)
        self._rows[name] = row
        row.apply_theme_styles(
            S.palette(self._theme),
            S.font_px(10, self._scale),
            S.font_px(11, self._scale),
        )

    def remove_channel(self, name: str) -> None:
        row = self._rows.pop(name, None)
        if row is not None:
            self._list_layout.removeWidget(row)
            row.deleteLater()

    def set_label(self, name: str, label: str) -> None:
        row = self._rows.get(name)
        if row is not None:
            row.set_label(label)

    def update_value(self, name: str, value_text: str) -> None:
        row = self._rows.get(name)
        if row is not None:
            row.set_value(value_text)

    def is_checked(self, name: str) -> bool:
        row = self._rows.get(name)
        return row.is_checked() if row else False

    def channel_names(self) -> List[str]:
        return list(self._rows.keys())

    # ---- theme ----

    def apply_theme(self, theme: str = "dark", scale: str = "medium") -> None:
        self._theme = S._normalize_theme(theme)
        self._scale = scale
        p = S.palette(self._theme)
        title_px = S.font_px(12, scale)
        btn_px = S.font_px(11, scale)
        value_px = S.font_px(10, scale)
        name_px = S.font_px(11, scale)

        self.setStyleSheet(
            f"ChannelPanel {{ background-color: {p['panel']}; "
            f"border: 1px solid {p['border']}; border-radius: 2px; }}"
        )
        self._title.setStyleSheet(
            f"color: {p['text']}; font-weight: bold; font-size: {title_px}px;"
        )
        for btn in (self._btn_all, self._btn_none):
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {p['input_bg']}; color: {p['text']}; "
                f"border: 1px solid {p['input_border']}; border-radius: 2px; "
                f"padding: 2px 8px; font-size: {btn_px}px; }}"
                f"QPushButton:hover {{ background-color: {p['card_alt']}; }}"
            )
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {p['panel']}; border: none; }}"
        )
        self._list_host.setStyleSheet(f"background-color: {p['panel']};")
        for row in self._rows.values():
            row.apply_theme_styles(p, value_px, name_px)

    # ---- internal ----

    def _on_select_all(self) -> None:
        for name, row in self._rows.items():
            if not row.is_checked():
                row.set_checked(True)
                self.selection_changed.emit(name, True)
        self.select_all_clicked.emit()

    def _on_clear_all(self) -> None:
        for name, row in self._rows.items():
            if row.is_checked():
                row.set_checked(False)
                self.selection_changed.emit(name, False)
        self.clear_all_clicked.emit()
