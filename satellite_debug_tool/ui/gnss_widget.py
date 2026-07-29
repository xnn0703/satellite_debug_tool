"""Live/Playback 共用的 GNSS 天空图与逐频点 C/N₀ 浮窗。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from PySide6.QtCore import QCoreApplication, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.data.gnss_store import (
    GnssSnapshot,
    GnssStore,
    LOCK_CODE,
    LOCK_PHASE,
    LOCK_PRIMARY,
    LOCK_PRN,
    SYSTEM_NAMES,
    SOURCE_NAMES,
    infer_sky_system,
    observation_locked,
    satellite_label,
    signal_band,
    signal_name,
    signal_record_cnr_valid,
    signal_record_locked,
    signal_record_used,
)
from satellite_debug_tool.core.protocol import GnssCnrObservation, GnssSatRecord, GnssSignalRecord
from satellite_debug_tool.i18n import (
    register_translatable,
    set_translatable_n_text,
    set_translatable_text,
    tr,
    trn,
)
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.flow_layout import FlowLayout


SYSTEM_COLORS = {
    0: "#3B82F6",  # GPS
    1: "#EF4444",  # GLONASS
    2: "#F59E0B",  # SBAS
    3: "#A855F7",  # Galileo
    4: "#22C55E",  # BDS
    5: "#06B6D4",  # QZSS
    6: "#EC4899",  # NavIC
    7: "#94A3B8",
}
BAND_COLORS = {
    "L1": "#F59E0B", "L2": "#22C55E", "L5": "#2563EB",
    "G1": "#F59E0B", "G2": "#22C55E",
    "E1": "#F59E0B", "E5a": "#2563EB", "E5b": "#22C55E",
    "E6": "#A855F7", "E5 AltBOC": "#06B6D4",
    "B1": "#F59E0B", "B2": "#22C55E", "B3": "#2563EB",
}
_BAND_ORDER = {
    "L1": 0, "G1": 0, "E1": 0, "B1": 0,
    "L2": 1, "G2": 1, "E5a": 1, "B2": 1,
    "L5": 2, "E5b": 2, "B3": 2,
    "E6": 3, "E5 AltBOC": 4,
}
CN0_COLOR_MIN_DBHZ = 20
CN0_COLOR_MAX_DBHZ = 51
CN0_UNKNOWN_COLOR = "#94A3B8"
CN0_LEGEND_VALUES = (CN0_COLOR_MAX_DBHZ, 40, 30, CN0_COLOR_MIN_DBHZ)
# Backward-compatible data export. Rendering uses _cn0_legend_ticks() so labels
# can follow the active application language.
CN0_LEGEND_TICKS = (
    (CN0_COLOR_MAX_DBHZ, "≥51 强"),
    (40, "40"),
    (30, "30"),
    (CN0_COLOR_MIN_DBHZ, "≤20 弱"),
)


if False:  # Translation extraction declarations for Qt numerus messages.
    QCoreApplication.translate(
        "",
        "%n satellite record(s) received\nAzimuth/elevation is not valid yet",
        None,
        0,
    )
    QCoreApplication.translate(
        "",
        "%n satellite record(s) · {drawable} drawable · {valid} valid C/N₀ · "
        "average {average:.1f} / maximum {maximum:.0f} dB-Hz · {sky_state} · {signal_state}",
        None,
        0,
    )
    QCoreApplication.translate(
        "",
        "%n satellite record(s) · {drawable} drawable · 0 valid C/N₀ · "
        "{sky_state} · {signal_state}",
        None,
        0,
    )


def _cn0_legend_ticks() -> Tuple[Tuple[int, str], ...]:
    return (
        (CN0_COLOR_MAX_DBHZ, tr("≥51 strong")),
        (40, "40"),
        (30, "30"),
        (CN0_COLOR_MIN_DBHZ, tr("≤20 weak")),
    )


@dataclass(frozen=True)
class FrequencyBar:
    system: int
    prn: int
    band: str
    cn0_dbhz: int
    details: Tuple[object, ...]
    namespace: str = "UG016"

    @property
    def satellite(self) -> str:
        return satellite_label(self.system, self.prn)


def build_frequency_bars(
    snapshot: GnssSnapshot,
    enabled_systems: Optional[Set[int]] = None,
) -> List[FrequencyBar]:
    """按 system+PRN+物理频段聚合；柱高取已锁定观测最大 C/N₀。"""
    grouped: Dict[Tuple[int, int, str], List[object]] = {}
    entries: List[Tuple[object, bool, int, int, int]] = []
    if snapshot.cnr is not None:
        entries.extend(
            (item, observation_locked(item), item.system, item.prn, item.signal_type)
            for item in snapshot.cnr.observations
        )
    if snapshot.signal is not None:
        entries.extend(
            (item, signal_record_cnr_valid(item), item.system, item.sv_id, item.raw_signal_id)
            for item in snapshot.signal.records
        )
    for observation, _locked, system, prn, signal_id in entries:
        if enabled_systems is not None and system not in enabled_systems:
            continue
        key = (system, prn, signal_band(system, signal_id, snapshot.signal_namespace))
        grouped.setdefault(key, []).append(observation)

    bars: List[FrequencyBar] = []
    for (system, prn, band), details in grouped.items():
        locked = [
            observation for observation in details
            if (observation_locked(observation) if isinstance(observation, GnssCnrObservation)
                else signal_record_cnr_valid(observation))
        ]
        if not locked:
            continue
        bars.append(FrequencyBar(
            system=system,
            prn=prn,
            band=band,
            cn0_dbhz=max(getattr(observation, "cn0_dbhz") for observation in locked),
            details=tuple(details),
            namespace=snapshot.signal_namespace,
        ))
    bars.sort(key=lambda bar: (bar.system, bar.prn, _BAND_ORDER.get(bar.band, 99), bar.band))
    return bars


def sky_point(center: QPointF, radius: float, elevation_deg: float, azimuth_deg: float) -> QPointF:
    """北向上、方位顺时针、中心天顶的天空图投影。"""
    radial = radius * (90.0 - elevation_deg) / 90.0
    azimuth = math.radians(azimuth_deg)
    return QPointF(center.x() + radial * math.sin(azimuth), center.y() - radial * math.cos(azimuth))


def sky_geometry_valid(elevation_deg: int, azimuth_deg: int) -> bool:
    """只有地平线及以上、方位角有效的卫星位置才能投影到天空图。"""
    return 0 <= elevation_deg <= 90 and 0 <= azimuth_deg <= 360


def nav_sat_position_valid(record: GnssSatRecord) -> bool:
    """判断一条 NAV-SAT 记录是否具备可绘制的天空位置。"""
    return sky_geometry_valid(record.elevation_deg, record.azimuth_deg)


def satellite_record_counts(snapshot: GnssSnapshot, systems: Set[int]) -> Tuple[int, int]:
    """返回当前筛选下的（卫星记录数，可绘制天空位置数）。"""
    record_count = 0
    drawable_count = 0
    for talker, report in snapshot.sky_by_talker.items():
        for satellite in report.satellites:
            if infer_sky_system(talker, satellite.prn) not in systems:
                continue
            record_count += 1
            if (
                (satellite.valid_flags & 0x03) == 0x03
                and sky_geometry_valid(satellite.elevation_deg, satellite.azimuth_deg)
            ):
                drawable_count += 1
    if snapshot.sat is not None:
        for record in snapshot.sat.records:
            if record.system not in systems:
                continue
            record_count += 1
            if nav_sat_position_valid(record):
                drawable_count += 1
    return record_count, drawable_count


def _cn0_color(cn0: int) -> QColor:
    ratio = max(
        0.0,
        min(1.0, (cn0 - CN0_COLOR_MIN_DBHZ) / (CN0_COLOR_MAX_DBHZ - CN0_COLOR_MIN_DBHZ)),
    )
    return QColor.fromRgbF(1.0 - ratio, 0.35 + 0.55 * ratio, 0.18)


class SkyPlotWidget(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(360, 360)
        self.setMouseTracking(True)
        self._snapshot = GnssSnapshot(timestamp=0)
        self._systems: Set[int] = set(SYSTEM_NAMES)
        self._cn0_coloring = False
        self._stale = False
        self._dark = True
        self._scale = "small"
        self._legend_font_px = 10
        self._hit_points: List[Tuple[QPointF, str]] = []
        self._record_count = 0
        self._drawable_count = 0
        self._empty_text = ""
        self.set_theme(True, "small")
        register_translatable(self)

    def set_data(self, snapshot: GnssSnapshot, systems: Set[int], cn0_coloring: bool, stale: bool) -> None:
        self._snapshot = snapshot
        self._systems = set(systems)
        self._cn0_coloring = cn0_coloring
        self._stale = stale
        self._record_count, self._drawable_count = satellite_record_counts(snapshot, self._systems)
        self._refresh_empty_text()
        self.update()

    def _refresh_empty_text(self) -> None:
        self._empty_text = (
            trn(
                "%n satellite record(s) received\nAzimuth/elevation is not valid yet",
                self._record_count,
            )
            if self._record_count > 0 and self._drawable_count == 0
            else ""
        )

    def retranslate_ui(self) -> None:
        self._refresh_empty_text()
        self.update()

    def set_theme(self, dark: bool, scale: str) -> None:
        self._dark = dark
        self._scale = scale
        self._legend_font_px = max(9, S.font_px(10, scale))
        self.update()

    def set_dark_theme(self, dark: bool) -> None:
        self.set_theme(dark, self._scale)

    def plot_geometry(self) -> Tuple[QPointF, float]:
        """返回天空圆的中心和半径；C/N₀ overlay 不得改变这两个值。"""
        margin = 34.0
        radius = max(10.0, min(self.width(), self.height()) / 2.0 - margin)
        return QPointF(self.width() / 2.0, self.height() / 2.0), radius

    def cn0_legend_layout(self) -> Tuple[QFont, QRectF, QRectF]:
        """选择不碰天空圆保护区的最大字号，并返回 font/card/gradient。"""
        center, radius = self.plot_geometry()
        fallback = None
        ticks = _cn0_legend_ticks()
        for pixel_size in range(self._legend_font_px, 7, -1):
            font = self.font()
            font.setPixelSize(pixel_size)
            metrics = QFontMetrics(font)
            line_height = float(metrics.height())
            unknown_box = max(6.0, min(8.0, line_height - 3.0))
            tick_width = max(metrics.horizontalAdvance(label) for _, label in ticks)
            title_row_width = (
                metrics.horizontalAdvance("C/N₀")
                + 2.0
                + unknown_box
                + 2.0
                + metrics.horizontalAdvance(tr("None"))
            )
            card_width = max(46.0, title_row_width + 6.0, 18.0 + tick_width)
            gradient_height = max(44.0, line_height * 4.0)
            card_height = line_height + gradient_height + 9.0
            card = QRectF(6.0, 6.0, card_width, card_height)
            gradient = QRectF(
                card.left() + 5.0,
                card.top() + line_height + 3.0,
                7.0,
                gradient_height,
            )
            fallback = (font, card, gradient)
            if (
                card.right() < center.x()
                and card.bottom() < center.y()
                and math.hypot(center.x() - card.right(), center.y() - card.bottom()) >= radius + 12.0
            ):
                return fallback
        assert fallback is not None
        return fallback

    def cn0_legend_geometry(self) -> Tuple[QRectF, QRectF]:
        """返回画布内 overlay 卡片和色带区域，不参与任何 Qt layout。"""
        _, card, gradient = self.cn0_legend_layout()
        return card, gradient

    def _paint_cn0_legend(self, painter: QPainter) -> None:
        legend_font, card, gradient_rect = self.cn0_legend_layout()
        palette = S.palette("dark" if self._dark else "light")
        card_bg = QColor("#0B1220" if self._dark else "#FFFFFF")
        card_bg.setAlpha(218)
        fg = QColor(palette["text"])
        border = QColor(palette["border_2"])
        painter.save()
        # 图例是固定比例尺，即使卫星数据 stale 也保持原始色彩，不继承数据层的淡化透明度。
        painter.setOpacity(1.0)
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(card_bg)
        painter.drawRoundedRect(card, 4.0, 4.0)
        painter.setBrush(Qt.NoBrush)
        painter.setFont(legend_font)
        metrics = QFontMetrics(legend_font)
        line_height = float(metrics.height())

        painter.setPen(fg)
        title_width = metrics.horizontalAdvance("C/N₀")
        painter.drawText(
            QRectF(card.left() + 3.0, card.top(), title_width, line_height),
            Qt.AlignLeft | Qt.AlignVCenter,
            "C/N₀",
        )
        unknown_box = max(6.0, min(8.0, line_height - 3.0))
        unknown_x = card.left() + 5.0 + title_width
        unknown_y = card.top() + (line_height - unknown_box) / 2.0
        painter.fillRect(QRectF(unknown_x, unknown_y, unknown_box, unknown_box), QColor(CN0_UNKNOWN_COLOR))
        painter.drawText(
            QRectF(unknown_x + unknown_box + 2.0, card.top(), card.right() - unknown_x - unknown_box - 3.0, line_height),
            Qt.AlignLeft | Qt.AlignVCenter,
            tr("None"),
        )

        gradient = QLinearGradient(0.0, gradient_rect.top(), 0.0, gradient_rect.bottom())
        gradient.setColorAt(0.0, _cn0_color(CN0_COLOR_MAX_DBHZ))
        gradient.setColorAt(1.0, _cn0_color(CN0_COLOR_MIN_DBHZ))
        painter.fillRect(gradient_rect, QBrush(gradient))
        painter.setPen(QPen(border, 1.0))
        painter.drawRect(gradient_rect)

        painter.setPen(fg)
        for value, label in _cn0_legend_ticks():
            ratio = (CN0_COLOR_MAX_DBHZ - value) / (CN0_COLOR_MAX_DBHZ - CN0_COLOR_MIN_DBHZ)
            y = gradient_rect.top() + gradient_rect.height() * ratio
            label_rect = QRectF(
                gradient_rect.right() + 4.0,
                y - line_height / 2.0,
                max(1.0, card.right() - gradient_rect.right() - 6.0),
                line_height,
            )
            if value == CN0_COLOR_MAX_DBHZ:
                label_rect.moveTop(gradient_rect.top())
                alignment = Qt.AlignLeft | Qt.AlignTop
            elif value == CN0_COLOR_MIN_DBHZ:
                label_rect.moveBottom(gradient_rect.bottom())
                alignment = Qt.AlignLeft | Qt.AlignBottom
            else:
                alignment = Qt.AlignLeft | Qt.AlignVCenter
            painter.drawText(label_rect, alignment, label)

        painter.restore()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        bg = QColor("#0F172A" if self._dark else "#F8FAFC")
        fg = QColor("#CBD5E1" if self._dark else "#334155")
        grid = QColor("#475569" if self._dark else "#CBD5E1")
        if self._stale:
            painter.setOpacity(0.38)
        painter.fillRect(self.rect(), bg)
        center, radius = self.plot_geometry()
        painter.setPen(QPen(grid, 1.0))
        for elevation in (0, 30, 60):
            ring = radius * (90 - elevation) / 90.0
            painter.drawEllipse(center, ring, ring)
        painter.drawLine(QPointF(center.x(), center.y() - radius), QPointF(center.x(), center.y() + radius))
        painter.drawLine(QPointF(center.x() - radius, center.y()), QPointF(center.x() + radius, center.y()))
        painter.setPen(fg)
        painter.drawText(QRectF(center.x() - 10, center.y() - radius - 23, 20, 18), Qt.AlignCenter, "N")
        painter.drawText(QRectF(center.x() + radius + 5, center.y() - 9, 20, 18), Qt.AlignCenter, "E")
        painter.drawText(QRectF(center.x() - 10, center.y() + radius + 5, 20, 18), Qt.AlignCenter, "S")
        painter.drawText(QRectF(center.x() - radius - 25, center.y() - 9, 20, 18), Qt.AlignCenter, "W")
        if self._cn0_coloring:
            self._paint_cn0_legend(painter)

        cn0_by_satellite: Dict[Tuple[int, int], int] = {}
        if self._snapshot.cnr is not None:
            for observation in self._snapshot.cnr.observations:
                if observation_locked(observation):
                    key = (observation.system, observation.prn)
                    cn0_by_satellite[key] = max(cn0_by_satellite.get(key, 0), observation.cn0_dbhz)

        sky_rows: List[Tuple[int, int, int, int, Optional[int], str]] = []
        for talker, report in self._snapshot.sky_by_talker.items():
            for satellite in report.satellites:
                if (
                    (satellite.valid_flags & 0x03) != 0x03
                    or not sky_geometry_valid(satellite.elevation_deg, satellite.azimuth_deg)
                ):
                    continue
                system = infer_sky_system(talker, satellite.prn)
                cn0 = satellite.snr if satellite.valid_flags & 0x04 else None
                sky_rows.append((
                    system, satellite.prn, satellite.elevation_deg, satellite.azimuth_deg, cn0,
                    f"BYNAV/GSV valid=0x{satellite.valid_flags:02X}",
                ))
        if self._snapshot.sat is not None:
            for satellite in self._snapshot.sat.records:
                if not nav_sat_position_valid(satellite):
                    continue
                sky_rows.append((
                    satellite.system, satellite.sv_id, satellite.elevation_deg,
                    satellite.azimuth_deg, satellite.cn0_dbhz,
                    f"MG902/NAV-SAT flags=0x{satellite.raw_sat_flags:08X}",
                ))

        self._hit_points.clear()
        for system, sv_id, elevation, azimuth, native_cn0, raw_detail in sky_rows:
            if system not in self._systems:
                continue
            point = sky_point(center, radius, elevation, azimuth)
            cn0 = cn0_by_satellite.get((system, sv_id), native_cn0)
            if self._cn0_coloring:
                color = _cn0_color(cn0) if cn0 is not None else QColor(CN0_UNKNOWN_COLOR)
            else:
                color = QColor(SYSTEM_COLORS.get(system, CN0_UNKNOWN_COLOR))
            painter.setPen(QPen(bg, 1.0))
            painter.setBrush(color)
            painter.drawEllipse(point, 10.0, 10.0)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(QRectF(point.x() - 13, point.y() - 8, 26, 16), Qt.AlignCenter, str(sv_id))
            cn0_label = tr("Color C/N₀") if self._cn0_coloring else "C/N₀"
            cn0_detail = f"{cn0_label}={cn0 if cn0 is not None else '—'} dB-Hz"
            if native_cn0 is not None and cn0 != native_cn0:
                cn0_detail += tr(
                    " · satellite record C/N₀={value} dB-Hz",
                    value=native_cn0,
                )
            self._hit_points.append((
                point,
                tr(
                    "{satellite} · elev={elevation}° · az={azimuth}° · {cn0_detail}\n{raw_detail}",
                    satellite=satellite_label(system, sv_id),
                    elevation=elevation,
                    azimuth=azimuth,
                    cn0_detail=cn0_detail,
                    raw_detail=raw_detail,
                ),
            ))
        if self._empty_text:
            painter.setPen(fg)
            painter.drawText(
                QRectF(center.x() - radius, center.y() - 30, radius * 2, 60),
                Qt.AlignCenter,
                self._empty_text,
            )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        point = event.position()
        for center, detail in self._hit_points:
            if abs(point.x() - center.x()) <= 12 and abs(point.y() - center.y()) <= 12:
                self.setToolTip(detail)
                return
        if self._cn0_coloring and self.cn0_legend_geometry()[0].contains(point):
            self.setToolTip(
                tr(
                    "Continuous sky-plot C/N₀ scale: ≤20 dB-Hz is weak, "
                    "≥51 dB-Hz is strong, and gray means no C/N₀ data."
                )
            )
            return
        self.setToolTip("")


class CnrBarWidget(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(360)
        self.setMouseTracking(True)
        self._bars: List[FrequencyBar] = []
        self._hit_rects: List[Tuple[QRectF, FrequencyBar]] = []
        self._stale = False
        self._dark = True
        self._signal_record_count = 0
        self._locked_signal_count = 0
        self._empty_text = tr("Waiting for receiver signal-level C/N₀ data")
        register_translatable(self)

    def set_data(
        self,
        bars: Sequence[FrequencyBar],
        stale: bool,
        signal_record_count: int = 0,
        locked_signal_count: int = 0,
    ) -> None:
        self._bars = list(bars)
        groups = len({(bar.system, bar.prn) for bar in bars})
        self.setMinimumWidth(max(420, groups * 92))
        self._stale = stale
        self._signal_record_count = signal_record_count
        self._locked_signal_count = locked_signal_count
        self._refresh_empty_text()
        self.update()

    def _refresh_empty_text(self) -> None:
        if self._bars:
            self._empty_text = ""
        elif self._locked_signal_count > 0:
            self._empty_text = tr("Signals are locked, but no valid C/N₀ is available")
        elif self._signal_record_count > 0:
            self._empty_text = tr("Signal records received; no signal is locked")
        else:
            self._empty_text = tr("Waiting for receiver signal-level C/N₀ data")

    def retranslate_ui(self) -> None:
        self._refresh_empty_text()
        self.update()

    def set_dark_theme(self, dark: bool) -> None:
        self._dark = dark
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        bg = QColor("#0F172A" if self._dark else "#F8FAFC")
        fg = QColor("#CBD5E1" if self._dark else "#334155")
        grid = QColor("#334155" if self._dark else "#E2E8F0")
        painter.fillRect(self.rect(), bg)
        if self._stale:
            painter.setOpacity(0.38)
        left, top, right, bottom = 42.0, 22.0, 16.0, 44.0
        chart = QRectF(left, top, max(10.0, self.width() - left - right), max(10.0, self.height() - top - bottom))
        painter.setPen(QPen(grid, 1.0))
        for value in (0, 15, 30, 45, 60):
            y = chart.bottom() - chart.height() * value / 60.0
            painter.drawLine(QPointF(chart.left(), y), QPointF(chart.right(), y))
            painter.setPen(fg)
            painter.drawText(QRectF(0, y - 9, left - 6, 18), Qt.AlignRight | Qt.AlignVCenter, str(value))
            painter.setPen(QPen(grid, 1.0))

        grouped: Dict[Tuple[int, int], List[FrequencyBar]] = {}
        for bar in self._bars:
            grouped.setdefault((bar.system, bar.prn), []).append(bar)
        self._hit_rects.clear()
        if not grouped:
            painter.setPen(fg)
            painter.drawText(chart, Qt.AlignCenter, self._empty_text)
            return
        group_width = chart.width() / len(grouped)
        for group_index, ((system, prn), bars) in enumerate(grouped.items()):
            bar_width = min(18.0, (group_width - 12.0) / max(1, len(bars)))
            total_width = bar_width * len(bars)
            start_x = chart.left() + group_width * (group_index + 0.5) - total_width / 2.0
            for index, bar in enumerate(bars):
                height = chart.height() * min(60, bar.cn0_dbhz) / 60.0
                rect = QRectF(start_x + index * bar_width + 1.0, chart.bottom() - height, max(3.0, bar_width - 2.0), height)
                painter.fillRect(rect, QColor(BAND_COLORS.get(bar.band, SYSTEM_COLORS.get(system, "#94A3B8"))))
                self._hit_rects.append((rect, bar))
            painter.setPen(fg)
            painter.drawText(
                QRectF(chart.left() + group_index * group_width, chart.bottom() + 6, group_width, 20),
                Qt.AlignCenter,
                satellite_label(system, prn),
            )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        point = event.position()
        for rect, bar in self._hit_rects:
            if rect.contains(point):
                detail = ", ".join(
                    f"{signal_name(item.system, getattr(item, 'signal_type', getattr(item, 'raw_signal_id', 0)), bar.namespace)}"
                    f"={item.cn0_dbhz} dB-Hz"
                    for item in bar.details
                )
                self.setToolTip(f"{bar.satellite} {bar.band}: {bar.cn0_dbhz} dB-Hz\n{detail}")
                return
        self.setToolTip("")


class _FilterFlowHost(QWidget):
    """GNSS 筛选项的 HeightForWidth 宿主，确保换行高度反馈给父布局。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.flow = FlowLayout(self, margin=0, h_spacing=6, v_spacing=4)
        size_policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        size_policy.setHeightForWidth(True)
        self.setSizePolicy(size_policy)
        self.setMinimumWidth(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt API)
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self.flow.heightForWidth(width)


class GnssWidget(QWidget):
    """共享 GNSS 浮窗组件；Playback 模式额外显示历史快照控制。"""

    def __init__(self, store: GnssStore, *, playback: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._playback = playback
        self._theme = "dark"
        self._scale = "small"
        self._selected_history: Optional[int] = None
        self._follow_latest = True
        self._system_checks: Dict[int, QCheckBox] = {}
        self._info_font_px: Optional[int] = None
        self._info_font_candidates: Tuple[int, ...] = (12, 11, 10)
        self._setup_ui()
        self._apply_filter_font()
        self._store.changed.connect(self.refresh)
        self._store.cleared.connect(self._on_store_cleared)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        register_translatable(self)
        self.refresh()

    def _on_store_cleared(self) -> None:
        self._selected_history = None
        self._follow_latest = True
        self.refresh()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(5)

        self._filters_host = _FilterFlowHost()
        for system, name in SYSTEM_NAMES.items():
            checkbox = QCheckBox(name)
            checkbox.setChecked(system != 7)
            checkbox.setAttribute(Qt.WA_LayoutUsesWidgetRect, True)
            checkbox.toggled.connect(self.refresh)
            self._system_checks[system] = checkbox
            self._filters_host.flow.addWidget(checkbox)
        self._cn0_color = QCheckBox(tr("Color sky plot by C/N₀"))
        self._cn0_color.setAttribute(Qt.WA_LayoutUsesWidgetRect, True)
        self._cn0_color.toggled.connect(self.refresh)
        self._filters_host.flow.addWidget(self._cn0_color)
        root.addWidget(self._filters_host)

        self._info_row = QWidget()
        self._info_layout = QHBoxLayout(self._info_row)
        self._info_layout.setContentsMargins(0, 0, 0, 0)
        self._info_layout.setSpacing(8)
        self._source_badge = QLabel(tr("SOURCE: {source}", source="—"))
        self._source_badge.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._source_badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._info_layout.addWidget(self._source_badge)
        self._stats = QLabel(tr("Waiting for GNSS data"))
        self._stats.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self._stats.setWordWrap(True)
        self._stats.setMinimumWidth(0)
        self._stats.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._info_layout.addWidget(self._stats, 1)
        info_size_policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        info_size_policy.setHeightForWidth(True)
        self._info_row.setSizePolicy(info_size_policy)
        root.addWidget(self._info_row)

        self._history_row = QWidget()
        history_layout = QHBoxLayout(self._history_row)
        history_layout.setContentsMargins(0, 0, 0, 0)
        self._prev = QPushButton("◀")
        self._next = QPushButton("▶")
        self._history_slider = QSlider(Qt.Horizontal)
        self._history_label = QLabel("0/0")
        self._prev.clicked.connect(lambda: self._step_history(-1))
        self._next.clicked.connect(lambda: self._step_history(1))
        self._history_slider.valueChanged.connect(self._select_history)
        history_layout.addWidget(QLabel(tr("Snapshot")))
        history_layout.addWidget(self._prev)
        history_layout.addWidget(self._history_slider, 1)
        history_layout.addWidget(self._next)
        history_layout.addWidget(self._history_label)
        self._history_row.setVisible(self._playback)
        root.addWidget(self._history_row)

        splitter = QSplitter(Qt.Horizontal)
        self._sky = SkyPlotWidget()
        splitter.addWidget(self._sky)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._bars = CnrBarWidget()
        scroll.setWidget(self._bars)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([440, 760])
        root.addWidget(splitter, 1)

        self._legend = QLabel("")
        root.addWidget(self._legend)
        self._details_toggle = QToolButton()
        self._details_toggle.setText(tr("Observation details"))
        self._details_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._details_toggle.setCheckable(True)
        self._details_toggle.setArrowType(Qt.RightArrow)
        self._details_toggle.toggled.connect(self._toggle_details)
        root.addWidget(self._details_toggle)
        self._table = QTableWidget(0, 13)
        self._table.setHorizontalHeaderLabels([
            tr("Source"), tr("System"), tr("Satellite"), tr("Band"),
            "Signal", "Raw ID", "C/N₀", "Quality/Tracking",
            "Lock / Used", "Freq/GLO", "Corr", "Residual", "Raw Flags",
        ])
        self._table.setVisible(False)
        root.addWidget(self._table)

    def _toggle_details(self, visible: bool) -> None:
        self._details_toggle.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)
        self._table.setVisible(visible)

    def _enabled_systems(self) -> Set[int]:
        return {system for system, checkbox in self._system_checks.items() if checkbox.isChecked()}

    def _apply_filter_font(self) -> None:
        """筛选项保持主题字号，不参与窄屏降档，空间不足时交给 FlowLayout 换行。"""
        pixel_size = S.font_px(12, self._scale)
        for checkbox in (*self._system_checks.values(), self._cn0_color):
            checkbox.setStyleSheet(f"QCheckBox {{ font-size: {pixel_size}px; }}")
            checkbox.updateGeometry()
        self._filters_host.flow.invalidate()
        self._filters_host.updateGeometry()

    def _available_header_width(self) -> int:
        margins = self.layout().contentsMargins()
        return max(0, self.width() - margins.left() - margins.right())

    def _fit_info_font(self) -> None:
        """信息行按完整文本宽度选择字号；最小档仍放不下时由 QLabel 自动换行。"""
        available = self._available_header_width()
        if available <= 0:
            return
        selected = self._info_font_candidates[-1]
        spacing = self._info_layout.spacing()
        for candidate in self._info_font_candidates:
            source_font = self._source_badge.font()
            source_font.setPixelSize(candidate)
            stats_font = self._stats.font()
            stats_font.setPixelSize(candidate)
            required = (
                QFontMetrics(source_font).horizontalAdvance(self._source_badge.text())
                + QFontMetrics(stats_font).horizontalAdvance(self._stats.text())
                + spacing
            )
            if required <= available:
                selected = candidate
                break
        if selected == self._info_font_px:
            return
        self._info_font_px = selected
        for label in (self._source_badge, self._stats):
            label.setStyleSheet(f"font-size: {selected}px; background: transparent;")
            label.updateGeometry()
        self._info_row.updateGeometry()

    def _current_snapshot(self) -> GnssSnapshot:
        history = self._store.history()
        if self._playback and history:
            index = self._selected_history if self._selected_history is not None else len(history) - 1
            index = max(0, min(index, len(history) - 1))
            return history[index]
        return self._store.snapshot()

    def _select_history(self, index: int) -> None:
        self._selected_history = index
        self._follow_latest = index >= len(self._store.history()) - 1
        self.refresh()

    def _step_history(self, delta: int) -> None:
        self._history_slider.setValue(self._history_slider.value() + delta)

    @staticmethod
    def _stream_state(stream: str, age: str, *, stale: bool, pending: bool) -> str:
        states = []
        if stale:
            states.append(tr("STALE"))
        if pending:
            states.append(tr("PENDING"))
        if states:
            return tr(
                "{stream} {age} [{states}]",
                stream=stream,
                age=age,
                states=", ".join(states),
            )
        return tr("{stream} {age}", stream=stream, age=age)

    def retranslate_ui(self) -> None:
        self._sky.retranslate_ui()
        self._bars.retranslate_ui()
        self.refresh()

    def refresh(self) -> None:
        if not self._playback:
            self._store.expire_incomplete()
        history = self._store.history()
        if self._playback:
            maximum = max(0, len(history) - 1)
            self._history_slider.blockSignals(True)
            self._history_slider.setRange(0, maximum)
            if self._follow_latest and history:
                self._selected_history = maximum
            elif self._selected_history is None or self._selected_history > maximum:
                self._selected_history = maximum if history else None
            self._history_slider.setValue(self._selected_history or 0)
            self._history_slider.blockSignals(False)
            enabled = bool(history)
            self._prev.setEnabled(enabled and self._history_slider.value() > 0)
            self._next.setEnabled(enabled and self._history_slider.value() < maximum)
            self._history_slider.setEnabled(enabled)
            self._history_label.setText(
                f"{self._history_slider.value() + 1}/{len(history)} · t={self._current_snapshot().timestamp} ms"
                if history else "0/0"
            )

        snapshot = self._current_snapshot()
        systems = self._enabled_systems()
        sky_stale = not self._playback and self._store.sky_is_stale(3.0)
        signal_stale = not self._playback and self._store.signal_is_stale(3.0)
        bars = build_frequency_bars(snapshot, systems)
        signal_record_count = (
            sum(1 for item in snapshot.cnr.observations if item.system in systems)
            if snapshot.cnr is not None else 0
        )
        if snapshot.signal is not None:
            signal_record_count += sum(1 for item in snapshot.signal.records if item.system in systems)
        locked_signal_count = (
            sum(
                1 for item in snapshot.cnr.observations
                if item.system in systems and observation_locked(item)
            )
            if snapshot.cnr is not None else 0
        )
        if snapshot.signal is not None:
            locked_signal_count += sum(
                1 for item in snapshot.signal.records
                if item.system in systems and signal_record_locked(item)
            )
        self._sky.set_data(snapshot, systems, self._cn0_color.isChecked(), sky_stale)
        self._bars.set_data(bars, signal_stale, signal_record_count, locked_signal_count)
        satellite_records, drawable_satellites = satellite_record_counts(snapshot, systems)
        locked = [] if snapshot.cnr is None else [
            observation.cn0_dbhz
            for observation in snapshot.cnr.observations
            if observation.system in systems and observation_locked(observation)
        ]
        if snapshot.signal is not None:
            locked.extend(
                item.cn0_dbhz for item in snapshot.signal.records
                if item.system in systems and signal_record_cnr_valid(item)
            )
        sky_age = self._store.sky_age_s()
        signal_age = self._store.signal_age_s()
        sky_age_text = "—" if sky_age is None or self._playback else f"{sky_age:.1f}s"
        signal_age_text = "—" if signal_age is None or self._playback else f"{signal_age:.1f}s"
        sky_state = self._stream_state(
            tr("Sky"),
            sky_age_text,
            stale=sky_stale,
            pending=self._store.sky_pending(),
        )
        signal_state = self._stream_state(
            tr("Signal"),
            signal_age_text,
            stale=signal_stale,
            pending=self._store.signal_pending(),
        )
        set_translatable_text(
            "SOURCE: {source}",
            self._source_badge,
            source=SOURCE_NAMES.get(snapshot.source, snapshot.source),
        )
        if locked:
            set_translatable_n_text(
                "%n satellite record(s) · {drawable} drawable · {valid} valid C/N₀ · "
                "average {average:.1f} / maximum {maximum:.0f} dB-Hz · "
                "{sky_state} · {signal_state}",
                self._stats,
                satellite_records,
                drawable=drawable_satellites,
                valid=len(locked),
                average=sum(locked) / len(locked),
                maximum=max(locked),
                sky_state=sky_state,
                signal_state=signal_state,
            )
        else:
            set_translatable_n_text(
                "%n satellite record(s) · {drawable} drawable · 0 valid C/N₀ · "
                "{sky_state} · {signal_state}",
                self._stats,
                satellite_records,
                drawable=drawable_satellites,
                sky_state=sky_state,
                signal_state=signal_state,
            )
        self._fit_info_font()
        bands = sorted({bar.band for bar in bars}, key=lambda band: (_BAND_ORDER.get(band, 99), band))
        legend_items = [
            f'<span style="color:{BAND_COLORS.get(band, "#94A3B8")}">■</span> {band}'
            for band in bands
        ]
        set_translatable_text(
            "Band legend: {items}",
            self._legend,
            items="&nbsp;&nbsp;".join(legend_items) if bands else "—",
        )
        self._refresh_table(snapshot, systems)

    def _refresh_table(self, snapshot: GnssSnapshot, systems: Set[int]) -> None:
        observations: List[object] = [] if snapshot.cnr is None else [
            observation for observation in snapshot.cnr.observations if observation.system in systems
        ]
        if snapshot.signal is not None:
            observations.extend(item for item in snapshot.signal.records if item.system in systems)
        observations.sort(key=lambda item: (
            item.system,
            getattr(item, "prn", getattr(item, "sv_id", 0)),
            getattr(item, "signal_type", getattr(item, "raw_signal_id", 0)),
        ))
        self._table.setRowCount(len(observations))
        for row, observation in enumerate(observations):
            signal_id = getattr(observation, "signal_type", getattr(observation, "raw_signal_id", 0))
            prn = getattr(observation, "prn", getattr(observation, "sv_id", 0))
            if isinstance(observation, GnssCnrObservation):
                flags = observation.lock_flags
                values = [
                    "BYNAV/UG016", SYSTEM_NAMES.get(observation.system, "Other"),
                    satellite_label(observation.system, prn),
                    signal_band(observation.system, signal_id, snapshot.signal_namespace),
                    signal_name(observation.system, signal_id, snapshot.signal_namespace), str(signal_id),
                    str(observation.cn0_dbhz), str(observation.tracking_state),
                    "LOCK" if observation_locked(observation) else "",
                    str(observation.glo_freq_channel) if observation.system == 1 else "—",
                    "—", "—", f"lock=0x{flags:02X}",
                ]
            else:
                flags = observation.raw_sig_flags
                lock_used = []
                if signal_record_locked(observation):
                    lock_used.append("LOCK")
                if signal_record_used(observation):
                    lock_used.append("USED")
                values = [
                    f"{SOURCE_NAMES.get(snapshot.source, snapshot.source)}/{snapshot.signal_namespace}",
                    SYSTEM_NAMES.get(observation.system, "Other"),
                    satellite_label(observation.system, prn),
                    signal_band(observation.system, signal_id, snapshot.signal_namespace),
                    signal_name(observation.system, signal_id, snapshot.signal_namespace), str(signal_id),
                    str(observation.cn0_dbhz), str(observation.quality_ind),
                    " / ".join(lock_used),
                    str(observation.freq_id) if observation.system == 1 and observation.freq_id != -128 else "—",
                    str(observation.corr_source), f"{observation.pr_res_0p1m / 10.0:.1f} m",
                    f"0x{flags:04X} iono={observation.iono_model}",
                ]
            for column, value in enumerate(values):
                self._table.setItem(row, column, QTableWidgetItem(value))

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._fit_info_font()

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
        self._scale = scale
        dark = theme != "light"
        self._sky.set_theme(dark, scale)
        self._bars.set_dark_theme(dark)
        palette = S.palette(theme)
        self.setStyleSheet(f"GnssWidget {{ background: {palette['bg']}; color: {palette['text']}; }}")
        self._info_font_candidates = tuple(dict.fromkeys(
            max(10, S.font_px(base, scale)) for base in (12, 11, 10)
        ))
        self._info_font_px = None
        self._apply_filter_font()
        self._fit_info_font()
