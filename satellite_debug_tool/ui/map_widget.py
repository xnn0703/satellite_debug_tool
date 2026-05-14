"""MapWidget —— Leaflet + QWebEngineView 离线地图组件（M8-S2）。

完全离线策略：
- `~/.satellite_debug_tool/tiles/<label>/{z}/{x}/{y}.png` 目录由
  `tools/tile_downloader.py` 预先下载
- 应用启动时自动检测该目录，构造 `file://` URL 给 Leaflet
- 没 tile 时显示 placeholder 文字指引用户运行 tile_downloader，应用仍可用

Python 端单向调用 JS（`runJavaScript`），不依赖 QWebChannel —— 数据流只
Python → JS，无回调需求。

公共 API:
    set_track(timestamps_ms, lats, lons)   全量轨迹（含起点/终点 marker）
    set_track_highlight(start_ms, end_ms)  高亮一段
    add_event(ts_ms, name, level, lat, lon)
    clear()
    fit_track_bounds()
    set_region(label)                       多区域切换（如 nanjing / shanghai）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QUrl, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget


_LOG = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_MAP_HTML = _ASSETS_DIR / "map.html"

DEFAULT_TILES_ROOT = Path.home() / ".satellite_debug_tool" / "tiles"


def _detect_default_region(tiles_root: Path) -> Optional[Path]:
    """挑一个非空 region 目录（按字母序第一个有内容的）。"""
    if not tiles_root.is_dir():
        return None
    for child in sorted(tiles_root.iterdir()):
        if child.is_dir() and any(child.iterdir()):
            return child
    return None


def _tiles_url_template(region_dir: Path) -> str:
    """构造 Leaflet 用的 file:// URL 模板（含 {z}/{x}/{y} 占位）。"""
    base = QUrl.fromLocalFile(str(region_dir.resolve())).toString().rstrip("/")
    return f"{base}/{{z}}/{{x}}/{{y}}.png"


class MapWidget(QWidget):
    """嵌入 Leaflet 地图的浮窗组件。

    Args:
        tiles_root: 缓存根目录，默认 ~/.satellite_debug_tool/tiles
        region: 子目录名（如 "nanjing"），None 时自动挑第一个非空 region
    """

    # HTML 完成加载并完成 tile URL 注入后 emit
    map_ready = Signal()

    def __init__(
        self,
        tiles_root: Optional[Path] = None,
        region: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._tiles_root = tiles_root or DEFAULT_TILES_ROOT
        self._region: Optional[Path] = None
        self._region_label: Optional[str] = region
        self._pending_js: list[str] = []   # HTML 还没加载完时缓冲的 JS 调用
        self._loaded = False
        self._theme = "dark"

        self._setup_ui()
        self._set_region_internal(region)
        self._load_html()

    def _setup_ui(self) -> None:
        # WebEngine import 放在方法里，避免顶层 import 阻塞测试收集（headless 环境）
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWebEngineCore import QWebEngineSettings

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._view = QWebEngineView(self)
        # 允许 file:// 协议加载本地资源（Leaflet 引用 ./leaflet/leaflet.js 等）
        settings = self._view.settings()
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)
        layout.addWidget(self._view)
        self._view.loadFinished.connect(self._on_load_finished)

    def _load_html(self) -> None:
        if not _MAP_HTML.exists():
            _LOG.error("map.html missing at %s", _MAP_HTML)
            return
        self._view.setUrl(QUrl.fromLocalFile(str(_MAP_HTML.resolve())))

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            _LOG.warning("map.html load failed")
            return
        self._loaded = True
        # 注入 tile URL
        if self._region is not None:
            url = _tiles_url_template(self._region)
            attribution = f"© OpenStreetMap · 离线缓存 {self._region.name}"
            self._call_js(f"setTileURL({json.dumps(url)}, {json.dumps(attribution)}, 19)")
        else:
            self._call_js("setTileURL(null, '', 19)")
        # 主题
        self._call_js(f"setTheme({json.dumps(self._theme)})")
        # 把缓冲的 JS 调用回放
        for code in self._pending_js:
            self._call_js(code)
        self._pending_js.clear()
        self.map_ready.emit()

    # =============================== API ===============================

    def set_track(
        self,
        timestamps_ms: np.ndarray,
        lats: np.ndarray,
        lons: np.ndarray,
    ) -> None:
        """灌入完整 GPS 轨迹（自动加起点 / 终点 marker + fit bounds）。"""
        if lats is None or lons is None:
            return
        n = min(len(lats), len(lons), len(timestamps_ms))
        if n == 0:
            self._enqueue_js("setTrack([])")
            return
        points = [
            [float(lats[i]), float(lons[i]), float(timestamps_ms[i])]
            for i in range(n)
        ]
        self._enqueue_js(f"setTrack({json.dumps(points)})")

    def set_track_highlight(self, start_ms: float, end_ms: float) -> None:
        """高亮 [start_ms, end_ms] 内的轨迹段（粗、亮色叠在底色之上）。"""
        self._enqueue_js(f"setTrackHighlight({float(start_ms)}, {float(end_ms)})")

    def add_event(
        self,
        ts_ms: float,
        name: str,
        level: int,
        lat: float,
        lon: float,
    ) -> None:
        """在 (lat, lon) 处加一个事件 marker，按 level 着色。"""
        self._enqueue_js(
            f"addEvent({float(lat)}, {float(lon)}, {json.dumps(name)}, {int(level)})"
        )

    def clear(self) -> None:
        self._enqueue_js("clearAll()")

    def fit_track_bounds(self) -> None:
        self._enqueue_js("fitTrackBounds()")

    def set_region(self, label: Optional[str]) -> bool:
        """切换 region 子目录（如 nanjing / shanghai）。

        Returns:
            True 表示找到该目录并应用了；False 表示未找到（保持原状）
        """
        prev = self._region
        self._set_region_internal(label)
        if self._region is None and prev is not None:
            return False
        if self._loaded and self._region is not None:
            url = _tiles_url_template(self._region)
            attribution = f"© OpenStreetMap · 离线缓存 {self._region.name}"
            self._call_js(f"setTileURL({json.dumps(url)}, {json.dumps(attribution)}, 19)")
        return self._region is not None

    def _set_region_internal(self, label: Optional[str]) -> None:
        if label is None:
            self._region = _detect_default_region(self._tiles_root)
            self._region_label = self._region.name if self._region else None
        else:
            candidate = self._tiles_root / label
            self._region = candidate if candidate.is_dir() else None
            self._region_label = label if self._region else None

    @property
    def current_region(self) -> Optional[str]:
        return self._region_label

    @property
    def is_offline_ready(self) -> bool:
        """有本地 tile 可用时 True；没下就 False（地图本身可加载，只是空底图）。"""
        return self._region is not None

    # =============================== 主题 ===============================

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme if theme in ("dark", "dark_hc", "light") else "dark"
        if self._loaded:
            self._call_js(f"setTheme({json.dumps(self._theme)})")

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light")

    # =============================== 内部 ===============================

    def _enqueue_js(self, code: str) -> None:
        """HTML 还没加载完时缓冲；加载完后立即执行或 flush。"""
        if not self._loaded:
            self._pending_js.append(code)
        else:
            self._call_js(code)

    def _call_js(self, code: str) -> None:
        try:
            self._view.page().runJavaScript(code)
        except Exception as exc:
            _LOG.warning("runJavaScript failed: %s", exc)
