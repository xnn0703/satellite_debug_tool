"""Mission Console 全局 QSS 生成器。

按 palette 角色生成一份覆盖标准 Qt chrome 控件的全局样式表（按钮 / 输入 / 下拉 /
复选框 / Tab / 滚动条 / 菜单 / 工具提示 / 表格 / 对话框）。在 MainWindow 主题切换时
`app.setStyleSheet(qss.build(theme))` 注入。

设计来源：handoff/components.css。Qt QSS 不支持 box-shadow / transition / color-mix /
letter-spacing 等 —— 已用 Qt 兼容写法替代（focus 用 1px accent 边框替代柔光环）。

变体按钮：给 QPushButton setProperty("variant", "primary"/"danger"/"ghost") 即命中下方规则
（设置后需 style().unpolish/polish 或在创建时设好）。
"""
from __future__ import annotations

from satellite_debug_tool.ui import styles as S


def _rgba(hex_color: str, alpha: float) -> str:
    """#RRGGBB + alpha(0~1) → rgba(r,g,b,a) Qt 字符串。"""
    h = hex_color.lstrip("#")
    if len(h) >= 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({r},{g},{b},{alpha:.3f})"
    return hex_color


def build(theme: str = "dark", scale: str = "small") -> str:
    """生成完整全局 QSS 字符串。"""
    p = S.palette(theme)
    fz = S.font_px(13, scale)        # 基准正文
    fz_sm = S.font_px(12, scale)
    fz_xs = S.font_px(11, scale)
    mono = S.monospace_family()

    accent = p["accent"]
    accent_soft = _rgba(accent, 0.12)
    accent_ink = p["accent_ink"]
    err = p["err"]
    err_soft = _rgba(err, 0.14)

    return f"""
/* ===== 基础 ===== */
QWidget {{
    background-color: {p['bg']};
    color: {p['text']};
    font-size: {fz}px;
}}
QToolTip {{
    background-color: {p['card_2']};
    color: {p['text']};
    border: 1px solid {p['border_2']};
    border-radius: 5px;
    padding: 4px 8px;
}}

/* ===== 按钮（默认 = 次按钮） ===== */
QPushButton {{
    background-color: {p['card_2']};
    color: {p['text']};
    border: 1px solid {p['border_2']};
    border-radius: 5px;
    padding: 4px 11px;
    font-size: {fz_sm}px;
}}
QPushButton:hover {{
    background-color: {p['card_hover']};
    border-color: {p['text_3']};
}}
QPushButton:pressed {{
    background-color: {p['card']};
}}
QPushButton:disabled {{
    color: {p['text_3']};
    background-color: {p['panel']};
    border-color: {p['border']};
}}
QPushButton:checked {{
    background-color: {accent_soft};
    border-color: {accent};
    color: {p['accent_2']};
}}

/* 主按钮 variant=primary（Connect 等） */
QPushButton[variant="primary"] {{
    background-color: {accent};
    color: {accent_ink};
    border: 1px solid {accent};
    font-weight: 600;
}}
QPushButton[variant="primary"]:hover {{
    background-color: {p['accent_2']};
    border-color: {p['accent_2']};
}}
QPushButton[variant="primary"]:disabled {{
    background-color: {p['panel']};
    color: {p['text_3']};
    border-color: {p['border']};
}}

/* 危险 variant=danger */
QPushButton[variant="danger"] {{
    background-color: {err_soft};
    color: {err};
    border: 1px solid {_rgba(err, 0.4)};
}}
QPushButton[variant="danger"]:hover {{
    background-color: {_rgba(err, 0.22)};
}}

/* 幽灵 variant=ghost */
QPushButton[variant="ghost"] {{
    background-color: transparent;
    color: {p['text_2']};
    border: 1px solid transparent;
}}
QPushButton[variant="ghost"]:hover {{
    background-color: {p['card_2']};
    color: {p['text']};
}}

/* ===== 输入框 / 下拉 / 数字框 ===== */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {p['input_bg']};
    color: {p['text']};
    border: 1px solid {p['border_2']};
    border-radius: 5px;
    padding: 3px 8px;
    selection-background-color: {accent};
    selection-color: {accent_ink};
    font-family: "{mono}";
    font-size: {fz_sm}px;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {accent};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: {p['text_3']};
    background-color: {p['panel']};
}}
QComboBox::drop-down {{
    border: 0;
    width: 18px;
}}
QComboBox::down-arrow {{
    width: 0; height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p['text_2']};
    margin-right: 6px;
}}
QComboBox QAbstractItemView {{
    background-color: {p['card']};
    color: {p['text']};
    border: 1px solid {p['border_2']};
    border-radius: 5px;
    selection-background-color: {accent_soft};
    selection-color: {p['accent_2']};
    outline: 0;
    padding: 2px;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 14px;
    background-color: {p['card_2']};
    border: 0;
}}
QSpinBox::up-arrow {{
    width: 0; height: 0;
    border-left: 3px solid transparent; border-right: 3px solid transparent;
    border-bottom: 4px solid {p['text_2']};
}}
QSpinBox::down-arrow {{
    width: 0; height: 0;
    border-left: 3px solid transparent; border-right: 3px solid transparent;
    border-top: 4px solid {p['text_2']};
}}

/* ===== 复选框 ===== */
QCheckBox {{
    color: {p['text']};
    spacing: 6px;
    font-size: {fz_sm}px;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    border: 1.5px solid {p['text_3']};
    border-radius: 3px;
    background: transparent;
}}
QCheckBox::indicator:hover {{
    border-color: {accent};
}}
QCheckBox::indicator:checked {{
    background-color: {accent};
    border-color: {accent};
    image: none;
}}

/* ===== Tab ===== */
QTabWidget::pane {{
    border: 1px solid {p['border']};
    background: {p['bg']};
    top: -1px;
}}
QTabBar::tab {{
    background: {p['panel']};
    color: {p['text_2']};
    padding: 6px 18px;
    border: 1px solid {p['border']};
    border-bottom: 0;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
    margin-right: 2px;
    font-size: {fz_sm}px;
}}
QTabBar::tab:hover {{
    color: {p['text']};
    background: {p['card_2']};
}}
QTabBar::tab:selected {{
    background: {p['bg']};
    color: {p['accent_2']};
    border-color: {p['border_2']};
    border-bottom: 2px solid {accent};
}}

/* ===== 工具栏 ===== */
QToolBar {{
    background-color: {p['panel']};
    border: 0;
    border-bottom: 1px solid {p['border']};
    spacing: 4px;
    padding: 3px 6px;
}}
QToolBar::separator {{
    background: {p['border']};
    width: 1px;
    margin: 4px 4px;
}}

/* ===== 滚动条（细，Mission Console 风） ===== */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {p['border_2']};
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {p['text_3']}; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {p['border_2']};
    border-radius: 5px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p['text_3']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; background: none; border: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* ===== 分组框 ===== */
QGroupBox {{
    background-color: {p['card']};
    border: 1px solid {p['border']};
    border-radius: 7px;
    margin-top: 10px;
    padding: 10px 10px 8px;
    font-size: {fz_sm}px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
    color: {p['text_2']};
}}

/* ===== 表格 ===== */
QTableWidget, QTableView {{
    background-color: {p['card']};
    alternate-background-color: {p['card_2']};
    color: {p['text']};
    gridline-color: {p['border']};
    border: 1px solid {p['border']};
    border-radius: 5px;
    selection-background-color: {accent_soft};
    selection-color: {p['accent_2']};
    outline: 0;
}}
QHeaderView::section {{
    background-color: {p['panel_2']};
    color: {p['text_3']};
    border: 0;
    border-right: 1px solid {p['border']};
    border-bottom: 1px solid {p['border']};
    padding: 5px 8px;
    font-size: {fz_xs}px;
    font-weight: 600;
}}

/* ===== 菜单 ===== */
QMenu {{
    background-color: {p['card']};
    color: {p['text']};
    border: 1px solid {p['border_2']};
    border-radius: 7px;
    padding: 4px;
}}
QMenu::item {{
    padding: 5px 18px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {accent_soft};
    color: {p['accent_2']};
}}
QMenu::separator {{
    height: 1px;
    background: {p['border']};
    margin: 4px 6px;
}}

/* ===== 对话框 / 状态栏 ===== */
QDialog {{ background-color: {p['panel']}; }}
QStatusBar {{
    background-color: {p['panel']};
    color: {p['text_2']};
    border-top: 1px solid {p['border']};
}}
QStatusBar::item {{ border: 0; }}

/* ===== 进度条 ===== */
QProgressBar {{
    background-color: {p['panel_2']};
    border: 1px solid {p['border']};
    border-radius: 5px;
    text-align: center;
    color: {p['text_2']};
    font-family: "{mono}";
    font-size: {fz_xs}px;
    height: 16px;
}}
QProgressBar::chunk {{
    background-color: {accent};
    border-radius: 4px;
}}

/* ===== 文本编辑 / 列表 ===== */
QTextEdit, QPlainTextEdit, QListWidget, QListView {{
    background-color: {p['card']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: 5px;
    selection-background-color: {accent_soft};
    selection-color: {p['accent_2']};
}}
QListWidget::item:selected, QListView::item:selected {{
    background-color: {accent_soft};
    color: {p['accent_2']};
}}
"""
