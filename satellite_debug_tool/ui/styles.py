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


def palette(is_dark: bool) -> dict:
    """返回当前主题下各类 widget 用的色值。各 UI 子组件统一调用，避免硬编码散落。"""
    if is_dark:
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
        }
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
    }
