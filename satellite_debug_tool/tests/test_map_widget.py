"""MapWidget 单测（M8-S2）。

不真正渲染地图（headless QWebEngine 限制），只验证：
- 实例化不崩
- region 自动检测 / 手动切换
- JS 缓冲队列在 load 前后行为正确
- 离线 fallback 检测
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class TestRegionDetection:
    def test_no_tiles_dir(self, qapp, tmp_path):
        from satellite_debug_tool.ui.map_widget import MapWidget
        w = MapWidget(tiles_root=tmp_path / "missing")
        assert w.current_region is None
        assert w.is_offline_ready is False

    def test_empty_tiles_dir(self, qapp, tmp_path):
        from satellite_debug_tool.ui.map_widget import MapWidget
        (tmp_path / "tiles").mkdir()
        w = MapWidget(tiles_root=tmp_path / "tiles")
        assert w.current_region is None
        assert w.is_offline_ready is False

    def test_auto_pick_first_region(self, qapp, tmp_path):
        from satellite_debug_tool.ui.map_widget import MapWidget
        nj = tmp_path / "tiles" / "nanjing" / "14" / "13597"
        nj.mkdir(parents=True)
        (nj / "6651.png").write_bytes(b"\x89PNG fake")
        sh = tmp_path / "tiles" / "shanghai" / "14" / "13720"
        sh.mkdir(parents=True)
        (sh / "6671.png").write_bytes(b"\x89PNG fake")

        w = MapWidget(tiles_root=tmp_path / "tiles")
        # 按字母序：nanjing < shanghai
        assert w.current_region == "nanjing"
        assert w.is_offline_ready is True

    def test_explicit_region(self, qapp, tmp_path):
        from satellite_debug_tool.ui.map_widget import MapWidget
        for name in ("nanjing", "shanghai"):
            d = tmp_path / "tiles" / name / "14" / "0"
            d.mkdir(parents=True)
            (d / "0.png").write_bytes(b"fake")
        w = MapWidget(tiles_root=tmp_path / "tiles", region="shanghai")
        assert w.current_region == "shanghai"

    def test_set_region_missing_returns_false(self, qapp, tmp_path):
        from satellite_debug_tool.ui.map_widget import MapWidget
        (tmp_path / "tiles").mkdir()
        w = MapWidget(tiles_root=tmp_path / "tiles")
        assert w.set_region("nonexistent") is False
        assert w.current_region is None


class TestJSBuffering:
    """HTML 加载是异步的，set_track 在 _loaded=False 时应进缓冲。"""

    def test_buffer_before_loaded(self, qapp, tmp_path):
        from satellite_debug_tool.ui.map_widget import MapWidget
        w = MapWidget(tiles_root=tmp_path / "missing")
        # 模拟未加载完
        w._loaded = False
        w._pending_js.clear()
        ts = np.array([0, 100, 200], dtype=np.float64)
        lats = np.array([32.04, 32.05, 32.06], dtype=np.float32)
        lons = np.array([118.78, 118.79, 118.80], dtype=np.float32)
        w.set_track(ts, lats, lons)
        w.add_event(150.0, "test event", 2, 32.05, 118.79)
        w.clear()
        # 三个调用都应在缓冲里
        assert len(w._pending_js) == 3
        assert "setTrack" in w._pending_js[0]
        assert "addEvent" in w._pending_js[1]
        assert "clearAll" in w._pending_js[2]

    def test_empty_track_call(self, qapp, tmp_path):
        from satellite_debug_tool.ui.map_widget import MapWidget
        w = MapWidget(tiles_root=tmp_path / "missing")
        w._loaded = False
        w._pending_js.clear()
        w.set_track(np.array([]), np.array([]), np.array([]))
        assert w._pending_js == ["setTrack([])"]


class TestThemeApi:
    def test_set_theme_accepts_known_values(self, qapp, tmp_path):
        from satellite_debug_tool.ui.map_widget import MapWidget
        w = MapWidget(tiles_root=tmp_path / "missing")
        w.set_theme("light")
        assert w._theme == "light"
        w.set_theme("dark_hc")
        assert w._theme == "dark_hc"
        # 未知主题回退 dark
        w.set_theme("unknown")
        assert w._theme == "dark"

    def test_set_dark_theme_bool(self, qapp, tmp_path):
        from satellite_debug_tool.ui.map_widget import MapWidget
        w = MapWidget(tiles_root=tmp_path / "missing")
        w.set_dark_theme(False)
        assert w._theme == "light"
        w.set_dark_theme(True)
        assert w._theme == "dark"
