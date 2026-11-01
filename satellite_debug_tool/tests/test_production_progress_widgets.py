"""M48 节点、时间与夹具证据的独立呈现回归。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from satellite_debug_tool.core.production.models import AttemptStatus
from satellite_debug_tool.ui import production_progress_widgets as module
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.production_progress_widgets import (
    FixturePresentation,
    ProductionFixtureStrip,
    ProductionProgressPanel,
    StepPresentation,
)


def _steps() -> tuple[StepPresentation, ...]:
    return (
        StepPresentation("firmware", "Firmware version check", AttemptStatus.PASS),
        StepPresentation("static", "Static alignment", AttemptStatus.RUNNING, "Observation evidence"),
        StepPresentation("swing", "Tracking after lock", AttemptStatus.PENDING),
    )


def test_steps_keep_pending_nodes_and_preserve_identity_on_status_updates(qapplication_session):
    panel = ProductionProgressPanel()
    panel.set_steps(_steps())
    assert panel.node_ids == ("firmware", "static", "swing")
    current = panel._nodes["static"]
    assert current.property("statusLevel") == "current"
    assert panel._nodes["firmware"].property("statusLevel") == "complete"
    assert panel._nodes["swing"].property("statusLevel") == "pending"
    panel.set_steps((_steps()[0], replace(_steps()[1], status=AttemptStatus.FAIL), _steps()[2]))
    assert panel._nodes["static"] is current
    assert current.property("statusLevel") == "failed"
    assert "Observation evidence" in current.toolTip()
    panel.set_steps((_steps()[1],))
    assert panel.node_ids == ("static",)
    panel.close()


@pytest.mark.parametrize("status", [AttemptStatus.FAIL, AttemptStatus.INCOMPLETE, AttemptStatus.ABORTED])
def test_failure_states_keep_explicit_state_text(qapplication_session, status):
    panel = ProductionProgressPanel()
    panel.set_steps((StepPresentation("test", "Static alignment", status),))
    node = panel._nodes["test"]
    assert node.property("statusLevel") == "failed"
    assert node.state_label.text() == module.tr(module._STATUS_SOURCES[status])
    assert node.state_label.text() in node.accessibleName()
    panel.close()


def test_countdown_uses_snapshot_and_distinguishes_waiting_from_zero(qapplication_session):
    panel = ProductionProgressPanel()
    panel.set_progress(title_source="Static alignment", phase_source="Observing", ordinal=2,
                       total=3, elapsed_s=61.9, remaining_s=1800.2, reason="waiting for a frame")
    assert panel.remaining_label.text() == "00:30:01"
    assert "00:01:01" in panel.summary_label.text()
    assert panel.message_label.text() == "waiting for a frame"
    assert panel.findChildren(QTimer) == []
    QTest.qWait(25)
    assert panel.remaining_label.text() == "00:30:01"
    panel.set_progress(title_source="Static alignment", phase_source="Preparing", ordinal=2,
                       total=3, elapsed_s=65, remaining_s=None)
    assert panel.remaining_label.text() == "—"
    panel.set_progress(title_source="Static alignment", phase_source="Finishing", ordinal=2,
                       total=3, elapsed_s=1800, remaining_s=0)
    assert panel.remaining_label.text() == "00:00:00"
    panel.show_message("Waiting for analysis evidence")
    assert panel.message_label.text() == "Waiting for analysis evidence"
    assert panel.remaining_label.text() == "00:00:00"
    panel.close()


def test_live_status_change_repaints_the_existing_node(qapplication_session):
    panel = ProductionProgressPanel()
    panel.set_theme("light")
    step = StepPresentation("static", "Static alignment", AttemptStatus.RUNNING)
    panel.set_steps((step,))
    panel.show()
    qapplication_session.processEvents()
    node = panel._nodes["static"]
    assert node.state_label.palette().color(QPalette.ColorRole.WindowText) == QColor(S.palette("light")["accent"])
    panel.set_steps((replace(step, status=AttemptStatus.FAIL),))
    qapplication_session.processEvents()
    assert panel._nodes["static"] is node
    assert node.state_label.palette().color(QPalette.ColorRole.WindowText) == QColor(S.palette("light")["err"])
    panel.close()


def test_terminal_message_clears_expired_countdown_and_keeps_terminal_title(qapplication_session):
    panel = ProductionProgressPanel()
    assert panel.summary_label.text() == module.tr("Automatic execution has not started.")
    panel.set_progress(title_source="Static alignment", phase_source="Observing", ordinal=2,
                       total=3, elapsed_s=180, remaining_s=1620)
    assert panel.remaining_label.text() == "00:27:00"
    panel.show_message("Operator stopped the batch", clear_progress=True, title_source="Aborted")
    assert panel.remaining_label.text() == "—"
    assert panel.title_label.text() == module.tr("Aborted")
    assert panel.summary_label.text() == ""
    assert panel.phase_label.text() == ""
    assert panel.message_label.text() == "Operator stopped the batch"
    panel.retranslate_ui()
    assert panel.remaining_label.text() == "—"
    assert panel.title_label.text() == module.tr("Aborted")
    assert panel.summary_label.text() == ""
    panel.close()


def test_narrow_progress_scroll_reaches_last_node_and_keyboard_details(qapplication_session, monkeypatch):
    panel = ProductionProgressPanel()
    panel.set_steps(tuple(StepPresentation(str(i), f"Test stage {i}", AttemptStatus.PENDING,
                                           f"Evidence for {i}") for i in range(7)))
    panel.resize(390, panel.sizeHint().height())
    panel.show()
    qapplication_session.processEvents()
    assert panel._scroll.horizontalScrollBar().maximum() > 0
    last = panel._nodes["6"]
    assert last.focusPolicy() == Qt.FocusPolicy.StrongFocus
    panel._nodes["0"].setFocus()
    for _ in range(6):
        QTest.keyClick(qapplication_session.focusWidget(), Qt.Key.Key_Tab)
        qapplication_session.processEvents()
    qapplication_session.processEvents()
    assert last.hasFocus()
    assert panel._scroll.horizontalScrollBar().value() > 0
    shown = []
    monkeypatch.setattr(module.QToolTip, "showText", lambda *args: shown.append(args[1]))
    last.setFocus()
    QTest.keyClick(last, Qt.Key.Key_Space)
    assert last.accessibleDescription() == "Evidence for 6"
    assert "Evidence for 6" in shown[-1]
    assert len(panel.node_ids) == 7
    panel.close()


def test_retranslation_uses_sources_and_preserves_raw_evidence(qapplication_session, monkeypatch):
    panel = ProductionProgressPanel()
    panel.set_steps(_steps())
    panel.set_progress(title_source="Static alignment", phase_source="Observing", ordinal=2,
                       total=3, elapsed_s=10, remaining_s=20, reason="raw domain reason")
    strip = ProductionFixtureStrip()
    strip.set_fixtures((FixturePresentation("power", "LAN power supply", "Ready", "good", "24 V confirmed"),))

    def translated(source, **values):
        return f"translated:{source.format(**values)}"

    monkeypatch.setattr(module, "tr", translated)
    panel.retranslate_ui()
    strip.retranslate_ui()
    assert panel.title_label.text() == "translated:Static alignment"
    assert panel.phase_label.text() == "translated:Observing"
    assert panel._nodes["static"].title_label.text() == "translated:Static alignment"
    assert panel.message_label.text() == "raw domain reason"
    assert "translated:LAN power supply" in strip._chips["power"].text_label.text()
    assert strip._chips["power"].accessibleDescription() == "24 V confirmed"
    panel.retranslate_ui()
    assert "translated:translated:" not in panel.title_label.text()
    panel.close()
    strip.close()


def test_narrow_fixture_strip_scrolls_to_keyboard_focus(qapplication_session):
    strip = ProductionFixtureStrip()
    strip.set_fixtures(tuple(FixturePresentation(str(index), f"Fixture {index}", "Configured", "pending")
                             for index in range(6)))
    strip.resize(320, strip.height())
    strip.show()
    qapplication_session.processEvents()
    assert strip._scroll.horizontalScrollBar().maximum() > 0
    strip._chips["0"].setFocus()
    for _ in range(5):
        QTest.keyClick(qapplication_session.focusWidget(), Qt.Key.Key_Tab)
        qapplication_session.processEvents()
    assert strip._chips["5"].hasFocus()
    assert strip._scroll.horizontalScrollBar().value() > 0
    strip.close()


@pytest.mark.parametrize("theme", S.THEMES)
def test_theme_uses_shared_customer_palette(qapplication_session, theme):
    panel = ProductionProgressPanel()
    panel.set_steps(_steps())
    strip = ProductionFixtureStrip()
    strip.set_fixtures((FixturePresentation("power", "LAN power supply", "Ready", "good"),))
    panel.set_theme(theme)
    strip.set_theme(theme)
    palette = S.palette(theme)
    assert palette["card"] in panel.styleSheet()
    for role in ("accent", "ok", "err", "text_muted", "focus"):
        assert palette[role] in panel.styleSheet()
    for role in ("ok", "err", "text_muted", "focus"):
        assert palette[role] in strip.styleSheet()
    assert panel._nodes["static"].theme == theme
    panel.close()
    strip.close()


def test_fixture_level_is_explicit_and_evidence_is_keyboard_accessible(qapplication_session, monkeypatch):
    strip = ProductionFixtureStrip()
    fixtures = (
        FixturePresentation("power", "LAN power supply", "Configured", "pending", "IP 192.168.1.16"),
        FixturePresentation("motion", "Motion platform", "Running", "good", "Motion feedback confirmed"),
        FixturePresentation("car", "Vehicle gate", "Not applicable", "irrelevant", "Recipe does not use vehicle"),
    )
    strip.set_fixtures(fixtures)
    assert strip.fixtures == fixtures
    for fixture in fixtures:
        chip = strip._chips[fixture.fixture_id]
        assert isinstance(chip, QPushButton)
        assert chip.indicator.property("level") == fixture.level
        assert module.tr(fixture.state_source) in chip.accessibleName()
        assert fixture.evidence == chip.accessibleDescription()
    chip = strip._chips["power"]
    shown = []
    monkeypatch.setattr(module.QToolTip, "showText", lambda *args: shown.append(args[1]))
    QTest.keyClick(chip, Qt.Key.Key_Space)
    assert "IP 192.168.1.16" in shown[-1]
    assert strip.findChildren(QTimer) == []
    strip.set_fixtures((replace(fixtures[0], state_source="Ready", level="good"),))
    assert strip._chips["power"].indicator.property("level") == "good"
    assert len(strip.fixtures) == 1
    strip.close()


def test_ambiguous_presentation_inputs_are_rejected(qapplication_session):
    with pytest.raises(ValueError, match="level"):
        FixturePresentation("power", "LAN power supply", "Ready", "success")
    panel = ProductionProgressPanel()
    with pytest.raises(ValueError, match="unique"):
        panel.set_steps((_steps()[0], _steps()[0]))
    strip = ProductionFixtureStrip()
    fixture = FixturePresentation("power", "LAN power supply", "Ready", "good")
    with pytest.raises(ValueError, match="unique"):
        strip.set_fixtures((fixture, fixture))
    panel.close()
    strip.close()
