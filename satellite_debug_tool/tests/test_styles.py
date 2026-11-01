"""M6: styles 模块 — 三档主题 + 四档字号 + 等宽字体探测。"""

import pytest

from satellite_debug_tool.ui import styles as S


# ---- palette 契约 ----

_REQUIRED_KEYS = {
    "bg", "panel", "card", "card_alt", "border",
    "text", "text_muted", "text_faint",
    "input_bg", "input_border",
    "button_bg",
    "value_number",
    "success", "warning", "error", "primary",
}


class TestPalette:
    @pytest.mark.parametrize("theme", ["dark", "dark_hc", "light"])
    def test_palette_complete(self, theme):
        p = S.palette(theme)
        missing = _REQUIRED_KEYS - set(p.keys())
        assert not missing, f"{theme} 缺少键: {missing}"
        # 所有值都是 "#" 开头的 7 位 hex
        for k, v in p.items():
            assert isinstance(v, str) and v.startswith("#") and len(v) in (4, 7, 9), (
                f"{theme}.{k} 非法色值 {v!r}"
            )

    def test_palette_bool_compat(self):
        """bool 老签名向下兼容：True→dark，False→light。"""
        assert S.palette(True) == S.palette("dark")
        assert S.palette(False) == S.palette("light")

    def test_palette_none_default(self):
        assert S.palette(None) == S.palette("dark")

    def test_palette_unknown_fallback(self):
        # 未知主题名回落到 dark，不应抛
        assert S.palette("weird") == S.palette("dark")

    def test_dark_hc_higher_contrast(self):
        """dark_hc 应比 dark 更黑底更白字。"""
        dark = S.palette("dark")
        hc = S.palette("dark_hc")
        assert hc["bg"] == "#000000"
        # 主文字色应至少与 dark 同亮，通常更亮
        assert hc["text"].lower() in ("#f5f5f5", "#ffffff")
        assert hc["text"] != dark["text"]  # 区别于普通 dark

    def test_theme_list(self):
        assert S.THEMES == ("dark", "dark_hc", "light")
        for t in S.THEMES:
            assert t in S.THEME_LABELS


# ---- font_px ----

class TestFontPx:
    def test_baseline_is_identity(self):
        # small = 1.0 → 基准不变
        assert S.font_px(12, "small") == 12
        assert S.font_px(14, "small") == 14

    def test_xlarge_reaches_22(self):
        # 基准 12, xlarge(1.83) ≈ 21.96 → 22
        assert S.font_px(12, "xlarge") == 22

    def test_medium_and_large(self):
        assert S.font_px(12, "medium") == 14   # 12*1.17 ≈ 14.04
        assert S.font_px(12, "large") == 18    # 12*1.5

    def test_unknown_scale_falls_back_medium(self):
        assert S.font_px(12, "huge") == S.font_px(12, "medium")

    def test_numeric_scale_accepted(self):
        assert S.font_px(10, 2.0) == 20

    def test_scale_label_labels(self):
        assert set(S.FONT_SCALES.keys()) == {"small", "medium", "large", "xlarge"}
        assert set(S.FONT_SCALE_LABELS.keys()) == set(S.FONT_SCALES.keys())

    def test_min_clamp(self):
        # 不允许缩小到小于 6px（可读下限）
        assert S.font_px(1, 0.1) >= 6


# ---- monospace family ----

class TestMonospaceFamily:
    def test_returns_non_empty_string(self):
        name = S.monospace_family()
        assert isinstance(name, str) and len(name) > 0

    def test_no_crash_without_qapp(self):
        """无 QGuiApplication 时不应崩溃（返回占位 "monospace"）。"""
        name = S.monospace_family()
        assert name  # 至少非空；具体值取决于系统字体


@pytest.mark.parametrize("theme", ["dark", "dark_hc", "light"])
@pytest.mark.parametrize("control_kind", ["checkbox", "radio"])
def test_selection_control_focus_preserves_size(qapplication_session, theme, control_kind) -> None:
    """键盘焦点只改变选择控件边框颜色，普通态与焦点态拥有同一尺寸。"""
    from PySide6.QtCore import QSize
    from PySide6.QtWidgets import QCheckBox, QRadioButton, QStyle, QStyleOptionButton
    from satellite_debug_tool.ui.qss import build

    app = qapplication_session
    original_style = app.styleSheet()
    widget = None
    try:
        app.setStyleSheet(build(theme))
        control_type, content_type = {
            "checkbox": (QCheckBox, QStyle.ContentsType.CT_CheckBox),
            "radio": (QRadioButton, QStyle.ContentsType.CT_RadioButton),
        }[control_kind]
        widget = control_type("Focus control")
        widget.ensurePolished()
        option = QStyleOptionButton()
        widget.initStyleOption(option)
        option.state &= ~QStyle.StateFlag.State_HasFocus
        content_size = QSize(60, 14)
        normal = widget.style().sizeFromContents(content_type, option, content_size, widget)
        option.state |= QStyle.StateFlag.State_HasFocus
        focused = widget.style().sizeFromContents(content_type, option, content_size, widget)
        assert focused == normal
    finally:
        if widget is not None:
            widget.close()
            widget.deleteLater()
        app.setStyleSheet(original_style)
        app.processEvents()
