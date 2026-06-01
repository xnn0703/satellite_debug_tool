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
# 2026-06：值迁移到 "Mission Console" 设计语言（深蓝仪表台 + 青色强调）
# --------------------------------------------------------------------

BG_DARK = "#090D12"
PANEL_DARK = "#0E141C"
BORDER = "#1F2C38"
TEXT = "#D6E0E9"
PRIMARY = "#2DD4BF"        # 青色 accent（原深蓝 → Mission Console teal）
SUCCESS = "#34D399"
WARNING = "#FBBF24"
ERROR = "#FB7185"
INPUT_DARK = "#10171F"
BUTTON_DARK = "#16202C"

BG_LIGHT = "#EEF1F4"
PANEL_LIGHT = "#F6F8FA"
BORDER_LIGHT = "#D8DEE4"
TEXT_LIGHT = "#1B2630"
TEXT_LIGHT_MUTED = "#5A6976"
PRIMARY_LIGHT = "#0EA5A4"
INPUT_LIGHT = "#FFFFFF"
BUTTON_LIGHT = "#F4F7F9"
CARD_LIGHT = "#FFFFFF"
CARD_DARK = "#121A24"
CARD_ALT_DARK = "#16202C"
CARD_ALT_LIGHT = "#F4F7F9"

# --------------------------------------------------------------------
# Mission Console 信号色板（曲线专用，16 色高区分度）
# 相邻通道色相拉开；同一通道在三主题下保持一致。
# --------------------------------------------------------------------

SIGNAL_PALETTE: tuple[str, ...] = (
    "#2DD4BF", "#38BDF8", "#FBBF24", "#FB7185",
    "#A78BFA", "#4ADE80", "#FB923C", "#22D3EE",
    "#F472B6", "#A3E635", "#818CF8", "#FCD34D",
    "#2DD4BF", "#F87171", "#60A5FA", "#C084FC",
)

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

    2026-06 "Mission Console" 设计语言：深蓝仪表台 + 青色 accent。三主题同源，
    所有组件只引用语义角色，切主题零改动。

    返回字典键分两类：

    **兼容键**（test_styles 契约 + 老代码引用，勿删）：
        bg / panel / card / card_alt / border
        text / text_muted / text_faint
        input_bg / input_border / button_bg / value_number
        success / warning / error / primary

    **新语义键**（Mission Console，新组件优先用这些）：
        bg_grid / panel_2 / card_2 / card_hover / border_2 / border_glow
        text_2 / text_3 / text_inv
        accent / accent_2 / accent_dim / accent_soft / accent_ink
        ok / warn / err / info / violet
        ok_soft / warn_soft / err_soft / info_soft / accent_soft
        grid_line
    """
    t = _normalize_theme(theme)

    if t == "dark":
        accent, accent_2, accent_dim = "#2DD4BF", "#5EEAD4", "#14B8A6"
        ok, warn, err, info, violet = "#34D399", "#FBBF24", "#FB7185", "#38BDF8", "#A78BFA"
        return {
            # 兼容键
            "bg": "#090D12", "panel": "#0E141C", "card": "#121A24",
            "card_alt": "#16202C", "border": "#1F2C38",
            "text": "#D6E0E9", "text_muted": "#8696A4", "text_faint": "#586976",
            "input_bg": "#10171F", "input_border": "#2A3B49", "button_bg": "#16202C",
            "value_number": "#D6E0E9",
            "success": ok, "warning": warn, "error": err, "primary": accent,
            # 新语义键
            "bg_grid": "#0D1219", "panel_2": "#10171F", "card_2": "#16202C",
            "card_hover": "#1A2632", "border_2": "#2A3B49", "border_glow": "#2DD4BF33",
            "text_2": "#8696A4", "text_3": "#586976", "text_inv": "#04110F",
            "accent": accent, "accent_2": accent_2, "accent_dim": accent_dim,
            "accent_soft": "#2DD4BF1F", "accent_ink": "#04110F",
            "ok": ok, "warn": warn, "err": err, "info": info, "violet": violet,
            "ok_soft": "#34D3991F", "warn_soft": "#FBBF241F",
            "err_soft": "#FB71851F", "info_soft": "#38BDF81F",
            "grid_line": "#FFFFFF0A",
        }

    if t == "dark_hc":
        # 纯黑底 + 纯白字 + 饱和强调色，用于 500cd/m² 车载强光屏
        accent, accent_2, accent_dim = "#00F5D4", "#5BFFE6", "#00D9BD"
        ok, warn, err, info, violet = "#00FF95", "#FFD000", "#FF5470", "#4DC4FF", "#C4A6FF"
        return {
            "bg": "#000000", "panel": "#060606", "card": "#0E0E0E",
            "card_alt": "#141414", "border": "#3A3A3A",
            "text": "#FFFFFF", "text_muted": "#C8C8C8", "text_faint": "#909090",
            "input_bg": "#0A0A0A", "input_border": "#545454", "button_bg": "#141414",
            "value_number": "#FFFFFF",
            "success": ok, "warning": warn, "error": err, "primary": accent,
            "bg_grid": "#000000", "panel_2": "#0A0A0A", "card_2": "#141414",
            "card_hover": "#1C1C1C", "border_2": "#545454", "border_glow": "#00F5D466",
            "text_2": "#C8C8C8", "text_3": "#909090", "text_inv": "#000000",
            "accent": accent, "accent_2": accent_2, "accent_dim": accent_dim,
            "accent_soft": "#00F5D422", "accent_ink": "#00120F",
            "ok": ok, "warn": warn, "err": err, "info": info, "violet": violet,
            "ok_soft": "#00FF9522", "warn_soft": "#FFD00022",
            "err_soft": "#FF547022", "info_soft": "#4DC4FF22",
            "grid_line": "#FFFFFF14",
        }

    # light
    accent, accent_2, accent_dim = "#0EA5A4", "#0D9488", "#0F766E"
    ok, warn, err, info, violet = "#059669", "#B45309", "#DC2626", "#0284C7", "#7C3AED"
    return {
        "bg": "#EEF1F4", "panel": "#F6F8FA", "card": "#FFFFFF",
        "card_alt": "#F4F7F9", "border": "#D8DEE4",
        "text": "#1B2630", "text_muted": "#5A6976", "text_faint": "#8895A0",
        "input_bg": "#FFFFFF", "input_border": "#C2CCD4", "button_bg": "#F4F7F9",
        "value_number": "#1B2630",
        "success": ok, "warning": warn, "error": err, "primary": accent,
        "bg_grid": "#E7EBEF", "panel_2": "#FFFFFF", "card_2": "#F4F7F9",
        "card_hover": "#EDF1F4", "border_2": "#C2CCD4", "border_glow": "#0EA5A433",
        "text_2": "#5A6976", "text_3": "#8895A0", "text_inv": "#FFFFFF",
        "accent": accent, "accent_2": accent_2, "accent_dim": accent_dim,
        "accent_soft": "#0EA5A414", "accent_ink": "#FFFFFF",
        "ok": ok, "warn": warn, "err": err, "info": info, "violet": violet,
        "ok_soft": "#05966914", "warn_soft": "#B4530914",
        "err_soft": "#DC262614", "info_soft": "#0284C714",
        "grid_line": "#10203005",
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
_SANS_FAMILY_CACHE: str = ""


def _first_available_family(candidates: list[str], fallback: str) -> str:
    """返回 candidates 中本机首个存在的字体族名；无 QApplication 时返回 fallback。"""
    try:
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtCore import QCoreApplication

        if QCoreApplication.instance() is None:
            return fallback
        available = set(QFontDatabase.families())
        for name in candidates:
            if name in available:
                return name
    except Exception:
        pass
    return fallback


def monospace_family() -> str:
    """返回本机存在的首选等宽字体族名。运行时探测一次缓存。

    Mission Console 偏好：IBM Plex Mono → JetBrains Mono → SF Mono → Menlo → Consolas
    （等宽 + tabular 数字，避免值跳动）
    """
    global _MONO_FAMILY_CACHE
    if _MONO_FAMILY_CACHE:
        return _MONO_FAMILY_CACHE
    _MONO_FAMILY_CACHE = _first_available_family(
        ["IBM Plex Mono", "JetBrains Mono", "SF Mono", "Menlo", "Consolas", "Courier New"],
        "monospace",
    )
    return _MONO_FAMILY_CACHE


def load_bundled_fonts() -> list[str]:
    """加载 ui/assets/fonts/ 下打包的 .ttf/.otf 字体到 Qt 字体库。

    Mission Console 设计偏好 IBM Plex Sans / IBM Plex Mono；若把对应字体文件放进
    assets/fonts/，启动时自动注册，sans_family()/monospace_family() 即可命中。
    目录不存在或为空时静默返回（回退系统字体，不影响功能）。返回成功加载的族名列表。
    """
    loaded: list[str] = []
    try:
        from pathlib import Path
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtCore import QCoreApplication

        if QCoreApplication.instance() is None:
            return loaded
        fonts_dir = Path(__file__).resolve().parent / "assets" / "fonts"
        if not fonts_dir.is_dir():
            return loaded
        for f in sorted(fonts_dir.glob("*.[to]tf")):
            fid = QFontDatabase.addApplicationFont(str(f))
            if fid >= 0:
                loaded.extend(QFontDatabase.applicationFontFamilies(fid))
    except Exception:
        pass
    # 加载后清缓存，让 sans/mono 重新探测
    global _SANS_FAMILY_CACHE, _MONO_FAMILY_CACHE
    _SANS_FAMILY_CACHE = ""
    _MONO_FAMILY_CACHE = ""
    return loaded


def sans_family() -> str:
    """返回本机存在的首选无衬线界面字体族名（Mission Console 主字体）。

    偏好：IBM Plex Sans → 系统中文回退（PingFang SC / Microsoft YaHei）→ 系统默认
    """
    global _SANS_FAMILY_CACHE
    if _SANS_FAMILY_CACHE:
        return _SANS_FAMILY_CACHE
    _SANS_FAMILY_CACHE = _first_available_family(
        ["IBM Plex Sans", "PingFang SC", "Microsoft YaHei", "Helvetica Neue", "Segoe UI"],
        "",   # 空 = 用 Qt 系统默认
    )
    return _SANS_FAMILY_CACHE


# --------------------------------------------------------------------
# 全局应用字体
# --------------------------------------------------------------------

def apply_global_font(app, scale: Union[str, float, None] = "medium", base_px: int = 12) -> None:
    """把 QApplication 默认字号按档位缩放 + 设 Mission Console 界面字体（IBM Plex Sans）。

    QLabel/QComboBox/... 未单独设字号 / 字体的控件会跟随。
    """
    try:
        from PySide6.QtGui import QFont

        font = app.font()
        fam = sans_family()
        if fam:
            font.setFamily(fam)
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
    "SIGNAL_PALETTE",
    # 主题
    "Theme", "THEMES", "THEME_LABELS", "palette",
    # 字号
    "FontScale", "FONT_SCALES", "FONT_SCALE_LABELS", "font_px",
    # 字体
    "monospace_family", "sans_family", "load_bundled_fonts", "apply_global_font",
]
