"""批次进度和夹具状态的纯呈现控件。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from PySide6.QtCore import QPoint, QRectF, Qt, Slot
from PySide6.QtGui import QColor, QPaintEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.production.models import AttemptStatus
from satellite_debug_tool.i18n import tr, tr_source
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.semantic_style import set_semantic_property


@dataclass(frozen=True)
class StepPresentation:
    test_id: str
    title_source: str
    status: AttemptStatus
    detail: str = ""


@dataclass(frozen=True)
class FixturePresentation:
    fixture_id: str
    title_source: str
    state_source: str
    level: str
    evidence: str = ""

    def __post_init__(self) -> None:
        if self.level not in ("good", "pending", "irrelevant"):
            raise ValueError(f"Unknown fixture presentation level: {self.level}")


_STATUS_SOURCES = {
    AttemptStatus.PENDING: tr_source("Pending"),
    AttemptStatus.WAITING_PREREQUISITE: tr_source("Waiting for prerequisites"),
    AttemptStatus.ARMED: tr_source("Preparing"),
    AttemptStatus.RUNNING: tr_source("Running"),
    AttemptStatus.ANALYZING: tr_source("Analyzing"),
    AttemptStatus.PASS: tr_source("Pass"),
    AttemptStatus.FAIL: tr_source("Fail"),
    AttemptStatus.INCOMPLETE: tr_source("Incomplete"),
    AttemptStatus.SKIPPED: tr_source("Not applicable"),
    AttemptStatus.ABORTED: tr_source("Aborted"),
}


def _step_level(status: AttemptStatus) -> str:
    if status == AttemptStatus.PASS:
        return "complete"
    if status in (AttemptStatus.FAIL, AttemptStatus.INCOMPLETE, AttemptStatus.ABORTED):
        return "failed"
    if status in (
        AttemptStatus.WAITING_PREREQUISITE,
        AttemptStatus.ARMED,
        AttemptStatus.RUNNING,
        AttemptStatus.ANALYZING,
    ):
        return "current"
    return "pending"


def _clock_text(seconds: float, *, round_up: bool = False) -> str:
    """只格式化快照；剩余时间上取整，避免提前显示为零。"""
    seconds = float(seconds)
    if not math.isfinite(seconds):
        return "—"
    total = max(0, math.ceil(seconds) if round_up else math.floor(seconds))
    hours, tail = divmod(total, 3600)
    minutes, second = divmod(tail, 60)
    return f"{hours:02d}:{minutes:02d}:{second:02d}"


class _NodeGlyph(QWidget):
    """连接线和圆点属于同一个节点，缩放时始终对齐。"""

    def __init__(self, parent: "_StepNode") -> None:
        super().__init__(parent)
        self._node = parent
        self.setFixedHeight(32)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        node = self._node
        palette = S.palette(node.theme)
        role = {"complete": "ok", "current": "accent", "failed": "err", "pending": "text_muted"}[
            _step_level(node.presentation.status)
        ]
        color = QColor(palette[role])
        center = QPoint(self.width() // 2, 16)
        painter.setPen(QPen(QColor(palette["border"]), 2))
        if node.ordinal > 1:
            painter.drawLine(0, center.y(), center.x() - 14, center.y())
        if node.ordinal < node.total:
            painter.drawLine(center.x() + 14, center.y(), self.width(), center.y())
        active = _step_level(node.presentation.status) != "pending"
        painter.setPen(QPen(color, 2))
        painter.setBrush(color if active else QColor(palette["card"]))
        painter.drawEllipse(QRectF(center.x() - 11, 5, 22, 22))
        painter.setPen(QColor(palette["card"]) if active else color)
        font = self.font()
        font.setPixelSize(S.font_px(11, node.scale))
        font.setBold(True)
        painter.setFont(font)
        symbol = "✓" if node.presentation.status == AttemptStatus.PASS else str(node.ordinal)
        if _step_level(node.presentation.status) == "failed":
            symbol = "!"
        painter.drawText(QRectF(center.x() - 11, 5, 22, 22), Qt.AlignmentFlag.AlignCenter, symbol)


class _StepNode(QPushButton):
    def __init__(self, presentation: StepPresentation, parent: QWidget) -> None:
        super().__init__(parent)
        self.presentation = presentation
        self.ordinal = 1
        self.total = 1
        self.theme = "dark"
        self.scale = "small"
        self.setObjectName("productionProgressNode")
        self.setProperty("variant", "ghost")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(2)
        self.glyph = _NodeGlyph(self)
        self.title_label = QLabel(self)
        self.title_label.setObjectName("productionNodeTitle")
        self.state_label = QLabel(self)
        self.state_label.setObjectName("productionNodeState")
        for label in (self.title_label, self.state_label):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.glyph)
        layout.addWidget(self.title_label)
        layout.addWidget(self.state_label)
        self.clicked.connect(self._show_details)
        self.refresh()

    def refresh(self) -> None:
        title = tr(self.presentation.title_source)
        state = tr(_STATUS_SOURCES[self.presentation.status])
        self.title_label.setText(title)
        self.state_label.setText(state)
        detail = "\n".join(value for value in (title, state, self.presentation.detail) if value)
        self.setToolTip(detail)
        self.setAccessibleName(f"{title} · {state}")
        self.setAccessibleDescription(self.presentation.detail)
        set_semantic_property(self, "statusLevel", _step_level(self.presentation.status))
        set_semantic_property(self.state_label, "statusLevel", _step_level(self.presentation.status))
        self.setMinimumWidth(max(S.font_px(144, self.scale), self.title_label.sizeHint().width() + 20))
        self.setFixedHeight(self.layout().sizeHint().height())
        self.glyph.update()

    @Slot()
    def _show_details(self) -> None:
        QToolTip.showText(self.mapToGlobal(QPoint(self.width() // 2, self.height())), self.toolTip(), self)


class ProductionProgressPanel(QWidget):
    """只消费步骤和进度快照；页面隐藏或重排不会创建计时 owner。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("productionProgressPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._theme = "dark"
        self._scale = "small"
        self._steps: tuple[StepPresentation, ...] = ()
        self._nodes: dict[str, _StepNode] = {}
        self._progress: Optional[dict] = None
        self._message = ""
        self._idle_title_source = tr_source("Test progress")
        self._idle_summary_source = tr_source("Automatic execution has not started.")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 8)
        outer.setSpacing(4)
        header = QHBoxLayout()
        header.setSpacing(12)
        heading = QVBoxLayout()
        heading.setSpacing(2)
        self.title_label = QLabel()
        self.title_label.setObjectName("productionProgressTitle")
        self.title_label.setWordWrap(True)
        self.phase_label = QLabel()
        self.phase_label.setObjectName("productionProgressPhase")
        self.phase_label.setWordWrap(True)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("productionProgressSummary")
        self.summary_label.setWordWrap(True)
        heading.addWidget(self.title_label)
        heading.addWidget(self.phase_label)
        heading.addWidget(self.summary_label)
        header.addLayout(heading, 1)
        remaining = QVBoxLayout()
        remaining.setSpacing(0)
        self._remaining_caption = QLabel()
        self._remaining_caption.setObjectName("productionRemainingCaption")
        self.remaining_label = QLabel()
        self.remaining_label.setObjectName("productionProgressRemaining")
        for label in (self._remaining_caption, self.remaining_label):
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        remaining.addWidget(self._remaining_caption)
        remaining.addWidget(self.remaining_label)
        header.addLayout(remaining)
        outer.addLayout(header)
        self._scroll = QScrollArea()
        self._scroll.setObjectName("productionProgressScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._track = QWidget()
        self._track.setObjectName("productionProgressTrack")
        self._track_layout = QHBoxLayout(self._track)
        self._track_layout.setContentsMargins(0, 0, 0, 0)
        self._track_layout.setSpacing(0)
        self._track_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self._scroll.setWidget(self._track)
        outer.addWidget(self._scroll)
        self.message_label = QLabel()
        self.message_label.setObjectName("productionProgressMessage")
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.message_label)
        self.set_theme("dark")
        self.retranslate_ui()

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(step.test_id for step in self._steps)

    def set_steps(self, steps: Sequence[StepPresentation]) -> None:
        values = tuple(steps)
        ids = tuple(step.test_id for step in values)
        if len(set(ids)) != len(ids):
            raise ValueError("Step presentation IDs must be unique")
        if ids != self.node_ids:
            while self._track_layout.count():
                item = self._track_layout.takeAt(0)
                item.widget().deleteLater()
            self._nodes = {}
            for step in values:
                node = _StepNode(step, self._track)
                self._nodes[step.test_id] = node
                self._track_layout.addWidget(node, 1)
        self._steps = values
        for ordinal, step in enumerate(values, 1):
            node = self._nodes[step.test_id]
            node.presentation = step
            node.ordinal, node.total = ordinal, len(values)
            node.theme, node.scale = self._theme, self._scale
            node.refresh()
        self._scroll.setVisible(bool(values))
        self._resize_track_height()

    def set_progress(
        self, *, title_source: str, phase_source: str, ordinal: int, total: int,
        elapsed_s: float, remaining_s: Optional[float], reason: str = "",
    ) -> None:
        self._progress = dict(title_source=title_source, phase_source=phase_source,
                              ordinal=ordinal, total=total, elapsed_s=elapsed_s, remaining_s=remaining_s)
        self._message = reason
        self.retranslate_ui()

    def show_message(
        self, message: str, *, clear_progress: bool = False, title_source: str = "Test progress",
    ) -> None:
        self._message = str(message)
        if clear_progress:
            self._progress = None
            self._idle_title_source = title_source
            self._idle_summary_source = ""
        self.retranslate_ui()

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme, self._scale = theme, scale
        p = S.palette(theme)
        self.setStyleSheet(
            f"#productionProgressPanel {{ background: {p['card']}; border: 1px solid {p['border']}; "
            f"border-radius: 6px; }}"
            f"#productionProgressScroll, #productionProgressTrack {{ background: {p['card']}; border: 0; }}"
            f"#productionProgressTitle {{ color: {p['text']}; font-size: {S.font_px(17, scale)}px; font-weight: 700; }}"
            f"#productionProgressPhase {{ color: {p['accent']}; font-size: {S.font_px(13, scale)}px; }}"
            f"#productionProgressSummary, #productionRemainingCaption, #productionProgressMessage "
            f"{{ color: {p['text_muted']}; font-size: {S.font_px(11, scale)}px; }}"
            f"#productionProgressRemaining {{ color: {p['text']}; font-size: {S.font_px(32, scale)}px; "
            f"font-family: '{S.monospace_family()}'; font-weight: 700; }}"
            f"QPushButton#productionProgressNode {{ background: transparent; border: 1px solid transparent; "
            f"padding: 0; border-radius: 4px; }}"
            f"QPushButton#productionProgressNode:hover {{ background: {p['card_alt']}; }}"
            f"QPushButton#productionProgressNode:focus {{ border-color: {p['focus']}; }}"
            f"#productionNodeTitle {{ color: {p['text']}; font-size: {S.font_px(12, scale)}px; }}"
            f"#productionNodeState {{ color: {p['text_muted']}; font-size: {S.font_px(10, scale)}px; }}"
            f"#productionNodeState[statusLevel='complete'] {{ color: {p['ok']}; }}"
            f"#productionNodeState[statusLevel='current'] {{ color: {p['accent']}; }}"
            f"#productionNodeState[statusLevel='failed'] {{ color: {p['err']}; }}"
        )
        for node in self._nodes.values():
            node.theme, node.scale = theme, scale
            node.refresh()
        self._resize_track_height()

    @Slot()
    def retranslate_ui(self) -> None:
        self._remaining_caption.setText(tr("Condition remaining"))
        if self._progress is None:
            self.title_label.setText(tr(self._idle_title_source))
            self.phase_label.clear()
            self.summary_label.setText(tr(self._idle_summary_source) if self._idle_summary_source else "")
            self.remaining_label.setText("—")
        else:
            progress = self._progress
            self.title_label.setText(tr(progress["title_source"]))
            self.phase_label.setText(tr(progress["phase_source"]))
            self.summary_label.setText(tr(
                "Step {step}/{total} · Item elapsed {elapsed}", step=progress["ordinal"], total=progress["total"],
                elapsed=_clock_text(progress["elapsed_s"]),
            ))
            self.remaining_label.setText("—" if progress["remaining_s"] is None else
                                         _clock_text(progress["remaining_s"], round_up=True))
        self.remaining_label.setAccessibleName(f"{self._remaining_caption.text()}: {self.remaining_label.text()}")
        self.message_label.setText(self._message)
        self.message_label.setVisible(bool(self._message))
        for node in self._nodes.values():
            node.refresh()
        self._resize_track_height()

    def _resize_track_height(self) -> None:
        height = max((node.height() for node in self._nodes.values()), default=0)
        self._scroll.setFixedHeight(height + self._scroll.horizontalScrollBar().sizeHint().height() + 2)


class _FixtureChip(QPushButton):
    def __init__(self, presentation: FixturePresentation, parent: QWidget) -> None:
        super().__init__(parent)
        self.presentation = presentation
        self.setObjectName("productionFixtureChip")
        self.setProperty("variant", "ghost")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(7)
        self.indicator = QFrame()
        self.indicator.setObjectName("productionFixtureIndicator")
        self.indicator.setFixedSize(8, 8)
        self.text_label = QLabel()
        for child in (self.indicator, self.text_label):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.indicator)
        layout.addWidget(self.text_label)
        self.clicked.connect(self._show_evidence)
        self.refresh()

    def refresh(self) -> None:
        name, state = tr(self.presentation.title_source), tr(self.presentation.state_source)
        self.text_label.setText(f"{name} · {state}")
        self.setAccessibleName(f"{name} · {state}")
        self.setAccessibleDescription(self.presentation.evidence)
        self.setToolTip("\n".join(value for value in (f"{name} · {state}", self.presentation.evidence) if value))
        set_semantic_property(self.indicator, "level", self.presentation.level)
        self.setMinimumSize(self.layout().sizeHint())

    @Slot()
    def _show_evidence(self) -> None:
        QToolTip.showText(self.mapToGlobal(QPoint(self.width() // 2, self.height())), self.toolTip(), self)


class ProductionFixtureStrip(QWidget):
    """夹具灯消费明确的 level；文案与证据保持独立。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("productionFixtureStrip")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._fixtures: tuple[FixturePresentation, ...] = ()
        self._chips: dict[str, _FixtureChip] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setObjectName("productionFixtureScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._content = QWidget()
        self._content.setObjectName("productionFixtureContent")
        self._content_layout = QHBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(6)
        self._content_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self._scroll.setWidget(self._content)
        layout.addWidget(self._scroll)
        self.set_theme("dark")

    @property
    def fixtures(self) -> tuple[FixturePresentation, ...]:
        return self._fixtures

    def set_fixtures(self, fixtures: Sequence[FixturePresentation]) -> None:
        values = tuple(fixtures)
        ids = tuple(fixture.fixture_id for fixture in values)
        if len(set(ids)) != len(ids):
            raise ValueError("Fixture presentation IDs must be unique")
        if ids != tuple(fixture.fixture_id for fixture in self._fixtures):
            while self._content_layout.count():
                item = self._content_layout.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
            self._chips = {}
            for fixture in values:
                chip = _FixtureChip(fixture, self._content)
                self._chips[fixture.fixture_id] = chip
                self._content_layout.addWidget(chip)
            self._content_layout.addStretch(1)
        self._fixtures = values
        for fixture in values:
            chip = self._chips[fixture.fixture_id]
            chip.presentation = fixture
            chip.refresh()
        self._resize_strip_height()

    def set_theme(self, theme: str, scale: str = "small") -> None:
        p = S.palette(theme)
        self.setStyleSheet(
            f"#productionFixtureStrip, #productionFixtureScroll, #productionFixtureContent "
            f"{{ background: {p['card']}; border: 0; }}"
            f"QPushButton#productionFixtureChip {{ background: transparent; border: 1px solid transparent; "
            f"border-radius: 4px; padding: 0; }}"
            f"QPushButton#productionFixtureChip:hover {{ background: {p['card_alt']}; }}"
            f"QPushButton#productionFixtureChip:focus {{ border-color: {p['focus']}; }}"
            f"#productionFixtureChip QLabel {{ color: {p['text_muted']}; font-size: {S.font_px(11, scale)}px; }}"
            f"#productionFixtureIndicator {{ border: 0; border-radius: 4px; }}"
            f"#productionFixtureIndicator[level='good'] {{ background: {p['ok']}; }}"
            f"#productionFixtureIndicator[level='pending'] {{ background: {p['err']}; }}"
            f"#productionFixtureIndicator[level='irrelevant'] {{ background: {p['text_muted']}; }}"
        )
        for chip in self._chips.values():
            chip.refresh()
        self._resize_strip_height()

    @Slot()
    def retranslate_ui(self) -> None:
        for chip in self._chips.values():
            chip.refresh()
        self._resize_strip_height()

    def _resize_strip_height(self) -> None:
        height = max((chip.minimumHeight() for chip in self._chips.values()), default=0)
        self.setFixedHeight(height + self._scroll.horizontalScrollBar().sizeHint().height() + 2)


__all__ = ["StepPresentation", "FixturePresentation", "ProductionProgressPanel", "ProductionFixtureStrip"]
