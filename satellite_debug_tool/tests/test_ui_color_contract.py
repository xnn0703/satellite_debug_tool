"""M38：验证真实文字可读性与已有呈现随主题同步。"""

import pytest

from satellite_debug_tool.core.data import EventLog
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import EventReport, StateDefEntry, StateType
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.event_timeline_widget import EventTimelineWidget
from satellite_debug_tool.ui.state_panel_widget import StateItemRow


def contrast(first: str, second: str) -> float:
    def luminance(color: str) -> float:
        rgb = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    low, high = sorted((luminance(first), luminance(second)))
    return (high + 0.05) / (low + 0.05)


def test_existing_event_text_is_readable_after_switch_to_light(qapplication_session):
    log = EventLog()
    widget = EventTimelineWidget(log)
    try:
        log.add("afd01c", EventReport(timestamp=1, event_id=1), ProfileStore())
        widget.set_theme("light", "small")
        item = widget._list.item(0)
        assert item is not None
        assert contrast(item.foreground().color().name(), S.palette("light")["card"]) >= 4.5
    finally:
        widget.deleteLater()


def test_unknown_state_recolors_without_new_device_report(qapplication_session):
    row = StateItemRow(StateDefEntry(1, int(StateType.BOOL), 0, "LOCK"))
    try:
        row.set_theme("light", "small")
        assert S.palette("light")["text_faint"] in row._value_label.styleSheet()
        assert row._value_label.text() == "—"
    finally:
        row.deleteLater()


@pytest.mark.parametrize("theme", S.THEMES)
def test_effective_text_and_action_pairs_have_sufficient_contrast(theme):
    p = S.palette(theme)
    for foreground in ("text", "text_muted", "text_faint", "ok", "warn", "err", "info", "recording"):
        for background in ("bg", "panel", "card", "card_alt", "card_hover"):
            assert contrast(p[foreground], p[background]) >= 4.5, (theme, foreground, background)
    assert contrast(p["selected_text"], p["selected_bg"]) >= 4.5
    for role in ("primary_bg", "primary_hover", "primary_pressed"):
        assert contrast(p["primary_text"], p[role]) >= 4.5, (theme, role)
    for role in ("bg", "panel", "card", "selected_bg"):
        assert contrast(p["focus"], p[role]) >= 3
    assert contrast(p["input_border"], p["input_bg"]) >= 3
    for background in ("progress_bg", "panel_2", "input_bg"):
        assert contrast(p["progress_text"], p[background]) >= 4.5
    assert contrast(p["progress_bg"], p["panel_2"]) >= 3
    if theme == "dark_hc":
        assert contrast(p["text"], p["card_hover"]) >= 7


@pytest.mark.parametrize("theme", S.THEMES)
def test_data_categories_are_readable_in_all_themes(theme):
    p = S.palette(theme)
    colors = (*S.SIGNAL_PALETTE, *S.ATTITUDE_COLORS.values(), *S.GNSS_SYSTEM_COLORS.values(),
              *S.GNSS_BAND_COLORS.values(), *S.ANSI_FOREGROUND.values())
    for source in colors:
        adjusted = S.chart_color(source, theme)
        for background in ("bg", "panel", "card", "card_alt"):
            assert contrast(adjusted, p[background]) >= 4.5, (theme, source, background)


def test_chart_colors_accept_the_existing_qt_color_contract():
    assert S.chart_color("#123", "light") == S.chart_color("#112233", "light")
    assert S.chart_color("red", "dark") == S.chart_color("#ff0000", "dark")


def test_recording_marker_recolors_without_changing_recording_fact(qapplication_session):
    from satellite_debug_tool.core.data import StateStore
    from satellite_debug_tool.ui.status_strip_widget import StatusStripWidget

    widget = StatusStripWidget(ProfileStore(), StateStore())
    try:
        widget.set_recording(True)
        widget.set_link_state(connected=True)
        for theme in S.THEMES:
            widget.set_theme(theme, "small")
            assert S.palette(theme)["recording"] in widget._recording_chip._dot_label.styleSheet()
            assert widget._recording_chip._text.text() == "REC ●"
            assert S.palette(theme)["ok"] in widget._link_chip._dot_label.styleSheet()
    finally:
        widget.deleteLater()


def test_shell_existing_ansi_and_timestamps_recolor_while_retaining_text(
    qapplication_session, tmp_path, monkeypatch,
):
    from pathlib import Path
    from PySide6.QtGui import QTextCursor
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.ui.tcp_shell_workspace import TcpShellWorkspace, _TIMESTAMP_LEN

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = TcpShellWorkspace(Settings())
    try:
        workspace.feed_rx(b"\x1b[32mgreen\x1b[0m plain\r")
        original = workspace._terminal.toPlainText()
        terminal_column = workspace._screen.column
        workspace.set_theme("light")
        cursor = QTextCursor(workspace._terminal.document())
        for position in (1, _TIMESTAMP_LEN + 1):
            cursor.setPosition(position)
            cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor)
            color = cursor.charFormat().foreground().color().name()
            assert contrast(color, S.palette("light")["card"]) >= 4.5
        assert workspace._terminal.toPlainText() == original
        assert workspace._screen.column == terminal_column
    finally:
        workspace.shutdown()
        workspace.deleteLater()


def test_plot_and_channel_swatch_share_the_actual_theme_color(qapplication_session):
    from satellite_debug_tool.core.protocol import ChannelDefEntry
    from satellite_debug_tool.ui.channel_panel import ChannelPanel
    from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget

    profiles = ProfileStore()
    profiles.apply_channel_define("hw", 1, [
        ChannelDefEntry(5, 1, 0, 0, "roll", "deg", -180, 180),
        ChannelDefEntry(2, 1, 1, 0, "yaw", "deg", -180, 180),
    ])
    chart = GroupedChartWidget()
    chart.set_profile_store(profiles)
    chart.set_hw_type("hw")
    panel = ChannelPanel()
    try:
        for theme in S.THEMES:
            chart.set_theme(theme, "small")
            panel.apply_theme(theme, "small")
            for name, color in chart.channel_colors().items():
                if name not in panel.channel_names():
                    panel.add_channel(name, "#123", name)
                panel.set_channel_color(name, color)
                assert color.lower() in panel._rows[name]._dot.styleSheet().lower()
    finally:
        chart.deleteLater()
        panel.deleteLater()
