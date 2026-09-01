"""Read-only Product Service component-temperature history windows."""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPoint, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication, QShowEvent
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.product import (
    Availability,
    COMPONENT_TEMPERATURE_HISTORY_SECONDS,
    ProductServiceStore,
)
from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.ui import styles as S


_COMPONENT_KEYS = frozenset(("converter", "tx_array", "rx_array"))


class ComponentTemperatureAnchor(QPushButton):
    """Keyboard-accessible click target used by one component summary."""

    activated = Signal()

    def __init__(
        self,
        component_title_source: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._component_title_source = str(component_title_source)
        self.setFlat(True)
        self.setText("")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.clicked.connect(lambda _checked=False: self.activated.emit())
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        title = tr(self._component_title_source)
        description = f"{title} · {tr('Temperature')} · {tr('Last 30 minutes')}"
        self.setToolTip(description)
        self.setAccessibleName(description)


class ComponentTemperatureWindow(QMainWindow):
    """Native top-level window rendering one Store-owned temperature history."""

    def __init__(
        self,
        store: ProductServiceStore,
        component_key: str,
        component_title_source: str,
        *,
        theme: str = "dark",
        parent: Optional[QWidget] = None,
    ) -> None:
        key = str(component_key).strip().lower()
        if key not in _COMPONENT_KEYS:
            raise ValueError(f"unknown Product component: {component_key}")
        flags = (
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        super().__init__(parent, flags)
        self._store = store
        self._component_key = key
        self._component_title_source = str(component_title_source)
        self._theme = str(theme)
        self._positioned_once = False

        central = QWidget()
        central.setObjectName("componentTemperatureWindow")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(7)

        self._current = QLabel()
        self._current.setObjectName("componentTemperatureCurrent")
        layout.addWidget(self._current)

        self._plot = pg.PlotWidget()
        self._plot.setObjectName("componentTemperaturePlot")
        self._plot.setMouseEnabled(x=True, y=True)
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()
        self._plot.showGrid(x=True, y=True, alpha=0.18)
        self._plot.getAxis("bottom").enableAutoSIPrefix(False)
        self._plot.setDownsampling(mode="peak", auto=True)
        self._plot.setClipToView(True)
        self._curve = self._plot.plot([], [], connect="finite")
        layout.addWidget(self._plot, 1)

        self.setCentralWidget(central)
        self.resize(520, 300)
        self._store.updated.connect(self._on_store_updated)
        self.set_theme(self._theme)
        self.retranslate_ui()
        register_translatable(self)

    @property
    def component_key(self) -> str:
        return self._component_key

    def present_near(self, anchor: QWidget) -> None:
        """Show or reactivate this window, positioning it only on first open."""

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

    def refresh(self) -> None:
        times, values = self._store.component_temperature_history(
            self._component_key,
            window_s=COMPONENT_TEMPERATURE_HISTORY_SECONDS,
        )
        self._curve.setData(times, values)
        self._update_current_value()
        self._update_ranges(times, values)

    def _update_current_value(self) -> None:
        component = getattr(self._store.snapshot(), self._component_key)
        temperature = component.temperature_c
        if temperature.value is None or not math.isfinite(float(temperature.value)):
            text = tr("Unavailable")
        else:
            text = f"{float(temperature.value):.1f} °C"
            if temperature.availability is Availability.STALE:
                text = tr("{value} (stale)", value=text)
        self._current.setText(f"{tr('Temperature')}: {text}")

    def _update_ranges(self, times: np.ndarray, values: np.ndarray) -> None:
        if times.size:
            end = float(times[-1])
            start = max(0.0, end - COMPONENT_TEMPERATURE_HISTORY_SECONDS)
            self._plot.setXRange(start, max(end, start + 1.0), padding=0.0)
        finite = values[np.isfinite(values)]
        if not finite.size:
            return
        minimum = float(np.min(finite))
        maximum = float(np.max(finite))
        spread = maximum - minimum
        if spread < 2.0:
            center = (minimum + maximum) / 2.0
            minimum = center - 2.0
            maximum = center + 2.0
        else:
            padding = max(0.5, spread * 0.1)
            minimum -= padding
            maximum += padding
        self._plot.setYRange(minimum, maximum, padding=0.0)

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = str(theme)
        palette = S.palette(self._theme)
        self.centralWidget().setStyleSheet(
            f"#componentTemperatureWindow {{ background: {palette['panel']}; color: {palette['text']}; }}"
            f"#componentTemperatureCurrent {{ color: {palette['text']}; font-weight: 600; }}"
        )
        self._plot.setBackground(palette["panel"])
        self._curve.setPen(pg.mkPen(palette["accent_2"], width=2))
        for axis in ("left", "bottom"):
            self._plot.getAxis(axis).setPen(pg.mkPen(palette["border_2"]))
            self._plot.getAxis(axis).setTextPen(pg.mkPen(palette["text_2"]))

    def retranslate_ui(self) -> None:
        component = tr(self._component_title_source)
        self.setWindowTitle(
            f"{component} · {tr('Temperature')} · {tr('Last 30 minutes')}"
        )
        self._plot.setLabel("left", tr("Temperature"), units="°C")
        self._plot.setLabel("bottom", tr("Device uptime"), units="s")
        self._update_current_value()


__all__ = ["ComponentTemperatureAnchor", "ComponentTemperatureWindow"]
