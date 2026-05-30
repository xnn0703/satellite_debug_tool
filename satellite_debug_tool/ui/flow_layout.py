"""FlowLayout — 自适应换行的 Qt 布局。

参考 Qt 官方 example flowlayout（C++ → Python 直译）。从左到右贪心摆放
子 item，当前行宽放不下就换行。常用于 chip / tag / 通道指示灯等"宽度不定、
个数不定"的扁平控件集合。

特点：
- `heightForWidth(w)` 返回在给定宽度下的总高（Qt 据此分配空间）
- `hasHeightForWidth() == True` 让父布局知道这是"高度跟随宽度"的布局
- 极窄场景：单个 item 都放不下时，每行 1 个，**不出滚动条**（FlowLayout 自身
  无滚动概念；如需滚动由外层 QScrollArea 提供，但 M10 设计上不用）

用法：
    layout = FlowLayout(parent, margin=0, h_spacing=6, v_spacing=4)
    layout.addWidget(chip1)
    layout.addWidget(chip2)
    parent.setLayout(layout)
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (
    QLayout,
    QLayoutItem,
    QStyle,
    QWidget,
)


class FlowLayout(QLayout):
    def __init__(
        self,
        parent: Optional[QWidget] = None,
        margin: int = 0,
        h_spacing: int = 6,
        v_spacing: int = 4,
    ) -> None:
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        self._h_space = h_spacing
        self._v_space = v_spacing
        self._items: List[QLayoutItem] = []

    # ---- 必须实现的 QLayout 接口 ----

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 (Qt API)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    # ---- 内部：核心排版逻辑 ----

    def _smart_spacing(self, pm: QStyle.PixelMetric) -> int:
        """从父 widget 的 style 拿默认 spacing；没有则用构造时配的值。"""
        parent = self.parent()
        if parent is None:
            return -1
        if parent.isWidgetType():
            return parent.style().pixelMetric(pm, None, parent)
        return parent.spacing()

    def horizontal_spacing(self) -> int:
        if self._h_space >= 0:
            return self._h_space
        return self._smart_spacing(QStyle.PixelMetric.PM_LayoutHorizontalSpacing)

    def vertical_spacing(self) -> int:
        if self._v_space >= 0:
            return self._v_space
        return self._smart_spacing(QStyle.PixelMetric.PM_LayoutVerticalSpacing)

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        """把所有 item 在 rect 里贪心排版；test_only=True 时不真正 setGeometry，
        只返回需要的总高。"""
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0

        h_space = self.horizontal_spacing()
        v_space = self.vertical_spacing()

        for item in self._items:
            sz = item.sizeHint()
            next_x = x + sz.width() + h_space
            if next_x - h_space > effective.right() and line_height > 0:
                # 当前行放不下 → 换行
                x = effective.x()
                y = y + line_height + v_space
                next_x = x + sz.width() + h_space
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), sz))
            x = next_x
            line_height = max(line_height, sz.height())

        return y + line_height - rect.y() + m.bottom()
