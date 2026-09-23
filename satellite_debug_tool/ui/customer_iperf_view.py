"""Customer-facing global iperf3 power-test presentation."""

from __future__ import annotations

from pathlib import Path
import socket
import time
from typing import Optional

import psutil
import pyqtgraph as pg
from PySide6.QtCore import QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.iperf_test import (
    IperfDirection,
    IperfLanePhase,
    IperfProtocol,
    IperfTestConfig,
    IperfTestController,
    IperfTestPhase,
    IperfTestStore,
    IperfValidationError,
)
from satellite_debug_tool.i18n import register_translatable, tr, tr_source
from satellite_debug_tool.ui import styles as S


_TEST_PHASE_SOURCES = {
    IperfTestPhase.IDLE: tr_source("Idle"),
    IperfTestPhase.STARTING: tr_source("Starting"),
    IperfTestPhase.RUNNING: tr_source("Running"),
    IperfTestPhase.DEGRADED: tr_source("Degraded"),
    IperfTestPhase.STOPPING: tr_source("Stopping"),
    IperfTestPhase.COMPLETED: tr_source("Completed"),
    IperfTestPhase.STOPPED: tr_source("Stopped"),
    IperfTestPhase.FAILED: tr_source("Failed"),
}
_LANE_PHASE_SOURCES = {
    IperfLanePhase.DISABLED: tr_source("Disabled"),
    IperfLanePhase.STARTING: tr_source("Starting"),
    IperfLanePhase.RUNNING: tr_source("Running"),
    IperfLanePhase.RETRYING: tr_source("Retrying"),
    IperfLanePhase.COMPLETED: tr_source("Completed"),
    IperfLanePhase.BLOCKED: tr_source("Blocked"),
    IperfLanePhase.STOPPED: tr_source("Stopped"),
}


class CustomerIperfView(QWidget):
    status_message = Signal(str, int)

    def __init__(
        self,
        controller: IperfTestController,
        store: IperfTestStore,
        power_store,
        settings,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._store = store
        self._power_store = power_store
        self._settings = settings
        self._theme = "dark"
        self._active = False
        self._build_ui()
        self._load_preferences()
        self._store.updated.connect(self._refresh)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh)
        register_translatable(self)
        self._refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 16)
        root.setSpacing(10)

        self._config_group = QGroupBox(tr("Network test configuration"))
        form = QGridLayout(self._config_group)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(7)

        self._executable = QLineEdit()
        self._browse = QPushButton(tr("Browse"))
        self._browse.clicked.connect(self._browse_executable)
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.addWidget(self._executable, 1)
        path_row.addWidget(self._browse)
        path_widget = QWidget()
        path_widget.setLayout(path_row)

        self._server = QLineEdit()
        self._local_host = QComboBox()
        self._local_host.setEditable(True)
        self._protocol = QComboBox()
        self._protocol.addItem("UDP", IperfProtocol.UDP.value)
        self._protocol.addItem("TCP", IperfProtocol.TCP.value)
        self._direction = QComboBox()
        self._direction.addItem(tr("Upload and download"), IperfDirection.BOTH.value)
        self._direction.addItem(tr("Upload only"), IperfDirection.UL.value)
        self._direction.addItem(tr("Download only"), IperfDirection.DL.value)

        self._ul_port = self._port_spin(5201)
        self._dl_port = self._port_spin(5202)
        self._ul_rate = QLineEdit("491K")
        self._dl_rate = QLineEdit("200K")
        self._continuous = QCheckBox(tr("Continuous test"))
        self._continuous.setChecked(True)
        self._duration_hours = QDoubleSpinBox()
        self._duration_hours.setRange(0.01, 10000.0)
        self._duration_hours.setDecimals(2)
        self._duration_hours.setValue(24.0)
        self._duration_hours.setSuffix(tr(" h"))
        self._continuous.toggled.connect(
            lambda checked: self._duration_hours.setEnabled(not checked)
        )
        self._duration_hours.setEnabled(False)

        labels = (
            (tr_source("iperf3 executable"), path_widget, 0, 0, 3),
            (tr_source("Server IPv4"), self._server, 1, 0, 1),
            (tr_source("Satellite local IPv4"), self._local_host, 1, 2, 1),
            (tr_source("Protocol"), self._protocol, 2, 0, 1),
            (tr_source("Test direction"), self._direction, 2, 2, 1),
            (tr_source("Upload port"), self._ul_port, 3, 0, 1),
            (tr_source("Upload bitrate"), self._ul_rate, 3, 2, 1),
            (tr_source("Download port"), self._dl_port, 4, 0, 1),
            (tr_source("Download bitrate"), self._dl_rate, 4, 2, 1),
        )
        self._form_labels: list[tuple[QLabel, str]] = []
        for source, widget, row, column, widget_span in labels:
            label = QLabel(tr(source))
            self._form_labels.append((label, source))
            form.addWidget(label, row, column)
            form.addWidget(widget, row, column + 1, 1, widget_span)
        duration_row = QHBoxLayout()
        duration_row.addWidget(self._continuous)
        duration_row.addWidget(self._duration_hours)
        duration_row.addStretch(1)
        form.addLayout(duration_row, 5, 0, 1, 4)

        action_row = QHBoxLayout()
        self._start = QPushButton(tr("Start test"))
        self._stop = QPushButton(tr("Stop test"))
        self._open_directory = QPushButton(tr("Open result folder"))
        self._start.clicked.connect(self._start_test)
        self._stop.clicked.connect(self._stop_test)
        self._open_directory.clicked.connect(self._open_result_directory)
        action_row.addWidget(self._start)
        action_row.addWidget(self._stop)
        action_row.addWidget(self._open_directory)
        action_row.addStretch(1)
        form.addLayout(action_row, 6, 0, 1, 4)
        root.addWidget(self._config_group)

        self._status_group = QGroupBox(tr("Current test"))
        status = QGridLayout(self._status_group)
        self._status_values: dict[str, QLabel] = {}
        status_defs = (
            ("phase", tr_source("Status")), ("elapsed", tr_source("Elapsed")),
            ("local", tr_source("Actual local address")), ("power", tr_source("Power")),
            ("ul", tr_source("Upload")), ("dl", tr_source("Download")),
            ("ul_quality", tr_source("Upload quality")), ("dl_quality", tr_source("Download quality")),
        )
        self._status_labels: list[tuple[QLabel, str]] = []
        for index, (key, label) in enumerate(status_defs):
            row, column = divmod(index, 2)
            cell = QWidget()
            cell_layout = QFormLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            value = QLabel("—")
            value.setTextInteractionFlags(value.textInteractionFlags())
            title = QLabel(tr(label))
            self._status_labels.append((title, label))
            cell_layout.addRow(title, value)
            status.addWidget(cell, row, column)
            self._status_values[key] = value
        root.addWidget(self._status_group)

        charts = QHBoxLayout()
        self._throughput_plot = pg.PlotWidget(title=tr("Throughput (Kbit/s)"))
        self._power_plot = pg.PlotWidget(title=tr("Power (W)"))
        self._throughput_plot.setMinimumHeight(180)
        self._power_plot.setMinimumHeight(180)
        charts.addWidget(self._throughput_plot, 2)
        charts.addWidget(self._power_plot, 1)
        root.addLayout(charts, 1)

        self._events = QTableWidget(0, 3)
        self._events.setHorizontalHeaderLabels((tr("Time"), tr("Level"), tr("Event")))
        self._events.horizontalHeader().setStretchLastSection(True)
        self._events.setMinimumHeight(120)
        root.addWidget(self._events)

    @staticmethod
    def _port_spin(value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(1, 65535)
        spin.setValue(value)
        return spin

    def _load_preferences(self) -> None:
        self._executable.setText(str(self._settings.get("iperf.executable", "")))
        if not self._executable.text().strip():
            from shutil import which
            self._executable.setText(which("iperf3") or "")
        self._server.setText(str(self._settings.get("iperf.server", "60.205.157.141")))
        self._refresh_local_addresses(str(self._settings.get("iperf.local_host", "")))
        self._set_combo_data(self._protocol, str(self._settings.get("iperf.protocol", "udp")))
        self._set_combo_data(self._direction, str(self._settings.get("iperf.direction", "both")))
        self._ul_port.setValue(int(self._settings.get("iperf.ul_port", 5201)))
        self._dl_port.setValue(int(self._settings.get("iperf.dl_port", 5202)))
        self._ul_rate.setText(str(self._settings.get("iperf.ul_rate", "491K")))
        self._dl_rate.setText(str(self._settings.get("iperf.dl_rate", "200K")))
        continuous = bool(self._settings.get("iperf.continuous", True))
        self._continuous.setChecked(continuous)
        self._duration_hours.setValue(float(self._settings.get("iperf.duration_hours", 24.0)))
        self._duration_hours.setEnabled(not continuous)

    def _refresh_local_addresses(self, selected: str = "") -> None:
        addresses: list[str] = []
        for entries in psutil.net_if_addrs().values():
            for entry in entries:
                if entry.family == socket.AF_INET and entry.address != "127.0.0.1":
                    addresses.append(entry.address)
        self._local_host.clear()
        self._local_host.addItems(sorted(set(addresses)))
        if selected:
            index = self._local_host.findText(selected)
            if index < 0:
                self._local_host.addItem(selected)
                index = self._local_host.count() - 1
            self._local_host.setCurrentIndex(index)

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _config(self) -> IperfTestConfig:
        continuous = self._continuous.isChecked()
        return IperfTestConfig(
            executable=self._executable.text().strip(),
            server=self._server.text().strip(),
            local_host=self._local_host.currentText().strip(),
            protocol=IperfProtocol(str(self._protocol.currentData())),
            direction=IperfDirection(str(self._direction.currentData())),
            ul_port=self._ul_port.value(),
            dl_port=self._dl_port.value(),
            ul_rate=self._ul_rate.text().strip(),
            dl_rate=self._dl_rate.text().strip(),
            continuous=continuous,
            duration_seconds=(
                0 if continuous else max(1, round(self._duration_hours.value() * 3600.0))
            ),
        )

    @Slot()
    def _start_test(self) -> None:
        config = self._config()
        try:
            self._controller.start(config)
        except (IperfValidationError, OSError, ValueError) as exc:
            self.status_message.emit(str(exc), 8000)
            self._status_values["phase"].setText(tr("Cannot start: {error}", error=str(exc)))
            return
        self._persist_preferences(config)
        self.status_message.emit(tr("iperf3 test started"), 4000)

    @Slot()
    def _stop_test(self) -> None:
        self._controller.stop()

    def _persist_preferences(self, config: IperfTestConfig) -> None:
        values = {
            "executable": config.executable,
            "server": config.server,
            "local_host": config.local_host,
            "protocol": config.protocol.value,
            "direction": config.direction.value,
            "ul_port": config.ul_port,
            "dl_port": config.dl_port,
            "ul_rate": config.ul_rate,
            "dl_rate": config.dl_rate,
            "continuous": config.continuous,
            "duration_hours": self._duration_hours.value(),
        }
        for key, value in values.items():
            self._settings.set(f"iperf.{key}", value)
        self._settings.persist_preferences()

    @Slot()
    def _browse_executable(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            tr("Select iperf3 executable"),
            self._executable.text().strip() or str(Path.home()),
        )
        if selected:
            self._executable.setText(selected)

    @Slot()
    def _open_result_directory(self) -> None:
        directory = self._store.snapshot.session_directory
        if directory:
            QDesktopServices.openUrl(QUrl.fromLocalFile(directory))

    def _refresh(self) -> None:
        snapshot = self._store.snapshot
        self._set_running(snapshot.active)
        self._status_values["phase"].setText(tr(_TEST_PHASE_SOURCES[snapshot.phase]))
        elapsed = 0.0
        if snapshot.started_ns:
            end_ns = snapshot.finished_ns or time.time_ns()
            elapsed = max(0.0, (end_ns - snapshot.started_ns) / 1e9)
        self._status_values["elapsed"].setText(self._format_duration(elapsed))
        local_hosts = sorted({value for value in (snapshot.ul.local_host, snapshot.dl.local_host) if value})
        self._status_values["local"].setText(", ".join(local_hosts) or "—")
        self._status_values["ul"].setText(self._lane_text(snapshot.ul))
        self._status_values["dl"].setText(self._lane_text(snapshot.dl))
        self._status_values["ul_quality"].setText(self._quality_text(snapshot.ul))
        self._status_values["dl_quality"].setText(self._quality_text(snapshot.dl))
        power = self._power_store.snapshot.sample
        self._status_values["power"].setText(
            f"{power.voltage_v:.2f} V / {power.current_a:.3f} A / {power.power_w:.2f} W"
            if power is not None else tr("Unavailable")
        )
        if self._active:
            self._refresh_plots()
            self._refresh_events()

    def _set_running(self, running: bool) -> None:
        for widget in (
            self._executable, self._browse, self._server, self._local_host,
            self._protocol, self._direction, self._ul_port, self._dl_port,
            self._ul_rate, self._dl_rate, self._continuous, self._duration_hours,
        ):
            widget.setEnabled(not running)
        if not running:
            self._duration_hours.setEnabled(not self._continuous.isChecked())
        self._start.setEnabled(not running)
        self._stop.setEnabled(running and self._store.snapshot.phase is not IperfTestPhase.STOPPING)
        self._open_directory.setEnabled(bool(self._store.snapshot.session_directory))

    @staticmethod
    def _lane_text(lane) -> str:
        return tr(
            "{phase} · {rate:.1f} Kbit/s · {size:.2f} MiB · sessions {sessions} / retries {retries}",
            phase=tr(_LANE_PHASE_SOURCES[lane.phase]),
            rate=lane.bits_per_second / 1000.0,
            size=lane.total_bytes / (1024.0 * 1024.0),
            sessions=lane.sessions,
            retries=lane.retries,
        )

    @staticmethod
    def _quality_text(lane) -> str:
        values = []
        if lane.jitter_ms is not None:
            values.append(tr("jitter {value:.3f} ms", value=lane.jitter_ms))
        if lane.lost_percent is not None:
            values.append(tr("loss {value:.2f}%", value=lane.lost_percent))
        if lane.out_of_order is not None:
            values.append(tr("out-of-order {value}", value=lane.out_of_order))
        if lane.retransmits is not None:
            values.append(tr("retransmits {value}", value=lane.retransmits))
        if lane.error:
            values.append(lane.error)
        return " · ".join(values) or "—"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _refresh_plots(self) -> None:
        self._throughput_plot.clear()
        for direction, color in (("ul", "#00a895"), ("dl", "#f0a202")):
            values = [item for item in self._store.measurements() if item.direction == direction]
            if values:
                self._throughput_plot.plot(
                    [item.elapsed_seconds for item in values],
                    [item.bits_per_second / 1000.0 for item in values],
                    pen=pg.mkPen(color, width=2),
                    name=direction.upper(),
                )
        self._power_plot.clear()
        power = self._store.power_samples()
        if power and self._store.snapshot.started_ns:
            origin = self._store.snapshot.started_ns / 1e9
            self._power_plot.plot(
                [item.host_timestamp_ns / 1e9 - origin for item in power],
                [item.power_w for item in power],
                pen=pg.mkPen("#e15d44", width=2),
            )

    def _refresh_events(self) -> None:
        events = self._store.events()[-100:]
        self._events.setRowCount(len(events))
        for row, event in enumerate(events):
            timestamp = int(event.get("timestamp_ns", 0) or 0)
            values = (
                time.strftime("%H:%M:%S", time.localtime(timestamp / 1e9)) if timestamp else "",
                str(event.get("level", "")),
                str(event.get("message", "")),
            )
            for column, value in enumerate(values):
                self._events.setItem(row, column, QTableWidgetItem(value))
        if events:
            self._events.scrollToBottom()

    def activate_view(self) -> None:
        if self._active:
            return
        self._active = True
        self._timer.start()
        self._refresh()

    def deactivate_view(self) -> None:
        self._active = False
        self._timer.stop()

    def shutdown(self) -> bool:
        self.deactivate_view()
        return True

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
        pal = S.palette(theme)
        for plot in (self._throughput_plot, self._power_plot):
            plot.setBackground(pal["card"])
            plot.getAxis("left").setPen(pal["border"])
            plot.getAxis("bottom").setPen(pal["border"])
        self.setStyleSheet(
            f"CustomerIperfView {{ background: {pal['bg']}; color: {pal['text']}; }}"
            f"QGroupBox {{ border: 1px solid {pal['border']}; border-radius: 6px; "
            f"margin-top: 8px; padding-top: 8px; color: {pal['text']}; }}"
        )

    def retranslate_ui(self) -> None:
        self._config_group.setTitle(tr("Network test configuration"))
        self._browse.setText(tr("Browse"))
        self._start.setText(tr("Start test"))
        self._stop.setText(tr("Stop test"))
        self._open_directory.setText(tr("Open result folder"))
        self._continuous.setText(tr("Continuous test"))
        self._duration_hours.setSuffix(tr(" h"))
        for label, source in self._form_labels:
            label.setText(tr(source))
        self._status_group.setTitle(tr("Current test"))
        for label, source in self._status_labels:
            label.setText(tr(source))
        current_protocol = self._protocol.currentData()
        current_direction = self._direction.currentData()
        self._protocol.setItemText(0, "UDP")
        self._protocol.setItemText(1, "TCP")
        self._direction.setItemText(0, tr("Upload and download"))
        self._direction.setItemText(1, tr("Upload only"))
        self._direction.setItemText(2, tr("Download only"))
        self._set_combo_data(self._protocol, str(current_protocol))
        self._set_combo_data(self._direction, str(current_direction))
        self._throughput_plot.setTitle(tr("Throughput (Kbit/s)"))
        self._power_plot.setTitle(tr("Power (W)"))
        self._events.setHorizontalHeaderLabels((tr("Time"), tr("Level"), tr("Event")))
        self._refresh()


__all__ = ["CustomerIperfView"]
