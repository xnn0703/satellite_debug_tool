"""Compact per-device SNR chart used by the production workspace."""

from __future__ import annotations

import math
from typing import Iterable, Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.production import SnrSample
from satellite_debug_tool.i18n import tr
from satellite_debug_tool.ui import styles as S


class _ElidedLabel(QLabel):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    @property
    def full_text(self) -> str:
        return self._full_text

    def set_full_text(self, text: str) -> None:
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._update_elision()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_elision()

    def _update_elision(self) -> None:
        available = max(0, self.contentsRect().width())
        QLabel.setText(
            self,
            self.fontMetrics().elidedText(
                self._full_text,
                Qt.TextElideMode.ElideRight,
                available,
            ),
        )


class ProductionSnrPanel(QFrame):
    """One stable production slot with status header and SNR history."""

    def __init__(
        self,
        slot: int,
        color: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.slot = int(slot)
        self._color = str(color)
        self._serial_number = ""
        self._endpoint = "-"
        self._connection = tr("Offline")
        self._recording = tr("Batch not created")
        self._current_test = "-"
        self._result = "-"
        self._current_snr_db: Optional[float] = None
        self._compact = False
        self._curve_x = np.array([], dtype=np.float64)
        self._curve_y = np.array([], dtype=np.float32)

        self.setObjectName("productionSnrPanel")
        self.setMinimumSize(240, 100)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self._identity_label = _ElidedLabel()
        self._identity_label.setObjectName("productionSnrIdentity")
        self._value_label = QLabel("-")
        self._value_label.setObjectName("productionSnrValue")
        self._value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        top.addWidget(self._identity_label, 1)
        top.addWidget(self._value_label)
        layout.addLayout(top)

        self._connection_label = _ElidedLabel()
        self._connection_label.setObjectName("productionSnrStatus")
        self._test_label = _ElidedLabel()
        self._test_label.setObjectName("productionSnrTest")
        layout.addWidget(self._connection_label)
        layout.addWidget(self._test_label)

        self._plot = pg.PlotWidget()
        self._plot.setObjectName("productionSnrPlot")
        self._plot.setMinimumHeight(38)
        self._plot.setMouseEnabled(x=False, y=False)
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()
        self._plot.showGrid(x=True, y=True, alpha=0.16)
        self._plot.setLabel("left", "SNR", units="dB")
        self._plot.setLabel("bottom", tr("Batch time"), units="s")
        self._plot.getAxis("bottom").enableAutoSIPrefix(False)
        self._plot.setDownsampling(mode="peak", auto=True)
        self._plot.setClipToView(True)
        self._curve = self._plot.plot([], [], connect="finite")
        layout.addWidget(self._plot, 1)

        self.set_status()
        self.set_theme("dark")

    @property
    def serial_number(self) -> str:
        return self._serial_number

    @property
    def current_snr_db(self) -> Optional[float]:
        return self._current_snr_db

    @property
    def curve_data(self) -> tuple[np.ndarray, np.ndarray]:
        return self._curve_x.copy(), self._curve_y.copy()

    def set_status(
        self,
        *,
        serial_number: Optional[str] = None,
        endpoint: Optional[str] = None,
        connection: Optional[str] = None,
        recording: Optional[str] = None,
        current_test: Optional[str] = None,
        result: Optional[str] = None,
    ) -> None:
        if serial_number is not None:
            self._serial_number = str(serial_number)
        if endpoint is not None:
            self._endpoint = str(endpoint) or "-"
        if connection is not None:
            self._connection = str(connection) or "-"
        if recording is not None:
            self._recording = str(recording) or "-"
        if current_test is not None:
            self._current_test = str(current_test) or "-"
        if result is not None:
            self._result = str(result) or "-"
        self._refresh_header()

    def set_samples(
        self,
        samples: Iterable[SnrSample],
        *,
        origin_monotonic_ns: int,
        online: bool,
        gap_s: float = 1.0,
    ) -> None:
        x_values: list[float] = []
        y_values: list[float] = []
        previous_ns: Optional[int] = None
        latest_value: Optional[float] = None
        gap_ns = int(max(0.05, float(gap_s)) * 1_000_000_000)
        for sample in samples:
            if previous_ns is not None and sample.monotonic_ns - previous_ns > gap_ns:
                midpoint_ns = previous_ns + (sample.monotonic_ns - previous_ns) // 2
                x_values.append(
                    (midpoint_ns - int(origin_monotonic_ns)) / 1_000_000_000.0
                )
                y_values.append(math.nan)
            x_values.append(
                (sample.monotonic_ns - int(origin_monotonic_ns)) / 1_000_000_000.0
            )
            y_values.append(float(sample.value_db))
            latest_value = float(sample.value_db)
            previous_ns = sample.monotonic_ns

        self._curve_x = np.asarray(x_values, dtype=np.float64)
        self._curve_y = np.asarray(y_values, dtype=np.float32)
        self._curve.setData(self._curve_x, self._curve_y, connect="finite")
        self._current_snr_db = latest_value if online else None
        self._value_label.setText(
            f"{self._current_snr_db:.2f} dB"
            if self._current_snr_db is not None
            else "- dB"
        )

    def set_ranges(
        self,
        *,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
    ) -> None:
        self._plot.setXRange(float(x_min), float(x_max), padding=0.0)
        self._plot.setYRange(float(y_min), float(y_max), padding=0.0)

    def set_theme(self, theme: str) -> None:
        palette = S.palette(theme)
        self.setStyleSheet(
            f"#productionSnrPanel {{ background: {palette['card']}; "
            f"border: 1px solid {palette['border']}; border-radius: 4px; }}"
            f"#productionSnrIdentity {{ color: {palette['text']}; font-weight: 700; }}"
            f"#productionSnrValue {{ color: {self._color}; font-size: 17px; font-weight: 700; "
            f"font-family: '{S.monospace_family()}'; }}"
            f"#productionSnrStatus, #productionSnrTest {{ color: {palette['text_2']}; "
            f"font-size: 10px; }}"
        )
        self._plot.setBackground(palette["panel"])
        self._curve.setPen(pg.mkPen(self._color, width=1.7))
        for axis_name in ("left", "bottom"):
            axis = self._plot.getAxis(axis_name)
            axis.setPen(pg.mkPen(palette["border_2"]))
            axis.setTextPen(pg.mkPen(palette["text_2"]))

    def retranslate_ui(self) -> None:
        self._update_axis_label()
        self._refresh_header()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        compact = self.height() < 145
        if compact != self._compact:
            self._compact = compact
            self._update_axis_label()

    def _update_axis_label(self) -> None:
        if self._compact:
            self._plot.setLabel("bottom", "")
        else:
            self._plot.setLabel("bottom", tr("Batch time"), units="s")

    def _refresh_header(self) -> None:
        identity = self._serial_number or tr("Waiting for device")
        self._identity_label.set_full_text(
            tr("Slot {slot} | {identity}", slot=self.slot, identity=identity)
        )
        self._connection_label.set_full_text(
            tr(
                "{endpoint} | {connection} | {recording}",
                endpoint=self._endpoint,
                connection=self._connection,
                recording=self._recording,
            )
        )
        self._test_label.set_full_text(
            tr(
                "Test: {test} | Result: {result}",
                test=self._current_test,
                result=self._result,
            )
        )


__all__ = ["ProductionSnrPanel"]
