"""Compact polar indicator for customer-facing antenna beam direction."""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from satellite_debug_tool.ui import styles as S


def beam_endpoint(
    azimuth_deg: float,
    off_axis_deg: float,
    *,
    max_off_axis_deg: float = 90.0,
) -> Optional[tuple[float, float]]:
    """Return a normalized endpoint with 0 degrees up and clockwise positive."""

    values = (azimuth_deg, off_axis_deg, max_off_axis_deg)
    if not all(math.isfinite(float(value)) for value in values):
        return None
    if off_axis_deg < 0.0 or max_off_axis_deg <= 0.0:
        return None
    radius = min(float(off_axis_deg) / float(max_off_axis_deg), 1.0)
    angle = math.radians(float(azimuth_deg) % 360.0)
    return math.sin(angle) * radius, -math.cos(angle) * radius


class BeamPolarWidget(QWidget):
    """Draw beam azimuth and off-axis angle without an OpenGL dependency."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._theme = "dark"
        self._azimuth_deg: Optional[float] = None
        self._off_axis_deg: Optional[float] = None
        self._stale = False
        self.setMinimumSize(210, 210)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:
        return QSize(270, 270)

    def set_theme(self, theme: str) -> None:
        self._theme = S._normalize_theme(theme)
        self.update()

    def set_beam(
        self,
        azimuth_deg: Optional[float],
        off_axis_deg: Optional[float],
        *,
        stale: bool = False,
    ) -> None:
        endpoint = None
        if azimuth_deg is not None and off_axis_deg is not None:
            endpoint = beam_endpoint(float(azimuth_deg), float(off_axis_deg))
        if endpoint is None:
            self._azimuth_deg = None
            self._off_axis_deg = None
        else:
            self._azimuth_deg = float(azimuth_deg) % 360.0
            self._off_axis_deg = float(off_axis_deg)
        self._stale = bool(stale)
        self.update()

    def has_beam(self) -> bool:
        return self._azimuth_deg is not None and self._off_axis_deg is not None

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pal = S.palette(self._theme)

        label_margin = 29.0
        diameter = max(2.0, min(self.width(), self.height()) - 2.0 * label_margin)
        radius = diameter / 2.0
        center = QPointF(self.width() / 2.0, self.height() / 2.0)

        grid_pen = QPen(QColor(pal["border_2"]), 1.0)
        painter.setPen(grid_pen)
        for ring_angle in (30, 60, 90):
            ring_radius = radius * ring_angle / 90.0
            painter.drawEllipse(center, ring_radius, ring_radius)

        axis_color = QColor(pal["border_2"])
        axis_color.setAlpha(72)
        painter.setPen(QPen(axis_color, 1.0))
        painter.drawLine(
            QPointF(center.x() - radius, center.y()),
            QPointF(center.x() + radius, center.y()),
        )
        painter.drawLine(
            QPointF(center.x(), center.y() - radius),
            QPointF(center.x(), center.y() + radius),
        )

        small_font = QFont(self.font())
        small_font.setPointSizeF(max(8.0, self.font().pointSizeF() - 1.0))
        painter.setFont(small_font)
        painter.setPen(QColor(pal["text_2"]))
        cardinal = (
            ("0°", QRectF(center.x() - 24, center.y() - radius - 25, 48, 20)),
            ("90°", QRectF(center.x() + radius - 46, center.y() - 10, 42, 20)),
            ("180°", QRectF(center.x() - 25, center.y() + radius + 5, 50, 20)),
            ("270°", QRectF(center.x() - radius + 4, center.y() - 10, 42, 20)),
        )
        for text, rect in cardinal:
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

        for ring_angle in (30, 60, 90):
            ring_radius = radius * ring_angle / 90.0
            x = center.x() + ring_radius * 0.68
            y = center.y() - ring_radius * 0.68
            painter.drawText(QRectF(x - 3, y - 16, 38, 18), f"{ring_angle}°")

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(pal["text_2"]))
        painter.drawEllipse(center, 4.5, 4.5)

        if not self.has_beam():
            painter.setPen(QColor(pal["text_3"]))
            painter.drawText(
                QRectF(center.x() - 20, center.y() + 10, 40, 24),
                Qt.AlignmentFlag.AlignCenter,
                "—",
            )
            return

        endpoint = beam_endpoint(self._azimuth_deg, self._off_axis_deg)
        if endpoint is None:
            return
        x_norm, y_norm = endpoint
        point = QPointF(center.x() + x_norm * radius, center.y() + y_norm * radius)
        beam_color = QColor(pal["text_3"] if self._stale else pal["accent"])
        beam_pen = QPen(beam_color, 3.0)
        beam_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(beam_pen)
        painter.drawLine(center, point)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(beam_color)
        painter.drawEllipse(point, 6.0, 6.0)
