"""Shared customer-facing voltage/current history for the external PSU."""

from __future__ import annotations

from typing import Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPoint, QRectF, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication, QShowEvent
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.external_power_monitor import (
    EXTERNAL_POWER_HISTORY_SECONDS,
    ExternalPowerPhase,
    ExternalPowerSnapshot,
    ExternalPowerStore,
)
from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.ui import styles as S


class ExternalPowerAnchor(QPushButton):
    activated = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFlat(True)
        self.setText("")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.clicked.connect(lambda _checked=False: self.activated.emit())
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        description = " · ".join(
            (tr("External power"), tr("Voltage / current"), tr("Last 30 minutes"))
        )
        self.setToolTip(description)
        self.setAccessibleName(description)


class ExternalPowerHistoryWindow(QMainWindow):
    """One process-wide top-level view over the process-wide power Store."""

    def __init__(
        self,
        store: ExternalPowerStore,
        *,
        theme: str = "dark",
        parent: Optional[QWidget] = None,
    ) -> None:
        flags = (
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        super().__init__(parent, flags)
        self._store = store
        self._theme = str(theme)
        self._positioned_once = False

        central = QWidget()
        central.setObjectName("externalPowerWindow")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(7)

        self._status = QLabel()
        self._status.setObjectName("externalPowerStatus")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        bottom_axis = pg.DateAxisItem(orientation="bottom")
        self._plot = pg.PlotWidget(axisItems={"bottom": bottom_axis})
        self._plot.setObjectName("externalPowerPlot")
        self._plot.setMouseEnabled(x=True, y=True)
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()
        self._plot.showGrid(x=True, y=True, alpha=0.18)
        self._plot.setDownsampling(mode="peak", auto=True)
        self._plot.setClipToView(True)
        self._plot.showAxis("right")
        self._voltage_curve = self._plot.plot([], [], connect="finite")
        self._current_view = pg.ViewBox()
        self._plot.scene().addItem(self._current_view)
        self._plot.getAxis("right").linkToView(self._current_view)
        self._current_view.setXLink(self._plot.getPlotItem())
        self._current_curve = pg.PlotCurveItem([], [], connect="finite")
        self._current_view.addItem(self._current_curve)
        self._plot.getPlotItem().vb.sigResized.connect(self._sync_current_geometry)
        layout.addWidget(self._plot, 1)

        self.setCentralWidget(central)
        self.resize(620, 340)
        self._store.updated.connect(self._on_store_updated)
        self.set_theme(self._theme)
        self.retranslate_ui()
        register_translatable(self)

    def present_near(self, anchor: QWidget) -> None:
        position_after_show = not self._positioned_once
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        if position_after_show:
            self._move_near(anchor)
            self._positioned_once = True
        self.raise_()
        self.activateWindow()

    def _move_near(self, anchor: QWidget) -> None:
        anchor_center = anchor.mapToGlobal(anchor.rect().center())
        screen = QGuiApplication.screenAt(anchor_center) or QGuiApplication.primaryScreen()
        if screen is None:
            self.move(anchor_center + QPoint(12, 12))
            return
        available = screen.availableGeometry()
        width = self.frameGeometry().width()
        height = self.frameGeometry().height()
        x = anchor_center.x() - width // 2
        above_y = anchor.mapToGlobal(QPoint(0, 0)).y() - height - 10
        below_y = anchor.mapToGlobal(QPoint(0, anchor.height())).y() + 10
        y = above_y if above_y >= available.top() else below_y
        x = min(max(x, available.left()), max(available.left(), available.right() - width + 1))
        y = min(max(y, available.top()), max(available.top(), available.bottom() - height + 1))
        self.move(x, y)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        self.refresh()
        super().showEvent(event)

    @Slot()
    def _on_store_updated(self) -> None:
        if self.isVisible():
            self.refresh()

    @Slot()
    def _sync_current_geometry(self) -> None:
        plot_item = self._plot.getPlotItem()
        self._current_view.setGeometry(QRectF(plot_item.vb.sceneBoundingRect()))
        self._current_view.linkedViewChanged(plot_item.vb, self._current_view.XAxis)

    def refresh(self) -> None:
        snapshot = self._store.snapshot
        times, voltages, currents = self._store.history()
        self._voltage_curve.setData(times, voltages)
        self._current_curve.setData(times, currents)
        self._status.setText(self._snapshot_text(snapshot))
        self._update_ranges(times, voltages, currents)
        self._sync_current_geometry()

    @staticmethod
    def _snapshot_text(snapshot: ExternalPowerSnapshot) -> str:
        if snapshot.phase is ExternalPowerPhase.UNCONFIGURED:
            return tr("External power is not configured")
        if snapshot.phase is ExternalPowerPhase.INACTIVE:
            return tr("External power monitoring is inactive")
        if snapshot.phase is ExternalPowerPhase.CONNECTING:
            return tr("Connecting to external power...")
        if snapshot.phase is ExternalPowerPhase.READ_FAILED:
            return tr("External power read failed: {detail}", detail=snapshot.error or "—")
        sample = snapshot.sample
        if sample is None:
            return tr("External power data is unavailable")
        output = tr("On") if sample.output_enabled else tr("Off")
        protection = tr("Protection tripped") if sample.protection_tripped else tr("No protection trip")
        output_label = tr("Output")
        operation_label = tr("Operation status")
        questionable_label = tr("Questionable status")
        return (
            f"{sample.identity.model} · SN {sample.identity.serial_number or '—'} · "
            f"{output_label} {output} · {sample.voltage_v:.2f} V · "
            f"{sample.current_a:.3f} A · {sample.power_w:.2f} W · {protection} · "
            f"{operation_label} 0x{sample.operation_condition:X} · "
            f"{questionable_label} 0x{sample.questionable_condition:X}"
        )

    def _update_ranges(
        self,
        times: np.ndarray,
        voltages: np.ndarray,
        currents: np.ndarray,
    ) -> None:
        if times.size:
            end = float(times[-1])
            start = max(float(times[0]), end - EXTERNAL_POWER_HISTORY_SECONDS)
            self._plot.setXRange(start, max(end, start + 1.0), padding=0.0)
        self._set_y_range(self._plot.getPlotItem().vb, voltages)
        self._set_y_range(self._current_view, currents)

    @staticmethod
    def _set_y_range(view: pg.ViewBox, values: np.ndarray) -> None:
        finite = values[np.isfinite(values)]
        if not finite.size:
            return
        minimum = float(np.min(finite))
        maximum = float(np.max(finite))
        spread = maximum - minimum
        if spread < 0.2:
            padding = 0.1
        else:
            padding = max(0.05, spread * 0.1)
        view.setYRange(minimum - padding, maximum + padding, padding=0.0)

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = str(theme)
        palette = S.palette(self._theme)
        self.centralWidget().setStyleSheet(
            f"#externalPowerWindow {{ background: {palette['panel']}; color: {palette['text']}; }}"
            f"#externalPowerStatus {{ color: {palette['text']}; font-weight: 600; }}"
        )
        self._plot.setBackground(palette["panel"])
        self._voltage_curve.setPen(pg.mkPen(palette["accent_2"], width=2))
        self._current_curve.setPen(pg.mkPen(palette["warn"], width=2))
        for axis in ("left", "bottom", "right"):
            self._plot.getAxis(axis).setPen(pg.mkPen(palette["border_2"]))
            self._plot.getAxis(axis).setTextPen(pg.mkPen(palette["text_2"]))

    def retranslate_ui(self) -> None:
        self.setWindowTitle(
            " · ".join(
                (tr("External power"), tr("Voltage / current"), tr("Last 30 minutes"))
            )
        )
        self._plot.setLabel("left", tr("Voltage"), units="V")
        self._plot.setLabel("right", tr("Current"), units="A")
        self._plot.setLabel("bottom", tr("Sample time"))
        self._status.setText(self._snapshot_text(self._store.snapshot))


__all__ = ["ExternalPowerAnchor", "ExternalPowerHistoryWindow"]
