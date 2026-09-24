"""Shared live-session facade and lazily constructed engineering presentation.

The session core, transport intent, stores, recording, and control state exist as
soon as the object is created.  Channel, chart, 3D, and diagnostic widgets are
constructed by :meth:`ensure_presentation` on first engineering-Live access.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.comm import (
    DeviceConnectionPhase,
    SerialWorker,
    UdpWorker,
)
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.data import DataStore, EventRecord, StateStore
from satellite_debug_tool.core.link_trace import trace_message
from satellite_debug_tool.core.product import (
    CustomerRecordingState,
    ProductServiceStore,
)
from satellite_debug_tool.core.profile import (
    CHANNEL_ROLE_ANTENNA_AZ,
    CHANNEL_ROLE_ANTENNA_EL,
    CHANNEL_ROLE_INTERNAL_INS_YAW,
    CHANNEL_ROLE_PITCH,
    CHANNEL_ROLE_ROLL,
    CHANNEL_ROLE_YAW,
    CONTROL_SUBCMD_SET_TRACE_MODE,
    CONTROL_VALUE_FROM_ENUM_VALUE,
    ProfileStore,
    STATE_ROLE_INTERNAL_INS_YAW_REFERENCE,
)
from satellite_debug_tool.core.profile.ins_yaw_display import select_attitude_yaw_channel
from satellite_debug_tool.core.protocol import (
    DataReport,
    EventReport,
    Heartbeat,
    GnssCnrReport,
    GnssSatReport,
    GnssSignalReport,
    GnssSkyReport,
    StateReport,
    ORBIT_FEATURE_SKY_SNAPSHOT,
    OrbitCapabilitiesReport,
    OrbitOperation,
    OrbitSkyReport,
    OrbitStatus,
    OrbitStatusReport,
    build_orbit_capabilities,
    build_orbit_select,
    build_orbit_sky_snapshot,
)
from satellite_debug_tool.core.session import (
    CaptureProfileController,
    CaptureProfileResult,
    DISCOVERY_FAST_ATTEMPTS,
    DISCOVERY_FAST_INTERVAL_MS,
    DISCOVERY_SLOW_INTERVAL_MS,
    DebugController,
    DebugRequestResult,
    DeviceSessionCore,
    ProductSubscriptionController,
    SessionOperationClass,
    SessionRecorderKind,
    SessionRecorderLease,
    SessionRegistry,
)
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.io.data_recorder import DataRecorder, SDB_VERSION_V3
from satellite_debug_tool.io.recording_path_registry import (
    RecordingPathError,
    RecordingPathRegistry,
)
from satellite_debug_tool.i18n import (
    register_translatable,
    set_raw_text,
    set_translatable_text,
    tr,
)
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.attitude_widget import AttitudeWidget
from satellite_debug_tool.ui.channel_panel import ChannelPanel
from satellite_debug_tool.ui.chart_widget import COLORS
from satellite_debug_tool.ui.control_panel_widget import ControlPanelWidget
from satellite_debug_tool.ui.dashboard_widget import DashboardWidget
from satellite_debug_tool.ui.event_timeline_widget import EventTimelineWidget
from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget
from satellite_debug_tool.ui.state_panel_widget import StatePanelWidget
from satellite_debug_tool.ui.status_strip_widget import StatusStripWidget


DEVICE_ACTIVITY_TIMEOUT_S = 3.0
ORBIT_SKY_REQUEST_TIMEOUT_S = 3.0
TRANSPORT_WORKER_STOP_TIMEOUT_MS = 1000


class LiveView(QWidget):
    """实时模式主视图。"""

    # 短消息上报到 MainWindow statusbar：(message, timeout_ms)
    status_message = Signal(str, int)
    # M9: 连接共享 — DeviceView 通过这些信号接入同一条链路
    connected_worker_changed = Signal(object)  # emit worker 或 None
    frame_received = Signal(object)            # emit 每个 parsed FrameV2Record
    debug_request_finished = Signal(bool, bool, str)  # target, ok, detail
    debug_state_changed = Signal(bool)
    connection_state_changed = Signal(bool)
    device_connection_phase_changed = Signal(str)
    profile_ready = Signal(str)
    recording_state_changed = Signal(bool, str)
    customer_recording_state_changed = Signal(str, str)
    capture_profile_resync_changed = Signal(bool)
    orbit_capability_changed = Signal(bool)

    def __init__(
        self,
        settings: Settings,
        parent: Optional[QWidget] = None,
        *,
        session_registry: Optional[SessionRegistry] = None,
        customer_binding=None,
        serial_only: bool = False,
        defer_presentation: bool = False,
    ):
        super().__init__(parent)
        self._settings = settings
        self._customer_binding = customer_binding
        self._managed_runtime = (
            None if customer_binding is None else customer_binding.runtime
        )
        self._serial_only = bool(serial_only)

        # ---------- 业务状态（原 MainWindow.__init__）----------
        self._worker = None
        self._worker_signal_slots: dict[str, object] = {}
        self._session_registry = session_registry
        if customer_binding is None:
            # Engineering serial and the legacy single-endpoint worker are
            # independent transports.  Shared UDP authority exists only in
            # EndpointSessionDirectory-backed customer bindings.
            initial_endpoint = (
                str(settings.get("udp.remote_ip", "192.168.1.12")),
                int(settings.get("udp.remote_port", 4004)),
            )
            self._session_core = DeviceSessionCore(parent=self)
        else:
            initial_endpoint = customer_binding.endpoint
            self._session_core = customer_binding.core
        self._data_store = self._session_core.data_store
        self._profile_store = self._session_core.profile_store
        self._state_store = self._session_core.state_store
        self._event_log = self._session_core.event_log
        self._gnss_store = self._session_core.gnss_store
        self._orbit_store = self._session_core.orbit_store
        self._orbit_sky_consumers: set[str] = set()
        self._orbit_sky_request_active = False
        self._orbit_sky_request_id = 0
        self._orbit_sky_request_started_at = 0.0
        self._orbit_sky_snapshot_id = 0
        self._orbit_sky_expected_page = 0
        self._orbit_sky_timer = QTimer(self)
        self._orbit_sky_timer.setInterval(1000)
        self._orbit_sky_timer.timeout.connect(self.request_orbit_sky_refresh)
        self._product_store = self._session_core.product_store
        self._product_subscription_controller = (
            ProductSubscriptionController(self._session_core, parent=self)
            if customer_binding is None
            else None
        )
        self._managed_subscription_timer = QTimer(self)
        self._product_store.updated.connect(self._on_product_store_updated)
        self._gnss_store.changed.connect(self._on_gnss_store_changed)
        self._gnss_widget = None
        self._gnss_dock: Optional[QDockWidget] = None
        self._orbit_widget = None
        self._orbit_dock: Optional[QDockWidget] = None
        self._orbit_store.capability_changed.connect(self._on_orbit_capability_changed)
        self._orbit_store.changed.connect(self._on_orbit_store_changed)
        self._event_log.event_added.connect(self._on_event_added_for_chart)
        self._profile_store.profile_changed.connect(self._on_profile_changed_sync)
        self._session_core.record_received.connect(self._on_session_record)
        if customer_binding is None:
            self._session_core.activity.connect(self._mark_device_activity)
        self._session_core.heartbeat_received.connect(self._status_strip_heartbeat)
        self._session_core.profile_ready.connect(self._on_handshake_ready)
        self._session_core.link_lost.connect(self._on_link_lost)
        self._session_core.link_restored.connect(self._on_link_restored)
        self._session_core.device_transaction_changed.connect(
            lambda _active: self._update_debug_button_enabled()
        )
        self._handshake_timer = QTimer(self)
        self._handshake_timer.setInterval(100)
        self._handshake_timer.timeout.connect(self._on_handshake_tick)
        self._is_connected = False
        self._device_connection_phase = DeviceConnectionPhase.DISCONNECTED
        self._last_valid_frame_at: Optional[float] = None
        self._device_activity_timer = QTimer(self)
        self._device_activity_timer.setInterval(250)
        self._device_activity_timer.timeout.connect(self._check_device_activity)
        self._active_hw_type: Optional[str] = None
        self._debug_controller = DebugController(
            self._session_core,
            parent=self,
            operation_gateway=self.new_operation_gateway(),
        )
        self._debug_controller.state_changed.connect(self._on_debug_state_changed)
        self._debug_controller.pending_changed.connect(self._on_debug_pending_changed)
        self._debug_controller.request_finished.connect(self._on_debug_request_finished)
        self._customer_auto_debug = False
        self._recorder = None
        self._engineering_recorder_lease: SessionRecorderLease | None = None
        self._engineering_recorder_owner = object()
        self._is_recording = False
        self._customer_recording = False
        self._customer_recording_state = CustomerRecordingState.IDLE
        self._customer_recording_path: Optional[Path] = None
        self._customer_capture_confirmed = False
        self._capture_restore_debug = False
        self._capture_profile_controller = CaptureProfileController(
            self._session_core,
            parent=self,
            operation_gateway=self.new_operation_gateway(),
        )
        self._capture_profile_controller.finished.connect(
            self._on_capture_profile_finished
        )
        self._capture_profile_controller.resync_required_changed.connect(
            self.capture_profile_resync_changed
        )
        self._frame_times: list[float] = []
        self._frame_count = 0
        self._error_count = 0
        self._theme = "dark"
        self._is_dark_theme = True
        self._view_active = False
        self._presentation_ready = False
        self._presentation_build_elapsed_ms: Optional[float] = None

        self._connection_type = (
            "UDP"
            if customer_binding is not None
            else str(self._settings.get("general.connection_type", "Serial"))
        )
        if self._connection_type not in {"Serial", "UDP"}:
            self._connection_type = "Serial"
        self._serial_port = str(self._settings.get("serial.last_port", ""))
        self._serial_baudrate = int(
            self._settings.get("serial.default_baudrate", "115200")
        )
        self._udp_remote_ip = str(initial_endpoint[0])
        self._udp_remote_port = int(initial_endpoint[1])
        self._udp_local_port = int(
            self._settings.get("device_udp.local_port", 45678)
        )
        self._active_connection_config: Optional[dict] = None

        # Rendering timers exist for the lifetime of the view, while the heavy
        # presentation tree is constructed only when engineering Live opens.
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(100)
        self._update_timer.timeout.connect(self._update_display)
        self._heavy_timer = QTimer(self)
        self._heavy_timer.setInterval(200)
        self._heavy_timer.timeout.connect(self._update_heavy)

        if customer_binding is not None:
            customer_binding.connection_state_changed.connect(
                self._on_managed_attachment_changed
            )
            customer_binding.device_connection_phase_changed.connect(
                self._on_managed_presence_changed
            )
            customer_binding.attachment_failed.connect(self._on_error)
            runtime_datagram = getattr(self._managed_runtime, "datagram_received", None)
            if runtime_datagram is not None:
                runtime_datagram.connect(self._on_runtime_datagram_received)
            self._managed_runtime.datagram_sent.connect(
                self._on_runtime_datagram_sent
            )
            self._apply_managed_attachment_state(emit_signals=False)

        if not defer_presentation:
            self.ensure_presentation()
        register_translatable(self)

    @property
    def presentation_ready(self) -> bool:
        return self._presentation_ready

    @property
    def presentation_build_elapsed_ms(self) -> Optional[float]:
        return self._presentation_build_elapsed_ms

    def ensure_presentation(self) -> None:
        """Build the engineering-only widget tree once and bind current state."""
        if self._presentation_ready:
            return
        started = time.perf_counter()
        self._setup_ui()
        self._load_settings()
        self._presentation_ready = True
        self._presentation_build_elapsed_ms = (
            time.perf_counter() - started
        ) * 1000.0
        self._apply_theme(self._theme)
        self._sync_presentation_from_state()

    def profile_store(self) -> ProfileStore:
        """供 DeviceView 复用 Live 页当前连接的 profile/capability。"""
        return self._profile_store

    def session_core(self) -> DeviceSessionCore:
        return self._session_core

    @property
    def fixed_endpoint(self) -> Optional[tuple[str, int]]:
        """Endpoint frozen into a managed Customer presentation, if any."""

        binding = self._customer_binding
        return None if binding is None else binding.endpoint

    def new_operation_gateway(self):
        """Return one controller-private gateway for this fixed session."""

        binding = self._customer_binding
        return None if binding is None else binding.new_operation_gateway()

    def session_command_sender(self):
        """Return the attachment-scoped sender used by read-only probes."""

        binding = self._customer_binding
        return None if binding is None else binding.command_sender

    def data_store(self) -> DataStore:
        return self._data_store

    def state_store(self) -> StateStore:
        return self._state_store

    def gnss_store(self) -> GnssStore:
        return self._gnss_store

    def product_store(self) -> ProductServiceStore:
        return self._product_store

    def customer_service_state(self):
        """Return the shared session's registered customer Product Service state."""
        return self._session_core.customer_service_state()

    @property
    def _product_subscribe_attempts(self) -> int:
        controller = self._current_subscription_controller()
        return 0 if controller is None else controller.attempts

    @property
    def _product_subscribe_pending_id(self) -> int | None:
        controller = self._current_subscription_controller()
        return None if controller is None else controller.pending_request_id

    @property
    def _product_subscription_confirmed(self) -> bool:
        controller = self._current_subscription_controller()
        return bool(controller is not None and controller.confirmed)

    @property
    def _product_subscribe_timer(self) -> QTimer:
        controller = self._current_subscription_controller()
        return (
            self._managed_subscription_timer
            if controller is None
            else controller.retry_timer
        )

    def _current_subscription_controller(self):
        if self._managed_runtime is not None:
            return self._managed_runtime.subscription_controller
        return self._product_subscription_controller

    @property
    def _capture_pending_id(self) -> int | None:
        return self._capture_profile_controller.pending_request_id

    @property
    def _capture_pending_target(self) -> bool | None:
        return self._capture_profile_controller.pending_target

    @property
    def _capture_timer(self) -> QTimer:
        return self._capture_profile_controller.timeout_timer

    @property
    def capture_profile_resync_required(self) -> bool:
        return self._capture_profile_controller.resync_required

    def product_snapshot(self):
        return self._session_core.product_snapshot()

    def orbit_store(self) -> OrbitStore:
        return self._orbit_store

    def set_orbit_sky_consumer(self, name: str, active: bool) -> None:
        """Register a visible view that needs the shared 1 Hz array-sky snapshot."""
        key = str(name).strip()
        if not key:
            return
        if active:
            self._orbit_sky_consumers.add(key)
            self._sync_orbit_sky_timer()
            if self._orbit_sky_timer.isActive():
                QTimer.singleShot(0, self.request_orbit_sky_refresh)
        else:
            self._orbit_sky_consumers.discard(key)
            self._sync_orbit_sky_timer()

    def _orbit_sky_supported(self) -> bool:
        capabilities = self._orbit_store.capabilities
        return bool(
            capabilities is not None
            and capabilities.status == OrbitStatus.OK
            and capabilities.feature_flags & ORBIT_FEATURE_SKY_SNAPSHOT
        )

    def _sync_orbit_sky_timer(self) -> None:
        should_run = bool(
            self._orbit_sky_consumers
            and self._is_connected
            and self._orbit_sky_supported()
        )
        if should_run:
            if not self._orbit_sky_timer.isActive():
                self._orbit_sky_timer.start()
        else:
            self._orbit_sky_timer.stop()
            self._reset_orbit_sky_request()

    def request_orbit_sky_refresh(self) -> bool:
        """Start one snapshot only when a consumer, connection, and capability are present."""
        if self._orbit_sky_request_active:
            if time.monotonic() - self._orbit_sky_request_started_at < ORBIT_SKY_REQUEST_TIMEOUT_S:
                return False
            self._reset_orbit_sky_request()
        if (
            not self._orbit_sky_consumers
            or not self._is_connected
            or not self._orbit_sky_supported()
        ):
            return False
        request_id = self.next_product_request_id()
        self._orbit_sky_request_active = True
        self._orbit_sky_request_id = request_id
        self._orbit_sky_request_started_at = time.monotonic()
        self._orbit_sky_snapshot_id = 0
        self._orbit_sky_expected_page = 0
        if not self._send_control_frame(
            build_orbit_sky_snapshot(request_id, 0, 0),
            operation=SessionOperationClass.READ_ONLY_QUERY,
        ):
            self._reset_orbit_sky_request()
            return False
        return True

    def select_orbit_tracking_target(self, norad_id: int) -> bool:
        """Send the existing confirmed single-target selection; this never controls TX."""
        if not self._is_connected or not (1 <= int(norad_id) <= 0xFFFFFFFF):
            return False
        return self._send_control_frame(
            build_orbit_select(self.next_product_request_id(), int(norad_id)),
            operation=SessionOperationClass.MUTATING,
        )

    def _reset_orbit_sky_request(self) -> None:
        self._orbit_sky_request_active = False
        self._orbit_sky_request_id = 0
        self._orbit_sky_request_started_at = 0.0
        self._orbit_sky_snapshot_id = 0
        self._orbit_sky_expected_page = 0

    def is_connected(self) -> bool:
        """Return whether the transport/session is active."""
        return self._is_connected

    def is_device_online(self) -> bool:
        return self._device_connection_phase == DeviceConnectionPhase.ONLINE

    def connection_phase(self) -> DeviceConnectionPhase:
        return self._device_connection_phase

    def is_recording(self) -> bool:
        return self._is_recording

    def customer_recording_state(self) -> CustomerRecordingState:
        return self._customer_recording_state

    def connect_udp(
        self,
        remote_ip: str,
        remote_port: int = 4004,
        local_port: int = 45678,
        *,
        auto_debug: bool = True,
    ) -> bool:
        """Customer workspace UDP connection entry using the shared Live session."""
        if self._is_connected:
            return True
        if self._customer_binding is not None:
            requested = (str(remote_ip).strip(), int(remote_port))
            if requested != self._customer_binding.endpoint:
                self.status_message.emit(
                    tr("The customer session endpoint is fixed; edit it in the device list"),
                    3500,
                )
                return False
            if int(local_port) != int(self._udp_local_port):
                self.status_message.emit(
                    tr("The shared UDP local port is fixed in Settings"),
                    3500,
                )
                return False
            self._customer_auto_debug = bool(auto_debug)
            return bool(self._customer_binding.attach())
        self._connection_type = "UDP"
        self._udp_remote_ip = str(remote_ip).strip()
        self._udp_remote_port = int(remote_port)
        self._udp_local_port = int(local_port)
        self._settings.set("general.connection_type", self._connection_type)
        self._settings.set("udp.remote_ip", self._udp_remote_ip)
        self._settings.set("udp.remote_port", self._udp_remote_port)
        self._settings.set("udp.local_port", self._udp_local_port)
        self._settings.persist_preferences()
        if self._presentation_ready:
            index = self._type_combo.findData("UDP")
            if index >= 0:
                self._type_combo.setCurrentIndex(index)
            self._remote_ip.setText(self._udp_remote_ip)
            self._remote_port.setValue(self._udp_remote_port)
            self._local_port.setValue(self._udp_local_port)
        self._customer_auto_debug = bool(auto_debug)
        self._connect_transport(
            {
                "type": "udp",
                "remote_ip": self._udp_remote_ip,
                "remote_port": self._udp_remote_port,
                "local_port": self._udp_local_port,
            }
        )
        return self._is_connected

    def disconnect_device(self) -> bool:
        self._customer_auto_debug = False
        return self._on_disconnect_clicked()

    def toggle_recording(self) -> None:
        self._on_record_clicked()

    def toggle_customer_recording(self) -> None:
        state = self._customer_recording_state
        if state == CustomerRecordingState.ACTIVE and self._is_recording:
            self._stop_recording(restore_customer_profile=self._customer_recording)
            return
        if state == CustomerRecordingState.ARMED:
            self._clear_customer_recording_intent()
            self.status_message.emit(tr("Pending recording request cancelled"), 2500)
            return
        if state == CustomerRecordingState.PREPARING:
            self._capture_profile_controller.reset()
            self._set_customer_recording_state(CustomerRecordingState.RESTORING)
            self._request_capture_profile(False)
            return
        if state == CustomerRecordingState.RESTORING:
            return

        filepath = self._choose_recording_path(customer=True)
        if filepath is None:
            return
        self._customer_recording_path = filepath
        self._set_customer_recording_state(CustomerRecordingState.ARMED)
        self.status_message.emit(
            tr("Recording requested; waiting for device..."),
            0,
        )
        self._try_start_armed_customer_recording()

    def show_gnss_details(self) -> None:
        self._toggle_gnss()

    def show_orbit_details(self) -> None:
        self._toggle_orbit()

    def probe_orbit_capabilities(self) -> bool:
        if not self._is_connected:
            return False
        return self._send_control_frame(
            build_orbit_capabilities(self.next_product_request_id()),
            operation=SessionOperationClass.READ_ONLY_QUERY,
        )

    def next_product_request_id(self) -> int:
        return self._session_core.next_request_id()

    def send_product_frame(self, frame: bytes) -> bool:
        return self._send_control_frame(
            frame,
            operation=SessionOperationClass.MUTATING,
        )

    def is_debug_enabled(self) -> bool:
        return self._debug_controller.enabled

    @Slot(bool)
    def request_debug_mode(self, target: bool) -> None:
        """Live/Device 共用的严格 Debug 控制入口。"""
        self._sync_session_transport()
        self._debug_controller.request(bool(target))

    @Slot(bool)
    def set_handshake_retries_paused(self, paused: bool) -> None:
        self._session_core.set_handshake_retries_paused(bool(paused))

    # ============================ 主题 / 字号 ============================

    def set_theme(self, theme: str, scale: str = "small") -> None:
        """由 MainWindow 广播：切换主题。M7 字号已固化 small，scale 参数忽略。"""
        self._theme = theme
        self._is_dark_theme = theme != "light"
        if self._presentation_ready:
            self._apply_theme(theme)

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light")

    # ============================ 初始化 ============================

    def _load_settings(self):
        index = self._type_combo.findData(self._connection_type)
        self._type_combo.setCurrentIndex(index if index >= 0 else 0)
        self._on_type_changed(self._connection_type)
        self._baudrate_combo.setCurrentText(str(self._serial_baudrate))
        if self._serial_port:
            port_index = self._port_combo.findData(self._serial_port)
            if port_index >= 0:
                self._port_combo.setCurrentIndex(port_index)
        # 主题由 MainWindow 应用到本 view；这里只更新 UDP 字段
        self._remote_ip.setText(self._udp_remote_ip)
        self._remote_port.setValue(self._udp_remote_port)
        self._local_port.setValue(self._udp_local_port)
        if self._customer_binding is not None:
            self._type_combo.setEnabled(False)
            self._remote_ip.setReadOnly(True)
            self._remote_port.setReadOnly(True)
            self._remote_port.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            self._local_port.setReadOnly(True)
            self._local_port.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            self._connect_btn.setEnabled(not self._is_connected)
            self._disconnect_btn.setEnabled(self._is_connected)

    def _setup_ui(self):
        # 整个 LiveView 用 QVBoxLayout：顶部 toolbar + 主区 + 底部 status row
        self.setStyleSheet(f"background-color: {S.BG_DARK}; color: {S.TEXT};")
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(3)

        # ---------- 顶部工具栏（连接 / 控制 / 录制 / 导入 / 清空）----------
        self._toolbar = QToolBar()
        self._toolbar.setStyleSheet(
            f"background-color: {S.PANEL_DARK}; border: none; padding: 4px;"
        )
        self._toolbar.setMovable(False)
        self._toolbar_scroll = QScrollArea()
        self._toolbar_scroll.setWidgetResizable(True)
        self._toolbar_scroll.setFrameShape(QScrollArea.NoFrame)
        self._toolbar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._toolbar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._toolbar_scroll.setWidget(self._toolbar)
        root.addWidget(self._toolbar_scroll)

        # 设备状态卡（Mission Console conn-state）：图标框 + 设备名 / 连接状态点
        self._conn_card = QFrame()
        self._conn_card.setObjectName("connCard")
        cc_row = QHBoxLayout(self._conn_card)
        cc_row.setContentsMargins(11, 5, 13, 5)
        cc_row.setSpacing(9)
        self._cs_icon = QLabel()
        self._cs_icon.setObjectName("csIcon")
        self._cs_icon.setFixedSize(28, 28)
        self._cs_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cc_meta = QVBoxLayout()
        cc_meta.setContentsMargins(0, 0, 0, 0)
        cc_meta.setSpacing(1)
        self._cs_dev = QLabel("—")
        self._cs_dev.setObjectName("csDev")
        self._cs_stat = QLabel("Disconnected")
        self._cs_stat.setObjectName("csStat")
        cc_meta.addWidget(self._cs_dev)
        cc_meta.addWidget(self._cs_stat)
        cc_row.addWidget(self._cs_icon)
        cc_row.addLayout(cc_meta)
        self._toolbar.addWidget(self._conn_card)
        self._toolbar.addSeparator()

        self._type_combo = QComboBox()
        if self._customer_binding is not None:
            self._type_combo.addItem(tr("Shared customer UDP"), "UDP")
        elif self._serial_only:
            self._type_combo.addItem(tr("Engineering serial"), "Serial")
        else:
            self._type_combo.addItem(tr("Serial"), "Serial")
            self._type_combo.addItem("UDP", "UDP")
        self._type_combo.setFixedWidth(80)
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        self._toolbar.addWidget(QLabel(tr("Type:")))
        self._toolbar.addWidget(self._type_combo)

        self._config_stack = QStackedWidget()

        self._serial_widget = QWidget()
        serial_layout = QHBoxLayout(self._serial_widget)
        serial_layout.setContentsMargins(0, 0, 0, 0)
        serial_layout.setSpacing(4)

        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(80)
        self._refresh_ports()
        serial_layout.addWidget(QLabel(tr("Port:")))
        serial_layout.addWidget(self._port_combo)

        self._baudrate_combo = QComboBox()
        self._baudrate_combo.addItems(
            ["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"]
        )
        self._baudrate_combo.setCurrentText("115200")
        serial_layout.addWidget(QLabel(tr("Baud rate:")))
        serial_layout.addWidget(self._baudrate_combo)
        self._config_stack.addWidget(self._serial_widget)

        self._udp_widget = QWidget()
        udp_layout = QHBoxLayout(self._udp_widget)
        udp_layout.setContentsMargins(0, 0, 0, 0)
        udp_layout.setSpacing(4)

        self._remote_ip = QLineEdit("192.168.1.12")
        self._remote_ip.setMinimumWidth(124)   # 容下完整 IP（等宽字体）
        udp_layout.addWidget(QLabel(tr("Remote IP:")))
        udp_layout.addWidget(self._remote_ip)

        self._remote_port = QSpinBox()
        self._remote_port.setRange(1, 65535)
        self._remote_port.setValue(4004)
        self._remote_port.setMinimumWidth(78)
        udp_layout.addWidget(QLabel(tr("Remote port:")))
        udp_layout.addWidget(self._remote_port)

        self._local_port = QSpinBox()
        self._local_port.setRange(1, 65535)
        self._local_port.setValue(45678)
        self._local_port.setMinimumWidth(82)
        udp_layout.addWidget(QLabel(tr("Local port:")))
        udp_layout.addWidget(self._local_port)
        self._config_stack.addWidget(self._udp_widget)

        self._toolbar.addWidget(self._config_stack)
        self._toolbar.addSeparator()

        # Connect = 主按钮(accent + plug)；Disconnect = 危险图标按钮(连接后才有意义)
        self._connect_btn = QPushButton(tr("Connect"))
        self._connect_btn.setMinimumSize(92, 29)
        self._connect_btn.setToolTip(
            tr("Open the serial port or bind UDP, then start the handshake")
        )
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        self._toolbar.addWidget(self._connect_btn)

        self._disconnect_btn = QPushButton("")   # icon-only（设计）
        self._disconnect_btn.setFixedSize(30, 29)
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.setToolTip(
            tr("Disconnect without clearing received data or the Profile")
        )
        self._disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        self._toolbar.addWidget(self._disconnect_btn)

        self._toolbar.addSeparator()

        self._debug_btn = QPushButton(tr("Debug: OFF"))
        self._debug_btn.setMinimumSize(100, 29)
        self._debug_btn.setEnabled(False)
        self._debug_btn.setToolTip(
            tr("Send CONTROL.DEBUG_ENABLE to start or stop device data reports")
        )
        self._debug_btn.clicked.connect(self._on_debug_toggled)
        self._toolbar.addWidget(self._debug_btn)

        self._record_btn = QPushButton("REC")
        self._record_btn.setMinimumSize(74, 29)
        self._record_btn.setToolTip(
            tr("Start or stop recording .sdb v2 with a Profile snapshot")
        )
        self._record_btn.clicked.connect(self._on_record_clicked)
        self._toolbar.addWidget(self._record_btn)

        self._import_btn = QPushButton(tr("Import"))
        self._import_btn.setMinimumSize(84, 29)
        self._import_btn.setToolTip(
            tr("Import an .sdb v2 file into Live; Playback is recommended")
        )
        self._import_btn.clicked.connect(self._on_import_clicked)
        self._toolbar.addWidget(self._import_btn)

        self._clear_btn = QPushButton("")   # icon-only ghost（设计）
        self._clear_btn.setFixedSize(30, 29)
        self._clear_btn.setToolTip(
            tr(
                "Clear charts, dashboard, events, and counters while retaining "
                "the Profile and current state panel"
            )
        )
        self._clear_btn.clicked.connect(self.clear_display_data)
        self._toolbar.addWidget(self._clear_btn)

        self._gnss_btn = QPushButton("GNSS")
        self._gnss_btn.setMinimumSize(72, 29)
        self._gnss_btn.setEnabled(False)
        self._gnss_btn.setToolTip(tr("Open the sky plot and signal-level C/N₀ window"))
        self._gnss_btn.clicked.connect(self._toggle_gnss)
        self._toolbar.addWidget(self._gnss_btn)

        self._orbit_btn = QPushButton("Orbit/TLE")
        self._orbit_btn.setMinimumSize(88, 29)
        self._orbit_btn.setEnabled(False)
        self._orbit_btn.setToolTip(tr("Open the XESA01 satellite catalog and prediction window"))
        self._orbit_btn.clicked.connect(self._toggle_orbit)
        self._toolbar.addWidget(self._orbit_btn)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._toolbar.addWidget(spacer)

        # 隐藏的兼容 label（旧逻辑仍引用 _hw_label / _conn_status_label）
        self._hw_label = QLabel(tr("Device: {hardware}", hardware="—"))
        self._hw_label.hide()
        self._conn_status_label = QLabel("Disconnected")
        self._conn_status_label.hide()

        # 统计行（Mission Console statline）：FPS / CH / FRM / ERR，移到连接条右侧
        self._statline = QWidget()
        self._statline.setObjectName("statline")
        sl_row = QHBoxLayout(self._statline)
        sl_row.setContentsMargins(4, 0, 8, 0)
        sl_row.setSpacing(14)
        self._fps_label = QLabel("FPS 0")
        self._channel_count_label = QLabel("CH 0")
        self._frame_count_label = QLabel("FRM 0")
        self._error_count_label = QLabel("ERR 0")
        self._error_count_label.setObjectName("statErr")
        for lbl in (self._fps_label, self._channel_count_label,
                    self._frame_count_label, self._error_count_label):
            lbl.setObjectName(lbl.objectName() or "statItem")
            sl_row.addWidget(lbl)
        self._toolbar.addWidget(self._statline)

        # ---------- StatusStrip（全宽） ----------
        self._status_strip = StatusStripWidget(self._profile_store, self._state_store)
        root.addWidget(self._status_strip)

        # Dashboard（KPI 卡行）—— 移到中心列顶部（设计 kpi-row）
        self._dashboard = DashboardWidget(self._profile_store, self._state_store)
        self._dashboard.mode_requested.connect(self._on_dashboard_mode_requested)

        # ---------- 主区：3 列 左 ChannelPanel | 中(KPI+chart) | 右 rpanel ----------
        splitter = QSplitter(Qt.Vertical)
        top_splitter = QSplitter(Qt.Horizontal)
        self._top_splitter = top_splitter   # 保存引用供持久化

        # 左侧：通道选择面板
        self._channel_panel = ChannelPanel()
        self._channel_panel.setMinimumWidth(160)
        # D6 P0 修复：勾选 = 控制曲线显隐（所见即所得）
        self._channel_panel.selection_changed.connect(self._on_channel_visibility_changed)
        top_splitter.addWidget(self._channel_panel)

        # 中心列：KPI 卡行 + chart
        center_col = QWidget()
        center_v = QVBoxLayout(center_col)
        center_v.setContentsMargins(0, 0, 0, 0)
        center_v.setSpacing(4)
        center_v.addWidget(self._dashboard)
        self._chart = GroupedChartWidget()
        self._chart.setMinimumHeight(360)
        self._chart.set_dark_theme(True)
        self._chart.set_profile_store(self._profile_store)
        self._chart.set_settings(self._settings)   # M10 P7：恢复归一化状态等
        # D6：切单图/分组模式后曲线重建为全可见 → 重新套用通道勾选态
        self._chart.mode_changed.connect(lambda *_: self._resync_channel_visibility())
        center_v.addWidget(self._chart, 1)
        top_splitter.addWidget(center_col)

        # 右栏 rpanel：姿态 3D（上）+ 状态面板（中）+ 事件时间线（下），合为一列
        right_panel = QSplitter(Qt.Vertical)
        self._right_panel = right_panel
        right_panel.setObjectName("rpanel")
        right_panel.setMinimumWidth(300)

        self._attitude = AttitudeWidget()
        # 1024x600 leaves about 390 px for this complete column. A 300 px
        # attitude minimum forced QSplitter children to paint over one another.
        self._attitude.setMinimumHeight(180)
        self._attitude.set_dark_theme(True)
        right_panel.addWidget(self._attitude)

        self._state_panel = StatePanelWidget(
            self._profile_store, self._state_store, data_store=self._data_store
        )
        self._state_panel.setMinimumHeight(56)
        right_panel.addWidget(self._state_panel)
        self._event_timeline = EventTimelineWidget(self._event_log)
        self._event_timeline.setMinimumHeight(115)
        self._event_timeline.jump_requested.connect(self._chart.jump_to_timestamp)
        right_panel.addWidget(self._event_timeline)
        right_panel.setStretchFactor(0, 1)   # 姿态可随窗口增高（波束不被裁）
        right_panel.setStretchFactor(1, 0)   # 状态紧凑
        right_panel.setStretchFactor(2, 1)   # 事件填充
        right_panel.setSizes([340, 200, 300])
        top_splitter.addWidget(right_panel)

        # 列：通道 / chart / 右栏
        top_splitter.setStretchFactor(0, 0)
        top_splitter.setStretchFactor(1, 1)
        top_splitter.setStretchFactor(2, 0)
        top_splitter.setSizes([212, 868, 320])
        top_splitter.splitterMoved.connect(self._on_top_splitter_moved)
        splitter.addWidget(top_splitter)

        self._control_panel = ControlPanelWidget()
        self._control_panel.setMaximumHeight(80)   # M7：防 splitter 拖动吞掉曲线区
        self._control_panel.set_profile_store(self._profile_store)
        self._control_panel.sample_rate_changed.connect(self._on_sample_rate_changed)
        self._control_panel.user_mark_requested.connect(self._on_user_mark_requested)
        self._control_panel.reset_stats_requested.connect(self._on_reset_stats_requested)
        self._control_panel.channel_enable_changed.connect(self._on_channel_enable_changed)

        splitter.addWidget(self._control_panel)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 0)
        root.addWidget(splitter)

        # 启动后恢复 splitter 状态（如果有持久化值）
        QTimer.singleShot(0, self._restore_splitter_state)

        # 应用初始按钮 / 输入框 stylesheet（_apply_theme 会做完整刷新）
        self._apply_button_styles_initial()

    def _apply_button_styles_initial(self):
        """setup_ui 期间应用一次按钮 / 输入框样式，避免 _apply_theme 调用前显示裸样式。

        Mission Console：按钮交给全局 QSS + variant 属性；这里只设 variant + 图标。
        Connect = 主按钮(accent)；Disconnect = 危险(err)；其余 = 次按钮(默认)。
        """
        self._style_toolbar_buttons(self._theme)

    @staticmethod
    def _set_variant(widget, variant: str) -> None:
        """设按钮 variant 属性并 repolish，让全局 QSS 的 [variant=...] 规则生效。"""
        widget.setProperty("variant", variant)
        widget.setStyleSheet("")    # 清内联，交给全局 QSS
        st = widget.style()
        st.unpolish(widget)
        st.polish(widget)

    def _style_toolbar_buttons(self, theme: str) -> None:
        """统一设连接条按钮的 variant + 图标着色（被 initial 与 _apply_theme 共用）。"""
        pal = S.palette(theme)
        try:
            from satellite_debug_tool.ui import icons as _ic
            ink = pal["accent_ink"]
            t2 = pal["text_2"]
            err = pal["err"]
            self._connect_btn.setIcon(_ic.icon("plug", color=ink, size=14))
            self._disconnect_btn.setIcon(_ic.icon("unplug", color=err, size=14))
            self._debug_btn.setIcon(_ic.icon("activity", color=t2, size=14))
            self._record_btn.setIcon(_ic.icon("record", color=t2, size=14))
            self._import_btn.setIcon(_ic.icon("import", color=t2, size=14))
            self._clear_btn.setIcon(_ic.icon("trash", color=t2, size=14))
        except Exception:
            pass
        self._set_variant(self._connect_btn, "primary")
        self._set_variant(self._disconnect_btn, "danger")
        self._set_variant(self._clear_btn, "ghost")
        for btn in (self._debug_btn, self._record_btn, self._import_btn):
            self._set_variant(btn, "")
        # 输入控件清内联样式 → 全局 QSS 接管
        for w in (self._type_combo, self._port_combo, self._baudrate_combo,
                  self._remote_ip, self._remote_port, self._local_port):
            w.setStyleSheet("")

    # ============================ 连接 / 工作流 ============================

    def _on_type_changed(self, value):
        if isinstance(value, int):
            conn_type = self._type_combo.itemData(value)
        else:
            conn_type = value
            if conn_type not in ("Serial", "UDP"):
                index = self._type_combo.findText(str(value))
                conn_type = self._type_combo.itemData(index) if index >= 0 else "Serial"
        if conn_type == "Serial":
            self._config_stack.setCurrentIndex(0)
        else:
            self._config_stack.setCurrentIndex(1)
        self._connection_type = conn_type
        if self._customer_binding is None:
            self._settings.set("general.connection_type", conn_type)
            self._settings.persist_preferences()

    def _refresh_ports(self):
        ports = SerialWorker.list_ports()
        self._port_combo.clear()
        if ports:
            for port in ports:
                self._port_combo.addItem(port, port)
        else:
            self._port_combo.addItem(tr("No ports"), None)

    def _on_connect_clicked(self):
        conn_type = self._type_combo.currentData() or "Serial"
        trace_message("DBG_UI", f"CLICK CONNECT type={conn_type}")
        if self._customer_binding is not None:
            self.connect_udp(
                self._udp_remote_ip,
                self._udp_remote_port,
                self._udp_local_port,
                auto_debug=True,
            )
            return
        if conn_type == "Serial":
            port = self._port_combo.currentData()
            if not port:
                self.status_message.emit(tr("No serial port is available"), 3000)
                return
            self._connection_type = "Serial"
            self._serial_port = str(port)
            self._serial_baudrate = int(self._baudrate_combo.currentText())
            config = {
                "type": "serial",
                "port": self._serial_port,
                "baudrate": self._serial_baudrate,
            }
            self._settings.set(
                "serial.default_baudrate", str(self._serial_baudrate)
            )
            self._settings.set("serial.last_port", self._serial_port)
            self._settings.persist_preferences()
        else:
            self._connection_type = "UDP"
            self._udp_remote_ip = self._remote_ip.text().strip()
            self._udp_remote_port = self._remote_port.value()
            self._udp_local_port = self._local_port.value()
            config = {
                "type": "udp",
                "remote_ip": self._udp_remote_ip,
                "remote_port": self._udp_remote_port,
                "local_port": self._udp_local_port,
            }
            self._settings.set("udp.remote_ip", self._udp_remote_ip)
            self._settings.set("udp.remote_port", self._udp_remote_port)
            self._settings.set("udp.local_port", self._udp_local_port)
            self._settings.persist_preferences()
        self._settings.set("general.connection_type", self._connection_type)
        self._settings.persist_preferences()
        self._connect_transport(config)

    def _connect_transport(self, config: dict) -> bool:
        """Start one transport from stable connection state."""
        if self._customer_binding is not None:
            raise RuntimeError("managed customer LiveView cannot create a transport worker")
        if self._worker is not None and not self._shutdown_transport_worker():
            self._on_error(tr("Transport worker did not stop"))
            return False
        conn_type = str(config.get("type", ""))
        if conn_type == "serial":
            worker = SerialWorker()
            detail = f"{config['port']} @ {config['baudrate']}"
        elif conn_type == "udp":
            worker = UdpWorker()
            detail = f"UDP {config['remote_ip']}:{config['remote_port']}"
        else:
            raise ValueError(f"unsupported connection type: {conn_type}")
        self._worker = worker
        self._active_connection_config = dict(config)
        if self._presentation_ready:
            set_raw_text(detail, self._conn_status_label)

        self._wire_transport_worker(worker)

        if worker.connect(config):
            self._is_connected = True
            return True
        else:
            if self._presentation_ready:
                set_translatable_text("Connection failed", self._conn_status_label)
                self._set_conn_state(
                    False,
                    dev="—",
                    detail_source="Connection failed",
                )
            self._shutdown_transport_worker()
            return False

    def _wire_transport_worker(self, worker: object) -> None:
        slots = {
            "connected": lambda worker=worker: self._on_worker_connected(worker),
            "disconnected": lambda worker=worker: self._on_worker_disconnected(worker),
            "error": lambda message, worker=worker: self._on_worker_error(worker, message),
            "data_received": (
                lambda data, worker=worker: self._on_worker_data_received(worker, data)
            ),
        }
        worker.connected.connect(slots["connected"])
        worker.disconnected.connect(slots["disconnected"])
        worker.error.connect(slots["error"])
        worker.data_received.connect(slots["data_received"])
        self._worker_signal_slots = slots

    def _unwire_transport_worker(self, worker: object) -> None:
        slots = self._worker_signal_slots
        self._worker_signal_slots = {}
        for name, slot in slots.items():
            signal = getattr(worker, name, None)
            if signal is None or not hasattr(signal, "disconnect"):
                continue
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _shutdown_transport_worker(self) -> bool:
        """Stop and reap only the unmanaged Serial/UdpWorker transport."""

        if self._customer_binding is not None:
            return True
        worker = self._worker
        if worker is None:
            return True
        try:
            worker.disconnect()
        except (RuntimeError, OSError):
            return False
        if self._is_connected:
            self._on_disconnected()

        is_running = getattr(worker, "isRunning", None)
        running = bool(is_running()) if callable(is_running) else False
        if running:
            wait = getattr(worker, "wait", None)
            if not callable(wait) or not bool(wait(TRANSPORT_WORKER_STOP_TIMEOUT_MS)):
                return False
        if callable(is_running) and bool(is_running()):
            return False

        self._unwire_transport_worker(worker)
        delete_later = getattr(worker, "deleteLater", None)
        if callable(delete_later):
            try:
                delete_later()
            except RuntimeError:
                pass
        if self._worker is worker:
            self._worker = None
        return True

    def _on_worker_connected(self, worker: object) -> None:
        if worker is self._worker:
            self._on_connected()

    def _on_worker_disconnected(self, worker: object) -> None:
        if worker is self._worker:
            self._on_disconnected()

    def _on_worker_error(self, worker: object, message: str) -> None:
        if worker is self._worker:
            self._on_error(message)

    def _on_worker_data_received(self, worker: object, data: bytes) -> None:
        if worker is self._worker:
            self._on_data_received(data)

    def _on_disconnect_clicked(self):
        trace_message("DBG_UI", "CLICK DISCONNECT")
        if self._customer_binding is not None:
            if self._is_recording:
                if self._customer_recording:
                    self._stop_recording(restore_customer_profile=True)
                    return False
                if not self._stop_recording(restore_customer_profile=False):
                    return False
            if self._customer_recording_state is not CustomerRecordingState.IDLE:
                self.status_message.emit(
                    tr("Finish restoring the customer stream before disconnecting"),
                    3500,
                )
                return False
            if self._debug_controller.pending_target is not None:
                self.status_message.emit(
                    tr("Finish the pending Debug operation before disconnecting"),
                    3500,
                )
                return False
            if self._debug_controller.enabled:
                self.status_message.emit(
                    tr("Disable Debug and wait for confirmation before disconnecting"),
                    3500,
                )
                self.request_debug_mode(False)
                return False
            return bool(self._customer_binding.detach())
        self._debug_controller.reset()
        if self._presentation_ready:
            set_translatable_text("Debug: OFF", self._debug_btn)
            self._debug_btn.setEnabled(False)
        if self._worker is None:
            return True
        stopped = self._shutdown_transport_worker()
        if not stopped:
            self._on_error(tr("Transport worker did not stop"))
        return stopped

    @property
    def worker(self):
        """当前 BaseWorker 实例，未连接时为 None。供 DeviceView 共享连接。"""
        return self._worker if self._is_connected else None

    @Slot(bool)
    def _on_managed_attachment_changed(self, _attached: bool) -> None:
        self._apply_managed_attachment_state(emit_signals=True)

    @Slot(str)
    def _on_managed_presence_changed(self, _phase: str) -> None:
        binding = self._customer_binding
        if binding is None or not binding.attached:
            return
        previous = self._device_connection_phase
        self._set_device_connection_phase(binding.connection_phase)
        if (
            binding.connection_phase is DeviceConnectionPhase.ONLINE
            and previous is not DeviceConnectionPhase.ONLINE
        ):
            self._try_start_armed_customer_recording()
            self._try_restore_customer_profile()
            QTimer.singleShot(0, self.probe_orbit_capabilities)
            if self._customer_auto_debug and not self._debug_controller.enabled:
                QTimer.singleShot(0, lambda: self.request_debug_mode(True))

    def _apply_managed_attachment_state(self, *, emit_signals: bool) -> None:
        binding = self._customer_binding
        if binding is None:
            return
        connected = bool(binding.attached)
        changed = connected != self._is_connected
        self._is_connected = connected
        self._active_connection_config = (
            {
                "type": "udp",
                "remote_ip": binding.endpoint[0],
                "remote_port": binding.endpoint[1],
                "local_port": self._udp_local_port,
            }
            if connected
            else None
        )
        self._debug_controller.set_connected(connected)
        if connected:
            self._set_device_connection_phase(binding.connection_phase)
            self._sync_orbit_sky_timer()
        else:
            if self._is_recording and not self._customer_recording:
                self._stop_recording(restore_customer_profile=False)
            self._orbit_sky_timer.stop()
            self._reset_orbit_sky_request()
            self._set_device_connection_phase(DeviceConnectionPhase.DISCONNECTED)
            self._customer_auto_debug = False
        if self._presentation_ready:
            self._connect_btn.setEnabled(not connected)
            self._disconnect_btn.setEnabled(connected)
            self._type_combo.setEnabled(False)
            self._remote_ip.setReadOnly(True)
            self._remote_port.setReadOnly(True)
            self._local_port.setReadOnly(True)
            self._control_panel.set_enabled(connected)
            self._update_debug_button_enabled()
            self._render_connection_phase()
        if emit_signals and changed:
            self.connected_worker_changed.emit(None)
            self.connection_state_changed.emit(connected)

    @Slot(object)
    def _on_runtime_datagram_received(self, datagram: object) -> None:
        if not self._is_recording or self._recorder is None:
            return
        data = getattr(datagram, "data", None)
        if data is None:
            return
        self._recorder.write_frame(
            bytes(data),
            host_timestamp_ns=int(getattr(datagram, "wall_time_ns", time.time_ns())),
        )

    @Slot(object)
    def _on_runtime_datagram_sent(self, event: object) -> None:
        if (
            not self._is_recording
            or self._recorder is None
            or self._recorder.format_version != SDB_VERSION_V3
        ):
            return
        frame = getattr(event, "frame", None)
        if frame is None:
            return
        self._recorder.write_control_frame(
            bytes(frame),
            host_timestamp_ns=int(getattr(event, "host_time_ns", time.time_ns())),
        )

    def _set_device_connection_phase(
        self, phase: DeviceConnectionPhase
    ) -> None:
        if self._device_connection_phase == phase:
            return
        previous = self._device_connection_phase
        self._device_connection_phase = phase
        trace_message(
            "PRODUCT_SERVICE",
            f"DEVICE_PHASE {previous.value}->{phase.value}",
        )
        self.device_connection_phase_changed.emit(phase.value)

        if (
            phase == DeviceConnectionPhase.RECONNECTING
            and self._recorder is not None
            and self._customer_recording
        ):
            self._customer_capture_confirmed = False
            self._recorder.write_metadata_event(
                {"event": "device_offline", "phase": phase.value}
            )
        elif (
            phase == DeviceConnectionPhase.ONLINE
            and previous == DeviceConnectionPhase.RECONNECTING
            and self._recorder is not None
            and self._customer_recording
        ):
            self._recorder.write_metadata_event(
                {"event": "device_online", "phase": phase.value}
            )

        self._render_connection_phase()
        if phase == DeviceConnectionPhase.ONLINE:
            self.status_message.emit(tr("Device online"), 2500)
        elif phase == DeviceConnectionPhase.WAITING:
            self.status_message.emit(tr("Waiting for device..."), 0)
        elif phase == DeviceConnectionPhase.RECONNECTING:
            self.status_message.emit(tr("Reconnecting to device..."), 0)

    def _render_connection_phase(self) -> None:
        if not self._presentation_ready:
            return
        phase = self._device_connection_phase
        if phase == DeviceConnectionPhase.ONLINE:
            hardware = self._profile_store.current_hw_type() or "—"
            self._status_strip.set_link_state(connected=True)
            self._set_conn_state(True, dev=hardware, detail_source="Device online")
        elif phase == DeviceConnectionPhase.WAITING:
            self._status_strip.set_link_state(connected=False)
            self._set_conn_state(False, dev="—", detail_source="Waiting for device...")
        elif phase == DeviceConnectionPhase.RECONNECTING:
            self._status_strip.set_link_state(connected=False)
            self._set_conn_state(
                False,
                dev=self._profile_store.current_hw_type() or "—",
                detail_source="Reconnecting to device...",
            )
        else:
            self._status_strip.set_link_state(connected=False)
            self._set_conn_state(False, dev="—", detail_source="Disconnected")

    def _mark_device_activity(self) -> None:
        if not self._is_connected:
            return
        self._last_valid_frame_at = time.monotonic()
        if self._device_connection_phase != DeviceConnectionPhase.ONLINE:
            self._set_device_connection_phase(DeviceConnectionPhase.ONLINE)
            self._try_start_armed_customer_recording()
            self._try_restore_customer_profile()
            if not self._product_subscription_controller.confirmed:
                self._start_product_subscription()
            QTimer.singleShot(0, self.probe_orbit_capabilities)

    def _check_device_activity(self) -> None:
        if (
            not self._is_connected
            or self._device_connection_phase != DeviceConnectionPhase.ONLINE
            or self._last_valid_frame_at is None
        ):
            return
        if time.monotonic() - self._last_valid_frame_at < DEVICE_ACTIVITY_TIMEOUT_S:
            return

        self._last_valid_frame_at = None
        self._product_store.clear()
        self._state_store.clear()
        self._gnss_store.clear()
        self._orbit_store.clear()
        self._set_device_connection_phase(DeviceConnectionPhase.RECONNECTING)
        self._start_product_subscription()

    def _on_connected(self):
        self._is_connected = True
        self._sync_orbit_sky_timer()
        self._last_valid_frame_at = None
        self._set_device_connection_phase(DeviceConnectionPhase.WAITING)
        self._device_activity_timer.start()
        if self._presentation_ready:
            self._connect_btn.setEnabled(False)
            self._disconnect_btn.setEnabled(True)
            self._update_debug_button_enabled()
            self._type_combo.setEnabled(False)
            self._control_panel.set_enabled(True)
        self.connected_worker_changed.emit(self._worker)
        self.connection_state_changed.emit(True)

        endpoint = self._active_session_endpoint()
        self._session_core.begin_connection(
            endpoint=endpoint,
            transport=self._worker,
            sender=self._session_send if self._worker is not None else None,
            handshake_enabled=self._worker is not None,
        )
        self._debug_controller.set_connected(True)
        QTimer.singleShot(0, self.probe_orbit_capabilities)
        # Customer product telemetry is intentionally independent of the
        # dynamic Debug profile handshake. Send its subscription as soon as the
        # transport is usable; profile negotiation continues in parallel.
        self._start_product_subscription()
        if self._session_core.handshake is not None:
            self._handshake_timer.start()

    def _on_handshake_tick(self):
        self._session_core.tick(self._handshake_timer.interval())

    def _active_session_endpoint(self) -> tuple[str, int]:
        config = self._active_connection_config or {}
        if config.get("type") == "udp":
            return str(config.get("remote_ip", "")).strip(), int(
                config.get("remote_port", 0)
            )
        port = str(config.get("port", self._serial_port)).strip()
        return f"serial:{port}", 0

    def _on_channel_enable_changed(self, mask: int):
        self._sync_session_transport()
        if self._debug_controller.set_channel_enable_mask(mask):
            self.status_message.emit(
                tr("Channel enable mask → 0x{mask:016X}", mask=mask),
                3000,
            )

    def _on_handshake_ready(self, hw_type: str):
        if self._active_hw_type != hw_type:
            self._state_store.clear()
            self._gnss_store.clear()
            self._active_hw_type = hw_type
        self.status_message.emit(tr("Profile ready: {hardware}", hardware=hw_type), 3000)
        if self._presentation_ready:
            self._apply_hardware_to_presentation(hw_type)
        self.profile_ready.emit(hw_type)
        if (
            self._customer_auto_debug
            and not self._debug_controller.enabled
            and self._debug_controller.pending_target is None
        ):
            QTimer.singleShot(0, lambda: self.request_debug_mode(True))

    def _apply_hardware_to_presentation(self, hw_type: str) -> None:
        if not self._presentation_ready:
            return
        set_translatable_text("Device: {hardware}", self._hw_label, hardware=hw_type)
        self._render_connection_phase()
        self._state_panel.set_hw_type(hw_type)
        self._dashboard.set_hw_type(hw_type)
        self._status_strip.set_hw_type(hw_type)
        self._chart.set_hw_type(hw_type)
        self._control_panel.set_hw_type(hw_type)
        self._attitude.try_load_device_model(hw_type)
        self._on_profile_changed_sync(hw_type)

    def _sync_presentation_from_state(self) -> None:
        """Render current session state after first engineering-page construction."""
        if not self._presentation_ready:
            return
        self._connect_btn.setEnabled(not self._is_connected)
        self._disconnect_btn.setEnabled(self._is_connected)
        self._type_combo.setEnabled(
            self._customer_binding is None and not self._is_connected
        )
        self._control_panel.set_enabled(self._is_connected)
        self._update_debug_button_enabled()
        self._render_debug_button()
        self._render_connection_phase()
        hardware = self._profile_store.current_hw_type()
        if hardware:
            self._apply_hardware_to_presentation(hardware)
        self._gnss_btn.setEnabled(self._gnss_store.has_data())
        self._orbit_btn.setEnabled(
            bool(self._orbit_store.available and self._is_connected)
        )
        self._frame_count_label.setText(f"FRM {self._frame_count}")
        self._error_count_label.setText(f"ERR {self._error_count}")
        self._render_recording_state()
        self._update_display()
        self._update_heavy()

    # ----- 命令下发 -----

    def _start_product_subscription(self) -> None:
        if self._managed_runtime is not None:
            return
        self._sync_session_transport()
        self._product_subscription_controller.start()

    def _send_product_subscription(self) -> None:
        if self._managed_runtime is not None:
            return
        self._sync_session_transport()
        self._product_subscription_controller.send_now()

    def _retry_product_subscription(self) -> None:
        if self._managed_runtime is not None:
            return
        self._product_subscription_controller.retry()

    def _on_product_store_updated(self) -> None:
        self._try_start_armed_customer_recording()
        self._try_restore_customer_profile()

    # ---------- splitter 状态持久化（M10 F3b） ----------

    def _on_top_splitter_moved(self, *_args) -> None:
        if self._settings is None:
            return
        sizes = self._top_splitter.sizes()
        self._settings.set("ui.live_top_splitter_sizes", sizes)

    def _restore_splitter_state(self) -> None:
        if self._settings is None:
            return
        sizes = self._settings.get("ui.live_top_splitter_sizes", None)
        if isinstance(sizes, list) and len(sizes) == self._top_splitter.count() \
                and all(isinstance(s, int) and s >= 0 for s in sizes):
            self._top_splitter.setSizes(sizes)

    def _send_control_frame(
        self,
        frame: bytes,
        *,
        operation: SessionOperationClass = SessionOperationClass.MUTATING,
    ) -> bool:
        if not self._is_connected:
            self.status_message.emit(tr("Not connected; command was not sent"), 3000)
            return False
        if self._customer_binding is not None:
            operation = SessionOperationClass(operation)
            if operation is not SessionOperationClass.MUTATING:
                return bool(
                    self._customer_binding.command_sender.send(
                        frame,
                        operation=operation,
                    )
                )
            gateway = self.new_operation_gateway()
            owner = object()
            if gateway is None or not gateway.allows_unconfirmed_mutation():
                self.status_message.emit(
                    tr("This command has no device confirmation contract"),
                    3000,
                )
                return False
            if not gateway.try_acquire_operation(
                owner,
                purpose="live-direct-control",
            ):
                self.status_message.emit(tr("Another device operation is active"), 3000)
                return False
            try:
                return bool(
                    gateway.send(
                        frame,
                        operation=SessionOperationClass.MUTATING,
                    )
                )
            finally:
                gateway.release_operation(owner)
        if self._worker is None:
            self.status_message.emit(tr("Not connected; command was not sent"), 3000)
            return False
        self._sync_session_transport()
        return self._session_core.send(frame)

    def _sync_session_transport(self) -> None:
        if self._customer_binding is not None:
            self._debug_controller.set_connected(self._customer_binding.attached)
            return
        worker = self._worker
        if worker is None:
            self._debug_controller.set_connected(False)
            return
        if self._session_core.transport is not worker:
            self._session_core.attach_transport(worker, self._session_send)
        self._debug_controller.set_connected(self._is_connected)

    def _session_send(self, frame: bytes) -> bool:
        if self._customer_binding is not None:
            return bool(
                self._customer_binding.command_sender.send(
                    frame,
                    operation=SessionOperationClass.READ_ONLY_QUERY,
                )
            )
        worker = self._worker
        if worker is None:
            return False
        sent = bool(worker.send(frame))
        if (
            sent
            and self._recorder is not None
            and self._recorder.format_version == SDB_VERSION_V3
        ):
            self._recorder.write_control_frame(frame)
        return sent

    def _on_sample_rate_changed(self, hz: int) -> None:
        self._sync_session_transport()
        if self._debug_controller.set_sample_rate(hz):
            self.status_message.emit(tr("Requested sample rate: {rate} Hz", rate=hz), 2000)

    def _on_user_mark_requested(self, mark_id: int, text: str) -> None:
        self._sync_session_transport()
        if self._debug_controller.send_user_mark(mark_id, text):
            self.status_message.emit(tr("Mark #{mark_id} sent", mark_id=mark_id), 2000)

    def _on_reset_stats_requested(self) -> None:
        self._sync_session_transport()
        if self._debug_controller.reset_statistics():
            self.status_message.emit(tr("Requested device statistics reset"), 2000)

    def _on_dashboard_mode_requested(self, state_id: int, target_value: int) -> None:
        hw = self._profile_store.current_hw_type()
        if hw is None:
            self.status_message.emit(
                tr("Profile handshake is incomplete; mode change was not sent"),
                3000,
            )
            return
        binding = self._profile_store.get_state_control_binding(hw, state_id)
        if binding is None:
            self.status_message.emit(
                tr(
                    "state_id={state_id} has no control_binding; mode change was not sent",
                    state_id=state_id,
                ),
                3000,
            )
            return
        if (
            binding.subcmd == CONTROL_SUBCMD_SET_TRACE_MODE
            and binding.value_from == CONTROL_VALUE_FROM_ENUM_VALUE
        ):
            self._sync_session_transport()
            if self._debug_controller.set_trace_mode(target_value):
                self.status_message.emit(
                    tr(
                        "Requested mode change (state_id={state_id} → {target_value})",
                        state_id=state_id,
                        target_value=target_value,
                    ),
                    2000,
                )
        else:
            self.status_message.emit(
                tr(
                    "control_binding={subcmd}/{value_from} is not supported",
                    subcmd=binding.subcmd,
                    value_from=binding.value_from,
                ),
                3000,
            )

    # ----- EventLog → Chart -----

    def _on_event_added_for_chart(self, record: EventRecord) -> None:
        if not self._presentation_ready:
            return
        hw = self._profile_store.current_hw_type()
        if hw is None or record.hw_type != hw:
            return
        self._chart.add_event_marker(
            record.timestamp_ms, record.level,
            name=record.name, event_id=record.event_id,
        )

    # ----- profile 变化同步 -----

    def _channel_display_label(self, key: str) -> str:
        hw = self._profile_store.current_hw_type()
        if hw is None or not key.startswith("ch_"):
            return key
        try:
            cid = int(key.split("_", 1)[1])
        except (IndexError, ValueError):
            return key
        entry = self._profile_store.get_channel(hw, cid)
        if entry is None:
            return key
        return f"{entry.name} ({entry.unit})" if entry.unit else entry.name

    def _channel_group_title(self, key: str) -> str:
        """通道 key → 分组标题（用 profile group_id；无则"其它"）。"""
        hw = self._profile_store.current_hw_type()
        if hw is None or not key.startswith("ch_"):
            return ""
        try:
            cid = int(key.split("_", 1)[1])
        except (IndexError, ValueError):
            return ""
        entry = self._profile_store.get_channel(hw, cid)
        if entry is None:
            return ""
        return f"__profile_group_{entry.group_id}"

    def _on_channel_visibility_changed(self, name: str, checked: bool) -> None:
        """D6 P0：ChannelPanel 勾选 → 控制 chart 该曲线显隐。"""
        self._chart.set_channel_visible(name, checked)

    def _resync_channel_visibility(self) -> None:
        """曲线重建后把 ChannelPanel 当前勾选态重新套到曲线上。"""
        for name in self._channel_panel.channel_names():
            self._chart.set_channel_visible(name, self._channel_panel.is_checked(name))

    def _on_profile_changed_sync(self, hw_type: str) -> None:
        if not self._presentation_ready:
            return
        if hw_type:
            set_translatable_text("Device: {hardware}", self._hw_label, hardware=hw_type)
        # Profile 变化时刷新每条通道在 ChannelPanel 里显示的名字（带 unit）
        for key in self._channel_panel.channel_names():
            self._channel_panel.set_label(key, self._channel_display_label(key))
        hw = self._profile_store.current_hw_type()
        if hw is None:
            return
        self._gnss_btn.setEnabled(
            self._gnss_store.has_data()
            or self._profile_store.has_capability(hw, "gnss_sky_report")
            or self._profile_store.has_capability(hw, "gnss_cnr_report")
        )
        name_to_key: dict[str, str] = {}
        for ch in self._profile_store.get_channels(hw):
            name_to_key[ch.name.lower()] = f"ch_{ch.channel_id:02d}"
        if not name_to_key:
            return
        role_to_axis = {
            "roll": CHANNEL_ROLE_ROLL,
            "pitch": CHANNEL_ROLE_PITCH,
            "yaw": CHANNEL_ROLE_YAW,
            "ant_az": CHANNEL_ROLE_ANTENNA_AZ,
            "ant_el": CHANNEL_ROLE_ANTENNA_EL,
        }
        semantic_picks: dict[str, str] = {}
        for axis, role in role_to_axis.items():
            ch = self._profile_store.find_channel_by_role(hw, role)
            if ch is not None:
                semantic_picks[axis] = f"ch_{ch.channel_id:02d}"
        internal_yaw = self._profile_store.find_channel_by_role(hw, CHANNEL_ROLE_INTERNAL_INS_YAW)
        internal_yaw_key = "" if internal_yaw is None else f"ch_{internal_yaw.channel_id:02d}"
        sig = (
            tuple(sorted(name_to_key.items())),
            tuple(sorted(semantic_picks.items())),
            internal_yaw_key,
        )
        if getattr(self, "_attitude_bind_sig", None) == sig:
            return
        self._attitude_bind_sig = sig
        self._attitude.auto_bind_from_profile(name_to_key)
        fallback_roll, fallback_pitch, fallback_yaw = self._attitude.current_attitude_bindings()
        _, _, fallback_ant_az, fallback_ant_el = self._attitude.current_pointing_bindings()
        self._attitude_business_yaw_ch = semantic_picks.get("yaw", fallback_yaw)
        self._attitude_internal_yaw_ch = internal_yaw_key
        self._attitude.set_auto_bindings(
            roll=semantic_picks.get("roll", fallback_roll),
            pitch=semantic_picks.get("pitch", fallback_pitch),
            yaw=self._attitude_business_yaw_ch,
            ant_az=semantic_picks.get("ant_az", fallback_ant_az),
            ant_el=semantic_picks.get("ant_el", fallback_ant_el),
        )

    def _attitude_yaw_input(self, hw_type: Optional[str]) -> tuple[str, str]:
        business = getattr(self, "_attitude_business_yaw_ch", "")
        internal = getattr(self, "_attitude_internal_yaw_ch", "")
        if hw_type is None:
            return business, "legacy"

        reference_state = self._profile_store.find_state_by_role(
            hw_type, STATE_ROLE_INTERNAL_INS_YAW_REFERENCE
        )
        reference_value = None
        if reference_state is not None:
            reference_value = self._state_store.get_value(hw_type, reference_state.state_id)
        return select_attitude_yaw_channel(
            reference_state_defined=reference_state is not None,
            reference_value=reference_value,
            business_yaw_channel=business,
            internal_yaw_channel=internal,
        )

    def _on_link_lost(self):
        self.status_message.emit("Heartbeat timeout (link lost)", 5000)
        if self._presentation_ready:
            self._status_strip.set_link_state(connected=False)

    def _on_link_restored(self):
        self.status_message.emit("Heartbeat restored", 2000)
        if self._presentation_ready:
            self._status_strip.set_link_state(connected=True)

    def _on_disconnected(self):
        if self._customer_binding is not None:
            self._apply_managed_attachment_state(emit_signals=True)
            return
        # M9: 断开前通知设备关闭数据上报
        if self._debug_controller.enabled and self._worker is not None:
            self._debug_controller.send_shutdown_notice()
        self._is_connected = False
        self._orbit_sky_timer.stop()
        self._last_valid_frame_at = None
        self._device_activity_timer.stop()
        self._set_device_connection_phase(DeviceConnectionPhase.DISCONNECTED)
        self.connected_worker_changed.emit(None)
        self.connection_state_changed.emit(False)
        self._debug_controller.set_connected(False)
        if self._presentation_ready:
            self._connect_btn.setEnabled(True)
            self._disconnect_btn.setEnabled(False)
            self._debug_btn.setEnabled(False)
            set_translatable_text("Debug: OFF", self._debug_btn)
            set_translatable_text("Disconnected", self._conn_status_label)
            set_translatable_text("Device: {hardware}", self._hw_label, hardware="—")
            self._set_conn_state(False, dev="—")
            self._type_combo.setEnabled(True)
            self._control_panel.set_enabled(False)
            self._status_strip.set_link_state(connected=False)
        self._handshake_timer.stop()
        self._product_subscription_controller.stop()
        self._capture_profile_controller.reset()
        self._capture_restore_debug = False
        if self._is_recording:
            self._stop_recording(restore_customer_profile=False)
        elif self._customer_recording_state != CustomerRecordingState.IDLE:
            self._clear_customer_recording_intent()
        self._session_core.end_connection()
        self._gnss_store.clear()
        self._orbit_store.clear()
        self._reset_orbit_sky_request()
        self._state_store.clear()
        self._product_store.clear()
        self._active_hw_type = None
        self._active_connection_config = None
        if self._presentation_ready:
            self._gnss_btn.setEnabled(False)
            self._orbit_btn.setEnabled(False)

    def _on_debug_toggled(self):
        target = not self._debug_controller.enabled
        trace_message(
            "DBG_UI",
            f"CLICK DEBUG target={1 if target else 0} "
            f"connected={int(self._is_connected)} "
            f"pending={self._debug_controller.pending_target!r}",
        )
        if not self._is_connected:
            trace_message("DBG_UI", "CLICK DEBUG ignored reason=no_worker")
            return
        if self._debug_controller.pending_target is not None:
            trace_message("DBG_UI", "CLICK DEBUG ignored reason=request_pending")
            return
        self.request_debug_mode(target)

    @Slot(object)
    def _on_debug_pending_changed(self, target: object) -> None:
        self._update_debug_button_enabled()
        if not self._presentation_ready:
            return
        if target is None:
            self._render_debug_button()
            return
        label = "ON" if bool(target) else "OFF"
        set_translatable_text("Debug: {state}...", self._debug_btn, state=label)
        self._debug_btn.setCheckable(True)
        self._debug_btn.setChecked(bool(target))

    @Slot(bool)
    def _on_debug_state_changed(self, target: bool) -> None:
        self._update_debug_button_enabled()
        if target:
            hw = self._profile_store.current_hw_type()
            self._state_store.clear(hw)
        else:
            self._state_store.clear()
            self._gnss_store.clear()
        self._render_debug_button()
        self.debug_state_changed.emit(target)

    @Slot(bool, bool, str)
    def _on_debug_request_finished(self, target: bool, ok: bool, result: str) -> None:
        detail = result
        if result == DebugRequestResult.NOT_CONNECTED.value:
            detail = tr("Device is not connected")
        elif result == DebugRequestResult.BUSY.value:
            detail = tr("Another Debug command is awaiting confirmation")
        elif result == DebugRequestResult.SEND_FAILED.value:
            detail = tr("Failed to send Debug command")
        elif result == DebugRequestResult.TIMEOUT.value:
            detail = tr("Debug command not confirmed")
        elif result.startswith(f"{DebugRequestResult.DEVICE_ERROR.value}:"):
            raw = result.split(":", 1)[1]
            detail = tr("Debug command failed: {detail}", detail=raw)
        if not ok and detail:
            self.status_message.emit(detail, 3000)
        self.debug_request_finished.emit(target, ok, detail)

    def _update_debug_button_enabled(self) -> None:
        if not self._presentation_ready:
            return
        self._debug_btn.setEnabled(
            self._is_connected
            and not self._session_core.device_transaction_active
            and self._debug_controller.pending_target is None
        )

    def _render_debug_button(self) -> None:
        if not self._presentation_ready:
            return
        set_translatable_text(
            "Debug: {state}",
            self._debug_btn,
            state="ON" if self._debug_controller.enabled else "OFF",
        )
        self._debug_btn.setCheckable(True)
        self._debug_btn.setChecked(self._debug_controller.enabled)

    def _on_error(self, msg: str):
        self._error_count += 1
        if self._presentation_ready:
            self._error_count_label.setText(f"ERR {self._error_count}")
        self.status_message.emit(tr("Error: {detail}", detail=msg), 5000)

    def _on_data_received(self, data: bytes):
        if self._is_recording and self._recorder:
            self._recorder.write_frame(data)

        self._session_core.feed_bytes(data)

    @Slot(object)
    def _on_session_record(self, record: object) -> None:
        self.frame_received.emit(record)
        if isinstance(record, DataReport):
            self._frame_count += 1
            self._frame_times.append(datetime.now().timestamp())

    @Slot()
    def _status_strip_heartbeat(self) -> None:
        if self._presentation_ready:
            self._status_strip.pulse_heartbeat()

    # ============================ Timers ============================

    def activate_view(self) -> None:
        """Start engineering presentation updates without changing the session."""
        if self._view_active:
            return
        self.ensure_presentation()
        self._view_active = True
        self._update_display()
        self._update_heavy()
        self._update_timer.start()
        self._heavy_timer.start()

    def deactivate_view(self) -> None:
        """Suspend hidden engineering rendering and keep device I/O running."""
        if not self._view_active:
            return
        self._view_active = False
        self._update_timer.stop()
        self._heavy_timer.stop()

    def shutdown(self) -> bool:
        """Stop this view's owned transport without touching a managed Broker."""

        self.deactivate_view()
        if self._customer_binding is not None:
            if self._is_recording:
                if self._customer_recording:
                    return False
                if not self._stop_recording(restore_customer_profile=False):
                    return False
            if self._engineering_recorder_lease is not None:
                return False
            if (
                self._capture_profile_controller.recording_lease_active
                and self._customer_binding.attached
            ):
                return False
            if self._customer_recording_state is not CustomerRecordingState.IDLE:
                return False
            self._managed_subscription_timer.stop()
            self._orbit_sky_timer.stop()
            self._handshake_timer.stop()
            self._device_activity_timer.stop()
            self._apply_managed_attachment_state(emit_signals=True)
            return True
        stopped = self._shutdown_transport_worker()
        if not stopped:
            self._on_error(tr("Transport worker did not stop"))
        return stopped

    def prepare_session_shutdown(self) -> bool:
        """Bound local recorder close and release an unconfirmed profile request."""

        if self._customer_binding is None:
            return False
        if self._is_recording and not self._customer_recording:
            if not self._stop_recording(restore_customer_profile=False):
                return False
            return self._engineering_recorder_lease is None
        if self._is_recording and self._customer_recording:
            if not self._stop_recording(restore_customer_profile=True):
                return False
        if not self._capture_profile_controller.recording_lease_active:
            if self._customer_recording_state is CustomerRecordingState.ARMED:
                self._clear_customer_recording_intent()
            return self._customer_recording_state is CustomerRecordingState.IDLE
        self._capture_profile_controller.reset()
        self._customer_capture_confirmed = False
        self._capture_restore_debug = False
        self._customer_recording_path = None
        self._set_customer_recording_state(CustomerRecordingState.IDLE)
        return True

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self.shutdown():
            event.ignore()
            return
        super().closeEvent(event)

    def _update_heavy(self):
        self._chart.refresh(self._data_store)
        self._dashboard.refresh(self._data_store)
        self._state_panel.refresh_channel_values()

    def _update_display(self):
        current_time = datetime.now().timestamp()
        self._frame_times = [t for t in self._frame_times if current_time - t < 1.0]
        fps = len(self._frame_times)
        self._fps_label.setText(f"FPS {fps}")

        channels = self._data_store.get_all_channels()
        self._channel_count_label.setText(f"CH {len(channels)}")
        self._frame_count_label.setText(f"FRM {self._frame_count}")

        hw = self._profile_store.current_hw_type()
        if hw is not None and self._is_connected and self._debug_controller.enabled:
            self._state_store.expire_stale(hw, 3.5)

        existing_names = set(self._channel_panel.channel_names())
        channel_set = set(channels)
        new_names = channel_set - existing_names
        # 新增通道：颜色按现有数量取 COLORS 索引（保证稳定）；按 profile group 分组
        for name in sorted(new_names):
            idx = len(self._channel_panel.channel_names())
            color = COLORS[idx % len(COLORS)]
            self._channel_panel.add_channel(
                name, color, self._channel_display_label(name),
                group=self._channel_group_title(name),
            )
        # 消失的通道：从 panel 移除
        for name in existing_names - channel_set:
            self._channel_panel.remove_channel(name)
        # 最新值刷新到 panel
        for name in self._channel_panel.channel_names():
            ch = self._data_store.get_channel(name)
            latest = ch.get_latest() if ch else None
            if latest:
                self._channel_panel.update_value(name, f"{latest[1]:.2f}")

        def _latest(ch_name: str):
            if not ch_name:
                return None
            ch = self._data_store.get_channel(ch_name)
            if ch is None:
                return None
            latest = ch.get_latest()
            return latest[1] if latest else None

        roll_ch, pitch_ch, _ = self._attitude.get_channel_selections()
        yaw_ch, yaw_reference = self._attitude_yaw_input(hw)
        self._attitude.set_yaw_reference(yaw_reference)
        roll_val = _latest(roll_ch) or 0.0
        pitch_val = _latest(pitch_ch) or 0.0
        yaw_val = _latest(yaw_ch) or 0.0
        self._attitude.update_attitude(
            roll_val, pitch_val, yaw_val, roll_ch, pitch_ch, yaw_ch
        )

        tgt_az_ch, tgt_el_ch, ant_az_ch, ant_el_ch = (
            self._attitude.get_pointing_selections()
        )
        self._attitude.update_pointing(
            _latest(tgt_az_ch), _latest(tgt_el_ch),
            _latest(ant_az_ch), _latest(ant_el_ch),
        )

    # ============================ 录制 / 导入 / 清空 ============================

    def _on_record_clicked(self):
        if self._is_recording:
            self._stop_recording(restore_customer_profile=self._customer_recording)
            return
        if self._customer_recording_state != CustomerRecordingState.IDLE:
            self.status_message.emit(
                tr("A customer recording operation is already pending"),
                3000,
            )
            return
        self._start_recording(format_version=2, customer=False)

    def _set_customer_recording_state(
        self, state: CustomerRecordingState
    ) -> None:
        if self._customer_recording_state == state:
            return
        self._customer_recording_state = state
        path = "" if self._customer_recording_path is None else str(
            self._customer_recording_path
        )
        self.customer_recording_state_changed.emit(state.value, path)

    def _clear_customer_recording_intent(self) -> None:
        self._capture_profile_controller.reset()
        self._capture_restore_debug = False
        self._customer_capture_confirmed = False
        self._customer_recording_path = None
        self._set_customer_recording_state(CustomerRecordingState.IDLE)

    def _try_start_armed_customer_recording(self) -> None:
        state = self._customer_recording_state
        if state not in {
            CustomerRecordingState.ARMED,
            CustomerRecordingState.ACTIVE,
        }:
            return
        if not self.is_device_online() or self._capture_pending_id is not None:
            return
        capabilities = self._product_store.capabilities_record
        if capabilities is None:
            return
        if not (capabilities.capture_profile_mask & 0x02):
            if state == CustomerRecordingState.ARMED:
                self.status_message.emit(
                    tr(
                        "Connected device firmware does not support full support recording"
                    ),
                    4000,
                )
                self._clear_customer_recording_intent()
            return
        if state == CustomerRecordingState.ACTIVE and self._customer_capture_confirmed:
            return

        if state == CustomerRecordingState.ARMED:
            self._capture_restore_debug = self._debug_controller.enabled
            self._set_customer_recording_state(CustomerRecordingState.PREPARING)
        self._request_capture_profile(True)

    def _try_restore_customer_profile(self) -> None:
        if (
            self._customer_recording_state == CustomerRecordingState.RESTORING
            and self.is_device_online()
            and self._capture_pending_id is None
        ):
            self._request_capture_profile(False)

    def _request_capture_profile(self, support_full: bool) -> bool:
        self._sync_session_transport()
        if not self._capture_profile_controller.request(support_full):
            return False
        self.status_message.emit(
            tr("Preparing full support recording...")
            if support_full
            else tr("Restoring customer live stream..."),
            0 if support_full else 2500,
        )
        return True

    def _on_capture_response(self, response) -> None:
        self._capture_profile_controller.feed_response(response)

    @Slot(bool, bool, str)
    def _on_capture_profile_finished(
        self,
        target: bool,
        ok: bool,
        result: str,
    ) -> None:
        if not ok:
            if result == CaptureProfileResult.SEND_FAILED.value:
                self.status_message.emit(
                    tr("Capture profile command could not be sent"),
                    3500,
                )
            elif result == CaptureProfileResult.TIMEOUT.value:
                self.status_message.emit(
                    tr("Full support recording was not confirmed")
                    if target
                    else tr("Customer stream restore was not confirmed"),
                    4500,
                )
                if (
                    target
                    and self._customer_recording_state
                    == CustomerRecordingState.PREPARING
                ):
                    self._set_customer_recording_state(
                        CustomerRecordingState.RESTORING
                    )
                    if self.is_device_online():
                        self._request_capture_profile(False)
                elif target:
                    self._customer_capture_confirmed = False
                else:
                    self._capture_restore_debug = False
                    self._customer_recording_path = None
                    self._set_customer_recording_state(CustomerRecordingState.IDLE)
                return
            else:
                code = result.split(":", 1)[1] if ":" in result else result
                self.status_message.emit(
                    tr("Device rejected the capture profile ({code})", code=code),
                    4500,
                )
            if (
                target
                and self._customer_recording_state
                == CustomerRecordingState.PREPARING
            ):
                self._clear_customer_recording_intent()
            elif target:
                self._customer_capture_confirmed = False
            else:
                self._capture_restore_debug = False
                self._customer_recording_path = None
                self._set_customer_recording_state(CustomerRecordingState.IDLE)
            return

        if target:
            self._customer_capture_confirmed = True
            if self._customer_recording_state == CustomerRecordingState.PREPARING:
                path = self._customer_recording_path
                if path is None or not self._start_recording(
                    format_version=SDB_VERSION_V3,
                    customer=True,
                    filepath=path,
                ):
                    self._set_customer_recording_state(
                        CustomerRecordingState.RESTORING
                    )
                    self._request_capture_profile(False)
            elif self._customer_recording_state == CustomerRecordingState.ACTIVE:
                self.status_message.emit(
                    tr("Full support recording resumed"),
                    2500,
                )
        else:
            self.status_message.emit(tr("Customer live stream restored"), 2500)
            self._customer_capture_confirmed = False
            self._customer_recording_path = None
            self._set_customer_recording_state(CustomerRecordingState.IDLE)
            self._restore_debug_after_capture()

    def _on_product_control_response(self, response) -> None:
        self._product_subscription_controller.feed_response(response)

    def _on_capture_timeout(self) -> None:
        self._capture_profile_controller.expire()

    def _restore_debug_after_capture(self) -> None:
        restore_debug = self._capture_restore_debug
        self._capture_restore_debug = False
        if not restore_debug or not self._is_connected:
            return

        # A confirmed customer-live profile has set device Debug OFF. Reflect
        # that state before using the normal strict-ACK path to restore Debug ON.
        if self._debug_controller.enabled:
            self._debug_controller.apply_confirmed_state(False)
        QTimer.singleShot(0, lambda: self.request_debug_mode(True))

    def _recording_profile_dict(self):
        hw = self._profile_store.current_hw_type()
        if hw is None:
            return None
        profile = self._profile_store.get_profile(hw)
        if profile is None:
            return None
        from satellite_debug_tool.core.profile.cache import profile_to_dict

        return profile_to_dict(profile)

    @staticmethod
    def _product_value(value):
        return value.value if value.value is not None else None

    def _customer_recording_metadata(self) -> dict:
        snapshot = self._product_store.snapshot()
        metadata = {
            "capture_profile": "support_full",
            "presentation": "customer",
            "hardware_type": self._profile_store.current_hw_type(),
            "identity": {
                "model": self._product_value(snapshot.identity.model),
                "serial_number": self._product_value(snapshot.identity.serial_number),
                "main_firmware": self._product_value(snapshot.identity.main_firmware),
                "boot_firmware": self._product_value(snapshot.identity.boot_firmware),
                "protocol_version": self._product_value(snapshot.identity.protocol_version),
            },
        }
        endpoint = self.fixed_endpoint
        if endpoint is not None:
            metadata["endpoint"] = {
                "ip": endpoint[0],
                "port": endpoint[1],
            }
        return metadata

    def _engineering_recording_metadata(self) -> Optional[dict]:
        endpoint = self.fixed_endpoint
        if endpoint is None:
            return None
        return {
            "capture_profile": "engineering_current_stream",
            "presentation": "engineering_shared_udp",
            "hardware_type": self._profile_store.current_hw_type(),
            "endpoint": {"ip": endpoint[0], "port": endpoint[1]},
        }

    def _choose_recording_path(self, *, customer: bool) -> Optional[Path]:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = "support" if customer else "recording"
        endpoint = self.fixed_endpoint
        if endpoint is not None:
            endpoint_suffix = f"_{endpoint[0].replace('.', '-')}_{endpoint[1]}"
            prefix = f"{prefix}{endpoint_suffix}"
        default_name = f"{prefix}_{timestamp}.sdb"
        rec_dir = self._settings.get("paths.recording_dir", "") or ""
        if rec_dir:
            initial = str(Path(rec_dir) / default_name)
        else:
            initial = default_name
        filepath, _ = QFileDialog.getSaveFileName(
            self,
            tr("Save recording"),
            initial,
            tr("SDB files (*.sdb);;All files (*)"),
        )
        if not filepath:
            return None
        return Path(filepath)

    def _render_recording_state(self) -> None:
        if not self._presentation_ready:
            return
        self._status_strip.set_recording(self._is_recording)
        set_translatable_text("Stop" if self._is_recording else "Record", self._record_btn)
        try:
            from satellite_debug_tool.ui import icons as _ic

            color_key = "err" if self._is_recording else "text_2"
            self._record_btn.setIcon(
                _ic.icon(
                    "record",
                    color=S.palette(self._theme)[color_key],
                    size=14,
                )
            )
        except Exception:
            pass
        if self._is_recording:
            pal = S.palette(self._theme)
            self._record_btn.setStyleSheet(
                f"QPushButton {{ background-color: {pal['card_2']}; color: {pal['err']}; "
                f"border: 1px solid {pal['err']}; border-radius: 5px; padding: 4px 11px; "
                f"font-weight: 600; }}"
            )
        else:
            self._record_btn.setStyleSheet("")

    def _start_recording(
        self,
        *,
        format_version: int,
        customer: bool,
        filepath: Optional[Path] = None,
    ) -> bool:
        target = filepath or self._choose_recording_path(customer=customer)
        if target is None:
            return False

        engineering_lease: SessionRecorderLease | None = None
        if not customer and self._customer_binding is not None:
            gateway = self.new_operation_gateway()
            if gateway is None:
                return False
            engineering_lease = gateway.acquire_recorder(
                self._engineering_recorder_owner,
                kind=SessionRecorderKind.ENGINEERING_PASSIVE,
            )
            if engineering_lease is None:
                self.status_message.emit(
                    tr("This device recorder is busy"),
                    3500,
                )
                return False

        profile_dict = self._recording_profile_dict()
        metadata = (
            self._customer_recording_metadata()
            if customer
            else self._engineering_recording_metadata()
        )
        try:
            reservation = RecordingPathRegistry.default().reserve_unique(target)
        except (OSError, RecordingPathError) as exc:
            if engineering_lease is not None:
                engineering_lease.release()
            self.status_message.emit(
                tr("Failed to reserve recording path: {detail}", detail=str(exc)),
                3500,
            )
            return False
        actual_target = reservation.path
        recorder_constructed = False
        try:
            self._recorder = DataRecorder(
                actual_target,
                profile_dict=profile_dict,
                format_version=format_version,
                metadata=metadata,
            )
            recorder_constructed = True
            started = self._recorder.start(reservation)
        except (OSError, RuntimeError, ValueError):
            if not recorder_constructed:
                reservation.discard_failed_file()
            started = False
        if not started:
            self._recorder = None
            if engineering_lease is not None:
                engineering_lease.release()
            self.status_message.emit(tr("Failed to start recording"), 3000)
            return False

        self._engineering_recorder_lease = engineering_lease
        self._is_recording = True
        self._customer_recording = customer
        if customer:
            self._customer_recording_path = Path(actual_target)
            self._set_customer_recording_state(CustomerRecordingState.ACTIVE)
        self._render_recording_state()
        self.status_message.emit(
            tr(
                "Recording to {path}{profile_suffix}",
                path=actual_target,
                profile_suffix=tr(" + Profile") if profile_dict else "",
            ),
            3000,
        )
        self.recording_state_changed.emit(True, str(actual_target))
        return True

    def record_external_power_sample(
        self,
        sample: object,
        *,
        device_id: str = "",
        endpoint: tuple[str, int] | None = None,
    ) -> bool:
        """Append one device-bound external-power sample to customer SDB v3."""

        if (
            not self._is_recording
            or not self._customer_recording
            or self._recorder is None
            or self._recorder.format_version != SDB_VERSION_V3
        ):
            return False
        metadata_builder = getattr(sample, "metadata_event", None)
        timestamp_ns = getattr(sample, "host_timestamp_ns", None)
        if metadata_builder is None or timestamp_ns is None:
            return False
        event = dict(metadata_builder())
        if device_id:
            event["device_id"] = str(device_id)
        if endpoint is not None:
            event["device_endpoint"] = f"{endpoint[0]}:{endpoint[1]}"
        return self._recorder.write_metadata_event(
            event,
            host_timestamp_ns=int(timestamp_ns),
        )

    def record_external_power_action(
        self,
        outcome: object,
        *,
        device_id: str = "",
        endpoint: tuple[str, int] | None = None,
    ) -> bool:
        """Append one device-bound external-power control result to customer SDB v3."""

        if (
            not self._is_recording
            or not self._customer_recording
            or self._recorder is None
            or self._recorder.format_version != SDB_VERSION_V3
        ):
            return False
        metadata_builder = getattr(outcome, "metadata_event", None)
        if metadata_builder is None:
            return False
        event = dict(metadata_builder())
        sample = getattr(outcome, "sample", None)
        timestamp_ns = getattr(sample, "host_timestamp_ns", None) or time.time_ns()
        if device_id:
            event["device_id"] = str(device_id)
        if endpoint is not None:
            event["device_endpoint"] = f"{endpoint[0]}:{endpoint[1]}"
        return self._recorder.write_metadata_event(
            event,
            host_timestamp_ns=int(timestamp_ns),
        )

    def _stop_recording(self, *, restore_customer_profile: bool) -> bool:
        recorder = self._recorder
        was_customer = self._customer_recording
        try:
            stopped = recorder.stop() if recorder is not None else True
            dropped = recorder.dropped_count if recorder is not None else 0
        except (OSError, RuntimeError):
            stopped = False
            dropped = recorder.dropped_count if recorder is not None else 0
        if not stopped:
            self.status_message.emit(
                tr("Recording stop did not complete cleanly"),
                4000,
            )
            return False

        self._recorder = None
        self._is_recording = False
        self._customer_recording = False
        self._render_recording_state()
        message = (
            tr("Recording stopped with {count} dropped chunk(s)", count=dropped)
            if dropped
            else tr("Recording stopped")
        )
        self.status_message.emit(message, 4000)
        self.recording_state_changed.emit(False, "")
        if not was_customer:
            lease, self._engineering_recorder_lease = (
                self._engineering_recorder_lease,
                None,
            )
            if lease is not None and not lease.release():
                self._engineering_recorder_lease = lease
                return False
            return True

        self._customer_capture_confirmed = False
        if restore_customer_profile:
            self._set_customer_recording_state(CustomerRecordingState.RESTORING)
            if self.is_device_online():
                self._request_capture_profile(False)
        else:
            self._capture_restore_debug = False
            self._customer_recording_path = None
            self._set_customer_recording_state(CustomerRecordingState.IDLE)
        return True

    def _on_import_clicked(self):
        """M7：此入口保留向后兼容；新建议用回放 Tab 独立 DataStore。"""
        last_dir = self._settings.get("paths.recording_dir", "") or ""
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            tr("Import data into Live"),
            last_dir,
            tr("SDB files (*.sdb);;All files (*)"),
        )
        if not filepath:
            return
        try:
            sdb = DataImporter.open_sdb(filepath)
            if sdb.profile is not None:
                hw_type = self._profile_store.import_dict(sdb.profile)
                if hw_type is not None:
                    self._state_panel.set_hw_type(hw_type)
                    self._dashboard.set_hw_type(hw_type)
                    self._status_strip.set_hw_type(hw_type)
                    self._chart.set_hw_type(hw_type)
                    self._control_panel.set_hw_type(hw_type)

            self._data_store.clear()
            self._state_store.clear()
            self._gnss_store.clear()
            hw = self._profile_store.current_hw_type()
            data_count = 0
            for rec in sdb.iter_records():
                if isinstance(rec, DataReport):
                    self._data_store.update(rec)
                    data_count += 1
                elif hw is not None and isinstance(rec, StateReport):
                    self._state_store.update(hw, rec)
                elif hw is not None and isinstance(rec, EventReport):
                    self._event_log.add(hw, rec, self._profile_store)
                elif isinstance(rec, (GnssSkyReport, GnssCnrReport, GnssSatReport, GnssSignalReport)):
                    self._gnss_store.update(rec)
            self._frame_count += data_count
            self._chart.set_auto_range(True)
            self.status_message.emit(
                tr(
                    "Imported {count} DataReport record(s) from {path}",
                    count=data_count,
                    path=filepath,
                ),
                3000,
            )
        except Exception as exc:
            self.status_message.emit(tr("Import failed: {detail}", detail=exc), 5000)
            return

    @Slot()
    def clear_display_data(self) -> None:
        """Clear local presentation data without changing the device session."""

        self._data_store.clear()
        self._event_log.clear()
        self._state_store.clear()
        self._gnss_store.clear()
        self._product_store.clear_history()
        self._frame_count = 0
        self._error_count = 0
        self._frame_times.clear()

        if self._presentation_ready:
            self._chart.clear()
            self._attitude.clear()
            self._dashboard.refresh(self._data_store)

            if hasattr(self._event_timeline, "_list"):
                self._event_timeline._list.clear()
                if hasattr(self._event_timeline, "_update_count"):
                    self._event_timeline._update_count()

            for name in list(self._channel_panel.channel_names()):
                self._channel_panel.remove_channel(name)

            self._channel_count_label.setText("CH 0")
            self._frame_count_label.setText("FRM 0")
            self._error_count_label.setText("ERR 0")
        self.status_message.emit(tr("Display cleared"), 2000)

    def _on_gnss_store_changed(self) -> None:
        if self._presentation_ready and self._gnss_store.has_data():
            self._gnss_btn.setEnabled(True)

    @Slot(bool)
    def _on_orbit_capability_changed(self, available: bool) -> None:
        enabled = bool(available and self._is_connected)
        if self._presentation_ready:
            self._orbit_btn.setEnabled(enabled)
        self.orbit_capability_changed.emit(enabled)
        self._sync_orbit_sky_timer()
        if self._orbit_sky_timer.isActive():
            QTimer.singleShot(0, self.request_orbit_sky_refresh)

    @Slot(object)
    def _on_orbit_store_changed(self, report: object) -> None:
        if isinstance(report, OrbitCapabilitiesReport):
            self._sync_orbit_sky_timer()
            return
        if isinstance(report, OrbitStatusReport) and report.operation == OrbitOperation.SKY_SNAPSHOT:
            if report.request_id == self._orbit_sky_request_id:
                self._reset_orbit_sky_request()
            return
        if isinstance(report, OrbitStatusReport) and report.operation == OrbitOperation.SELECT:
            if report.status == OrbitStatus.OK:
                self.status_message.emit(tr("Tracking target accepted; TX state was not changed"), 3500)
            else:
                self.status_message.emit(
                    tr("Orbit request {operation} failed: {status}", operation=report.operation.name,
                       status=report.status.name),
                    5000,
                )
            return
        if (
            not isinstance(report, OrbitSkyReport)
            or not self._orbit_sky_request_active
            or report.request_id != self._orbit_sky_request_id
        ):
            return
        if self._orbit_sky_snapshot_id == 0:
            if report.page != 0:
                self._reset_orbit_sky_request()
                return
            self._orbit_sky_snapshot_id = report.snapshot_id
        if (
            report.snapshot_id != self._orbit_sky_snapshot_id
            or report.page != self._orbit_sky_expected_page
        ):
            self._reset_orbit_sky_request()
            return
        snapshot = self._orbit_store.sky_snapshot()
        if snapshot is not None and snapshot.snapshot_id == report.snapshot_id:
            self._reset_orbit_sky_request()
            return
        self._orbit_sky_expected_page += 1
        request_id = self.next_product_request_id()
        self._orbit_sky_request_id = request_id
        self._orbit_sky_request_started_at = time.monotonic()
        if not self._send_control_frame(
            build_orbit_sky_snapshot(
                request_id,
                self._orbit_sky_snapshot_id,
                self._orbit_sky_expected_page,
            ),
            operation=SessionOperationClass.READ_ONLY_QUERY,
        ):
            self._reset_orbit_sky_request()

    def _toggle_gnss(self) -> None:
        if self._gnss_dock is None:
            from satellite_debug_tool.ui.gnss_widget import GnssWidget
            self._gnss_widget = GnssWidget(self._gnss_store, playback=False)
            self._gnss_widget.set_theme(self._theme)
            self._gnss_dock = QDockWidget(
                tr("GNSS sky plot and signal-level C/N₀ — Live"),
                self,
            )
            self._gnss_dock.setAllowedAreas(Qt.NoDockWidgetArea)
            self._gnss_dock.setFloating(True)
            self._gnss_dock.setWidget(self._gnss_widget)
            self._gnss_dock.resize(1180, 700)
        self._gnss_dock.setVisible(not self._gnss_dock.isVisible())

    def _toggle_orbit(self) -> None:
        if not self._orbit_store.available:
            self.status_message.emit(tr("The connected firmware does not advertise Orbit/TLE support"), 3500)
            self.probe_orbit_capabilities()
            return
        if self._orbit_dock is None:
            from satellite_debug_tool.ui.orbit_widget import OrbitWidget

            self._orbit_widget = OrbitWidget(
                self._orbit_store,
                self._send_control_frame,
                self.next_product_request_id,
                self.is_connected,
                self.set_orbit_sky_consumer,
                self.request_orbit_sky_refresh,
            )
            self._orbit_widget.set_theme(self._theme)
            self._orbit_widget.status_message.connect(self.status_message)
            self._orbit_dock = QDockWidget(tr("Satellite catalog and orbit prediction — Live"), self)
            self._orbit_dock.setAllowedAreas(Qt.NoDockWidgetArea)
            self._orbit_dock.setFloating(True)
            self._orbit_dock.setWidget(self._orbit_widget)
            self._orbit_dock.resize(1180, 760)
        self._orbit_dock.setVisible(not self._orbit_dock.isVisible())

    # ============================ 主题应用 ============================

    def _apply_theme(self, theme: str):
        """统一主题分发（M7：字号固化 small）。"""
        if not self._presentation_ready:
            return
        scale = "small"
        pal = S.palette(theme)
        bg, panel, border, text, input_bg = (
            pal["bg"], pal["panel"], pal["border"], pal["text"], pal["input_bg"]
        )
        primary = pal["primary"]
        error = pal["error"]

        # 用 LiveView 选择器，避免 bare 声明 cascade 到子按钮（盖掉 variant 主按钮色）
        self.setStyleSheet(f"LiveView {{ background-color: {bg}; color: {text}; }}")

        def dispatch(w):
            if hasattr(w, "set_theme"):
                w.set_theme(theme, scale)
            elif hasattr(w, "set_dark_theme"):
                w.set_dark_theme(self._is_dark_theme)

        for w in (
            self._chart, self._attitude,
            self._status_strip, self._dashboard,
            self._state_panel, self._event_timeline,
            self._control_panel,
        ):
            dispatch(w)
        if self._gnss_widget is not None:
            dispatch(self._gnss_widget)
        if self._orbit_widget is not None:
            dispatch(self._orbit_widget)

        # 工具栏（用 QToolBar 选择器，避免 bare 声明 cascade 到子按钮）
        self._toolbar.setStyleSheet(
            f"QToolBar {{ background-color: {panel}; border: none; padding: 4px; }}"
        )
        try:
            from PySide6.QtGui import QFont
            fixed = QFont(self._toolbar.font())
            fixed.setPixelSize(12)
            self._toolbar.setFont(fixed)
            for child in self._toolbar.findChildren(QWidget):
                child.setFont(fixed)
        except Exception:
            pass
        self._toolbar_scroll.setFixedHeight(48)
        self._toolbar_scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {panel}; border: none; "
            f"border-bottom: 1px solid {border}; }}"
        )
        self._serial_widget.setStyleSheet("background-color: transparent;")
        self._udp_widget.setStyleSheet("background-color: transparent;")
        for w in self._serial_widget.findChildren(QLabel):
            w.setStyleSheet(f"color: {text}; background-color: transparent;")
        for w in self._udp_widget.findChildren(QLabel):
            w.setStyleSheet(f"color: {text}; background-color: transparent;")

        # 连接条按钮 + 输入控件：交给全局 QSS + variant（Mission Console）
        self._style_toolbar_buttons(theme)
        # 设备状态卡 + 统计行
        self._style_conn_card(pal, scale)

        # Channel panel：交给 ChannelPanel 自己刷主题
        self._channel_panel.apply_theme(theme, scale)

    def _style_conn_card(self, pal: dict, scale: str) -> None:
        """设备状态卡 + statline 的主题样式 + 当前连接态着色。"""
        if not self._presentation_ready:
            return
        mono = S.monospace_family()
        connected = bool(self._is_connected)
        tint = pal["ok"] if connected else pal["err"]
        tint_soft = pal["ok_soft"] if connected else pal["err_soft"]
        self._conn_card.setStyleSheet(
            f"#connCard {{ background-color: {pal['card']}; border: 1px solid {pal['border_2']}; "
            f"border-radius: 7px; }}"
            f"#connCard QLabel {{ background: transparent; }}"
            f"#csIcon {{ background-color: {tint_soft}; border-radius: 7px; }}"
            f"#csDev {{ color: {pal['text']}; font-family: \"{mono}\"; font-weight: 600; "
            f"font-size: {S.font_px(13, scale)}px; background: transparent; }}"
            f"#csStat {{ color: {pal['text_2']}; font-size: {S.font_px(10, scale)}px; "
            f"background: transparent; }}"
        )
        try:
            from satellite_debug_tool.ui import icons as _ic
            self._cs_icon.setPixmap(_ic.icon("satellite", color=tint, size=16).pixmap(16, 16))
        except Exception:
            pass
        # statline
        self._statline.setStyleSheet(
            f"#statline QLabel {{ color: {pal['text_3']}; font-family: \"{mono}\"; "
            f"font-size: {S.font_px(11, scale)}px; }}"
            f"#statErr {{ color: {pal['text_3']}; font-family: \"{mono}\"; "
            f"font-size: {S.font_px(11, scale)}px; }}"
        )

    def _set_conn_state(
        self,
        connected: bool,
        dev: str = "—",
        detail: str = "",
        *,
        detail_source: str = "",
    ) -> None:
        """更新设备状态卡显示（dev 名 + 状态文案）+ 重新着色。"""
        if not self._presentation_ready:
            return
        set_raw_text(dev or "—", self._cs_dev)
        if detail_source:
            set_translatable_text(detail_source, self._cs_stat)
        elif detail:
            set_raw_text(detail, self._cs_stat)
        elif connected:
            set_raw_text("LINK OK", self._cs_stat)
        else:
            set_translatable_text("Disconnected", self._cs_stat)
        self._style_conn_card(S.palette(self._theme), "small")

    def retranslate_ui(self) -> None:
        if not self._presentation_ready:
            return
        self._render_debug_button()
        set_translatable_text(
            "Stop" if self._is_recording else "REC",
            self._record_btn,
        )
        hardware = self._profile_store.current_hw_type() or "—"
        set_translatable_text("Device: {hardware}", self._hw_label, hardware=hardware)
        if not self._is_connected:
            set_translatable_text("Disconnected", self._conn_status_label)
            self._set_conn_state(False, dev="—")
        if self._gnss_dock is not None:
            self._gnss_dock.setWindowTitle(
                tr("GNSS sky plot and signal-level C/N₀ — Live")
            )
