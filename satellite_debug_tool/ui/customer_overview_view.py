"""AFD01 customer live overview built on the shared Live device session."""

from __future__ import annotations

from typing import Optional

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.product import (
    Availability,
    ControlMode,
    LegacyV2Projector,
    NavigationState,
    ProductSnapshot,
    ProductValue,
    TrackingPhase,
)
from satellite_debug_tool.core.profile import CHANNEL_ROLE_SNR
from satellite_debug_tool.i18n import register_translatable, tr, tr_source
from satellite_debug_tool.ui import icons, styles as S
from satellite_debug_tool.ui.attitude_widget import AttitudeWidget


def _control_mode_text(value: ControlMode) -> str:
    return {
        ControlMode.AUTO: tr("Automatic"),
        ControlMode.MANUAL: tr("Manual"),
        ControlMode.UNKNOWN: tr("Unknown"),
    }.get(value, tr("Unknown"))


def _tracking_text(value: TrackingPhase) -> str:
    return {
        TrackingPhase.STANDBY: tr("Standby"),
        TrackingPhase.ACQUIRING: tr("Acquiring"),
        TrackingPhase.FINE_TRACKING: tr("Fine tracking"),
        TrackingPhase.LOCKED: tr("Locked"),
        TrackingPhase.REACQUIRING: tr("Reacquiring"),
        TrackingPhase.FAULT: tr("Fault"),
        TrackingPhase.UNKNOWN: tr("Unknown"),
    }.get(value, tr("Unknown"))


def _navigation_text(value: NavigationState) -> str:
    return {
        NavigationState.UNAVAILABLE: tr("Unavailable"),
        NavigationState.INITIALIZING: tr("Initializing"),
        NavigationState.ALIGNING: tr("Aligning"),
        NavigationState.READY: tr("Ready"),
        NavigationState.DEGRADED: tr("Degraded"),
        NavigationState.FAULT: tr("Fault"),
        NavigationState.UNKNOWN: tr("Unknown"),
    }.get(value, tr("Unknown"))


class _MetricValue(QWidget):
    def __init__(self, title: str, unit: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._title_source = title
        self._unit = unit
        row = QVBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(3)
        self.title = QLabel(tr(title))
        self.title.setObjectName("customerMetricTitle")
        self.value = QLabel("—")
        self.value.setObjectName("customerMetricValue")
        row.addWidget(self.title)
        row.addWidget(self.value)

    def set_product_value(self, value: ProductValue, decimals: int = 2) -> None:
        if value.value is None:
            text = "—"
        elif isinstance(value.value, bool):
            text = tr("On") if value.value else tr("Off")
        elif isinstance(value.value, float):
            text = f"{value.value:.{decimals}f}"
        else:
            text = str(value.value)
        if text != "—" and self._unit:
            text = f"{text} {self._unit}"
        if value.availability == Availability.STALE:
            text = tr("{value} (stale)", value=text)
        self.value.setText(text)
        self.setProperty("availability", value.availability.value)
        self.style().unpolish(self)
        self.style().polish(self)

    def retranslate_ui(self) -> None:
        self.title.setText(tr(self._title_source))


class CustomerOverviewView(QWidget):
    status_message = Signal(str, int)

    _STATUS_DEFS = (
        ("link", tr_source("Connection")),
        ("mode", tr_source("Control mode")),
        ("tracking", tr_source("Tracking")),
        ("lock", tr_source("Lock")),
        ("navigation", tr_source("Navigation")),
        ("gnss", tr_source("GNSS fix")),
    )

    _METRIC_DEFS = (
        ("beam_az", tr_source("Beam azimuth"), "°"),
        ("beam_el", tr_source("Beam elevation"), "°"),
        ("roll", tr_source("Roll"), "°"),
        ("pitch", tr_source("Pitch"), "°"),
        ("yaw", tr_source("Yaw"), "°"),
        ("snr", tr_source("SNR"), "dB"),
        ("longitude", tr_source("Longitude"), "°"),
        ("latitude", tr_source("Latitude"), "°"),
        ("altitude", tr_source("Altitude"), "m"),
    )

    def __init__(
        self,
        live_view,
        settings,
        parent: Optional[QWidget] = None,
        *,
        enable_3d: bool = True,
        playback_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self._live = live_view
        self._settings = settings
        self._theme = "dark"
        self._playback_mode = bool(playback_mode)
        self._projector = LegacyV2Projector(
            live_view.profile_store(),
            live_view.data_store(),
            live_view.state_store(),
            stale_after_s=float("inf") if playback_mode else 3.0,
        )
        self._service_store = (
            live_view.product_store() if hasattr(live_view, "product_store") else None
        )
        self._last_model = ""
        self._setup_ui(enable_3d)
        self._live.connection_state_changed.connect(self._on_connection_changed)
        self._live.profile_ready.connect(self._on_profile_ready)
        self._live.recording_state_changed.connect(self._on_recording_changed)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self._apply_theme()
        register_translatable(self)

    def _setup_ui(self, enable_3d: bool) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(8)

        connection = QFrame()
        connection.setObjectName("customerConnectionBar")
        connection_row = QHBoxLayout(connection)
        connection_row.setContentsMargins(10, 6, 10, 6)
        connection_row.setSpacing(7)
        self._identity_label = QLabel(tr("No device connected"))
        self._identity_label.setObjectName("customerIdentity")
        self._identity_label.setMinimumWidth(0)
        connection_row.addWidget(self._identity_label, 1)
        self._ip_label = QLabel(tr("Device IP"))
        connection_row.addWidget(self._ip_label)
        self._ip_edit = QLineEdit(str(self._settings.get("udp.remote_ip", "192.168.1.12")))
        self._ip_edit.setFixedWidth(132)
        connection_row.addWidget(self._ip_edit)
        self._remote_port = QSpinBox()
        self._remote_port.setRange(1, 65535)
        self._remote_port.setValue(int(self._settings.get("udp.remote_port", 4004)))
        self._remote_port.setFixedWidth(78)
        connection_row.addWidget(self._remote_port)
        self._connect_btn = QPushButton(tr("Connect"))
        self._connect_btn.clicked.connect(self._toggle_connection)
        connection_row.addWidget(self._connect_btn)
        self._gnss_btn = QPushButton("GNSS")
        self._gnss_btn.setEnabled(False)
        self._gnss_btn.clicked.connect(self._live.show_gnss_details)
        connection_row.addWidget(self._gnss_btn)
        self._record_btn = QPushButton(tr("Record"))
        record_action = getattr(self._live, "toggle_customer_recording", None)
        if record_action is None:
            record_action = self._live.toggle_recording
        self._record_btn.clicked.connect(record_action)
        connection_row.addWidget(self._record_btn)
        if self._playback_mode:
            for widget in (
                self._ip_edit,
                self._ip_label,
                self._remote_port,
                self._connect_btn,
                self._record_btn,
            ):
                widget.hide()
        root.addWidget(connection)

        status = QFrame()
        status.setObjectName("customerStatusBand")
        status_grid = QGridLayout(status)
        status_grid.setContentsMargins(8, 5, 8, 5)
        status_grid.setSpacing(6)
        self._status_values: dict[str, QLabel] = {}
        for index, (key, title) in enumerate(self._STATUS_DEFS):
            item = QLabel(f"{tr(title)}: —")
            item.setObjectName("customerStatusItem")
            item.setProperty("status", "neutral")
            item.setAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setMinimumHeight(30)
            item.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._status_values[key] = item
            status_grid.addWidget(item, index // 3, index % 3)
        for column in range(3):
            status_grid.setColumnStretch(column, 1)
        root.addWidget(status)

        main = QHBoxLayout()
        main.setSpacing(8)
        model_panel = QFrame()
        model_panel.setObjectName("customerSection")
        model_layout = QVBoxLayout(model_panel)
        model_layout.setContentsMargins(6, 6, 6, 6)
        self._attitude = AttitudeWidget() if enable_3d else None
        if self._attitude is not None:
            self._attitude.setMinimumSize(340, 260)
            self._attitude.set_auto_bindings(
                roll="customer_roll",
                pitch="customer_pitch",
                yaw="customer_yaw",
                ant_az="customer_beam_az",
                ant_el="customer_beam_el",
            )
            model_layout.addWidget(self._attitude)
        else:
            placeholder = QLabel(tr("3D model"))
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            model_layout.addWidget(placeholder)
        main.addWidget(model_panel, 3)

        metric_panel = QFrame()
        metric_panel.setObjectName("customerSection")
        metric_grid = QGridLayout(metric_panel)
        metric_grid.setContentsMargins(6, 6, 6, 6)
        metric_grid.setSpacing(5)
        self._metrics: dict[str, _MetricValue] = {}
        for index, (key, title, unit) in enumerate(self._METRIC_DEFS):
            metric = _MetricValue(title, unit)
            metric.setObjectName("customerMetric")
            metric.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            self._metrics[key] = metric
            metric_grid.addWidget(metric, index // 3, index % 3)
        for column in range(3):
            metric_grid.setColumnStretch(column, 1)
        main.addWidget(metric_panel, 2)
        root.addLayout(main, 5)

        signal_panel = QFrame()
        signal_panel.setObjectName("customerSection")
        signal_layout = QVBoxLayout(signal_panel)
        signal_layout.setContentsMargins(10, 7, 10, 7)
        signal_layout.setSpacing(3)
        self._snr_title = QLabel(tr("Signal strength - last 60 seconds"))
        self._snr_title.setObjectName("customerSectionTitle")
        signal_layout.addWidget(self._snr_title)
        self._snr_plot = pg.PlotWidget()
        self._snr_plot.setMinimumHeight(150)
        self._snr_plot.setMouseEnabled(x=False, y=False)
        self._snr_plot.hideButtons()
        self._snr_plot.showGrid(x=True, y=True, alpha=0.18)
        self._snr_plot.setLabel("left", "SNR", units="dB")
        self._snr_plot.setLabel("bottom", tr("Time"), units="s")
        self._snr_curve = self._snr_plot.plot([], [])
        signal_layout.addWidget(self._snr_plot)
        root.addWidget(signal_panel, 3)

        component_panel = QFrame()
        component_panel.setObjectName("customerSection")
        component_layout = QVBoxLayout(component_panel)
        component_layout.setContentsMargins(10, 7, 10, 7)
        self._component_title = QLabel(tr("Device components"))
        self._component_title.setObjectName("customerSectionTitle")
        component_layout.addWidget(self._component_title)
        self._component_table = QTableWidget(3, 5)
        self._component_table.setHorizontalHeaderLabels([
            tr("Component"), tr("Status"), tr("Temperature"), tr("Voltage"), tr("Version")
        ])
        self._component_table.verticalHeader().hide()
        self._component_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._component_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._component_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._component_table.horizontalHeader().setStretchLastSection(True)
        self._fit_component_table(self._component_table)
        component_layout.addWidget(self._component_table)
        root.addWidget(component_panel, 2)

    def _toggle_connection(self) -> None:
        if self._live.is_connected():
            self._live.disconnect_device()
            return
        ip = self._ip_edit.text().strip()
        if not ip:
            self.status_message.emit(tr("Enter a device IP address"), 3000)
            return
        ok = self._live.connect_udp(
            ip,
            self._remote_port.value(),
            int(self._settings.get("udp.local_port", 45678)),
            auto_debug=False,
        )
        if not ok:
            self.status_message.emit(tr("Connection failed"), 3000)

    def _on_connection_changed(self, connected: bool) -> None:
        self._connect_btn.setText(tr("Disconnect") if connected else tr("Connect"))
        self._ip_edit.setEnabled(not connected)
        self._remote_port.setEnabled(not connected)

    def _on_profile_ready(self, hw_type: str) -> None:
        if self._attitude is not None and hw_type != self._last_model:
            self._attitude.try_load_device_model(hw_type)
            self._last_model = hw_type

    def _on_recording_changed(self, recording: bool, _path: str) -> None:
        self._record_btn.setText(tr("Stop recording") if recording else tr("Record"))

    @staticmethod
    def _display(value: ProductValue) -> str:
        if value.value is None:
            return "—"
        text = str(value.value)
        return tr("{value} (stale)", value=text) if value.availability == Availability.STALE else text

    def _set_status(self, key: str, title: str, value: str, status: str = "neutral") -> None:
        label = self._status_values[key]
        label.setText(f"{tr(title)}: {value}")
        label.setProperty("status", status)
        label.style().unpolish(label)
        label.style().polish(label)

    def refresh(self) -> None:
        snapshot = self._projector.snapshot()
        if self._service_store is not None:
            snapshot = self._service_store.snapshot(snapshot)
        self._refresh_identity(snapshot)
        self._refresh_status(snapshot)
        self._refresh_metrics(snapshot)
        self._refresh_model(snapshot)
        self._refresh_snr()
        self._refresh_components(snapshot)

    def _refresh_identity(self, snapshot: ProductSnapshot) -> None:
        raw_model = snapshot.identity.model.value
        if self._attitude is not None and raw_model:
            model_key = str(raw_model).strip().lower()
            if model_key and model_key != self._last_model:
                self._attitude.try_load_device_model(model_key)
                self._last_model = model_key
        model = self._display(snapshot.identity.model)
        serial = self._display(snapshot.identity.serial_number)
        firmware = self._display(snapshot.identity.main_firmware)
        if self._live.is_connected():
            self._identity_label.setText(
                tr("{model} | SN {serial} | Firmware {firmware}", model=model, serial=serial, firmware=firmware)
            )
        else:
            self._identity_label.setText(tr("No device connected"))

    def _refresh_status(self, snapshot: ProductSnapshot) -> None:
        op = snapshot.operation
        link_value = (
            tr("Loaded")
            if self._playback_mode and self._live.is_connected()
            else tr("Online")
            if self._live.is_connected()
            else tr("Offline")
        )
        self._set_status(
            "link",
            "Connection",
            link_value,
            "ok" if self._live.is_connected() else "neutral",
        )
        mode = "—" if op.control_mode.value is None else _control_mode_text(op.control_mode.value)
        if op.control_mode.availability == Availability.STALE:
            mode = tr("{value} (stale)", value=mode)
        self._set_status("mode", "Control mode", mode)
        tracking = "—" if op.tracking_phase.value is None else _tracking_text(op.tracking_phase.value)
        if op.tracking_phase.availability == Availability.STALE:
            tracking = tr("{value} (stale)", value=tracking)
        self._set_status(
            "tracking", "Tracking", tracking,
            "ok" if op.tracking_phase.value == TrackingPhase.LOCKED else "warn",
        )
        lock = "—" if op.locked.value is None else tr("Locked") if op.locked.value else tr("Unlocked")
        self._set_status("lock", "Lock", lock, "ok" if op.locked.value else "warn")
        navigation = "—" if op.navigation.value is None else _navigation_text(op.navigation.value)
        if op.navigation.availability == Availability.STALE:
            navigation = tr("{value} (stale)", value=navigation)
        self._set_status(
            "navigation", "Navigation", navigation,
            "ok" if op.navigation.value == NavigationState.READY else "warn",
        )
        self._set_status("gnss", "GNSS fix", self._display(op.gnss_fix))
        self._gnss_btn.setEnabled(self._live.gnss_store().has_data())

    def _refresh_metrics(self, snapshot: ProductSnapshot) -> None:
        op = snapshot.operation
        values = {
            "beam_az": op.beam_az_deg,
            "beam_el": op.beam_el_deg,
            "roll": op.roll_deg,
            "pitch": op.pitch_deg,
            "yaw": op.yaw_deg,
            "snr": op.snr_db,
            "longitude": op.longitude_deg,
            "latitude": op.latitude_deg,
            "altitude": op.altitude_m,
        }
        for key, value in values.items():
            self._metrics[key].set_product_value(value, 6 if key in {"longitude", "latitude"} else 2)

    def _refresh_model(self, snapshot: ProductSnapshot) -> None:
        if self._attitude is None:
            return
        op = snapshot.operation
        roll = float(op.roll_deg.value or 0.0)
        pitch = float(op.pitch_deg.value or 0.0)
        yaw = float(op.yaw_deg.value or 0.0)
        self._attitude.update_attitude(
            roll, pitch, yaw,
            "customer_roll", "customer_pitch", "customer_yaw",
        )
        self._attitude.update_pointing(
            None, None,
            None if op.beam_az_deg.value is None else float(op.beam_az_deg.value),
            None if op.beam_el_deg.value is None else float(op.beam_el_deg.value),
        )

    def _refresh_snr(self) -> None:
        if self._service_store is not None and self._service_store.service_available:
            times, values = self._service_store.snr_history(window_s=60.0)
        else:
            times, values = self._projector.channel_history(CHANNEL_ROLE_SNR, window_s=60.0)
        self._snr_curve.setData(times, values)
        if times.size:
            self._snr_plot.setXRange(-60.0, 0.0, padding=0.0)

    def _refresh_components(self, snapshot: ProductSnapshot) -> None:
        rows = (
            (tr("Converter"), snapshot.converter),
            (tr("TX array"), snapshot.tx_array),
            (tr("RX array"), snapshot.rx_array),
        )
        for row, (name, component) in enumerate(rows):
            online = "—" if component.online.value is None else tr("Online") if component.online.value else tr("Offline")
            values = (
                name,
                online,
                self._format_component_value(component.temperature_c, "°C"),
                self._format_component_value(component.voltage_v, "V"),
                self._display(component.version),
            )
            for column, text in enumerate(values):
                self._component_table.setItem(row, column, QTableWidgetItem(text))

    @staticmethod
    def _format_component_value(value: ProductValue[float], unit: str) -> str:
        if value.value is None:
            return "—"
        text = f"{float(value.value):.1f} {unit}"
        return tr("{value} (stale)", value=text) if value.availability == Availability.STALE else text

    @staticmethod
    def _fit_component_table(table: QTableWidget) -> None:
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.horizontalHeader().setFixedHeight(28)
        for row in range(table.rowCount()):
            table.setRowHeight(row, 26)
        table.setFixedHeight(28 + table.rowCount() * 26 + 4)

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = theme
        self._apply_theme()
        if self._attitude is not None:
            self._attitude.set_theme(theme, "small")

    def _apply_theme(self) -> None:
        pal = S.palette(self._theme)
        self.setStyleSheet(
            f"CustomerOverviewView {{ background: {pal['bg']}; color: {pal['text']}; }}"
            f"#customerConnectionBar, #customerStatusBand, #customerSection {{ "
            f"background: {pal['panel']}; border: 1px solid {pal['border']}; border-radius: 6px; }}"
            f"#customerIdentity, #customerSectionTitle {{ color: {pal['text']}; font-weight: 600; }}"
            f"#customerStatusItem {{ background: {pal['card_2']}; border: 1px solid {pal['border']}; "
            f"border-radius: 5px; padding: 3px 7px; color: {pal['text_2']}; }}"
            f"#customerStatusItem[status='ok'] {{ color: {pal['ok']}; border-color: {pal['ok']}; }}"
            f"#customerStatusItem[status='warn'] {{ color: {pal['warn']}; border-color: {pal['warn']}; }}"
            f"#customerMetric {{ background: {pal['card_2']}; border: 1px solid {pal['border']}; "
            f"border-radius: 5px; }}"
            f"#customerMetricTitle {{ color: {pal['text_2']}; font-size: 11px; }}"
            f"#customerMetricValue {{ color: {pal['text']}; font-family: '{S.monospace_family()}'; "
            f"font-size: 17px; font-weight: 600; }}"
            f"#customerMetric[availability='stale'] #customerMetricValue {{ color: {pal['text_3']}; }}"
        )
        self._snr_plot.setBackground(pal["panel"])
        self._snr_curve.setPen(pg.mkPen(pal["accent_2"], width=2))
        for axis in ("left", "bottom"):
            self._snr_plot.getAxis(axis).setPen(pg.mkPen(pal["border_2"]))
            self._snr_plot.getAxis(axis).setTextPen(pg.mkPen(pal["text_2"]))

    def retranslate_ui(self) -> None:
        self._snr_title.setText(tr("Signal strength - last 60 seconds"))
        self._component_title.setText(tr("Device components"))
        self._component_table.setHorizontalHeaderLabels([
            tr("Component"), tr("Status"), tr("Temperature"), tr("Voltage"), tr("Version")
        ])
        for metric in self._metrics.values():
            metric.retranslate_ui()
        self._snr_plot.setLabel("bottom", tr("Time"), units="s")
        self._on_connection_changed(self._live.is_connected())
        self._on_recording_changed(self._live.is_recording(), "")
        self.refresh()
