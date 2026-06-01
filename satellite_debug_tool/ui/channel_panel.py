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
    QLineEdit,
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
        self._apply_checked_visual(checked)
        self.toggled.emit(self._name, checked)

    def _apply_dot_color(self) -> None:
        self._apply_checked_visual(self._checkbox.isChecked())

    def _apply_checked_visual(self, checked: bool) -> None:
        """未勾选 = 整行变暗（色块去饱和、文字弱化），所见即所得。"""
        # 色块：勾选用真彩，未勾用弱灰
        dot_color = self._color if checked else "#586976"
        self._dot.setStyleSheet(
            f"background-color: {dot_color}; border-radius: {_DOT_SIZE // 2}px;"
        )
        p = getattr(self, "_pal", None)
        if not p:
            return
        nm = p["text"] if checked else p["text_3"]
        vl = p["text_2"] if checked else p["text_3"]
        npx = getattr(self, "_name_px", 11)
        vpx = getattr(self, "_value_px", 10)
        mono = getattr(self, "_mono", "Menlo, Consolas, monospace")
        self._name_label.setStyleSheet(
            f"color: {nm}; font-size: {npx}px; background: transparent;"
        )
        self._value_label.setStyleSheet(
            f"color: {vl}; font-size: {vpx}px; font-family: {mono}; background: transparent;"
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
        self._apply_checked_visual(checked)

    def apply_theme_styles(self, p: dict, value_px: int, name_px: int) -> None:
        # 缓存供 _apply_checked_visual 复用
        self._pal = p
        self._name_px = name_px
        self._value_px = value_px
        try:
            from satellite_debug_tool.ui import styles as _S
            self._mono = f'"{_S.monospace_family()}"'
        except Exception:
            self._mono = "Menlo, Consolas, monospace"
        # 整行：默认透明 + hover 微高亮
        self.setStyleSheet(
            f"_ChannelRow {{ background-color: transparent; border-radius: 3px; }}"
            f"_ChannelRow:hover {{ background-color: {p['card_hover']}; }}"
        )
        self._checkbox.setStyleSheet("QCheckBox { background: transparent; }")
        # 按当前勾选态着色（含未勾变暗）
        self._apply_checked_visual(self._checkbox.isChecked())


class ChannelPanel(QWidget):
    """垂直堆叠的通道选择面板。"""

    selection_changed = Signal(str, bool)   # (channel_name, checked)
    select_all_clicked = Signal()
    clear_all_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._rows: Dict[str, _ChannelRow] = {}
        self._row_group: Dict[str, str] = {}        # name -> group title
        self._group_headers: Dict[str, QLabel] = {} # group -> header widget
        self._group_order: List[str] = []           # 首次出现顺序
        self._group_names: Dict[str, List[str]] = {} # group -> [names] 顺序
        self._theme = "dark"
        self._scale = "medium"

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 8, 6, 6)
        root.setSpacing(6)

        # 标题行：通道 + 计数徽标
        head_row = QHBoxLayout()
        head_row.setSpacing(6)
        self._title = QLabel("通道")
        self._count_label = QLabel("0 / 0")
        self._count_label.setObjectName("chCount")
        head_row.addWidget(self._title)
        head_row.addStretch(1)
        head_row.addWidget(self._count_label)
        root.addLayout(head_row)

        # 全选 / 清空 / 反选
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self._btn_all = QPushButton("全选")
        self._btn_all.setProperty("variant", "ghost")
        self._btn_none = QPushButton("清空")
        self._btn_none.setProperty("variant", "ghost")
        self._btn_invert = QPushButton("")
        self._btn_invert.setObjectName("chInvert")
        self._btn_invert.setProperty("variant", "ghost")
        self._btn_invert.setFixedSize(26, 24)
        self._btn_invert.setToolTip("反选")
        self._btn_all.clicked.connect(self._on_select_all)
        self._btn_none.clicked.connect(self._on_clear_all)
        self._btn_invert.clicked.connect(self._on_invert)
        btn_row.addWidget(self._btn_all)
        btn_row.addWidget(self._btn_none)
        btn_row.addStretch(1)
        btn_row.addWidget(self._btn_invert)
        root.addLayout(btn_row)

        # 搜索框
        self._search = QLineEdit()
        self._search.setObjectName("chSearch")
        self._search.setPlaceholderText("筛选通道…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        root.addWidget(self._search)

        # 通道列表（垂直堆叠 in scroll area）
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._list_host = QWidget()
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(1)
        self._list_layout.addStretch(1)   # 行加到 stretch 之前，保证从顶部开始排
        self._scroll.setWidget(self._list_host)
        root.addWidget(self._scroll, 1)

        self.apply_theme(self._theme, self._scale)

    # ---- public API ----

    def _make_group_header(self, group: str) -> QLabel:
        lbl = QLabel(group)
        lbl.setObjectName("chGroupLabel")
        p = S.palette(self._theme)
        lbl.setStyleSheet(
            f"#chGroupLabel {{ color: {p['text_3']}; font-size: {S.font_px(9, self._scale)}px; "
            f"font-weight: 600; padding: 10px 6px 4px; background: transparent; }}"
        )
        return lbl

    def _group_start_index(self, group: str) -> int:
        """该 group 块起始位置（标题插这里）= 之前所有组的 (标题+行) 之和。"""
        idx = 0
        for g in self._group_order:
            if g == group:
                return idx
            idx += 1 + len(self._group_names.get(g, []))
        return idx

    def _group_insert_index(self, group: str) -> int:
        """该 group 块末尾的插入索引（标题 + 已有行 之后）—— 新行插这里。"""
        idx = 0
        for g in self._group_order:
            idx += 1   # header
            idx += len(self._group_names.get(g, []))
            if g == group:
                return idx
        return idx

    def add_channel(self, name: str, color: str, display_label: str = "",
                    group: str = "") -> None:
        """添加一行；name 已存在则更新颜色 + label（+ group）。

        group 非空时按分组小节排列（带分组标题分隔线）；空则归到"其它"。
        """
        group = group or "其它"
        if name in self._rows:
            row = self._rows[name]
            row.set_color(color)
            if display_label:
                row.set_label(display_label)
            return
        # 确保分组标题存在
        if group not in self._group_headers:
            header = self._make_group_header(group)
            self._group_headers[group] = header
            self._group_order.append(group)
            self._group_names[group] = []
            # header 插到该 group 块起始位置（在其所有行之前）
            self._list_layout.insertWidget(self._group_start_index(group), header)
        row = _ChannelRow(name, color, display_label or name)
        row.toggled.connect(self.selection_changed)
        row.toggled.connect(lambda *_: self.update_count())
        insert_at = self._group_insert_index(group)
        self._list_layout.insertWidget(insert_at, row)
        self._rows[name] = row
        self._row_group[name] = group
        self._group_names[group].append(name)
        row.apply_theme_styles(
            S.palette(self._theme),
            S.font_px(10, self._scale),
            S.font_px(11, self._scale),
        )
        self.update_count()

    def remove_channel(self, name: str) -> None:
        row = self._rows.pop(name, None)
        if row is not None:
            self._list_layout.removeWidget(row)
            row.deleteLater()
            self.update_count()
        group = self._row_group.pop(name, None)
        if group and group in self._group_names:
            try:
                self._group_names[group].remove(name)
            except ValueError:
                pass
            # 组空了 → 删标题
            if not self._group_names[group]:
                header = self._group_headers.pop(group, None)
                if header is not None:
                    self._list_layout.removeWidget(header)
                    header.deleteLater()
                self._group_names.pop(group, None)
                if group in self._group_order:
                    self._group_order.remove(group)

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

    def update_count(self) -> None:
        """刷新计数徽标 "checked / total"。"""
        total = len(self._rows)
        on = sum(1 for r in self._rows.values() if r.is_checked())
        self._count_label.setText(f"{on} / {total}")

    # ---- theme ----

    def apply_theme(self, theme: str = "dark", scale: str = "medium") -> None:
        self._theme = S._normalize_theme(theme)
        self._scale = scale
        p = S.palette(self._theme)
        title_px = S.font_px(12, scale)
        value_px = S.font_px(10, scale)
        name_px = S.font_px(11, scale)
        mono = S.monospace_family()

        self.setStyleSheet(
            f"ChannelPanel {{ background-color: {p['panel']}; "
            f"border-right: 1px solid {p['border']}; }}"
        )
        self._title.setStyleSheet(
            f"color: {p['text']}; font-weight: 600; font-size: {title_px}px; "
            f"background: transparent;"
        )
        self._count_label.setStyleSheet(
            f"#chCount {{ color: {p['text_3']}; font-family: \"{mono}\"; "
            f"font-size: {S.font_px(10, scale)}px; background: transparent; }}"
        )
        # 全选/清空/反选：ghost 走全局 QSS，仅 repolish + 反选图标
        for btn in (self._btn_all, self._btn_none, self._btn_invert):
            btn.setStyleSheet("")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        try:
            from satellite_debug_tool.ui import icons as _ic
            self._btn_invert.setIcon(_ic.icon("layers", color=p["text_2"], size=13))
            self._search.addAction(
                _ic.icon("search", color=p["text_3"], size=13),
                QLineEdit.ActionPosition.LeadingPosition,
            ) if not getattr(self, "_search_icon_added", False) else None
            self._search_icon_added = True
        except Exception:
            pass
        self._search.setStyleSheet(
            f"#chSearch {{ background-color: {p['input_bg']}; color: {p['text']}; "
            f"border: 1px solid {p['border_2']}; border-radius: 5px; padding: 3px 8px; "
            f"font-size: {S.font_px(12, scale)}px; }}"
            f"#chSearch:focus {{ border-color: {p['accent']}; }}"
        )
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {p['panel']}; border: none; }}"
        )
        self._list_host.setStyleSheet(f"background-color: {p['panel']};")
        for group, header in self._group_headers.items():
            header.setStyleSheet(
                f"#chGroupLabel {{ color: {p['text_3']}; font-size: {S.font_px(9, scale)}px; "
                f"font-weight: 600; padding: 10px 6px 4px; background: transparent; }}"
            )
        for row in self._rows.values():
            row.apply_theme_styles(p, value_px, name_px)

    # ---- internal ----

    def _on_search(self, text: str) -> None:
        q = text.strip().lower()
        for name, row in self._rows.items():
            label = row._name_label.text().lower()
            row.setVisible(q in name.lower() or q in label)

    def _on_invert(self) -> None:
        for name, row in self._rows.items():
            new = not row.is_checked()
            row.set_checked(new)
            self.selection_changed.emit(name, new)
        self.update_count()

    def _on_select_all(self) -> None:
        for name, row in self._rows.items():
            if not row.is_checked():
                row.set_checked(True)
                self.selection_changed.emit(name, True)
        self.update_count()
        self.select_all_clicked.emit()

    def _on_clear_all(self) -> None:
        for name, row in self._rows.items():
            if row.is_checked():
                row.set_checked(False)
                self.selection_changed.emit(name, False)
        self.update_count()
        self.clear_all_clicked.emit()
