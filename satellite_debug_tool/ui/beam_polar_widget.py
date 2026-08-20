"""Compact polar indicator for customer-facing antenna beam direction."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from satellite_debug_tool.ui import styles as S


@dataclass(frozen=True)
class BeamSatelliteMarker:
    """One satellite rendered in the same canonical array frame as the beam."""

    norad_id: int
    label: str
    array_azimuth_deg: float
    array_offaxis_deg: float
    stale: bool = False
    active_target: bool = False
    candidate: bool = False
    trail: tuple[tuple[float, float], ...] = ()
    tooltip: str = ""


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

    satellite_clicked = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._theme = "dark"
        self._azimuth_deg: Optional[float] = None
        self._off_axis_deg: Optional[float] = None
        self._stale = False
        self._max_off_axis_deg = 90.0
        self._satellites: tuple[BeamSatelliteMarker, ...] = ()
        self._hit_targets: list[tuple[QPointF, BeamSatelliteMarker]] = []
        self.setMouseTracking(True)
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
            endpoint = beam_endpoint(
                float(azimuth_deg),
                float(off_axis_deg),
                max_off_axis_deg=self._max_off_axis_deg,
            )
        if endpoint is None:
            self._azimuth_deg = None
            self._off_axis_deg = None
        else:
            self._azimuth_deg = float(azimuth_deg) % 360.0
            self._off_axis_deg = float(off_axis_deg)
        self._stale = bool(stale)
        self.update()

    def set_satellites(
        self,
        satellites: tuple[BeamSatelliteMarker, ...],
        *,
        max_off_axis_deg: float,
    ) -> None:
        """Replace the complete array-sky layer and its hard off-axis envelope."""
        if not math.isfinite(max_off_axis_deg) or max_off_axis_deg <= 0.0:
            self._max_off_axis_deg = 90.0
            self._satellites = ()
            self.update()
            return
        valid = []
        for marker in satellites:
            if (
                marker.norad_id > 0
                and math.isfinite(marker.array_azimuth_deg)
                and math.isfinite(marker.array_offaxis_deg)
                and 0.0 <= marker.array_offaxis_deg <= max_off_axis_deg
            ):
                valid.append(marker)
        self._max_off_axis_deg = float(max_off_axis_deg)
        self._satellites = tuple(valid)
        self.update()

    def satellites(self) -> tuple[BeamSatelliteMarker, ...]:
        return self._satellites

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
        ring_angles = tuple(self._max_off_axis_deg * fraction for fraction in (1.0 / 3.0, 2.0 / 3.0, 1.0))
        for ring_angle in ring_angles:
            ring_radius = radius * ring_angle / self._max_off_axis_deg
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

        for ring_angle in ring_angles:
            ring_radius = radius * ring_angle / self._max_off_axis_deg
            x = center.x() + ring_radius * 0.68
            y = center.y() - ring_radius * 0.68
            painter.drawText(QRectF(x - 3, y - 16, 42, 18), f"{ring_angle:.0f}°")

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
            self._paint_satellites(painter, center, radius, pal)
            return

        endpoint = beam_endpoint(
            self._azimuth_deg,
            self._off_axis_deg,
            max_off_axis_deg=self._max_off_axis_deg,
        )
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
        self._paint_satellites(painter, center, radius, pal)

    def _paint_satellites(self, painter: QPainter, center: QPointF, radius: float, pal: dict) -> None:
        self._hit_targets.clear()
        show_all_labels = len(self._satellites) <= 12
        for marker in self._satellites:
            trail_points = []
            for azimuth_deg, offaxis_deg in marker.trail:
                endpoint = beam_endpoint(
                    azimuth_deg,
                    offaxis_deg,
                    max_off_axis_deg=self._max_off_axis_deg,
                )
                if endpoint is not None:
                    trail_points.append(
                        QPointF(center.x() + endpoint[0] * radius, center.y() + endpoint[1] * radius)
                    )
            if len(trail_points) >= 2:
                trail_color = QColor("#64748B" if marker.stale else "#38BDF8")
                trail_color.setAlpha(80)
                painter.setPen(QPen(trail_color, 1.4))
                for start, end in zip(trail_points, trail_points[1:]):
                    painter.drawLine(start, end)

            endpoint = beam_endpoint(
                marker.array_azimuth_deg,
                marker.array_offaxis_deg,
                max_off_axis_deg=self._max_off_axis_deg,
            )
            if endpoint is None:
                continue
            point = QPointF(center.x() + endpoint[0] * radius, center.y() + endpoint[1] * radius)
            color = QColor(
                pal["text_3"]
                if marker.stale
                else "#22C55E"
                if marker.active_target
                else "#F59E0B"
                if marker.candidate
                else "#38BDF8"
            )
            painter.setPen(QPen(color, 2.0 if marker.active_target else 1.2))
            painter.setBrush(color)
            size = 6.5 if marker.active_target else 4.5
            painter.drawEllipse(point, size, size)
            if marker.candidate and not marker.active_target:
                halo = QColor("#F59E0B")
                halo.setAlpha(190)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(halo, 1.5, Qt.PenStyle.DashLine))
                painter.drawEllipse(point, 8.0, 8.0)
            if show_all_labels or marker.active_target or marker.candidate:
                painter.setPen(color)
                painter.drawText(
                    QRectF(point.x() + 7, point.y() - 10, 88, 20),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    marker.label,
                )
            self._hit_targets.append((point, marker))

    def _marker_at(self, point: QPointF) -> Optional[BeamSatelliteMarker]:
        nearest: Optional[BeamSatelliteMarker] = None
        distance_squared = 12.0 * 12.0
        for center, marker in self._hit_targets:
            candidate = (center.x() - point.x()) ** 2 + (center.y() - point.y()) ** 2
            if candidate <= distance_squared:
                nearest = marker
                distance_squared = candidate
        return nearest

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        marker = self._marker_at(event.position())
        self.setToolTip("" if marker is None else marker.tooltip)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            marker = self._marker_at(event.position())
            if marker is not None:
                self.satellite_clicked.emit(marker.norad_id)
                event.accept()
                return
        super().mousePressEvent(event)


__all__ = ["BeamPolarWidget", "BeamSatelliteMarker", "beam_endpoint"]
