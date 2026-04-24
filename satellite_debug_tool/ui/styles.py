"""UI 样式中心：主题色板 + 字号档位 + 等宽字体选择。

M6 扩展：
- 主题三档：dark / dark_hc / light
- 字号四档：small(1.0) / medium(1.17) / large(1.5) / xlarge(1.83)
    基准 12px → 12 / 14 / 18 / 22
- Dashboard KPI 数字字体锁定等宽族（跨平台挑一个存在的）

兼容性：
- `palette(is_dark: bool)` 老签名仍可用（False→"light"，True→"dark"）
"""

from __future__ import annotations

from typing import Dict, Literal, Union

# --------------------------------------------------------------------
# 老常量（少数模块仍 import，保留以免破坏外部引用）
# --------------------------------------------------------------------

BG_DARK = "#1E1E1E"
PANEL_DARK = "#252526"
BORDER = "#3C3C3C"
TEXT = "#CCCCCC"
PRIMARY = "#0E639C"
SUCCESS = "#4EC9B0"
WARNING = "#DCDCAA"
ERROR = "#F14C4C"
INPUT_DARK = "#333333"
BUTTON_DARK = "#444444"

BG_LIGHT = "#FFFFFF"
PANEL_LIGHT = "#F5F5F5"
BORDER_LIGHT = "#E0E0E0"
TEXT_LIGHT = "#333333"
TEXT_LIGHT_MUTED = "#666666"
PRIMARY_LIGHT = "#0E639C"
INPUT_LIGHT = "#FFFFFF"
BUTTON_LIGHT = "#DDDDDD"
CARD_LIGHT = "#FAFAFA"
CARD_DARK = "#252526"
CARD_ALT_DARK = "#2A2A2A"
CARD_ALT_LIGHT = "#F0F0F0"

# --------------------------------------------------------------------
# 主题
# --------------------------------------------------------------------

Theme = Literal["dark", "dark_hc", "light"]
THEMES: tuple[str, ...] = ("dark", "dark_hc", "light")
THEME_LABELS: Dict[str, str] = {
    "dark": "深色",
    "dark_hc": "深色·高对比",
    "light": "浅色",
}


def _normalize_theme(theme: Union[str, bool, None]) -> str:
    """把 bool/None/别名归一化到 THEMES 内。"""
    if theme is None:
        return "dark"
    if isinstance(theme, bool):
        return "dark" if theme else "light"
    t = str(theme).strip().lower().replace(" ", "_").replace("-", "_")
    if t in THEMES:
        return t
    # 中文 / 老 UI 文案兼容
    if t in ("深色", "dark_theme"):
        return "dark"
    if t in ("浅色", "light_theme"):
        return "light"
    if t in ("深色_高对比", "高对比", "hc", "highcontrast", "dark_highcontrast"):
        return "dark_hc"
    return "dark"


def palette(theme: Union[str, bool, None] = "dark") -> Dict[str, str]:
    """返回当前主题各类色值。所有 UI 模块通过此函数获取颜色，禁止硬编码。

    兼容老签名：`palette(True)` == `palette("dark")`，`palette(False)` == `palette("light")`。

    返回字典键（契约，新增键请同步 test_styles）：
        bg / panel / card / card_alt / border
        text / text_muted / text_faint
        input_bg / input_border
        button_bg
        value_number         — KPI 数字主色
        success / warning / error    — 语义色（跟主题微调）
        primary              — 主按钮/强调色
    """
    t = _normalize_theme(theme)

    if t == "dark":
        return {
            "bg": BG_DARK,
            "panel": PANEL_DARK,
            "card": CARD_DARK,
            "card_alt": CARD_ALT_DARK,
            "border": BORDER,
            "text": TEXT,
            "text_muted": "#888888",
            "text_faint": "#666666",
            "input_bg": INPUT_DARK,
            "input_border": "#555555",
            "button_bg": BUTTON_DARK,
            "value_number": "#EEEEEE",
            "success": SUCCESS,
            "warning": WARNING,
            "error": ERROR,
            "primary": PRIMARY,
        }

    if t == "dark_hc":
        # 黑底 + 纯白 + 饱和度更高的语义色，用于 500cd/m² 车载屏
        return {
            "bg": "#000000",
            "panel": "#0A0A0A",
            "card": "#141414",
            "card_alt": "#1C1C1C",
            "border": "#404040",
            "text": "#F5F5F5",
            "text_muted": "#BBBBBB",
            "text_faint": "#888888",
            "input_bg": "#141414",
            "input_border": "#707070",
            "button_bg": "#222222",
            "value_number": "#FFFFFF",
            "success": "#00FF88",
            "warning": "#FFD700",
            "error": "#FF4040",
            "primary": "#1DA1F2",
        }

    # light
    return {
        "bg": BG_LIGHT,
        "panel": PANEL_LIGHT,
        "card": CARD_LIGHT,
        "card_alt": CARD_ALT_LIGHT,
        "border": BORDER_LIGHT,
        "text": TEXT_LIGHT,
        "text_muted": TEXT_LIGHT_MUTED,
        "text_faint": "#999999",
        "input_bg": INPUT_LIGHT,
        "input_border": "#C0C0C0",
        "button_bg": BUTTON_LIGHT,
        "value_number": "#111111",
        "success": "#007A5E",
        "warning": "#B88600",
        "error": "#C62828",
        "primary": PRIMARY_LIGHT,
    }


# --------------------------------------------------------------------
# 字号档位
# --------------------------------------------------------------------

FontScale = Literal["small", "medium", "large", "xlarge"]
FONT_SCALES: Dict[str, float] = {
    "small": 1.0,    # 基准 12px
    "medium": 1.17,  # 14px
    "large": 1.5,    # 18px
    "xlarge": 1.83,  # 22px
}
FONT_SCALE_LABELS: Dict[str, str] = {
    "small": "小",
    "medium": "中",
    "large": "大",
    "xlarge": "超大",
}


def _normalize_scale(scale: Union[str, float, None]) -> float:
    if scale is None:
        return FONT_SCALES["medium"]
    if isinstance(scale, (int, float)):
        return max(0.5, min(3.0, float(scale)))
    key = str(scale).strip().lower()
    if key in FONT_SCALES:
        return FONT_SCALES[key]
    # 中文兼容
    reverse = {v: k for k, v in FONT_SCALE_LABELS.items()}
    if key in reverse:
        return FONT_SCALES[reverse[key]]
    return FONT_SCALES["medium"]


def font_px(base_px: int, scale: Union[str, float, None] = "medium") -> int:
    """按档位缩放像素字号。四舍五入到整数。"""
    s = _normalize_scale(scale)
    return max(6, int(round(base_px * s)))


# --------------------------------------------------------------------
# 等宽字体（Dashboard KPI 数字专用，字名不随档位换）
# --------------------------------------------------------------------

_MONO_FAMILY_CACHE: str = ""


def monospace_family() -> str:
    """返回本机存在的首选等宽字体族名。运行时探测一次缓存。

    顺序：JetBrains Mono → Consolas → SF Mono → Menlo → Courier New → monospace
    """
    global _MONO_FAMILY_CACHE
    if _MONO_FAMILY_CACHE:
        return _MONO_FAMILY_CACHE

    candidates = ["JetBrains Mono", "Consolas", "SF Mono", "Menlo", "Courier New"]
    try:
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtCore import QCoreApplication

        # QFontDatabase 需要 QGuiApplication；无应用实例时直接返回占位名
        if QCoreApplication.instance() is None:
            return "monospace"

        available = set(QFontDatabase.families())
        for name in candidates:
            if name in available:
                _MONO_FAMILY_CACHE = name
                return name
    except Exception:
        pass

    _MONO_FAMILY_CACHE = "monospace"
    return _MONO_FAMILY_CACHE


# --------------------------------------------------------------------
# 全局应用字体
# --------------------------------------------------------------------

def apply_global_font(app, scale: Union[str, float, None] = "medium", base_px: int = 12) -> None:
    """把 QApplication 默认字号按档位缩放。QLabel/QComboBox/... 未单独设字号的控件会跟随。"""
    try:
        from PySide6.QtGui import QFont

        font = app.font()
        font.setPixelSize(font_px(base_px, scale))
        app.setFont(font)
    except Exception:
        pass


__all__ = [
    # 常量
    "BG_DARK", "PANEL_DARK", "BORDER", "TEXT", "PRIMARY", "SUCCESS", "WARNING", "ERROR",
    "INPUT_DARK", "BUTTON_DARK",
    "BG_LIGHT", "PANEL_LIGHT", "BORDER_LIGHT", "TEXT_LIGHT", "TEXT_LIGHT_MUTED",
    "PRIMARY_LIGHT", "INPUT_LIGHT", "BUTTON_LIGHT", "CARD_LIGHT", "CARD_DARK",
    "CARD_ALT_DARK", "CARD_ALT_LIGHT",
    # 主题
    "Theme", "THEMES", "THEME_LABELS", "palette",
    # 字号
    "FontScale", "FONT_SCALES", "FONT_SCALE_LABELS", "font_px",
    # 字体
    "monospace_family", "apply_global_font",
]
