"""Live/Playback 共用的 GNSS 天空图与逐频点 C/N₀ 浮窗。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
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
    infer_sky_system,
    observation_locked,
    satellite_label,
    signal_band,
    signal_name,
)
from satellite_debug_tool.core.protocol import GnssCnrObservation
from satellite_debug_tool.ui import styles as S


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


@dataclass(frozen=True)
class FrequencyBar:
    system: int
    prn: int
    band: str
    cn0_dbhz: int
    details: Tuple[GnssCnrObservation, ...]

    @property
    def satellite(self) -> str:
        return satellite_label(self.system, self.prn)


def build_frequency_bars(
    snapshot: GnssSnapshot,
    enabled_systems: Optional[Set[int]] = None,
) -> List[FrequencyBar]:
    """按 system+PRN+物理频段聚合；柱高取已锁定观测最大 C/N₀。"""
    if snapshot.cnr is None:
        return []
    grouped: Dict[Tuple[int, int, str], List[GnssCnrObservation]] = {}
    for observation in snapshot.cnr.observations:
        if enabled_systems is not None and observation.system not in enabled_systems:
            continue
        key = (observation.system, observation.prn, signal_band(observation.system, observation.signal_type))
        grouped.setdefault(key, []).append(observation)

    bars: List[FrequencyBar] = []
    for (system, prn, band), details in grouped.items():
        locked = [observation for observation in details if observation_locked(observation)]
        if not locked:
            continue
        bars.append(FrequencyBar(
            system=system,
            prn=prn,
            band=band,
            cn0_dbhz=max(observation.cn0_dbhz for observation in locked),
            details=tuple(details),
        ))
    bars.sort(key=lambda bar: (bar.system, bar.prn, _BAND_ORDER.get(bar.band, 99), bar.band))
    return bars


def sky_point(center: QPointF, radius: float, elevation_deg: float, azimuth_deg: float) -> QPointF:
    """北向上、方位顺时针、中心天顶的天空图投影。"""
    radial = radius * (90.0 - elevation_deg) / 90.0
    azimuth = math.radians(azimuth_deg)
    return QPointF(center.x() + radial * math.sin(azimuth), center.y() - radial * math.cos(azimuth))


def _cn0_color(cn0: int) -> QColor:
    ratio = max(0.0, min(1.0, (cn0 - 20.0) / 31.0))
    return QColor.fromRgbF(1.0 - ratio, 0.35 + 0.55 * ratio, 0.18)


class SkyPlotWidget(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(360, 360)
        self._snapshot = GnssSnapshot(timestamp=0)
        self._systems: Set[int] = set(SYSTEM_NAMES)
        self._cn0_coloring = False
        self._stale = False
        self._dark = True

    def set_data(self, snapshot: GnssSnapshot, systems: Set[int], cn0_coloring: bool, stale: bool) -> None:
        self._snapshot = snapshot
        self._systems = set(systems)
        self._cn0_coloring = cn0_coloring
        self._stale = stale
        self.update()

    def set_dark_theme(self, dark: bool) -> None:
        self._dark = dark
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        bg = QColor("#0F172A" if self._dark else "#F8FAFC")
        fg = QColor("#CBD5E1" if self._dark else "#334155")
        grid = QColor("#475569" if self._dark else "#CBD5E1")
        if self._stale:
            painter.setOpacity(0.38)
        painter.fillRect(self.rect(), bg)
        margin = 34.0
        radius = max(10.0, min(self.width(), self.height()) / 2.0 - margin)
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
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

        cn0_by_satellite: Dict[Tuple[int, int], int] = {}
        if self._snapshot.cnr is not None:
            for observation in self._snapshot.cnr.observations:
                if observation_locked(observation):
                    key = (observation.system, observation.prn)
                    cn0_by_satellite[key] = max(cn0_by_satellite.get(key, 0), observation.cn0_dbhz)

        for talker, report in self._snapshot.sky_by_talker.items():
            for satellite in report.satellites:
                if (satellite.valid_flags & 0x03) != 0x03:
                    continue
                system = infer_sky_system(talker, satellite.prn)
                if system not in self._systems:
                    continue
                point = sky_point(center, radius, satellite.elevation_deg, satellite.azimuth_deg)
                cn0 = cn0_by_satellite.get((system, satellite.prn))
                if cn0 is None and satellite.valid_flags & 0x04:
                    cn0 = satellite.snr
                if self._cn0_coloring:
                    color = _cn0_color(cn0) if cn0 is not None else QColor("#94A3B8")
                else:
                    color = QColor(SYSTEM_COLORS.get(system, "#94A3B8"))
                painter.setPen(QPen(bg, 1.0))
                painter.setBrush(color)
                painter.drawEllipse(point, 10.0, 10.0)
                painter.setPen(QColor("#FFFFFF"))
                painter.drawText(QRectF(point.x() - 13, point.y() - 8, 26, 16), Qt.AlignCenter, str(satellite.prn))


class CnrBarWidget(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(360)
        self.setMouseTracking(True)
        self._bars: List[FrequencyBar] = []
        self._hit_rects: List[Tuple[QRectF, FrequencyBar]] = []
        self._stale = False
        self._dark = True

    def set_data(self, bars: Sequence[FrequencyBar], stale: bool) -> None:
        self._bars = list(bars)
        groups = len({(bar.system, bar.prn) for bar in bars})
        self.setMinimumWidth(max(420, groups * 92))
        self._stale = stale
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
            painter.drawText(chart, Qt.AlignCenter, "等待已锁定的 RANGECMPB C/N₀ 数据")
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
                    f"{signal_name(item.system, item.signal_type)}={item.cn0_dbhz} dB-Hz"
                    for item in bar.details
                )
                self.setToolTip(f"{bar.satellite} {bar.band}: {bar.cn0_dbhz} dB-Hz\n{detail}")
                return
        self.setToolTip("")


class GnssWidget(QWidget):
    """共享 GNSS 浮窗组件；Playback 模式额外显示历史快照控制。"""

    def __init__(self, store: GnssStore, *, playback: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._playback = playback
        self._theme = "dark"
        self._selected_history: Optional[int] = None
        self._follow_latest = True
        self._system_checks: Dict[int, QCheckBox] = {}
        self._setup_ui()
        self._store.changed.connect(self.refresh)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(5)
        controls = QHBoxLayout()
        for system, name in SYSTEM_NAMES.items():
            checkbox = QCheckBox(name)
            checkbox.setChecked(system != 7)
            checkbox.toggled.connect(self.refresh)
            self._system_checks[system] = checkbox
            controls.addWidget(checkbox)
        self._cn0_color = QCheckBox("天空图按 C/N₀ 着色")
        self._cn0_color.toggled.connect(self.refresh)
        controls.addWidget(self._cn0_color)
        controls.addStretch(1)
        self._stats = QLabel("等待 GNSS 数据")
        controls.addWidget(self._stats)
        root.addLayout(controls)

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
        history_layout.addWidget(QLabel("快照"))
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
        self._details_toggle.setText("观测明细")
        self._details_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._details_toggle.setCheckable(True)
        self._details_toggle.setArrowType(Qt.RightArrow)
        self._details_toggle.toggled.connect(self._toggle_details)
        root.addWidget(self._details_toggle)
        self._table = QTableWidget(0, 11)
        self._table.setHorizontalHeaderLabels([
            "系统", "卫星", "频段", "Signal", "C/N₀", "Tracking", "Phase", "Code", "PRN", "Primary", "GLO Ch",
        ])
        self._table.setVisible(False)
        root.addWidget(self._table)

    def _toggle_details(self, visible: bool) -> None:
        self._details_toggle.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)
        self._table.setVisible(visible)

    def _enabled_systems(self) -> Set[int]:
        return {system for system, checkbox in self._system_checks.items() if checkbox.isChecked()}

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

    def refresh(self) -> None:
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
        stale = not self._playback and self._store.is_stale(3.0)
        bars = build_frequency_bars(snapshot, systems)
        self._sky.set_data(snapshot, systems, self._cn0_color.isChecked(), stale)
        self._bars.set_data(bars, stale)
        visible = sum(
            1
            for talker, report in snapshot.sky_by_talker.items()
            for satellite in report.satellites
            if infer_sky_system(talker, satellite.prn) in systems
        )
        locked = [] if snapshot.cnr is None else [
            observation.cn0_dbhz
            for observation in snapshot.cnr.observations
            if observation.system in systems and observation_locked(observation)
        ]
        age = self._store.data_age_s()
        age_text = "—" if age is None or self._playback else f"{age:.1f}s"
        self._stats.setText(
            f"可见星 {visible} · 有效 CNR {len(locked)} · "
            f"平均 {sum(locked) / len(locked):.1f} / 最大 {max(locked):.0f} dB-Hz · "
            f"年龄 {age_text}{' · STALE' if stale else ''}"
            if locked else f"可见星 {visible} · 有效 CNR 0 · 年龄 {age_text}{' · STALE' if stale else ''}"
        )
        bands = sorted({bar.band for bar in bars}, key=lambda band: (_BAND_ORDER.get(band, 99), band))
        legend_items = [
            f'<span style="color:{BAND_COLORS.get(band, "#94A3B8")}">■</span> {band}'
            for band in bands
        ]
        self._legend.setText("频段图例: " + "&nbsp;&nbsp;".join(legend_items) if bands else "频段图例: —")
        self._refresh_table(snapshot, systems)

    def _refresh_table(self, snapshot: GnssSnapshot, systems: Set[int]) -> None:
        observations = [] if snapshot.cnr is None else [
            observation for observation in snapshot.cnr.observations if observation.system in systems
        ]
        observations.sort(key=lambda item: (item.system, item.prn, signal_band(item.system, item.signal_type), item.signal_type))
        self._table.setRowCount(len(observations))
        for row, observation in enumerate(observations):
            flags = observation.lock_flags
            values = [
                SYSTEM_NAMES.get(observation.system, "Other"),
                satellite_label(observation.system, observation.prn),
                signal_band(observation.system, observation.signal_type),
                signal_name(observation.system, observation.signal_type),
                str(observation.cn0_dbhz),
                str(observation.tracking_state),
                "✓" if flags & LOCK_PHASE else "",
                "✓" if flags & LOCK_CODE else "",
                "✓" if flags & LOCK_PRN else "",
                "✓" if flags & LOCK_PRIMARY else "",
                str(observation.glo_freq_channel) if observation.system == 1 else "—",
            ]
            for column, value in enumerate(values):
                self._table.setItem(row, column, QTableWidgetItem(value))

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = theme
        dark = theme != "light"
        self._sky.set_dark_theme(dark)
        self._bars.set_dark_theme(dark)
        palette = S.palette(theme)
        self.setStyleSheet(f"GnssWidget {{ background: {palette['bg']}; color: {palette['text']}; }}")
