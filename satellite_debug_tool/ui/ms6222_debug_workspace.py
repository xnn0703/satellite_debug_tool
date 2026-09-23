"""Independent engineering workspace for the MS-6222 reference sensor."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
from pathlib import Path
import time
from typing import Callable, Optional

import pyqtgraph as pg
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.production import (
    Ms6222Conclusion,
    Ms6222ControlLease,
    Ms6222DebugError,
    Ms6222FrameEnvelope,
    Ms6222FrameType,
    Ms6222GnssRecord,
    Ms6222InsRecord,
    Ms6222LeaseHandle,
    Ms6222RawImuRecord,
    Ms6222SerialWorker,
    Ms6222SessionRecorder,
    Ms6222WorkerStatistics,
)
from satellite_debug_tool.i18n import register_translatable, tr, tr_source
from satellite_debug_tool.ui import styles as S


class _FinalizeThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        recorder: Ms6222SessionRecorder,
        *,
        conclusion: Ms6222Conclusion,
        notes: str,
        statistics: Optional[Ms6222WorkerStatistics],
        abort_reason: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._recorder = recorder
        self._conclusion = conclusion
        self._notes = notes
        self._statistics = statistics
        self._abort_reason = abort_reason

    def run(self) -> None:
        try:
            if self._abort_reason:
                result = self._recorder.abort(
                    reason=self._abort_reason,
                    statistics=self._statistics,
                )
            else:
                result = self._recorder.complete(
                    conclusion=self._conclusion,
                    notes=self._notes,
                    statistics=self._statistics,
                )
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit(result)


class Ms6222DebugWorkspace(QWidget):
    status_message = Signal(str, int)
    active_changed = Signal(bool)
    VALID_DATA_TIMEOUT_S = 5.0

    def __init__(
        self,
        settings: Settings,
        lease: Ms6222ControlLease,
        *,
        session_root: Optional[Path] = None,
        worker_factory: Callable[..., Ms6222SerialWorker] = Ms6222SerialWorker,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._lease = lease
        self._session_root = session_root
        self._worker_factory = worker_factory
        self._lease_handle: Optional[Ms6222LeaseHandle] = None
        self._worker: Optional[Ms6222SerialWorker] = None
        self._connected = False
        self._valid_data_confirmed = False
        self._last_frame_wall_ns = 0
        self._last_valid_monotonic_ns = 0
        self._capture_started_monotonic_ns = 0
        self._latest_statistics: Optional[Ms6222WorkerStatistics] = None
        self._latest_ins: Optional[Ms6222InsRecord] = None
        self._latest_gnss: Optional[Ms6222GnssRecord] = None
        self._latest_rawimu: Optional[Ms6222RawImuRecord] = None
        self._recorder: Optional[Ms6222SessionRecorder] = None
        self._finalizer: Optional[_FinalizeThread] = None
        self._session_path: Optional[Path] = None
        self._view_active = False
        self._theme = "dark"
        self._attitude_samples: deque[tuple[int, float, float, float]] = deque(maxlen=30000)
        self._build_ui()
        self._refresh_ports()
        self._plot_timer = QTimer(self)
        self._plot_timer.setInterval(100)
        self._plot_timer.timeout.connect(self._refresh_plot)
        register_translatable(self)

    @property
    def session_active(self) -> bool:
        return self._recorder is not None

    @property
    def connection_active(self) -> bool:
        return self._worker is not None

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._scroll = scroll
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(7)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        connection = QGroupBox(tr("MS-6222 connection"))
        self._connection_group = connection
        connection_layout = QGridLayout(connection)
        self._port_label = QLabel(tr("Serial port"))
        self._port = QComboBox()
        self._port.setEditable(True)
        self._refresh_button = QPushButton(tr("Refresh"))
        self._refresh_button.clicked.connect(self._refresh_ports)
        self._connect_button = QPushButton(tr("Connect"))
        self._connect_button.clicked.connect(self._toggle_connection)
        self._serial_parameters = QLabel("460800 8N1")
        self._connection_state = QLabel(tr("Serial port closed"))
        self._data_state = QLabel(tr("Valid data not confirmed"))
        connection_layout.addWidget(self._port_label, 0, 0)
        connection_layout.addWidget(self._port, 0, 1, 1, 2)
        connection_layout.addWidget(self._refresh_button, 0, 3)
        connection_layout.addWidget(self._connect_button, 0, 4)
        connection_layout.addWidget(self._serial_parameters, 0, 5)
        connection_layout.addWidget(self._connection_state, 1, 0, 1, 3)
        connection_layout.addWidget(self._data_state, 1, 3, 1, 3)
        connection_layout.setColumnStretch(1, 1)
        root.addWidget(connection)

        status = QGroupBox(tr("Stream quality"))
        self._status_group = status
        status_layout = QGridLayout(status)
        self._status_labels: dict[str, QLabel] = {}
        for column, (key, source) in enumerate(
            (
                ("rates", tr_source("Frame rates")),
                ("valid", tr_source("Valid ratio")),
                ("errors", tr_source("Parser errors")),
                ("last", tr_source("Last valid frame")),
            )
        ):
            title = QLabel(tr(source))
            value = QLabel("-")
            status_layout.addWidget(title, 0, column)
            status_layout.addWidget(value, 1, column)
            self._status_labels[f"{key}_title"] = title
            self._status_labels[key] = value
        root.addWidget(status)

        body = QSplitter()
        body.setChildrenCollapsible(False)
        values = QWidget()
        values_layout = QVBoxLayout(values)
        values_layout.setContentsMargins(0, 0, 4, 0)
        self._ins_group, self._ins_values = self._value_group(
            tr_source("INS attitude and quality"),
            (
                tr_source("Roll / Pitch / Yaw"),
                tr_source("Attitude standard deviation"),
                tr_source("Time / GNSS / EKF state"),
                tr_source("GPS time"),
            ),
        )
        self._gnss_group, self._gnss_values = self._value_group(
            tr_source("GNSS solution"),
            (
                tr_source("Latitude / Longitude / Altitude"),
                tr_source("Velocity N / E / U"),
                tr_source("Used / tracked satellites"),
                tr_source("Fix / baseline / heading std"),
            ),
        )
        self._rawimu_group, self._rawimu_values = self._value_group(
            tr_source("Raw IMU"),
            (
                tr_source("Acceleration X / Y / Z"),
                tr_source("Angular rate X / Y / Z"),
                tr_source("Temperature / status"),
            ),
        )
        values_layout.addWidget(self._ins_group)
        values_layout.addWidget(self._gnss_group)
        values_layout.addWidget(self._rawimu_group)
        values_layout.addStretch(1)
        body.addWidget(values)

        plot_group = QGroupBox(tr("Reference attitude — last five minutes"))
        self._plot_group = plot_group
        plot_layout = QVBoxLayout(plot_group)
        self._plot = pg.PlotWidget()
        self._plot.showGrid(x=True, y=True, alpha=0.15)
        self._plot.setLabel("left", tr("Angle"), units="deg")
        self._plot.setLabel("bottom", tr("Elapsed time"), units="s")
        self._plot.addLegend()
        self._curves = {
            "roll": self._plot.plot([], [], pen=pg.mkPen("#5f7cff"), name="Roll"),
            "pitch": self._plot.plot([], [], pen=pg.mkPen("#00b3a4"), name="Pitch"),
            "yaw": self._plot.plot([], [], pen=pg.mkPen("#ffb020"), name="Yaw"),
        }
        plot_layout.addWidget(self._plot)
        body.addWidget(plot_group)
        body.setStretchFactor(0, 2)
        body.setStretchFactor(1, 3)
        root.addWidget(body, 1)

        session = QGroupBox(tr("Independent evidence session"))
        self._session_group = session
        session_layout = QGridLayout(session)
        self._operator_label = QLabel(tr("Operator"))
        self._operator = QLineEdit(str(self._settings.get("production.last_operator", "") or ""))
        self._notes_label = QLabel(tr("Notes"))
        self._notes = QLineEdit()
        self._conclusion_label = QLabel(tr("Engineering conclusion"))
        self._conclusion = QComboBox()
        self._conclusion.addItem(tr("Inconclusive"), Ms6222Conclusion.INCONCLUSIVE.value)
        self._conclusion.addItem(tr("Pass"), Ms6222Conclusion.PASS.value)
        self._conclusion.addItem(tr("Fail"), Ms6222Conclusion.FAIL.value)
        self._start_button = QPushButton(tr("Start capture"))
        self._start_button.setProperty("variant", "primary")
        self._start_button.clicked.connect(self._start_capture)
        self._finish_button = QPushButton(tr("Finish capture"))
        self._finish_button.clicked.connect(self._finish_capture)
        self._finish_button.setEnabled(False)
        session_layout.addWidget(self._operator_label, 0, 0)
        session_layout.addWidget(self._operator, 0, 1)
        session_layout.addWidget(self._notes_label, 0, 2)
        session_layout.addWidget(self._notes, 0, 3)
        session_layout.addWidget(self._conclusion_label, 0, 4)
        session_layout.addWidget(self._conclusion, 0, 5)
        session_layout.addWidget(self._start_button, 0, 6)
        session_layout.addWidget(self._finish_button, 0, 7)
        session_layout.setColumnStretch(3, 2)
        root.addWidget(session)

        self._event_log = QPlainTextEdit()
        self._event_log.setReadOnly(True)
        self._event_log.setMaximumBlockCount(1000)
        self._event_log.setMaximumHeight(90)
        root.addWidget(self._event_log)
        self._update_actions()

    def _value_group(self, title: str, rows: tuple[str, ...]):
        group = QGroupBox(tr(title))
        form = QFormLayout(group)
        labels: list[tuple[QLabel, QLabel, str]] = []
        for source in rows:
            key = QLabel(tr(source))
            value = QLabel("-")
            value.setTextInteractionFlags(value.textInteractionFlags())
            form.addRow(key, value)
            labels.append((key, value, source))
        return group, labels

    def _refresh_ports(self) -> None:
        selected = self._port.currentText().strip() or str(
            self._settings.get("production.ms6222_port", "") or ""
        ).strip()
        ports = sorted({item.device for item in list_ports.comports()})
        self._port.clear()
        self._port.addItems(ports)
        if selected:
            index = self._port.findText(selected)
            if index < 0:
                self._port.addItem(selected)
                index = self._port.count() - 1
            self._port.setCurrentIndex(index)

    def _toggle_connection(self) -> None:
        if self._worker is not None:
            self._disconnect()
            return
        port = self._port.currentText().strip()
        if not port:
            self._show_error(tr("Select an MS-6222 serial port."))
            return
        try:
            handle = self._lease.acquire("MS-6222 diagnostics")
            worker = self._worker_factory(port, parent=self)
        except Exception as exc:
            self._lease.release(locals().get("handle"))
            self._show_error(str(exc))
            return
        worker.frame_received.connect(self._on_frame)
        worker.invalid_frame_received.connect(self._on_frame)
        worker.statistics_changed.connect(self._on_statistics)
        worker.connection_changed.connect(self._on_connection_changed)
        worker.error_occurred.connect(self._on_error)
        worker.finished.connect(self._on_worker_finished)
        self._lease_handle = handle
        self._worker = worker
        self._settings.set("production.ms6222_port", port)
        self._settings.persist_preferences()
        self._valid_data_confirmed = False
        self._connection_state.setText(tr("Opening serial port..."))
        self._data_state.setText(tr("Valid data not confirmed"))
        self._connect_button.setEnabled(False)
        worker.start()
        self._update_actions()

    def _disconnect(self) -> None:
        worker = self._worker
        if worker is None:
            return
        if self.session_active:
            self._finish_capture(abort_reason=tr("Serial disconnected during capture"))
        worker.stop()
        if worker.wait(5000) and self._worker is worker:
            self._worker = None
            self._connected = False
            self._lease.release(self._lease_handle)
            self._lease_handle = None
            self._connection_state.setText(tr("Serial port closed"))
            worker.deleteLater()
            self._update_actions()

    def _on_connection_changed(self, connected: bool, port: str) -> None:
        self._connected = bool(connected)
        self._connection_state.setText(
            tr("Serial port open: {port}", port=port)
            if connected
            else tr("Serial port closed")
        )
        self._append_event(self._connection_state.text(), "connection")
        self._update_actions()

    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if worker is not self._worker:
            return
        self._worker = None
        self._connected = False
        self._lease.release(self._lease_handle)
        self._lease_handle = None
        self._connect_button.setEnabled(True)
        self._connection_state.setText(tr("Serial port closed"))
        if worker is not None:
            worker.deleteLater()
        self._update_actions()

    def _on_error(self, details: str) -> None:
        message = tr("MS-6222 serial error: {details}", details=details)
        self._show_error(message)
        if self.session_active:
            self._finish_capture(abort_reason=message)

    def _on_frame(self, envelope: Ms6222FrameEnvelope) -> None:
        recorder = self._recorder
        if recorder is not None:
            try:
                recorder.record_frame(envelope)
            except Ms6222DebugError as exc:
                self._finish_capture(abort_reason=str(exc))
        if not envelope.valid:
            return
        self._valid_data_confirmed = True
        self._last_frame_wall_ns = envelope.host_wall_time_ns
        self._last_valid_monotonic_ns = envelope.host_monotonic_ns
        self._data_state.setText(
            tr(
                "Valid data confirmed: {frame_type} v{version}",
                frame_type=envelope.frame_type.value,
                version=envelope.protocol_version,
            )
        )
        record = envelope.record
        if isinstance(record, Ms6222InsRecord):
            self._latest_ins = record
            self._attitude_samples.append(
                (envelope.host_monotonic_ns, record.roll_deg, record.pitch_deg, record.yaw_deg)
            )
            self._render_ins(record)
        elif isinstance(record, Ms6222GnssRecord):
            self._latest_gnss = record
            self._render_gnss(record)
        elif isinstance(record, Ms6222RawImuRecord):
            self._latest_rawimu = record
            self._render_rawimu(record)
        self._update_actions()

    def _on_statistics(self, statistics: Ms6222WorkerStatistics) -> None:
        self._latest_statistics = statistics
        parser = statistics.parser
        self._status_labels["rates"].setText(
            f"INS {statistics.ins_rate_hz:.1f} Hz | GNSS {statistics.gnss_rate_hz:.1f} Hz | IMU {statistics.rawimu_rate_hz:.1f} Hz"
        )
        self._status_labels["valid"].setText(f"{statistics.valid_ratio * 100:.2f}%")
        self._status_labels["errors"].setText(
            f"CRC {parser.crc_errors} | LEN {parser.length_errors} | NOISE {parser.noise_bytes}"
        )
        if self._last_frame_wall_ns:
            age = max(0.0, (time.time_ns() - self._last_frame_wall_ns) / 1e9)
            self._status_labels["last"].setText(f"{age:.1f} s")
        if self._recorder is not None:
            try:
                self._recorder.record_statistics(statistics)
            except Ms6222DebugError as exc:
                self._finish_capture(abort_reason=str(exc))
                return
            now_ns = time.monotonic_ns()
            evidence_ns = max(
                self._capture_started_monotonic_ns,
                self._last_valid_monotonic_ns,
            )
            if (
                evidence_ns
                and now_ns - evidence_ns
                > int(self.VALID_DATA_TIMEOUT_S * 1_000_000_000)
            ):
                self._finish_capture(
                    abort_reason=tr(
                        "No valid MS-6222 data for {seconds} seconds",
                        seconds=f"{self.VALID_DATA_TIMEOUT_S:g}",
                    )
                )

    def _render_ins(self, value: Ms6222InsRecord) -> None:
        texts = (
            f"{value.roll_deg:.4f} / {value.pitch_deg:.4f} / {value.yaw_deg:.4f} °",
            f"{value.roll_std_deg:.4f} / {value.pitch_std_deg:.4f} / {value.yaw_std_deg:.4f} °",
            f"{value.time_state} / {value.gnss_state} / {value.ekf_state}",
            f"W{value.gps_week} {value.gps_ms / 1000.0:.3f} s",
        )
        for (_key, label, _source), text in zip(self._ins_values, texts):
            label.setText(text)

    def _render_gnss(self, value: Ms6222GnssRecord) -> None:
        texts = (
            f"{value.latitude_deg:.8f} / {value.longitude_deg:.8f} / {value.altitude_m:.3f} m",
            f"{value.velocity_n_m_s:.3f} / {value.velocity_e_m_s:.3f} / {value.velocity_u_m_s:.3f} m/s",
            f"{value.used_satellites} / {value.tracked_satellites}",
            f"{value.fix_sign} / {value.baseline_length_mm} mm / {value.heading_std_deg:.4f} °",
        )
        for (_key, label, _source), text in zip(self._gnss_values, texts):
            label.setText(text)

    def _render_rawimu(self, value: Ms6222RawImuRecord) -> None:
        texts = (
            f"{value.accel_x_m_s2:.5f} / {value.accel_y_m_s2:.5f} / {value.accel_z_m_s2:.5f} m/s²",
            f"{value.gyro_x_rad_s:.6f} / {value.gyro_y_rad_s:.6f} / {value.gyro_z_rad_s:.6f} rad/s",
            f"{value.temperature_c:.2f} °C / 0x{value.imu_status:04X}",
        )
        for (_key, label, _source), text in zip(self._rawimu_values, texts):
            label.setText(text)

    def _start_capture(self) -> None:
        if not self._connected:
            self._show_error(tr("Open the MS-6222 serial port before starting capture."))
            return
        if self._recorder is not None or self._finalizer is not None:
            return
        recorder = Ms6222SessionRecorder(
            port=self._port.currentText().strip(),
            operator=self._operator.text().strip(),
            notes=self._notes.text().strip(),
            root=self._session_root,
        )
        try:
            path = recorder.start()
            recorder.record_event("capture_started", "MS-6222 independent capture started")
        except Exception as exc:
            self._show_error(str(exc))
            return
        self._recorder = recorder
        self._session_path = path
        self._capture_started_monotonic_ns = time.monotonic_ns()
        self._append_event(tr("Capture started: {path}", path=str(path)))
        self.active_changed.emit(True)
        self._update_actions()

    def _finish_capture(self, *, abort_reason: str = "") -> None:
        recorder = self._recorder
        if recorder is None or self._finalizer is not None:
            return
        self._recorder = None
        self._capture_started_monotonic_ns = 0
        conclusion = Ms6222Conclusion(str(self._conclusion.currentData()))
        finalizer = _FinalizeThread(
            recorder,
            conclusion=conclusion,
            notes=self._notes.text().strip(),
            statistics=self._latest_statistics,
            abort_reason=abort_reason,
            parent=self,
        )
        finalizer.completed.connect(self._on_finalized)
        finalizer.failed.connect(self._on_finalize_failed)
        finalizer.finished.connect(finalizer.deleteLater)
        self._finalizer = finalizer
        self._append_event(tr("Finalizing MS-6222 evidence..."))
        finalizer.start()
        self._update_actions()

    def _on_finalized(self, result) -> None:
        self._append_event(
            tr("MS-6222 evidence saved: {path}", path=str(result.session_dir))
        )
        self._finalizer = None
        self.active_changed.emit(self.connection_active)
        self._update_actions()

    def _on_finalize_failed(self, details: str) -> None:
        self._finalizer = None
        self._show_error(tr("MS-6222 evidence finalization failed: {details}", details=details))
        self.active_changed.emit(self.connection_active)
        self._update_actions()

    def _append_event(self, message: str, event_type: str = "workspace") -> None:
        self._event_log.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {message}")
        if self._recorder is not None:
            try:
                self._recorder.record_event(event_type, message)
            except Ms6222DebugError:
                pass

    def _show_error(self, message: str) -> None:
        self._append_event(message, "error")
        self.status_message.emit(message, 8000)

    def _update_actions(self) -> None:
        busy = self._finalizer is not None
        self._port.setEnabled(self._worker is None and not busy)
        self._refresh_button.setEnabled(self._worker is None and not busy)
        self._connect_button.setEnabled(not busy)
        self._connect_button.setText(
            tr("Disconnect") if self._worker is not None else tr("Connect")
        )
        self._start_button.setEnabled(self._connected and not self.session_active and not busy)
        self._finish_button.setEnabled(self.session_active and not busy)
        self._operator.setEnabled(not self.session_active and not busy)
        self.active_changed.emit(self.connection_active or self.session_active or busy)

    def _refresh_plot(self) -> None:
        if not self._attitude_samples:
            return
        end_ns = self._attitude_samples[-1][0]
        start_ns = end_ns - 300_000_000_000
        samples = [value for value in self._attitude_samples if value[0] >= start_ns]
        x = [(value[0] - end_ns) / 1e9 for value in samples]
        for index, name in enumerate(("roll", "pitch", "yaw"), start=1):
            self._curves[name].setData(x, [value[index] for value in samples])

    def activate_view(self) -> None:
        if self._view_active:
            return
        self._view_active = True
        self._scroll.verticalScrollBar().setValue(0)
        QTimer.singleShot(0, lambda: self._scroll.verticalScrollBar().setValue(0))
        self._refresh_plot()
        self._plot_timer.start()

    def deactivate_view(self) -> None:
        if not self._view_active:
            return
        self._view_active = False
        self._plot_timer.stop()

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = theme
        palette = S.palette(theme)
        self._plot.setBackground(palette["panel"])
        for axis_name in ("left", "bottom"):
            axis = self._plot.getAxis(axis_name)
            axis.setPen(pg.mkPen(palette["border_2"]))
            axis.setTextPen(pg.mkPen(palette["text_2"]))

    def retranslate_ui(self) -> None:
        self._connection_group.setTitle(tr("MS-6222 connection"))
        self._port_label.setText(tr("Serial port"))
        self._refresh_button.setText(tr("Refresh"))
        self._status_group.setTitle(tr("Stream quality"))
        for key, source in (
            ("rates", "Frame rates"),
            ("valid", "Valid ratio"),
            ("errors", "Parser errors"),
            ("last", "Last valid frame"),
        ):
            self._status_labels[f"{key}_title"].setText(tr(source))
        for group, title in (
            (self._ins_group, "INS attitude and quality"),
            (self._gnss_group, "GNSS solution"),
            (self._rawimu_group, "Raw IMU"),
        ):
            group.setTitle(tr(title))
        for labels in (self._ins_values, self._gnss_values, self._rawimu_values):
            for key, _value, source in labels:
                key.setText(tr(source))
        self._plot_group.setTitle(tr("Reference attitude — last five minutes"))
        self._plot.setLabel("left", tr("Angle"), units="deg")
        self._plot.setLabel("bottom", tr("Elapsed time"), units="s")
        self._session_group.setTitle(tr("Independent evidence session"))
        self._operator_label.setText(tr("Operator"))
        self._notes_label.setText(tr("Notes"))
        self._conclusion_label.setText(tr("Engineering conclusion"))
        labels = {
            Ms6222Conclusion.INCONCLUSIVE.value: tr("Inconclusive"),
            Ms6222Conclusion.PASS.value: tr("Pass"),
            Ms6222Conclusion.FAIL.value: tr("Fail"),
        }
        for index in range(self._conclusion.count()):
            self._conclusion.setItemText(index, labels[str(self._conclusion.itemData(index))])
        self._start_button.setText(tr("Start capture"))
        self._finish_button.setText(tr("Finish capture"))
        self._update_actions()

    def confirm_shutdown(self) -> bool:
        if not self.session_active and self._finalizer is None:
            return True
        answer = QMessageBox.warning(
            self,
            tr("Close MS-6222 diagnostics"),
            tr("An MS-6222 capture is active. Closing will save it as incomplete. Close anyway?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def shutdown(self) -> bool:
        self.deactivate_view()
        if self.session_active:
            self._finish_capture(abort_reason="workspace shutdown")
        finalizer = self._finalizer
        if finalizer is not None:
            finalizer.wait(10000)
            self._finalizer = None
        self._disconnect()
        return self._worker is None

    def closeEvent(self, event) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)


__all__ = ["Ms6222DebugWorkspace"]
