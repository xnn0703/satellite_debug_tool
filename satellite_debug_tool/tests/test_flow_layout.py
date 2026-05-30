"""FlowLayout 单测：宽度变化时换行计算正确性 + 边界 case。

不依赖具体 widget，用 QLabel（固定 sizeHint）作 dummy item。
"""
from __future__ import annotations

import os
import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _make_chip(qapp, w: int, h: int = 20):
    """构造固定 sizeHint 的 chip（用于排版测试）。"""
    from PySide6.QtWidgets import QLabel
    lbl = QLabel("x")
    lbl.setFixedSize(w, h)
    return lbl


class TestBasicAPI:
    def test_empty_layout_count_zero(self, qapp):
        from satellite_debug_tool.ui.flow_layout import FlowLayout
        layout = FlowLayout()
        assert layout.count() == 0
        assert layout.itemAt(0) is None
        assert layout.takeAt(0) is None

    def test_add_and_count(self, qapp):
        from satellite_debug_tool.ui.flow_layout import FlowLayout
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        layout = FlowLayout(parent)
        for _ in range(5):
            layout.addWidget(_make_chip(qapp, 50))
        assert layout.count() == 5
        assert layout.itemAt(0) is not None
        assert layout.itemAt(99) is None

    def test_take_at_removes(self, qapp):
        from satellite_debug_tool.ui.flow_layout import FlowLayout
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        layout = FlowLayout(parent)
        layout.addWidget(_make_chip(qapp, 50))
        layout.addWidget(_make_chip(qapp, 50))
        assert layout.count() == 2
        taken = layout.takeAt(0)
        assert taken is not None
        assert layout.count() == 1

    def test_has_height_for_width(self, qapp):
        from satellite_debug_tool.ui.flow_layout import FlowLayout
        layout = FlowLayout()
        assert layout.hasHeightForWidth() is True


class TestWrapping:
    """核心：根据宽度计算换行行数 → 总高度。"""

    def test_single_row_when_wide(self, qapp):
        """足够宽时所有 item 一行。"""
        from satellite_debug_tool.ui.flow_layout import FlowLayout
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        layout = FlowLayout(parent, h_spacing=6, v_spacing=4)
        # 5 个 50px 宽 chip，spacing 6px → 总宽 ~5*50 + 4*6 = 274
        for _ in range(5):
            layout.addWidget(_make_chip(qapp, 50, 20))
        h = layout.heightForWidth(800)
        # 单行：高 = 20（chip 高）
        assert h == 20

    def test_two_rows_at_half_width(self, qapp):
        """宽度只够装 ~3 个 chip，应换成 2 行。"""
        from satellite_debug_tool.ui.flow_layout import FlowLayout
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        layout = FlowLayout(parent, h_spacing=6, v_spacing=4)
        for _ in range(5):
            layout.addWidget(_make_chip(qapp, 50, 20))
        # 宽度 180：50+6+50+6+50 = 162 < 180，第四个就放不下 → 2 行
        h = layout.heightForWidth(180)
        # 2 行：20 + 4(v_spacing) + 20 = 44
        assert h == 44

    def test_one_per_row_when_narrow(self, qapp):
        """宽度极小，每行 1 个 chip，不出滚动条（设计取舍）。"""
        from satellite_debug_tool.ui.flow_layout import FlowLayout
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        layout = FlowLayout(parent, h_spacing=6, v_spacing=4)
        for _ in range(4):
            layout.addWidget(_make_chip(qapp, 50, 20))
        # 宽度 30 < chip 宽 50：每行 1 个，4 行
        h = layout.heightForWidth(30)
        # 4 行：4*20 + 3*4 = 92
        assert h == 92

    def test_wider_reduces_rows(self, qapp):
        """宽度递增 → 行数单调不增。"""
        from satellite_debug_tool.ui.flow_layout import FlowLayout
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        layout = FlowLayout(parent, h_spacing=6, v_spacing=4)
        for _ in range(8):
            layout.addWidget(_make_chip(qapp, 50, 20))
        last_h = 1e9
        for w in (50, 100, 200, 400, 800):
            h = layout.heightForWidth(w)
            assert h <= last_h, f"width={w}: height={h} > prev {last_h}"
            last_h = h


class TestGeometryAssignment:
    """setGeometry 后每个 item 的实际位置应符合排版规则。"""

    def test_items_positioned_left_to_right(self, qapp):
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QWidget
        from satellite_debug_tool.ui.flow_layout import FlowLayout
        parent = QWidget()
        layout = FlowLayout(parent, h_spacing=6, v_spacing=4)
        chips = [_make_chip(qapp, 50, 20) for _ in range(3)]
        for c in chips:
            layout.addWidget(c)
        layout.setGeometry(QRect(0, 0, 800, 100))
        # 三个 chip 都在 y=0 那一行，x 单调递增
        ys = [c.geometry().y() for c in chips]
        xs = [c.geometry().x() for c in chips]
        assert ys == [0, 0, 0]
        assert xs == sorted(xs)
        assert xs[1] - xs[0] == 50 + 6   # chip 宽 + spacing
        assert xs[2] - xs[1] == 50 + 6

    def test_second_row_starts_at_left(self, qapp):
        """换行后下一行从最左开始。"""
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QWidget
        from satellite_debug_tool.ui.flow_layout import FlowLayout
        parent = QWidget()
        layout = FlowLayout(parent, h_spacing=6, v_spacing=4)
        chips = [_make_chip(qapp, 50, 20) for _ in range(4)]
        for c in chips:
            layout.addWidget(c)
        # 宽度 120：50+6+50 = 106 < 120，第三个 + 6 + 50 = 162 > 120 → 第三个换行
        layout.setGeometry(QRect(0, 0, 120, 100))
        assert chips[0].geometry().y() == chips[1].geometry().y()
        assert chips[2].geometry().y() > chips[1].geometry().y()
        # 第三个 chip x 回到最左
        assert chips[2].geometry().x() == chips[0].geometry().x()


class TestSizeHint:
    def test_minimum_size_covers_largest_item(self, qapp):
        from satellite_debug_tool.ui.flow_layout import FlowLayout
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        layout = FlowLayout(parent, h_spacing=6, v_spacing=4)
        layout.addWidget(_make_chip(qapp, 50, 20))
        layout.addWidget(_make_chip(qapp, 100, 30))
        ms = layout.minimumSize()
        assert ms.width() >= 100
        assert ms.height() >= 30
