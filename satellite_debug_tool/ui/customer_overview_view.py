"""AFD01 customer live overview built on the shared Live device session."""

from __future__ import annotations

import math
from typing import Optional

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.comm import DeviceConnectionPhase
from satellite_debug_tool.core.product import (
    Availability,
    ControlMode,
    CustomerRecordingState,
    LegacyV2Projector,
    NavigationState,
    ProductSnapshot,
    ProductValue,
    SatelliteMode,
    TrackingPhase,
)
from satellite_debug_tool.core.profile import CHANNEL_ROLE_SNR
from satellite_debug_tool.i18n import register_translatable, tr, tr_source
from satellite_debug_tool.ui import icons, styles as S
from satellite_debug_tool.ui.attitude_widget import AttitudeWidget
from satellite_debug_tool.ui.beam_polar_widget import BeamPolarWidget, BeamSatelliteMarker


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


def format_polarization(
    value: Optional[int],
    *,
    linear_angle_deg: Optional[float] = None,
) -> str:
    """Format the current enum or a future authoritative linear angle."""

    if linear_angle_deg is not None and math.isfinite(float(linear_angle_deg)):
        return tr("{angle}° linear", angle=f"{float(linear_angle_deg):.1f}")
    return {
        0: tr("Vertical"),
        1: tr("Horizontal"),
        2: tr("Left circular"),
        3: tr("Right circular"),
    }.get(value, tr("Unknown"))


def combined_polarization(
    rx: ProductValue[int],
    tx: ProductValue[int],
) -> ProductValue[str]:
    """Combine independent RX/TX polarization without inventing missing data."""

    entries: list[tuple[str, ProductValue[int]]] = []
    if rx.value is not None:
        entries.append(("RX", rx))
    if tx.value is not None:
        entries.append(("TX", tx))
    if not entries:
        return ProductValue.unsupported()

    if len(entries) == 2 and rx.value == tx.value:
        text = format_polarization(rx.value)
    elif len(entries) == 2:
        text = tr(
            "RX {rx} / TX {tx}",
            rx=format_polarization(rx.value),
            tx=format_polarization(tx.value),
        )
    elif entries[0][0] == "RX":
        text = tr("RX {value}", value=format_polarization(entries[0][1].value))
    else:
        text = tr("TX {value}", value=format_polarization(entries[0][1].value))

    timestamp = max(
        (
            item.device_timestamp_ms
            for _, item in entries
            if item.device_timestamp_ms is not None
        ),
        default=None,
    )
    if any(item.availability == Availability.STALE for _, item in entries):
        return ProductValue.stale(text, timestamp)
    return ProductValue.valid(text, timestamp)


def pll_lock_summary(
    clock: ProductValue[bool],
    tx: ProductValue[bool],
    rx: ProductValue[bool],
) -> tuple[ProductValue[str], str]:
    """Format explicit CLK/TX/RX lock states without inventing missing paths."""

    entries = (("clock", clock), ("tx", tx), ("rx", rx))
    if all(value.value is None for _, value in entries):
        return ProductValue.unsupported(), "neutral"

    markers = {
        key: "—" if value.value is None else "✓" if value.value else "×"
        for key, value in entries
    }
    text = tr(
        "CLK {clock} · TX {tx} · RX {rx}",
        clock=markers["clock"],
        tx=markers["tx"],
        rx=markers["rx"],
    )
    timestamp = max(
        (value.device_timestamp_ms for _, value in entries if value.device_timestamp_ms is not None),
        default=None,
    )
    if any(value.availability == Availability.STALE for _, value in entries):
        return ProductValue.stale(text, timestamp), "neutral"
    if any(value.value is False for _, value in entries):
        return ProductValue.valid(text, timestamp), "warn"
    if all(value.value is True for _, value in entries):
        return ProductValue.valid(text, timestamp), "ok"
    return ProductValue.valid(text, timestamp), "neutral"


class _MetricValue(QWidget):
    def __init__(self, title: str, unit: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._title_source = title
        self._unit = unit
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 8, 10, 8)
        self._layout.setSpacing(3)
        self.title = QLabel(tr(title))
        self.title.setObjectName("customerMetricTitle")
        self.value = QLabel("—")
        self.value.setObjectName("customerMetricValue")
        self.value.setWordWrap(True)
        self._layout.addWidget(self.title)
        self._layout.addWidget(self.value)

    def set_density(self, density: str) -> None:
        margins = {
            "regular": (10, 8, 10, 8, 3),
            "compact": (7, 5, 7, 5, 2),
            "dense": (5, 3, 5, 3, 1),
        }.get(density, (10, 8, 10, 8, 3))
        self._layout.setContentsMargins(*margins[:4])
        self._layout.setSpacing(margins[4])
        self.setProperty("density", density)
        self.style().unpolish(self)
        self.style().polish(self)

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
        ("tx", tr_source("TX")),
        ("modem", tr_source("Modem")),
    )

    _DATA_DEFS = (
        ("longitude", tr_source("Longitude"), "°"),
        ("latitude", tr_source("Latitude"), "°"),
        ("altitude", tr_source("Altitude"), "m"),
        ("rx_rf", tr_source("RX RF"), "MHz"),
        ("tx_rf", tr_source("TX RF"), "MHz"),
        ("rx_lo", tr_source("RX LO"), "MHz"),
        ("tx_lo", tr_source("TX LO"), "MHz"),
        ("satellite", tr_source("Satellite"), ""),
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
        orbit_store_getter = getattr(live_view, "orbit_store", None)
        self._orbit_store = orbit_store_getter() if orbit_store_getter is not None else None
        self._last_model = ""
        self._density = ""
        self._setup_ui(enable_3d)
        self._live.connection_state_changed.connect(self._on_connection_changed)
        phase_signal = getattr(
            self._live, "device_connection_phase_changed", None
        )
        if phase_signal is not None:
            phase_signal.connect(self._on_device_connection_phase_changed)
        self._live.profile_ready.connect(self._on_profile_ready)
        self._live.recording_state_changed.connect(self._on_recording_changed)
        recording_state_signal = getattr(
            self._live, "customer_recording_state_changed", None
        )
        if recording_state_signal is not None:
            recording_state_signal.connect(self._on_customer_recording_state_changed)
        orbit_signal = getattr(self._live, "orbit_capability_changed", None)
        if orbit_signal is not None:
            orbit_signal.connect(self._orbit_btn.setEnabled)
        if self._orbit_store is not None:
            self._orbit_store.sky_changed.connect(self._on_orbit_sky_changed)
            self._orbit_store.cleared.connect(self._on_orbit_cleared)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self._apply_theme()
        register_translatable(self)

    def _setup_ui(self, enable_3d: bool) -> None:
        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(10, 8, 10, 10)
        self._root_layout.setSpacing(8)

        connection = QFrame()
        connection.setObjectName("customerConnectionBar")
        self._connection_layout = QHBoxLayout(connection)
        self._connection_layout.setContentsMargins(10, 6, 10, 6)
        self._connection_layout.setSpacing(7)
        self._identity_label = QLabel(tr("No device connected"))
        self._identity_label.setObjectName("customerIdentity")
        self._identity_label.setMinimumWidth(0)
        self._connection_layout.addWidget(self._identity_label, 1)
        self._ip_label = QLabel(tr("Device IP"))
        self._connection_layout.addWidget(self._ip_label)
        self._ip_edit = QLineEdit(str(self._settings.get("udp.remote_ip", "192.168.1.12")))
        self._ip_edit.setFixedWidth(132)
        self._connection_layout.addWidget(self._ip_edit)
        self._remote_port = QSpinBox()
        self._remote_port.setRange(1, 65535)
        self._remote_port.setValue(int(self._settings.get("udp.remote_port", 4004)))
        self._remote_port.setFixedWidth(78)
        self._connection_layout.addWidget(self._remote_port)
        self._connect_btn = QPushButton(tr("Connect"))
        self._connect_btn.clicked.connect(self._toggle_connection)
        self._connection_layout.addWidget(self._connect_btn)
        self._gnss_btn = QPushButton("GNSS")
        self._gnss_btn.setEnabled(False)
        self._gnss_btn.clicked.connect(self._live.show_gnss_details)
        self._connection_layout.addWidget(self._gnss_btn)
        self._orbit_btn = QPushButton("Orbit/TLE")
        self._orbit_btn.setEnabled(False)
        self._orbit_btn.clicked.connect(getattr(self._live, "show_orbit_details", lambda: None))
        self._connection_layout.addWidget(self._orbit_btn)
        self._record_btn = QPushButton(tr("Record"))
        record_action = getattr(self._live, "toggle_customer_recording", None)
        if record_action is None:
            record_action = self._live.toggle_recording
        self._record_btn.clicked.connect(record_action)
        self._connection_layout.addWidget(self._record_btn)
        if self._playback_mode:
            for widget in (
                self._ip_edit,
                self._ip_label,
                self._remote_port,
                self._connect_btn,
                self._record_btn,
            ):
                widget.hide()
        self._root_layout.addWidget(connection)

        info_band = QFrame()
        info_band.setObjectName("customerInfoBand")
        info_band.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._info_layout = QHBoxLayout(info_band)
        self._info_layout.setContentsMargins(5, 4, 5, 4)
        self._info_layout.setSpacing(6)

        state_group = QFrame()
        state_group.setObjectName("customerInfoGroup")
        self._state_group_layout = QVBoxLayout(state_group)
        self._state_group_layout.setContentsMargins(4, 2, 4, 2)
        self._state_group_layout.setSpacing(3)
        self._state_group_title = QLabel(tr("Status"))
        self._state_group_title.setObjectName("customerInfoTitle")
        self._state_group_layout.addWidget(self._state_group_title)
        self._status_grid = QGridLayout()
        self._status_grid.setContentsMargins(0, 0, 0, 0)
        self._status_grid.setSpacing(4)
        self._status_values: dict[str, QLabel] = {}
        for index, (key, title) in enumerate(self._STATUS_DEFS):
            item = QLabel(f"{tr(title)}: —")
            item.setObjectName("customerStatusItem")
            item.setProperty("status", "neutral")
            item.setAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setMinimumHeight(24)
            item.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._status_values[key] = item
            self._status_grid.addWidget(item, index // 4, index % 4)
        for column in range(4):
            self._status_grid.setColumnStretch(column, 1)
        self._state_group_layout.addLayout(self._status_grid)

        data_group = QFrame()
        data_group.setObjectName("customerInfoGroup")
        self._data_group_layout = QVBoxLayout(data_group)
        self._data_group_layout.setContentsMargins(4, 2, 4, 2)
        self._data_group_layout.setSpacing(3)
        self._data_group_title = QLabel(tr("Runtime data"))
        self._data_group_title.setObjectName("customerInfoTitle")
        data_title_row = QHBoxLayout()
        data_title_row.setContentsMargins(0, 0, 0, 0)
        data_title_row.setSpacing(6)
        data_title_row.addWidget(self._data_group_title)
        data_title_row.addStretch(1)
        self._pll_lock_summary = QLabel(tr("PLL lock: —"))
        self._pll_lock_summary.setObjectName("customerPllLockSummary")
        self._pll_lock_summary.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._pll_lock_summary.setProperty("availability", Availability.UNSUPPORTED.value)
        self._pll_lock_summary.setProperty("status", "neutral")
        data_title_row.addWidget(self._pll_lock_summary)
        self._data_group_layout.addLayout(data_title_row)
        self._data_grid = QGridLayout()
        self._data_grid.setContentsMargins(0, 0, 0, 0)
        self._data_grid.setSpacing(4)
        self._data_values: dict[str, QLabel] = {}
        for index, (key, title, _unit) in enumerate(self._DATA_DEFS):
            item = QLabel(f"{tr(title)}: —")
            item.setObjectName("customerDataItem")
            item.setAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setMinimumHeight(24)
            item.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._data_values[key] = item
            self._data_grid.addWidget(item, index // 4, index % 4)
        for column in range(4):
            self._data_grid.setColumnStretch(column, 1)
        self._data_group_layout.addLayout(self._data_grid)

        self._info_layout.addWidget(state_group, 5)
        self._info_layout.addWidget(data_group, 7)
        self._root_layout.addWidget(info_band)

        self._main_grid = QGridLayout()
        self._main_grid.setSpacing(8)
        self._model_panel = QFrame()
        self._model_panel.setObjectName("customerSection")
        self._model_layout = QVBoxLayout(self._model_panel)
        self._model_layout.setContentsMargins(6, 6, 6, 6)
        self._attitude = AttitudeWidget() if enable_3d else None
        if self._attitude is not None:
            self._attitude.setMinimumSize(180, 150)
            self._attitude.set_readout_emphasis(True)
            self._attitude.set_auto_bindings(
                roll="customer_roll",
                pitch="customer_pitch",
                yaw="customer_yaw",
                ant_az="customer_beam_az",
                ant_el="customer_beam_el",
            )
            self._model_layout.addWidget(self._attitude)
        else:
            placeholder = QLabel(tr("3D model"))
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._model_layout.addWidget(placeholder)

        self._beam_panel = QFrame()
        self._beam_panel.setObjectName("customerSection")
        self._beam_layout = QVBoxLayout(self._beam_panel)
        self._beam_layout.setContentsMargins(8, 6, 8, 6)
        self._beam_layout.setSpacing(4)
        self._beam_title = QLabel(tr("Beam direction"))
        self._beam_title.setObjectName("customerSectionTitle")
        self._beam_layout.addWidget(self._beam_title)
        self._beam_legend = QLabel()
        self._beam_legend.setObjectName("customerBeamLegend")
        self._beam_legend.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._beam_legend.setTextFormat(Qt.TextFormat.RichText)
        self._beam_legend.setVisible(False)
        self._update_beam_legend(profile_characterized=True)
        self._beam_layout.addWidget(self._beam_legend)
        self._beam_polar = BeamPolarWidget()
        self._beam_polar.satellite_clicked.connect(self._on_satellite_clicked)
        self._beam_layout.addWidget(self._beam_polar, 1)
        beam_footer = QWidget()
        footer_layout = QHBoxLayout(beam_footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(4)
        self._beam_values: dict[str, _MetricValue] = {}
        for key, title, unit in (
            ("beam_el", tr_source("Elevation / off-axis"), "°"),
            ("beam_az", tr_source("Azimuth"), "°"),
            ("polarization", tr_source("Polarization"), ""),
        ):
            metric = _MetricValue(title, unit)
            metric.setObjectName("customerBeamValue")
            metric.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )
            self._beam_values[key] = metric
            footer_layout.addWidget(metric, 1)
        self._beam_layout.addWidget(beam_footer)

        self._main_grid.addWidget(self._model_panel, 0, 0)
        self._main_grid.addWidget(self._beam_panel, 0, 1)
        self._main_grid.setColumnStretch(0, 1)
        self._main_grid.setColumnStretch(1, 1)
        self._main_grid.setRowStretch(0, 1)
        self._root_layout.addLayout(self._main_grid, 1)

        self._signal_panel = QFrame()
        self._signal_panel.setObjectName("customerSection")
        self._signal_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._signal_panel.setMinimumHeight(188)
        self._signal_panel.setMaximumHeight(220)
        self._signal_layout = QVBoxLayout(self._signal_panel)
        self._signal_layout.setContentsMargins(10, 7, 10, 7)
        self._signal_layout.setSpacing(3)
        self._snr_title = QLabel(tr("Signal strength - last 5 minutes"))
        self._snr_title.setObjectName("customerSectionTitle")
        self._signal_layout.addWidget(self._snr_title)
        signal_body = QWidget()
        self._signal_body_layout = QHBoxLayout(signal_body)
        self._signal_body_layout.setContentsMargins(0, 0, 0, 0)
        self._signal_body_layout.setSpacing(10)
        self._snr_readout = _MetricValue(tr_source("SNR"), "dB")
        self._snr_readout.setObjectName("customerSnrReadout")
        self._snr_readout.setMinimumWidth(150)
        self._snr_readout.setMaximumWidth(220)
        self._snr_readout.setFixedHeight(150)
        self._signal_body_layout.addWidget(
            self._snr_readout, 0, Qt.AlignmentFlag.AlignTop
        )
        self._snr_plot = pg.PlotWidget()
        self._snr_plot.setMinimumHeight(150)
        self._snr_plot.setMouseEnabled(x=False, y=False)
        self._snr_plot.hideButtons()
        self._snr_plot.showGrid(x=True, y=True, alpha=0.18)
        self._snr_plot.setLabel("left", "SNR", units="dB")
        self._snr_plot.setLabel("bottom", tr("Device uptime"), units="s")
        self._snr_plot.getAxis("bottom").enableAutoSIPrefix(False)
        self._snr_curve = self._snr_plot.plot([], [])
        self._signal_body_layout.addWidget(self._snr_plot, 1)
        self._signal_layout.addWidget(signal_body)

        self._component_panel = QFrame()
        self._component_panel.setObjectName("customerSection")
        self._component_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._component_layout = QHBoxLayout(self._component_panel)
        self._component_layout.setContentsMargins(10, 5, 10, 5)
        self._component_layout.setSpacing(0)
        self._component_widgets: dict[str, QWidget] = {}
        self._component_names: dict[str, QLabel] = {}
        self._component_details: dict[str, QLabel] = {}
        for key, title in (
            ("converter", tr_source("Converter")),
            ("tx_array", tr_source("TX array")),
            ("rx_array", tr_source("RX array")),
        ):
            item = QWidget()
            item.setObjectName("customerComponentItem")
            item_layout = QVBoxLayout(item)
            item_layout.setContentsMargins(10, 1, 10, 1)
            item_layout.setSpacing(1)
            name = QLabel(tr(title))
            name.setObjectName("customerComponentName")
            detail = QLabel("— · — · — · —")
            detail.setObjectName("customerComponentDetail")
            detail.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            detail.setMinimumWidth(0)
            item_layout.addWidget(name)
            item_layout.addWidget(detail)
            self._component_widgets[key] = item
            self._component_names[key] = name
            self._component_details[key] = detail
            self._component_layout.addWidget(item, 1)

        self._root_layout.addWidget(self._signal_panel)
        self._root_layout.addWidget(self._component_panel)
        self._apply_density("dense")

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
        phase = self._connection_phase()
        if not connected:
            label = tr("Connect")
        elif phase == DeviceConnectionPhase.WAITING:
            label = tr("Cancel")
        else:
            label = tr("Disconnect")
        self._connect_btn.setText(label)
        self._ip_edit.setEnabled(not connected)
        self._remote_port.setEnabled(not connected)
        self.refresh()

    def _on_device_connection_phase_changed(self, _phase: str) -> None:
        self._on_connection_changed(self._live.is_connected())

    def _connection_phase(self) -> DeviceConnectionPhase:
        getter = getattr(self._live, "connection_phase", None)
        if getter is None:
            return (
                DeviceConnectionPhase.ONLINE
                if self._live.is_connected()
                else DeviceConnectionPhase.DISCONNECTED
            )
        raw = getter()
        if isinstance(raw, DeviceConnectionPhase):
            return raw
        try:
            return DeviceConnectionPhase(str(raw))
        except ValueError:
            return DeviceConnectionPhase.DISCONNECTED

    def _device_online(self) -> bool:
        checker = getattr(self._live, "is_device_online", None)
        return bool(checker()) if checker is not None else bool(self._live.is_connected())

    def _on_profile_ready(self, hw_type: str) -> None:
        if self._attitude is not None and hw_type != self._last_model:
            self._attitude.try_load_device_model(hw_type)
            self._last_model = hw_type

    def _on_recording_changed(self, recording: bool, _path: str) -> None:
        self._render_recording_control(recording=recording)

    def _on_customer_recording_state_changed(self, _state: str, _path: str) -> None:
        self._render_recording_control()

    def _recording_state(self) -> CustomerRecordingState:
        getter = getattr(self._live, "customer_recording_state", None)
        if getter is None:
            return (
                CustomerRecordingState.ACTIVE
                if self._live.is_recording()
                else CustomerRecordingState.IDLE
            )
        raw = getter()
        if isinstance(raw, CustomerRecordingState):
            return raw
        try:
            return CustomerRecordingState(str(raw))
        except ValueError:
            return CustomerRecordingState.IDLE

    def _render_recording_control(self, *, recording: Optional[bool] = None) -> None:
        state = self._recording_state()
        if state == CustomerRecordingState.ACTIVE or recording is True:
            text = tr("Stop recording")
            enabled = True
        elif state in {
            CustomerRecordingState.ARMED,
            CustomerRecordingState.PREPARING,
        }:
            text = tr("Cancel recording")
            enabled = True
        elif state == CustomerRecordingState.RESTORING:
            text = tr("Restoring...")
            enabled = False
        else:
            text = tr("Record")
            enabled = True
        self._record_btn.setText(text)
        self._record_btn.setEnabled(enabled)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_main_grid"):
            self._apply_density(self._density_for_height(event.size().height()))

    def sizeHint(self):
        # QScrollArea otherwise prefers the tall beam plot's natural hint and
        # creates a page scrollbar even though every panel can fit its minimum.
        return self.minimumSizeHint()

    @staticmethod
    def _density_for_height(height: int) -> str:
        if height >= 860:
            return "regular"
        if height >= 680:
            return "compact"
        return "dense"

    def _apply_density(self, density: str) -> None:
        if self._density == density:
            return
        self._density = density
        config = {
            "regular": {
                "root": (10, 8, 10, 10, 8),
                "connection": (10, 6, 10, 6, 7),
                "info": (5, 4, 5, 4, 6, 4, 28),
                "panel": (6, 6, 6, 6, 5),
                "beam": (8, 6, 8, 6, 4),
                "lower": (240, 188, 180, 60),
                "attitude": (240, 220),
                "polar": (210, 180),
                "main_min": 300,
            },
            "compact": {
                "root": (7, 5, 7, 7, 5),
                "connection": (8, 4, 8, 4, 5),
                "info": (4, 3, 4, 3, 4, 3, 25),
                "panel": (4, 4, 4, 4, 3),
                "beam": (5, 4, 5, 4, 3),
                "lower": (175, 132, 126, 52),
                "attitude": (180, 155),
                "polar": (170, 135),
                "main_min": 235,
            },
            "dense": {
                "root": (5, 4, 5, 5, 3),
                "connection": (6, 3, 6, 3, 4),
                "info": (3, 2, 3, 2, 3, 2, 22),
                "panel": (3, 3, 3, 3, 2),
                "beam": (4, 3, 4, 3, 2),
                "lower": (122, 90, 86, 44),
                "attitude": (150, 120),
                "polar": (135, 105),
                "main_min": 175,
            },
        }[density]

        root = config["root"]
        self._root_layout.setContentsMargins(*root[:4])
        self._root_layout.setSpacing(root[4])
        connection = config["connection"]
        self._connection_layout.setContentsMargins(*connection[:4])
        self._connection_layout.setSpacing(connection[4])

        info = config["info"]
        self._info_layout.setContentsMargins(*info[:4])
        self._info_layout.setSpacing(info[4])
        for group_layout in (self._state_group_layout, self._data_group_layout):
            group_layout.setContentsMargins(info[5], 1, info[5], 1)
            group_layout.setSpacing(info[5])
        self._status_grid.setSpacing(info[5])
        self._data_grid.setSpacing(info[5])
        self._reflow_status(4)
        for item in self._status_values.values():
            item.setFixedHeight(info[6])
            item.setProperty("density", density)
            item.style().unpolish(item)
            item.style().polish(item)
        for item in self._data_values.values():
            item.setFixedHeight(info[6])
            item.setProperty("density", density)
            item.style().unpolish(item)
            item.style().polish(item)
        self._pll_lock_summary.setProperty("density", density)
        self._pll_lock_summary.style().unpolish(self._pll_lock_summary)
        self._pll_lock_summary.style().polish(self._pll_lock_summary)

        panel = config["panel"]
        self._model_layout.setContentsMargins(*panel[:4])
        beam = config["beam"]
        self._beam_layout.setContentsMargins(*beam[:4])
        self._beam_layout.setSpacing(beam[4])

        main_min = config["main_min"]
        for item in (self._model_panel, self._beam_panel):
            item.setMinimumHeight(main_min)
        if self._attitude is not None:
            self._attitude.setMinimumSize(*config["attitude"])
        self._beam_polar.setMinimumSize(*config["polar"])

        for metric in self._beam_values.values():
            metric.set_density(density)
        self._snr_readout.set_density(density)

        signal_height, readout_height, plot_height, component_height = config["lower"]
        self._signal_panel.setFixedHeight(signal_height)
        self._component_panel.setFixedHeight(component_height)
        side_margin = 10 if density == "regular" else 6 if density == "compact" else 4
        vertical_margin = 7 if density == "regular" else 4 if density == "compact" else 3
        self._signal_layout.setContentsMargins(
            side_margin, vertical_margin, side_margin, vertical_margin
        )
        self._component_layout.setContentsMargins(
            side_margin, vertical_margin, side_margin, vertical_margin
        )
        for item in self._component_widgets.values():
            item.layout().setContentsMargins(
                side_margin, 1, side_margin, 1
            )
            item.setProperty("density", density)
            item.style().unpolish(item)
            item.style().polish(item)
        self._signal_body_layout.setSpacing(
            10 if density == "regular" else 6 if density == "compact" else 4
        )
        self._snr_readout.setFixedHeight(readout_height)
        self._snr_readout.setMinimumWidth(
            150 if density == "regular" else 122 if density == "compact" else 94
        )
        self._snr_readout.setMaximumWidth(
            220 if density == "regular" else 170 if density == "compact" else 128
        )
        self._snr_plot.setMinimumHeight(plot_height)

    def _reflow_status(self, columns: int) -> None:
        items = list(self._status_values.values())
        for item in items:
            self._status_grid.removeWidget(item)
        for column in range(4):
            self._status_grid.setColumnStretch(column, 1 if column < columns else 0)
        for index, item in enumerate(items):
            self._status_grid.addWidget(item, index // columns, index % columns)

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
        self._refresh_data_and_beam(snapshot)
        self._refresh_model(snapshot)
        self._refresh_snr(snapshot)
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
        if self._device_online():
            self._identity_label.setText(
                tr("{model} | SN {serial} | Firmware {firmware}", model=model, serial=serial, firmware=firmware)
            )
        elif self._connection_phase() == DeviceConnectionPhase.WAITING:
            self._identity_label.setText(tr("Waiting for device..."))
        elif self._connection_phase() == DeviceConnectionPhase.RECONNECTING:
            self._identity_label.setText(tr("Reconnecting to device..."))
        else:
            self._identity_label.setText(tr("No device connected"))

    def _refresh_status(self, snapshot: ProductSnapshot) -> None:
        op = snapshot.operation
        phase = self._connection_phase()
        if self._playback_mode and self._live.is_connected():
            link_value = tr("Loaded")
        elif phase == DeviceConnectionPhase.ONLINE:
            link_value = tr("Online")
        elif phase == DeviceConnectionPhase.WAITING:
            link_value = tr("Waiting")
        elif phase == DeviceConnectionPhase.RECONNECTING:
            link_value = tr("Reconnecting")
        else:
            link_value = tr("Offline")
        self._set_status(
            "link",
            "Connection",
            link_value,
            "ok" if phase == DeviceConnectionPhase.ONLINE else "neutral",
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
        # Old firmware can briefly report PA enabled while its component record says TX array Offline.
        # Never present that contradictory state as a customer-facing successful transmission.
        if snapshot.tx_array.online.value is False:
            tx = tr("Unavailable")
            tx_status = "warn"
        else:
            tx = "—" if op.tx_enabled.value is None else tr("On") if op.tx_enabled.value else tr("Off")
            if op.tx_enabled.availability == Availability.STALE:
                tx = tr("{value} (stale)", value=tx)
            tx_status = "ok" if op.tx_enabled.value else "neutral"
        self._set_status("tx", "TX", tx, tx_status)
        modem = (
            "—"
            if op.modem_online.value is None
            else tr("Online") if op.modem_online.value else tr("Offline")
        )
        if op.modem_online.availability == Availability.STALE:
            modem = tr("{value} (stale)", value=modem)
        self._set_status(
            "modem",
            "Modem",
            modem,
            "ok" if op.modem_online.value else "warn" if op.modem_online.value is False else "neutral",
        )
        self._gnss_btn.setEnabled(self._live.gnss_store().has_data())
        orbit_store_getter = getattr(self._live, "orbit_store", None)
        self._orbit_btn.setEnabled(
            bool(orbit_store_getter is not None and orbit_store_getter().available)
        )

    def _refresh_data_and_beam(self, snapshot: ProductSnapshot) -> None:
        op = snapshot.operation
        polarization = combined_polarization(op.rx_polarization, op.tx_polarization)
        data_values = {
            "longitude": (op.longitude_deg, 6),
            "latitude": (op.latitude_deg, 6),
            "altitude": (op.altitude_m, 2),
            "rx_rf": (op.rx_frequency_mhz, 2),
            "tx_rf": (op.tx_frequency_mhz, 2),
            "rx_lo": (op.rx_lo_mhz, 2),
            "tx_lo": (op.tx_lo_mhz, 2),
            "satellite": (self._satellite_summary(op), 2),
        }
        for key, (value, decimals) in data_values.items():
            self._set_data_value(key, value, decimals)
        pll_summary, pll_status = pll_lock_summary(
            op.clock_pll_locked,
            op.tx_pll_locked,
            op.rx_pll_locked,
        )
        self._set_pll_lock_summary(pll_summary, pll_status)

        beam_values = {
            "beam_el": op.beam_el_deg,
            "beam_az": op.beam_az_deg,
            "polarization": polarization,
        }
        for key, value in beam_values.items():
            self._beam_values[key].set_product_value(value, 2)

        beam_stale = (
            op.beam_az_deg.availability == Availability.STALE
            or op.beam_el_deg.availability == Availability.STALE
        )
        beam_az = None if op.beam_az_deg.value is None else float(op.beam_az_deg.value)
        beam_second = None if op.beam_el_deg.value is None else float(op.beam_el_deg.value)
        sky = None if self._orbit_store is None else self._orbit_store.sky_snapshot()
        if sky is not None and beam_az is not None and beam_second is not None:
            beam_az = (
                sky.azimuth_direction * (beam_az - sky.azimuth_zero_offset_deg)
            ) % 360.0
            beam_second = beam_second if sky.second_angle_type == 0 else 90.0 - beam_second
        self._beam_polar.set_beam(beam_az, beam_second, stale=beam_stale)
        self._refresh_satellite_layer()

    def _refresh_satellite_layer(self) -> None:
        if self._orbit_store is None:
            return
        snapshot = self._orbit_store.sky_snapshot()
        if snapshot is None:
            self._beam_legend.setVisible(False)
            self._beam_polar.set_satellites((), max_off_axis_deg=90.0)
            return
        self._update_beam_legend(profile_characterized=snapshot.profile_characterized)
        self._beam_legend.setVisible(True)
        catalog = {
            entry.norad_id: entry.name
            for entry in self._orbit_store.catalog_snapshot().entries
        }
        available = tuple(
            sample
            for sample in snapshot.samples
            if sample.geographic_visible
            and sample.front_hemisphere
            and sample.in_hard_envelope
        )
        candidates = tuple(
            sample
            for sample in available
            if not sample.stale and not sample.active_target
        )
        candidate_id = (
            min(candidates, key=lambda item: (item.array_offaxis_deg, item.norad_id)).norad_id
            if candidates
            else 0
        )
        markers = []
        for sample in available:
            name = catalog.get(sample.norad_id, "")
            label = name[:12] if name else str(sample.norad_id)
            trail = tuple(
                (point.array_azimuth_deg, point.array_offaxis_deg)
                for point in self._orbit_store.sky_trail(sample.norad_id)
            )
            tooltip = tr(
                "{name} (NORAD {norad})\n"
                "Array az/off-axis: {array_az}° / {offaxis}°\n"
                "Geographic az/el: {geo_az}° / {geo_el}°\n"
                "Range: {range_km} km · TLE age: {age_days} d",
                name=name or "NORAD",
                norad=sample.norad_id,
                array_az=f"{sample.array_azimuth_deg:.2f}",
                offaxis=f"{sample.array_offaxis_deg:.2f}",
                geo_az=f"{sample.azimuth_deg:.2f}",
                geo_el=f"{sample.elevation_deg:.2f}",
                range_km=f"{sample.slant_range_m / 1000.0:.1f}",
                age_days=f"{sample.tle_age_days:.1f}",
            )
            markers.append(
                BeamSatelliteMarker(
                    sample.norad_id,
                    label,
                    sample.array_azimuth_deg,
                    sample.array_offaxis_deg,
                    stale=sample.stale,
                    active_target=sample.active_target,
                    candidate=sample.norad_id == candidate_id,
                    trail=trail,
                    tooltip=tooltip,
                )
            )
        self._beam_polar.set_satellites(
            tuple(markers),
            max_off_axis_deg=snapshot.hard_offaxis_limit_deg,
        )

    @Slot(object)
    def _on_orbit_sky_changed(self, _snapshot: object) -> None:
        self._refresh_satellite_layer()

    @Slot()
    def _on_orbit_cleared(self) -> None:
        self._beam_legend.setVisible(False)
        self._beam_polar.set_satellites((), max_off_axis_deg=90.0)

    def _update_beam_legend(self, *, profile_characterized: bool) -> None:
        entries = [
            f'<span style="color:#22C55E">●</span> {tr("Current target")}',
            f'<span style="color:#F59E0B">●</span> {tr("Geometric candidate")}',
            f'<span style="color:#38BDF8">●</span> {tr("Other satellite")}',
            f'<span style="color:#64748B">●</span> {tr("Stale")}',
        ]
        if not profile_characterized:
            entries.append(f'<span style="color:#F59E0B">⚠</span> {tr("Profile fallback")}')
        self._beam_legend.setText(" · ".join(entries))

    @Slot(int)
    def _on_satellite_clicked(self, norad_id: int) -> None:
        if self._orbit_store is None:
            return
        name = next(
            (
                entry.name
                for entry in self._orbit_store.catalog_snapshot().entries
                if entry.norad_id == norad_id
            ),
            str(norad_id),
        )
        answer = QMessageBox.question(
            self,
            tr("Tracking target"),
            tr(
                "Set {name} (NORAD {norad}) as the tracking target? This does not enable TX.",
                name=name,
                norad=norad_id,
            ),
        )
        action = getattr(self._live, "select_orbit_tracking_target", None)
        if answer == QMessageBox.Yes and (action is None or not action(norad_id)):
            self.status_message.emit(tr("Failed to send tracking target request"), 3500)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        consumer = getattr(self._live, "set_orbit_sky_consumer", None)
        if consumer is not None and not self._playback_mode:
            consumer("customer_overview", True)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        consumer = getattr(self._live, "set_orbit_sky_consumer", None)
        if consumer is not None:
            consumer("customer_overview", False)
        super().hideEvent(event)

    def _set_data_value(self, key: str, value: ProductValue, decimals: int) -> None:
        title, unit = next(
            (title, unit) for item_key, title, unit in self._DATA_DEFS if item_key == key
        )
        if value.value is None:
            rendered = "—"
        elif isinstance(value.value, float):
            rendered = f"{value.value:.{decimals}f}"
        else:
            rendered = str(value.value)
        if rendered != "—" and unit:
            rendered = f"{rendered} {unit}"
        if value.availability == Availability.STALE:
            rendered = tr("{value} (stale)", value=rendered)
        label = self._data_values[key]
        label.setText(f"{tr(title)}: {rendered}")
        label.setToolTip(label.text())
        label.setProperty("availability", value.availability.value)
        label.style().unpolish(label)
        label.style().polish(label)

    def _set_pll_lock_summary(self, value: ProductValue[str], status: str) -> None:
        rendered = "—" if value.value is None else str(value.value)
        if value.availability == Availability.STALE:
            rendered = tr("{value} (stale)", value=rendered)
        self._pll_lock_summary.setText(tr("PLL lock: {value}", value=rendered))
        self._pll_lock_summary.setToolTip(self._pll_lock_summary.text())
        self._pll_lock_summary.setProperty("availability", value.availability.value)
        self._pll_lock_summary.setProperty("status", status)
        self._pll_lock_summary.style().unpolish(self._pll_lock_summary)
        self._pll_lock_summary.style().polish(self._pll_lock_summary)

    @staticmethod
    def _satellite_summary(op) -> ProductValue[str]:
        mode = op.satellite_mode
        selected: list[ProductValue] = [mode]
        text = ""
        if mode.value == SatelliteMode.GEO:
            longitude = op.satellite_longitude_deg
            selected.append(longitude)
            if longitude.value is None:
                text = "GEO"
            else:
                direction = "E" if float(longitude.value) >= 0.0 else "W"
                text = f"GEO {abs(float(longitude.value)):.2f}°{direction}"
        elif mode.value == SatelliteMode.LEO_TLE:
            name = op.satellite_name
            sat_id = op.satellite_id
            if name.value:
                selected.append(name)
                text = str(name.value)
            elif sat_id.value is not None:
                selected.append(sat_id)
                text = tr("NORAD {id}", id=sat_id.value)
            else:
                text = tr("LEO/TLE")
        else:
            for candidate in (op.satellite_name, op.satellite_id):
                if candidate.value is not None:
                    selected.append(candidate)
                    text = str(candidate.value)
                    break
        if not text:
            return ProductValue.unsupported()
        timestamp = max(
            (item.device_timestamp_ms for item in selected if item.device_timestamp_ms is not None),
            default=None,
        )
        if any(item.availability == Availability.STALE for item in selected):
            return ProductValue.stale(text, timestamp)
        return ProductValue.valid(text, timestamp)

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

    def _refresh_snr(self, snapshot: ProductSnapshot) -> None:
        self._snr_readout.set_product_value(snapshot.operation.snr_db, 2)
        if self._service_store is not None and self._service_store.service_available:
            times, values = self._service_store.snr_history(window_s=300.0)
        else:
            times, values = self._projector.channel_history(CHANNEL_ROLE_SNR, window_s=300.0)
        self._snr_curve.setData(times, values)
        finite_values = [float(value) for value in values if math.isfinite(float(value))]
        if finite_values:
            minimum = min(finite_values)
            maximum = max(finite_values)
            spread = maximum - minimum
            if spread < 2.0:
                center = (minimum + maximum) / 2.0
                minimum = center - 2.0
                maximum = center + 2.0
            else:
                padding = max(0.5, spread * 0.1)
                minimum -= padding
                maximum += padding
            self._snr_plot.setYRange(minimum, maximum, padding=0.0)
        if times.size:
            data_end = float(times[-1])
            end = max(300.0, data_end)
            start = max(0.0, end - 300.0)
            self._snr_plot.setXRange(start, end, padding=0.0)

    def _refresh_components(self, snapshot: ProductSnapshot) -> None:
        rows = (
            ("converter", snapshot.converter),
            ("tx_array", snapshot.tx_array),
            ("rx_array", snapshot.rx_array),
        )
        for key, component in rows:
            online = (
                "—"
                if component.online.value is None
                else tr("Online") if component.online.value else tr("Offline")
            )
            values = (
                online,
                self._format_component_value(component.temperature_c, "°C"),
                self._format_component_value(component.voltage_v, "V"),
                self._display(component.version),
            )
            self._component_details[key].setText(" · ".join(values))
            self._component_details[key].setToolTip(" · ".join(values))

    @staticmethod
    def _format_component_value(value: ProductValue[float], unit: str) -> str:
        if value.value is None:
            return "—"
        text = f"{float(value.value):.1f} {unit}"
        return tr("{value} (stale)", value=text) if value.availability == Availability.STALE else text

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = theme
        self._apply_theme()
        if self._attitude is not None:
            self._attitude.set_theme(theme, "small")
        self._beam_polar.set_theme(theme)

    def _apply_theme(self) -> None:
        pal = S.palette(self._theme)
        self.setStyleSheet(
            f"CustomerOverviewView {{ background: {pal['bg']}; color: {pal['text']}; }}"
            f"#customerConnectionBar, #customerInfoBand, #customerSection {{ "
            f"background: {pal['panel']}; border: 1px solid {pal['border']}; border-radius: 6px; }}"
            f"#customerIdentity, #customerSectionTitle, #customerInfoTitle {{ color: {pal['text']}; font-weight: 600; }}"
            f"#customerInfoGroup {{ background: {pal['panel']}; border: 0; }}"
            f"#customerStatusItem {{ background: {pal['card_2']}; border: 1px solid {pal['border']}; "
            f"border-radius: 5px; padding: 3px 7px; color: {pal['text_2']}; }}"
            f"#customerStatusItem[density='dense'] {{ padding: 1px 3px; font-size: 10px; }}"
            f"#customerStatusItem[status='ok'] {{ color: {pal['ok']}; border-color: {pal['ok']}; }}"
            f"#customerStatusItem[status='warn'] {{ color: {pal['warn']}; border-color: {pal['warn']}; }}"
            f"#customerDataItem {{ background: {pal['card_2']}; border: 1px solid {pal['border']}; "
            f"border-radius: 4px; padding: 2px 5px; color: {pal['text_2']}; }}"
            f"#customerDataItem[density='dense'] {{ padding: 1px 3px; font-size: 10px; }}"
            f"#customerDataItem[availability='stale'] {{ color: {pal['text_3']}; }}"
            f"#customerPllLockSummary {{ color: {pal['text_2']}; font-size: 11px; font-weight: 600; }}"
            f"#customerPllLockSummary[density='dense'] {{ font-size: 10px; }}"
            f"#customerPllLockSummary[status='ok'] {{ color: {pal['ok']}; }}"
            f"#customerPllLockSummary[status='warn'] {{ color: {pal['warn']}; }}"
            f"#customerPllLockSummary[availability='stale'] {{ color: {pal['text_3']}; }}"
            f"#customerMetric {{ background: {pal['card_2']}; border: 1px solid {pal['border']}; "
            f"border-radius: 5px; }}"
            f"#customerMetricTitle {{ color: {pal['text_2']}; font-size: 11px; }}"
            f"#customerMetricValue {{ color: {pal['text']}; font-family: '{S.monospace_family()}'; "
            f"font-size: 17px; font-weight: 600; }}"
            f"#customerMetric[density='compact'] #customerMetricTitle {{ font-size: 10px; }}"
            f"#customerMetric[density='compact'] #customerMetricValue {{ font-size: 15px; }}"
            f"#customerMetric[density='dense'] #customerMetricTitle {{ font-size: 9px; }}"
            f"#customerMetric[density='dense'] #customerMetricValue {{ font-size: 14px; }}"
            f"#customerMetric[availability='stale'] #customerMetricValue {{ color: {pal['text_3']}; }}"
            f"#customerBeamValue {{ border-right: 1px solid {pal['border']}; }}"
            f"#customerBeamValue #customerMetricTitle {{ font-size: 10px; }}"
            f"#customerBeamValue #customerMetricValue {{ font-size: 14px; }}"
            f"#customerBeamValue[density='dense'] #customerMetricTitle {{ font-size: 9px; }}"
            f"#customerBeamValue[density='dense'] #customerMetricValue {{ font-size: 12px; }}"
            f"#customerBeamValue[availability='stale'] #customerMetricValue {{ color: {pal['text_3']}; }}"
            f"#customerBeamLegend {{ color: {pal['text_2']}; font-size: 10px; }}"
            f"#customerSnrReadout {{ border-right: 1px solid {pal['border_2']}; }}"
            f"#customerSnrReadout #customerMetricTitle {{ font-size: 13px; }}"
            f"#customerSnrReadout #customerMetricValue {{ color: {pal['accent_2']}; "
            f"font-size: 32px; font-weight: 700; }}"
            f"#customerSnrReadout[density='compact'] #customerMetricValue {{ font-size: 27px; }}"
            f"#customerSnrReadout[density='dense'] #customerMetricTitle {{ font-size: 10px; }}"
            f"#customerSnrReadout[density='dense'] #customerMetricValue {{ font-size: 23px; }}"
            f"#customerSnrReadout[availability='stale'] #customerMetricValue {{ color: {pal['text_3']}; }}"
            f"#customerComponentItem {{ border-right: 1px solid {pal['border']}; }}"
            f"#customerComponentName {{ color: {pal['text_2']}; font-size: 10px; }}"
            f"#customerComponentDetail {{ color: {pal['text']}; font-family: '{S.monospace_family()}'; font-weight: 600; }}"
            f"#customerComponentItem[density='dense'] #customerComponentName {{ font-size: 9px; }}"
            f"#customerComponentItem[density='dense'] #customerComponentDetail {{ font-size: 10px; }}"
        )
        self._snr_plot.setBackground(pal["panel"])
        self._snr_curve.setPen(pg.mkPen(pal["accent_2"], width=2))
        for axis in ("left", "bottom"):
            self._snr_plot.getAxis(axis).setPen(pg.mkPen(pal["border_2"]))
            self._snr_plot.getAxis(axis).setTextPen(pg.mkPen(pal["text_2"]))

    def retranslate_ui(self) -> None:
        self._beam_title.setText(tr("Beam direction"))
        sky = None if self._orbit_store is None else self._orbit_store.sky_snapshot()
        self._update_beam_legend(profile_characterized=bool(sky is None or sky.profile_characterized))
        self._snr_title.setText(tr("Signal strength - last 5 minutes"))
        self._state_group_title.setText(tr("Status"))
        self._data_group_title.setText(tr("Runtime data"))
        for key, title in (
            ("converter", tr_source("Converter")),
            ("tx_array", tr_source("TX array")),
            ("rx_array", tr_source("RX array")),
        ):
            self._component_names[key].setText(tr(title))
        for metric in self._beam_values.values():
            metric.retranslate_ui()
        self._snr_readout.retranslate_ui()
        self._snr_plot.setLabel("bottom", tr("Device uptime"), units="s")
        self._on_connection_changed(self._live.is_connected())
        self._render_recording_control(recording=self._live.is_recording())
        self.refresh()
