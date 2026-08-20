"""XESA01 Orbit/TLE on-demand popup content."""

from __future__ import annotations

import csv
import json
import math
import time
import zlib
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.data import OrbitStore
from satellite_debug_tool.core.protocol import (
    ORBIT_UPLOAD_CHUNK_MAX,
    ORBIT_FEATURE_SKY_SNAPSHOT,
    OrbitCapabilitiesReport,
    OrbitCatalogReport,
    OrbitCurrentReport,
    OrbitCurrentSample,
    OrbitSkyReport,
    OrbitOperation,
    OrbitPassPage,
    OrbitPredictionAccepted,
    OrbitPredictionPage,
    OrbitStatus,
    OrbitStatusReport,
    OrbitUploadProgress,
    build_orbit_capabilities,
    build_orbit_catalog,
    build_orbit_current,
    build_orbit_predict,
    build_orbit_prediction_page,
    build_orbit_scan,
    build_orbit_select,
    build_orbit_upload_begin,
    build_orbit_upload_chunk,
    build_orbit_upload_end,
    build_orbit_upload_abort,
)
from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.ui import styles as S


def _utc_text(unix_ms: int) -> str:
    if unix_ms <= 0:
        return "—"
    return datetime.fromtimestamp(unix_ms / 1000.0, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _number(value: float, digits: int = 2) -> str:
    return "—" if not math.isfinite(value) else f"{value:.{digits}f}"


class OrbitSkyPlot(QWidget):
    """North-up, clockwise-azimuth sky plot for Orbit current samples."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(330, 330)
        self._samples: tuple[OrbitCurrentSample, ...] = ()
        self._dark = True

    def set_samples(self, samples: Sequence[OrbitCurrentSample]) -> None:
        self._samples = tuple(samples)
        self.update()

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._dark = theme == "dark"
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        palette = S.palette("dark" if self._dark else "light")
        painter.fillRect(self.rect(), QColor(palette["panel"]))
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        radius = max(20.0, min(self.width(), self.height()) / 2.0 - 38.0)
        painter.setPen(QPen(QColor(palette["border_2"]), 1.0))
        for elevation in (0, 30, 60):
            ring = radius * (90.0 - elevation) / 90.0
            painter.drawEllipse(center, ring, ring)
        painter.drawLine(QPointF(center.x(), center.y() - radius), QPointF(center.x(), center.y() + radius))
        painter.drawLine(QPointF(center.x() - radius, center.y()), QPointF(center.x() + radius, center.y()))
        painter.setPen(QColor(palette["text_muted"]))
        painter.drawText(QRectF(center.x() - 10, center.y() - radius - 24, 20, 18), Qt.AlignCenter, "N")
        painter.drawText(QRectF(center.x() + radius + 7, center.y() - 9, 20, 18), Qt.AlignCenter, "E")
        painter.drawText(QRectF(center.x() - 10, center.y() + radius + 5, 20, 18), Qt.AlignCenter, "S")
        painter.drawText(QRectF(center.x() - radius - 27, center.y() - 9, 20, 18), Qt.AlignCenter, "W")
        for sample in self._samples:
            if not math.isfinite(sample.azimuth_deg) or not math.isfinite(sample.elevation_deg):
                continue
            radial = radius * (90.0 - max(0.0, min(90.0, sample.elevation_deg))) / 90.0
            angle = math.radians(sample.azimuth_deg)
            point = QPointF(center.x() + radial * math.sin(angle), center.y() - radial * math.cos(angle))
            visible = bool(
                getattr(sample, "visible", getattr(sample, "geographic_visible", False))
            )
            color = QColor("#F59E0B" if sample.stale else "#22C55E" if visible else "#3B82F6")
            painter.setPen(QPen(color, 1.5))
            painter.setBrush(color)
            painter.drawEllipse(point, 5.0, 5.0)
            painter.drawText(QRectF(point.x() + 7, point.y() - 9, 70, 18), str(sample.norad_id))


class OrbitWidget(QWidget):
    """Lazy floating Orbit UI; all device I/O is delegated to LiveView's worker."""

    status_message = Signal(str, int)

    def __init__(
        self,
        store: OrbitStore,
        send_frame: Callable[[bytes], bool],
        next_request_id: Callable[[], int],
        is_connected: Callable[[], bool],
        set_sky_consumer: Optional[Callable[[str, bool], None]] = None,
        request_sky_refresh: Optional[Callable[[], bool]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._send_frame = send_frame
        self._next_request_id = next_request_id
        self._is_connected = is_connected
        self._set_sky_consumer = set_sky_consumer
        self._request_sky_refresh = request_sky_refresh
        self._theme = "dark"
        self._upload_data: Optional[bytes] = None
        self._upload_request_id = 0
        self._upload_offset = 0
        self._refresh_ticks = 0
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._on_refresh_tick)
        self._upload_timer = QTimer(self)
        self._upload_timer.setSingleShot(True)
        self._upload_timer.setInterval(5000)
        self._upload_timer.timeout.connect(self._on_upload_timeout)
        self._setup_ui()
        self._store.changed.connect(self._on_store_changed)
        self._store.cleared.connect(self._on_store_cleared)
        register_translatable(self)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        self._status = QLabel(tr("Waiting for Orbit capability..."))
        root.addWidget(self._status)
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)
        self._setup_catalog_tab()
        self._setup_current_tab()
        self._setup_prediction_tab()

    def _setup_catalog_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        tools = QHBoxLayout()
        self._upload_btn = QPushButton(tr("Upload tle.txt"))
        self._scan_btn = QPushButton(tr("Scan device"))
        self._catalog_refresh_btn = QPushButton(tr("Refresh"))
        self._select_btn = QPushButton(tr("Set tracking target"))
        self._upload_btn.clicked.connect(self._choose_upload)
        self._scan_btn.clicked.connect(self._scan)
        self._catalog_refresh_btn.clicked.connect(self.request_catalog)
        self._select_btn.clicked.connect(self._select_target)
        for button in (self._upload_btn, self._scan_btn, self._catalog_refresh_btn, self._select_btn):
            tools.addWidget(button)
        tools.addStretch(1)
        layout.addLayout(tools)
        self._catalog_summary = QLabel("—")
        layout.addWidget(self._catalog_summary)
        self._catalog_table = QTableWidget(0, 5)
        self._catalog_table.setHorizontalHeaderLabels(
            [tr("NORAD"), tr("Satellite"), tr("TLE epoch"), tr("Age / status"), tr("Source")]
        )
        self._catalog_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._catalog_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._catalog_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._catalog_table, 1)
        self._tabs.addTab(tab, tr("Catalog"))

    def _setup_current_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        row = QHBoxLayout()
        self._current_refresh_btn = QPushButton(tr("Refresh current positions"))
        self._current_refresh_btn.clicked.connect(self.request_current)
        row.addWidget(self._current_refresh_btn)
        row.addWidget(QLabel(tr("Minimum elevation")))
        self._current_min_el = QDoubleSpinBox()
        self._current_min_el.setRange(-5.0, 90.0)
        self._current_min_el.setValue(0.0)
        self._current_min_el.setSuffix("°")
        self._current_min_el.valueChanged.connect(self._render_current)
        row.addWidget(self._current_min_el)
        row.addStretch(1)
        layout.addLayout(row)
        splitter = QSplitter(Qt.Horizontal)
        self._sky_plot = OrbitSkyPlot()
        splitter.addWidget(self._sky_plot)
        self._current_table = QTableWidget(0, 8)
        self._current_table.setHorizontalHeaderLabels(
            [tr("NORAD"), tr("Azimuth"), tr("Elevation"), tr("Range km"), tr("Latitude"),
             tr("Longitude"), tr("Altitude km"), tr("TLE status")]
        )
        self._current_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._current_table.horizontalHeader().setStretchLastSection(True)
        splitter.addWidget(self._current_table)
        splitter.setSizes([380, 760])
        layout.addWidget(splitter, 1)
        self._tabs.addTab(tab, tr("Current sky"))

    def _setup_prediction_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        controls = QHBoxLayout()
        self._target_combo = QComboBox()
        self._target_combo.addItem(tr("All satellites (pass summary)"), 0)
        self._horizon = QSpinBox()
        self._horizon.setRange(60, 86400)
        self._horizon.setValue(3600)
        self._horizon.setSuffix(" s")
        self._step = QSpinBox()
        self._step.setRange(5, 600)
        self._step.setValue(30)
        self._step.setSuffix(" s")
        self._prediction_min_el = QDoubleSpinBox()
        self._prediction_min_el.setRange(-5.0, 90.0)
        self._prediction_min_el.setValue(0.0)
        self._prediction_min_el.setSuffix("°")
        self._predict_btn = QPushButton(tr("Predict"))
        self._export_btn = QPushButton(tr("Export CSV/JSON"))
        self._predict_btn.clicked.connect(self._predict)
        self._export_btn.clicked.connect(self._export_prediction)
        for label, widget in (
            (tr("Target"), self._target_combo),
            (tr("Horizon"), self._horizon),
            (tr("Step"), self._step),
            (tr("Minimum elevation"), self._prediction_min_el),
        ):
            controls.addWidget(QLabel(label))
            controls.addWidget(widget)
        controls.addWidget(self._predict_btn)
        controls.addWidget(self._export_btn)
        layout.addLayout(controls)
        self._prediction_summary = QLabel(tr("No prediction requested"))
        layout.addWidget(self._prediction_summary)
        splitter = QSplitter(Qt.Vertical)
        self._prediction_plot = pg.PlotWidget()
        self._prediction_plot.showGrid(x=True, y=True, alpha=0.25)
        self._prediction_plot.setLabel("left", tr("Angle"), units="deg")
        self._prediction_plot.setLabel("bottom", tr("Seconds from start"), units="s")
        splitter.addWidget(self._prediction_plot)
        self._prediction_table = QTableWidget(0, 7)
        self._prediction_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._prediction_table.horizontalHeader().setStretchLastSection(True)
        splitter.addWidget(self._prediction_table)
        splitter.setSizes([330, 300])
        layout.addWidget(splitter, 1)
        self._tabs.addTab(tab, tr("Prediction"))

    def _request(self, builder: Callable[..., bytes], *args) -> bool:
        if not self._is_connected():
            self._status.setText(tr("Device is not connected"))
            return False
        request_id = self._next_request_id()
        return self._send_frame(builder(request_id, *args))

    @Slot()
    def request_capabilities(self) -> None:
        self._request(build_orbit_capabilities)

    @Slot()
    def request_catalog(self) -> None:
        self._request(build_orbit_catalog, 0)

    @Slot()
    def request_current(self) -> None:
        capabilities = self._store.capabilities
        if (
            capabilities is not None
            and capabilities.feature_flags & ORBIT_FEATURE_SKY_SNAPSHOT
            and self._request_sky_refresh is not None
        ):
            self._request_sky_refresh()
            return
        self._request(build_orbit_current, 0, 0)

    @Slot()
    def _scan(self) -> None:
        if self._request(build_orbit_scan):
            self._status.setText(tr("Device scan requested"))

    @Slot()
    def _predict(self) -> None:
        norad_id = int(self._target_combo.currentData() or 0)
        points = self._horizon.value() // self._step.value() + 1
        capabilities = self._store.capabilities
        if capabilities is not None and norad_id != 0 and points > capabilities.max_output_points:
            QMessageBox.warning(self, tr("Prediction"), tr("The requested prediction exceeds the device point limit."))
            return
        if self._request(
            build_orbit_predict,
            norad_id,
            self._horizon.value(),
            self._step.value(),
            self._prediction_min_el.value(),
        ):
            self._prediction_summary.setText(tr("Prediction submitted..."))

    @Slot()
    def _select_target(self) -> None:
        row = self._catalog_table.currentRow()
        if row < 0:
            QMessageBox.information(self, tr("Tracking target"), tr("Select a catalog row first."))
            return
        norad_id = int(self._catalog_table.item(row, 0).text())
        name = self._catalog_table.item(row, 1).text()
        answer = QMessageBox.question(
            self,
            tr("Tracking target"),
            tr("Set {name} (NORAD {norad}) as the tracking target? This does not enable TX.", name=name, norad=norad_id),
        )
        if answer == QMessageBox.Yes:
            self._request(build_orbit_select, norad_id)

    @Slot()
    def _choose_upload(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("Select tle.txt"), "", tr("TLE text (*.txt);;All files (*)"))
        if not path:
            return
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            QMessageBox.warning(self, tr("TLE upload"), str(exc))
            return
        capabilities = self._store.capabilities
        limit = 65536 if capabilities is None else capabilities.max_file_size
        if not data or len(data) > limit:
            QMessageBox.warning(self, tr("TLE upload"), tr("The selected file is empty or exceeds {limit} bytes.", limit=limit))
            return
        self._upload_data = data
        self._upload_offset = 0
        self._upload_request_id = self._next_request_id()
        frame = build_orbit_upload_begin(self._upload_request_id, len(data), zlib.crc32(data) & 0xFFFFFFFF)
        if self._send_frame(frame):
            self._upload_timer.start()
            self._status.setText(tr("Uploading tle.txt: 0%"))
        else:
            self._reset_upload()

    def _send_upload_chunk(self, offset: int) -> None:
        if self._upload_data is None:
            return
        chunk = self._upload_data[offset:offset + min(768, ORBIT_UPLOAD_CHUNK_MAX)]
        if not chunk:
            if self._send_frame(build_orbit_upload_end(self._upload_request_id)):
                self._upload_timer.start()
            return
        if self._send_frame(build_orbit_upload_chunk(self._upload_request_id, offset, chunk)):
            self._upload_timer.start()

    @Slot()
    def _on_upload_timeout(self) -> None:
        if self._upload_request_id != 0 and self._is_connected():
            self._send_frame(build_orbit_upload_abort(self._upload_request_id))
        self._status.setText(tr("TLE upload timed out and was cancelled."))
        self._reset_upload()

    def _reset_upload(self) -> None:
        self._upload_timer.stop()
        self._upload_data = None
        self._upload_request_id = 0
        self._upload_offset = 0

    @Slot(object)
    def _on_store_changed(self, report: object) -> None:
        if isinstance(report, OrbitCapabilitiesReport):
            self._status.setText(
                tr("Orbit service ready · {count} satellites · {hours} h maximum", count=report.max_catalog_entries,
                   hours=report.max_horizon_s // 3600)
            )
            self.request_catalog()
            self.request_current()
        elif isinstance(report, OrbitCatalogReport):
            self._render_catalog()
            snapshot = self._store.catalog_snapshot()
            if len(snapshot.entries) < report.total_entries and report.entries:
                self._request(build_orbit_catalog, report.page + 1)
        elif isinstance(report, OrbitCurrentReport):
            self._render_current()
            if len(self._store.current_samples()) < report.total_entries and report.samples:
                self._request(build_orbit_current, 0, report.page + 1)
        elif isinstance(report, OrbitSkyReport):
            self._render_current()
        elif isinstance(report, OrbitPredictionAccepted):
            self._prediction_summary.setText(
                tr("Job {job} · generation {generation} · fixed station · start {start}",
                   job=report.job_id, generation=report.generation, start=_utc_text(report.start_utc_ms))
            )
            self._request(build_orbit_prediction_page, report.job_id, 0)
        elif isinstance(report, OrbitPredictionPage):
            self._render_prediction()
            if len(self._store.prediction_samples()) < report.total_samples and report.samples:
                self._request(build_orbit_prediction_page, report.job_id, report.page + 1)
        elif isinstance(report, OrbitPassPage):
            self._render_prediction()
            if len(self._store.pass_summaries()) < report.total_satellites and report.passes:
                self._request(build_orbit_prediction_page, report.job_id, report.page + 1)
        elif isinstance(report, OrbitUploadProgress) and report.request_id == self._upload_request_id:
            if self._upload_data is None:
                return
            if report.operation == OrbitOperation.UPLOAD_BEGIN:
                self._send_upload_chunk(0)
            elif report.operation == OrbitOperation.UPLOAD_CHUNK:
                self._upload_offset = report.acknowledged_size
                percent = 100 * self._upload_offset // len(self._upload_data)
                self._status.setText(tr("Uploading tle.txt: {percent}%", percent=percent))
                self._send_upload_chunk(self._upload_offset)
        elif isinstance(report, OrbitStatusReport):
            self._handle_status(report)

    def _handle_status(self, report: OrbitStatusReport) -> None:
        if report.status != OrbitStatus.OK:
            self._status.setText(tr("Orbit request {operation} failed: {status}", operation=report.operation.name,
                                    status=report.status.name))
            if report.request_id == self._upload_request_id:
                self._reset_upload()
            return
        if report.operation == OrbitOperation.UPLOAD_END and report.request_id == self._upload_request_id:
            self._reset_upload()
            self._status.setText(tr("tle.txt uploaded; device catalog scan requested"))
            QTimer.singleShot(250, self.request_catalog)
        elif report.operation == OrbitOperation.SELECT:
            self._status.setText(tr("Tracking target accepted; TX state was not changed"))
        elif report.operation == OrbitOperation.SCAN:
            QTimer.singleShot(200, self.request_catalog)

    @Slot()
    def _on_store_cleared(self) -> None:
        self._status.setText(tr("Orbit data cleared; waiting for device"))
        self._catalog_table.setRowCount(0)
        self._current_table.setRowCount(0)
        self._prediction_table.setRowCount(0)
        self._sky_plot.set_samples(())
        self._prediction_plot.clear()
        self._reset_upload()

    def _render_catalog(self) -> None:
        snapshot = self._store.catalog_snapshot()
        self._catalog_summary.setText(
            tr("Generation {generation} · {loaded}/{total} · invalid {invalid} · duplicate {duplicate} · capacity {capacity}",
               generation=snapshot.generation, loaded=len(snapshot.entries), total=snapshot.total_entries,
               invalid=snapshot.invalid_records, duplicate=snapshot.duplicate_records,
               capacity=snapshot.capacity_rejections)
        )
        selected = self._target_combo.currentData()
        self._catalog_table.setRowCount(len(snapshot.entries))
        self._target_combo.blockSignals(True)
        self._target_combo.clear()
        self._target_combo.addItem(tr("All satellites (pass summary)"), 0)
        now_s = time.time()
        stale_days = 7.0 if self._store.capabilities is None else self._store.capabilities.stale_days
        for row, entry in enumerate(snapshot.entries):
            age_days = abs(now_s - entry.epoch_unix_s) / 86400.0
            stale = age_days > stale_days
            values = (
                str(entry.norad_id),
                entry.name,
                _utc_text(entry.epoch_unix_s * 1000),
                tr("{age:.1f} d · stale", age=age_days) if stale else tr("{age:.1f} d · valid", age=age_days),
                entry.source,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if stale:
                    item.setForeground(QColor("#F59E0B"))
                self._catalog_table.setItem(row, column, item)
            self._target_combo.addItem(f"{entry.name} · {entry.norad_id}", entry.norad_id)
        index = self._target_combo.findData(selected)
        self._target_combo.setCurrentIndex(max(0, index))
        self._target_combo.blockSignals(False)

    @Slot()
    def _render_current(self) -> None:
        threshold = self._current_min_el.value()
        source = self._store.sky_samples() or self._store.current_samples()
        samples = tuple(sample for sample in source if sample.elevation_deg >= threshold)
        self._sky_plot.set_samples(samples)
        self._current_table.setRowCount(len(samples))
        for row, sample in enumerate(samples):
            values = (
                str(sample.norad_id), _number(sample.azimuth_deg), _number(sample.elevation_deg),
                _number(sample.slant_range_m / 1000.0, 1), _number(sample.latitude_deg, 4),
                _number(sample.longitude_deg, 4), _number(sample.altitude_m / 1000.0, 1),
                tr("stale ({age:.1f} d)", age=sample.tle_age_days) if sample.stale else tr("valid ({age:.1f} d)", age=sample.tle_age_days),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if sample.stale:
                    item.setForeground(QColor("#F59E0B"))
                self._current_table.setItem(row, column, item)

    def _render_prediction(self) -> None:
        samples = self._store.prediction_samples()
        passes = self._store.pass_summaries()
        self._prediction_plot.clear()
        if samples:
            start = samples[0].utc_unix_ms
            x = [(item.utc_unix_ms - start) / 1000.0 for item in samples]
            self._prediction_plot.plot(x, [item.elevation_deg for item in samples], pen=pg.mkPen("#22C55E", width=2), name="Elevation")
            self._prediction_plot.plot(x, [item.azimuth_deg for item in samples], pen=pg.mkPen("#3B82F6", width=1), name="Azimuth")
            self._prediction_table.setColumnCount(6)
            self._prediction_table.setHorizontalHeaderLabels(
                [tr("UTC"), tr("Azimuth"), tr("Elevation"), tr("Range km"), tr("Satellite LLA"), tr("TLE status")]
            )
            self._prediction_table.setRowCount(len(samples))
            for row, item in enumerate(samples):
                values = (_utc_text(item.utc_unix_ms), _number(item.azimuth_deg), _number(item.elevation_deg),
                          _number(item.slant_range_m / 1000.0, 1),
                          f"{item.latitude_deg:.3f}, {item.longitude_deg:.3f}, {item.altitude_m / 1000.0:.1f} km",
                          tr("stale") if item.stale else tr("valid"))
                for column, value in enumerate(values):
                    self._prediction_table.setItem(row, column, QTableWidgetItem(value))
        else:
            self._prediction_table.setColumnCount(6)
            self._prediction_table.setHorizontalHeaderLabels(
                [tr("NORAD"), tr("AOS"), tr("LOS"), tr("Maximum UTC"), tr("Maximum elevation"), tr("Status")]
            )
            self._prediction_table.setRowCount(len(passes))
            for row, item in enumerate(passes):
                state = tr("stale") if item.stale else tr("no pass") if not item.has_pass else tr("pass")
                values = (str(item.norad_id), _utc_text(item.aos_utc_ms), _utc_text(item.los_utc_ms),
                          _utc_text(item.maximum_elevation_utc_ms), _number(item.maximum_elevation_deg), state)
                for column, value in enumerate(values):
                    self._prediction_table.setItem(row, column, QTableWidgetItem(value))

    @Slot()
    def _export_prediction(self) -> None:
        samples = self._store.prediction_samples()
        passes = self._store.pass_summaries()
        job = self._store.prediction_job
        if job is None or (not samples and not passes):
            QMessageBox.information(self, tr("Export"), tr("There is no completed prediction to export."))
            return
        path, selected = QFileDialog.getSaveFileName(
            self, tr("Export prediction"), "orbit_prediction.json", tr("JSON (*.json);;CSV (*.csv)")
        )
        if not path:
            return
        catalog = self._store.catalog_snapshot()
        relevant_entries = [
            asdict(entry)
            for entry in catalog.entries
            if job.norad_id == 0 or entry.norad_id == job.norad_id
        ]
        metadata = {
            "request": asdict(job),
            "assumption": "FIXED_STATION",
            "catalog_generation": job.generation,
            "tle_catalog": relevant_entries,
        }
        try:
            if path.lower().endswith(".csv") or "CSV" in selected:
                rows = [asdict(item) for item in (samples or passes)]
                with Path(path).open("w", newline="", encoding="utf-8-sig") as stream:
                    stream.write("# " + json.dumps(metadata, ensure_ascii=False) + "\n")
                    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
            else:
                payload = {**metadata, "samples": [asdict(item) for item in samples],
                           "passes": [asdict(item) for item in passes]}
                Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, tr("Export"), str(exc))
            return
        self._status.setText(tr("Prediction exported to {path}", path=path))

    @Slot()
    def _on_refresh_tick(self) -> None:
        if not self.isVisible() or not self._store.available:
            return
        self._refresh_ticks += 1
        snapshot = self._store.catalog_snapshot()
        if snapshot.scan_pending or snapshot.scan_running:
            self.request_catalog()
        elif (
            self._refresh_ticks % 5 == 0
            and (
                self._store.capabilities is None
                or not self._store.capabilities.feature_flags & ORBIT_FEATURE_SKY_SNAPSHOT
            )
        ):
            self.request_current()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._refresh_ticks = 0
        self._refresh_timer.start()
        if self._set_sky_consumer is not None:
            self._set_sky_consumer("orbit_popup", True)
        if self._store.available:
            self.request_catalog()
            self.request_current()
        else:
            self.request_capabilities()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._refresh_timer.stop()
        if self._set_sky_consumer is not None:
            self._set_sky_consumer("orbit_popup", False)
        super().hideEvent(event)

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
        self._sky_plot.set_theme(theme, scale)
        palette = S.palette(theme)
        self._prediction_plot.setBackground(palette["panel"])
        self._prediction_plot.getAxis("left").setTextPen(palette["text_muted"])
        self._prediction_plot.getAxis("bottom").setTextPen(palette["text_muted"])


__all__ = ["OrbitWidget", "OrbitSkyPlot"]
