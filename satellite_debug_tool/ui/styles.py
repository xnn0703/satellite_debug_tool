"""UI 颜色单一来源：三主题角色、数据类别色与字体工具。"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, Literal, Union

Theme = Literal["dark", "dark_hc", "light"]
THEMES: tuple[str, ...] = ("dark", "dark_hc", "light")
THEME_LABELS: Dict[str, str] = {"dark": "Dark", "dark_hc": "High contrast", "light": "Light"}


def _normalize_theme(theme: Union[str, bool, None]) -> str:
    if isinstance(theme, bool):
        return "dark" if theme else "light"
    value = str(theme or "dark").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {"深色": "dark", "dark_theme": "dark", "浅色": "light", "light_theme": "light",
               "深色_高对比": "dark_hc", "高对比": "dark_hc", "hc": "dark_hc",
               "highcontrast": "dark_hc", "dark_highcontrast": "dark_hc"}
    return value if value in THEMES else aliases.get(value, "dark")


# 表面逐层提亮；操作前景与实底按钮分别配对。
_THEME_COLORS = {
    "dark": dict(bg="#18191B", panel="#202225", card="#25272B", card_alt="#292C31",
                 border="#36393F", text="#E8E9EC", text_muted="#ADB1BA", text_faint="#A2A8B3",
                 input_bg="#25272B", input_border="#747B87", button_bg="#292C31",
                 card_hover="#32353B", selected_bg="#2C3442", selected_text="#E8E9EC",
                 selected_border="#85ADFF", focus="#A9C7FF", accent="#85ADFF",
                 accent_2="#A9C7FF", accent_dim="#5B8CE0", accent_ink="#111C32",
                 primary_bg="#345FAE", primary_hover="#3C6ABD", primary_pressed="#2B5098",
                 primary_text="#FFFFFF", ok="#79C59B", warn="#E6B566", err="#F19B9B",
                 info="#85ADFF", violet="#C0A8EA", recording="#F19B9B", grid_line="#FFFFFF12"),
    "dark_hc": dict(bg="#000000", panel="#080808", card="#121212", card_alt="#1B1B1B",
                    border="#8B8B8B", text="#FFFFFF", text_muted="#E0E0E0", text_faint="#C0C0C0",
                    input_bg="#121212", input_border="#A0A0A0", button_bg="#1B1B1B",
                    card_hover="#252525", selected_bg="#16355D", selected_text="#FFFFFF",
                    selected_border="#9DC4FF", focus="#FFFFFF", accent="#9DC4FF",
                    accent_2="#C7DDFF", accent_dim="#6FA7FF", accent_ink="#000000",
                    primary_bg="#164A9A", primary_hover="#245BAD", primary_pressed="#123D80",
                    primary_text="#FFFFFF", ok="#7FF0AE", warn="#FFD580", err="#FFACAC",
                    info="#9DC4FF", violet="#DEC4FF", recording="#FFACAC", grid_line="#FFFFFF24"),
    "light": dict(bg="#F4F5F7", panel="#FFFFFF", card="#FFFFFF", card_alt="#F0F1F3",
                  border="#D5D8DE", text="#25282D", text_muted="#5D636E", text_faint="#5D636E",
                  input_bg="#FFFFFF", input_border="#7A808B", button_bg="#F0F1F3",
                  card_hover="#E6E8EB", selected_bg="#E9EFFA", selected_text="#234F9A",
                  selected_border="#245EC7", focus="#174C9E", accent="#245EC7",
                  accent_2="#234F9A", accent_dim="#174C9E", accent_ink="#FFFFFF",
                  primary_bg="#245EC7", primary_hover="#1E51B0", primary_pressed="#19438F",
                  primary_text="#FFFFFF", ok="#226D43", warn="#88520A", err="#B12E37",
                  info="#245EC7", violet="#7042A0", recording="#B12E37", grid_line="#25282D12"),
}


def palette(theme: Union[str, bool, None] = "dark") -> Dict[str, str]:
    """返回独立角色字典；历史键继续从同一权威色板派生。"""
    p = dict(_THEME_COLORS[_normalize_theme(theme)])
    p.update(panel_2=p["card"], card_2=p["card_alt"], border_2=p["input_border"],
             value_number=p["text"], text_2=p["text_muted"], text_3=p["text_faint"],
             text_inv=p["primary_text"], success=p["ok"], warning=p["warn"], error=p["err"],
             primary=p["accent"], bg_grid=p["bg"], border_glow=p["accent"] + "33")
    # 百分比文字同时跨越已完成与剩余区，两种背景共同满足对比度。
    p["progress_bg"] = {"dark": "#5075A5", "dark_hc": "#456EA5", "light": "#6E95D0"}[_normalize_theme(theme)]
    p["progress_text"] = p["text"] if _normalize_theme(theme) == "light" else "#FFFFFF"
    for role in ("accent", "ok", "warn", "err", "info"):
        p[role + "_soft"] = p[role] + "14"
    return p


# 历史导出继续履行已有调用合同，所有值均由主题中心派生。
BG_DARK, PANEL_DARK, BORDER, TEXT, PRIMARY, SUCCESS, WARNING, ERROR, INPUT_DARK, BUTTON_DARK = (
    palette("dark")[key] for key in
    ("bg", "panel", "border", "text", "primary", "success", "warning", "error", "input_bg", "button_bg")
)
BG_LIGHT, PANEL_LIGHT, BORDER_LIGHT, TEXT_LIGHT, TEXT_LIGHT_MUTED, PRIMARY_LIGHT, INPUT_LIGHT, BUTTON_LIGHT = (
    palette("light")[key] for key in
    ("bg", "panel", "border", "text", "text_muted", "primary", "input_bg", "button_bg")
)
CARD_LIGHT, CARD_DARK = palette("light")["card"], palette("dark")["card"]
CARD_ALT_DARK, CARD_ALT_LIGHT = palette("dark")["card_alt"], palette("light")["card_alt"]

# 数据颜色保留既有类别顺序，主题只调整在真实表面上的亮度。
SIGNAL_PALETTE: tuple[str, ...] = (
    "#2DD4BF", "#38BDF8", "#FBBF24", "#FB7185", "#A78BFA", "#4ADE80", "#FB923C", "#22D3EE",
    "#F472B6", "#A3E635", "#818CF8", "#FCD34D", "#2DD4BF", "#F87171", "#60A5FA", "#C084FC",
)
ATTITUDE_COLORS = {"roll": "#5B8FF9", "pitch": "#00B8A9", "yaw": "#F6BD16"}
GNSS_SYSTEM_COLORS = {0: "#3B82F6", 1: "#EF4444", 2: "#F59E0B", 3: "#A855F7",
                      4: "#22C55E", 5: "#06B6D4", 6: "#EC4899", 7: "#94A3B8"}
GNSS_BAND_COLORS = {"L1": "#F59E0B", "L2": "#22C55E", "L5": "#2563EB", "G1": "#F59E0B", "G2": "#22C55E",
                    "E1": "#F59E0B", "E5a": "#2563EB", "E5b": "#22C55E", "E6": "#A855F7",
                    "E5 AltBOC": "#06B6D4", "B1": "#F59E0B", "B2": "#22C55E", "B3": "#2563EB"}
ANSI_FOREGROUND = {30: "#5C6370", 31: "#E06C75", 32: "#98C379", 33: "#E5C07B",
                   34: "#61AFEF", 35: "#C678DD", 36: "#56B6C2", 37: "#D7DAE0",
                   90: "#7F848E", 91: "#FF7B86", 92: "#B5E890", 93: "#FFD479",
                   94: "#84C1FF", 95: "#E2A2FF", 96: "#8CE0E8", 97: "#FFFFFF"}


def enum_role(level: int) -> str:
    return {0: "ok", 1: "warn", 2: "err"}.get(level, "text_muted")


def enum_color(level: int, theme: Union[str, bool] = "dark") -> str:
    """DEBUG ENUM 的 0/1/2/3 分别表示正常/告警/错误/中性。"""
    return palette(theme)[enum_role(level)]


def event_color(level: int, theme: Union[str, bool] = "dark") -> str:
    """事件 INFO 表示信息，独立于 ENUM 的正常级别。"""
    return palette(theme)[{1: "info", 2: "warn", 3: "err"}.get(level, "text_muted")]


def _luminance(color: str) -> float:
    values = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return sum((v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4) * w
               for v, w in zip(values, (0.2126, 0.7152, 0.0722)))


@lru_cache(maxsize=1024)
def chart_color(color: str, theme: Union[str, bool] = "dark") -> str:
    """类别身份保持稳定；通过集中亮度映射保证图例文字与数据线均可读。"""
    from PySide6.QtGui import QColor

    source = QColor(color)
    if source.isValid():
        color = source.name()
    else:
        raise ValueError(f"Invalid chart color: {color}")
    p = palette(theme)
    bg = _luminance(p["card_hover"])
    target = 0 if _normalize_theme(theme) == "light" else 255
    rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    for step in range(101):
        adjusted = "#" + "".join(f"{round(v + (target - v) * step / 100):02X}" for v in rgb)
        low, high = sorted((_luminance(adjusted), bg))
        if (high + 0.05) / (low + 0.05) >= 4.5:
            return adjusted
    return p["text"]


def signal_palette(theme: Union[str, bool] = "dark") -> tuple[str, ...]:
    return tuple(chart_color(color, theme) for color in SIGNAL_PALETTE)


def qss_color(color: str) -> str:
    """应用色板使用 RGBA，统一转换为 Qt 无歧义的 rgba() 写法。"""
    if len(color) == 9 and color.startswith("#"):
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        return f"rgba({rgb[0]},{rgb[1]},{rgb[2]},{int(color[7:9], 16)})"
    return color


def apply_application_palette(app, theme: str) -> None:
    """Qt 原生绘制、占位文字及弹出视图与 QSS 消费同一颜色角色。"""
    from PySide6.QtGui import QColor, QPalette

    p = palette(theme)
    qt_palette = QPalette()
    roles = {"Window": "bg", "WindowText": "text", "Base": "input_bg", "AlternateBase": "card_alt",
             "Text": "text", "Button": "button_bg", "ButtonText": "text", "BrightText": "text",
             "ToolTipBase": "card", "ToolTipText": "text", "Highlight": "selected_bg",
             "HighlightedText": "selected_text", "PlaceholderText": "text_muted", "Link": "accent",
             "LinkVisited": "violet"}
    for name, role in roles.items():
        qt_palette.setColor(getattr(QPalette.ColorRole, name), QColor(p[role]))
    app.setPalette(qt_palette)


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
    "Theme", "THEMES", "THEME_LABELS", "palette", "qss_color", "enum_role", "enum_color", "event_color", "apply_application_palette",
    "chart_color", "signal_palette", "ATTITUDE_COLORS", "GNSS_SYSTEM_COLORS", "GNSS_BAND_COLORS",
    "ANSI_FOREGROUND",
    # 字号
    "FontScale", "FONT_SCALES", "FONT_SCALE_LABELS", "font_px",
    # 字体
    "monospace_family", "sans_family", "load_bundled_fonts", "apply_global_font",
]
