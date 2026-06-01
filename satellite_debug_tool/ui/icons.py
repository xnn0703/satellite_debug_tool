"""Mission Console 图标系统 — 线性 SVG 图标渲染为按主题着色的 QIcon。

设计来源：handoff/icons.js（Lucide / Tabler 工程风格，1.7px 描边，24×24 viewBox）。
SVG path 内嵌于本模块（无需外部 .svg 文件，离线可用）。

用法：
    from satellite_debug_tool.ui import icons
    btn.setIcon(icons.icon("settings", color="#D6E0E9", size=16))
    # 或按 palette 角色取色：
    btn.setIcon(icons.icon("refresh", color=palette["text_2"]))

渲染：QSvgRenderer 把 `stroke=currentColor` 的内层 path 包成完整 SVG（替换 currentColor
为目标色），渲染到透明 QPixmap，按设备像素比生成清晰 QIcon。结果带 (name,color,size,dpr)
缓存。
"""
from __future__ import annotations

from typing import Dict, Optional

# ---- SVG path 数据（24×24 viewBox，内层 markup，stroke=currentColor）----
# 直接对应 handoff/icons.js 的 P 对象。
_PATHS: Dict[str, str] = {
    # connectivity / device
    "satellite": '<path d="M5 9 9 5l4 4-4 4-4-4Z"/><path d="m9 13 4 4"/><path d="M13 9a4 4 0 0 1 0 6"/><path d="M16 6a8 8 0 0 1 0 12"/><path d="m5 17-2 2"/>',
    "antenna": '<path d="M12 13v8"/><path d="M8 21h8"/><circle cx="12" cy="9" r="2"/><path d="M7.5 13.5a6 6 0 0 1 0-9"/><path d="M16.5 4.5a6 6 0 0 1 0 9"/>',
    "plug": '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M6 8h12v3a6 6 0 0 1-12 0V8Z"/>',
    "unplug": '<path d="m19 5 3-3"/><path d="m2 22 3-3"/><path d="M6.3 13.7 4 16a3 3 0 0 0 0 4 3 3 0 0 0 4 0l2.3-2.3"/><path d="M13.7 6.3 16 4a3 3 0 0 1 4 0 3 3 0 0 1 0 4l-2.3 2.3"/><path d="m7 11 6 6"/>',
    "link": '<path d="M9 12h6"/><path d="M9.5 7H7a5 5 0 0 0 0 10h2.5"/><path d="M14.5 7H17a5 5 0 0 1 0 10h-2.5"/>',
    "linkOff": '<path d="M9 17H7a5 5 0 0 1-.9-9.9"/><path d="M15 7h2a5 5 0 0 1 4 8"/><path d="m2 2 20 20"/><path d="M8 12h3"/>',
    "cpu": '<rect x="6" y="6" width="12" height="12" rx="1.5"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
    "wifi": '<path d="M5 12.5a10 10 0 0 1 14 0"/><path d="M8.5 15.8a5 5 0 0 1 7 0"/><path d="M12 19h.01"/>',
    # record / transport
    "record": '<circle cx="12" cy="12" r="5"/>',
    "play": '<path d="M7 4.5v15l13-7.5-13-7.5Z"/>',
    "pause": '<rect x="7" y="5" width="3.5" height="14" rx="1"/><rect x="13.5" y="5" width="3.5" height="14" rx="1"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "skipBack": '<path d="M18 6v12L9 12l9-6Z"/><rect x="5" y="6" width="2" height="12" rx="1"/>',
    "skipFwd": '<path d="M6 6v12l9-6-9-6Z"/><rect x="17" y="6" width="2" height="12" rx="1"/>',
    # ui / tools
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2v2.5M12 19.5V22M22 12h-2.5M4.5 12H2M19 5l-1.8 1.8M6.8 17.2 5 19M19 19l-1.8-1.8M6.8 6.8 5 5"/>',
    "sliders": '<path d="M4 7h10M18 7h2"/><path d="M4 12h2M10 12h10"/><path d="M4 17h12M20 17h0"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="18" cy="17" r="2"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/>',
    "download": '<path d="M12 3v12"/><path d="m7 11 5 5 5-5"/><path d="M5 21h14"/>',
    "upload": '<path d="M12 21V9"/><path d="m7 13 5-5 5 5"/><path d="M5 3h14"/>',
    "import": '<path d="M12 3v10"/><path d="m8 9 4 4 4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    "trash": '<path d="M4 7h16"/><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/><path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/><path d="M10 11v6M14 11v6"/>',
    "map": '<path d="m9 4 6 2 5-2v14l-5 2-6-2-5 2V4l5-2Z"/><path d="M9 2v16M15 6v16"/>',
    "lock": '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    "lockOpen": '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 7.5-2"/>',
    "target": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
    "crosshair": '<circle cx="12" cy="12" r="2"/><path d="M12 2v6M12 16v6M2 12h6M16 12h6"/>',
    "compass": '<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5 5-2Z"/>',
    "location": '<path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11Z"/><circle cx="12" cy="10" r="2.5"/>',
    "activity": '<path d="M3 12h4l2.5-7 5 14L17 12h4"/>',
    "waveform": '<path d="M4 12v0M8 8v8M12 4v16M16 9v6M20 12v0"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4 12H2M22 12h-2M5 5l1.5 1.5M17.5 17.5 19 19M19 5l-1.5 1.5M6.5 17.5 5 19"/>',
    "moon": '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"/>',
    "contrast": '<circle cx="12" cy="12" r="9"/><path d="M12 3v18a9 9 0 0 0 0-18Z" fill="currentColor" stroke="none"/>',
    "eye": '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    "eyeOff": '<path d="M10.7 6.2A9 9 0 0 1 12 5c6 0 10 7 10 7a17 17 0 0 1-3.2 3.7M6.5 6.8A17 17 0 0 0 2 12s4 7 10 7a9 9 0 0 0 3.7-.8"/><path d="m2 2 20 20"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    "layers": '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 13 9 5 9-5"/>',
    "grid": '<rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1"/><rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
    "columns": '<rect x="4" y="5" width="16" height="14" rx="1.5"/><path d="M12 5v14"/>',
    "maximize": '<path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/>',
    "move": '<path d="M12 3v18M3 12h18"/><path d="m9 6 3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3"/>',
    # status / misc
    "check": '<path d="m5 12 5 5 9-11"/>',
    "x": '<path d="m6 6 12 12M18 6 6 18"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "pencil": '<path d="M4 20h4L19 9l-4-4L4 16v4Z"/><path d="m14 6 4 4"/>',
    "chevronDown": '<path d="m6 9 6 6 6-6"/>',
    "chevronRight": '<path d="m9 6 6 6-6 6"/>',
    "chevronLeft": '<path d="m15 6-6 6 6 6"/>',
    "chevronUp": '<path d="m6 15 6-6 6 6"/>',
    "alert": '<path d="M12 3 2 20h20L12 3Z"/><path d="M12 10v4M12 17h.01"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "flag": '<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>',
    "sigma": '<path d="M18 5H6l6 7-6 7h12"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
    "file": '<path d="M6 3h8l4 4v14H6V3Z"/><path d="M14 3v4h4"/>',
    "bolt": '<path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z"/>',
    "gauge": '<path d="M12 14a2 2 0 0 0 1.4-3.4L17 7"/><path d="M5.5 18a9 9 0 1 1 13 0"/>',
    "signal": '<rect x="3" y="14" width="3" height="6" rx="1"/><rect x="9" y="10" width="3" height="10" rx="1"/><rect x="15" y="6" width="3" height="14" rx="1"/>',
    "history": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
}

ICON_NAMES = tuple(_PATHS.keys())

# 渲染缓存：(name, color, size, dpr) → QIcon
_ICON_CACHE: dict = {}


def svg_markup(name: str, color: str = "#000000", stroke: float = 1.7) -> str:
    """返回某图标的完整 SVG 字符串（currentColor 已替换为指定色）。"""
    inner = _PATHS.get(name, _PATHS["x"])
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
        f'viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="{stroke}" '
        f'stroke-linecap="round" stroke-linejoin="round">{inner}</svg>'
    )


def icon(name: str, color: str = "#D6E0E9", size: int = 16, stroke: float = 1.7):
    """渲染图标为按 color 着色的 QIcon（带缓存，HiDPI 清晰）。

    Args:
        name: 图标名（见 ICON_NAMES）；未知名回退到 "x"
        color: 描边色（#RRGGBB）
        size:  逻辑像素尺寸
        stroke: 描边宽度
    """
    return _render(name, color, size, stroke)


def _render(name: str, color: str, size: int, stroke: float):
    from PySide6.QtGui import QIcon, QPixmap, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtCore import QByteArray, QRectF, Qt

    try:
        from PySide6.QtWidgets import QApplication
        dpr = QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0
    except Exception:
        dpr = 1.0
    dpr = max(1.0, float(dpr))

    key = (name, color.lower(), int(size), round(dpr, 2), stroke)
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached

    markup = svg_markup(name, color, stroke)
    renderer = QSvgRenderer(QByteArray(markup.encode("utf-8")))
    px = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.GlobalColor.transparent)
    painter = QPainter(px)
    # 显式渲染到 size×size 逻辑矩形（QPixmap 已设 dpr，painter 逻辑坐标 = size）。
    # 不传 rect 时 render() 用 device-px 的 viewport，在 dpr>1 的画布上会把图标
    # 放大 dpr 倍、只显示左上角 —— Retina 上 icon 被截断的根因。
    renderer.render(painter, QRectF(0.0, 0.0, float(size), float(size)))
    painter.end()

    result = QIcon(px)
    _ICON_CACHE[key] = result
    return result


def clear_cache() -> None:
    """主题切换后调用，丢弃旧着色缓存。"""
    _ICON_CACHE.clear()
