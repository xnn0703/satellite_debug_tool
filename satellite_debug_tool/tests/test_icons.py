"""icons 图标系统单测：SVG markup 生成 + QIcon 渲染 + 缓存。"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtSvg")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class TestSvgMarkup:
    def test_markup_contains_color(self):
        from satellite_debug_tool.ui import icons
        m = icons.svg_markup("settings", color="#2DD4BF")
        assert 'stroke="#2DD4BF"' in m
        assert m.startswith("<svg")
        assert "viewBox=\"0 0 24 24\"" in m

    def test_unknown_name_falls_back_to_x(self):
        from satellite_debug_tool.ui import icons
        m = icons.svg_markup("definitely_not_an_icon")
        # 回退到 'x' 图标的 path
        assert m == icons.svg_markup("x")

    def test_all_names_have_paths(self):
        from satellite_debug_tool.ui import icons
        assert len(icons.ICON_NAMES) >= 50
        for name in icons.ICON_NAMES:
            m = icons.svg_markup(name)
            assert "<path" in m or "<circle" in m or "<rect" in m


class TestIconRender:
    def test_icon_not_null(self, qapp):
        from satellite_debug_tool.ui import icons
        ic = icons.icon("settings", color="#D6E0E9", size=16)
        assert not ic.isNull()

    def test_icon_cache_returns_same_object(self, qapp):
        from satellite_debug_tool.ui import icons
        icons.clear_cache()
        a = icons.icon("refresh", color="#2DD4BF", size=16)
        b = icons.icon("refresh", color="#2DD4BF", size=16)
        assert a is b

    def test_different_color_different_icon(self, qapp):
        from satellite_debug_tool.ui import icons
        icons.clear_cache()
        a = icons.icon("refresh", color="#2DD4BF", size=16)
        b = icons.icon("refresh", color="#FB7185", size=16)
        assert a is not b

    def test_clear_cache(self, qapp):
        from satellite_debug_tool.ui import icons
        a = icons.icon("settings", color="#FFFFFF", size=20)
        icons.clear_cache()
        b = icons.icon("settings", color="#FFFFFF", size=20)
        # 清缓存后重新生成，不是同一对象
        assert a is not b
