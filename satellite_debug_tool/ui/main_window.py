from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QToolBar,
    QStatusBar,
    QLabel,
    QPushButton,
    QCheckBox,
    QSplitter,
    QScrollArea,
    QSizePolicy,
    QComboBox,
    QSpinBox,
    QLineEdit,
    QStackedWidget,
    QFileDialog,
)
from PySide6.QtCore import Qt, QTimer
from datetime import datetime
from satellite_debug_tool.core.comm import SerialWorker, UdpWorker
from satellite_debug_tool.core.protocol import (
    DataReport,
    EventReport,
    FrameReceiverV2,
    Heartbeat,
    StateReport,
    build_debug_enable_v2,
    build_reset_stats,
    build_set_sample_rate,
    build_set_trace_mode,
    build_user_mark,
)
from satellite_debug_tool.core.protocol.handshake import Handshake
from satellite_debug_tool.core.profile import ProfileCache, ProfileStore
from satellite_debug_tool.core.data import DataStore, EventLog, EventRecord, StateStore
from satellite_debug_tool.ui.state_panel_widget import StatePanelWidget
from satellite_debug_tool.ui.event_timeline_widget import EventTimelineWidget
from satellite_debug_tool.ui.dashboard_widget import DashboardWidget
from satellite_debug_tool.ui.status_strip_widget import StatusStripWidget
from satellite_debug_tool.ui.control_panel_widget import ControlPanelWidget
from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget
from satellite_debug_tool.io.data_recorder import DataRecorder
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.ui.chart_widget import COLORS   # COLORS 保留给通道选择面板使用
from satellite_debug_tool.ui.attitude_widget import AttitudeWidget
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.core.config import Settings


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._worker = None
        self._receiver = FrameReceiverV2()
        self._data_store = DataStore()
        self._profile_store = ProfileStore(cache=ProfileCache())
        self._state_store = StateStore()
        self._event_log = EventLog()
        # M3：事件到达时把标记绘制到分组曲线图上
        self._event_log.event_added.connect(self._on_event_added_for_chart)
        # profile 变化时同步刷新 channel panel 显示名、AttitudeWidget 自动绑定
        self._profile_store.profile_changed.connect(self._on_profile_changed_sync)
        self._handshake: Handshake | None = None   # 连接建立后创建
        self._handshake_timer = QTimer(self)
        self._handshake_timer.setInterval(100)     # 100ms 驱动握手/心跳检查
        self._handshake_timer.timeout.connect(self._on_handshake_tick)
        self._is_connected = False
        self._debug_enabled = False
        self._recorder = None
        self._is_recording = False
        self._frame_times = []
        self._is_dark_theme = True
        self._settings = Settings()
        self._setup_ui()
        self._load_settings()

    def _load_settings(self):
        conn_type = self._settings.get("general.connection_type", "Serial")
        self._type_combo.setCurrentText(conn_type)
        self._on_type_changed(conn_type)
        self._baudrate_combo.setCurrentText(
            self._settings.get("serial.default_baudrate", "115200")
        )
        theme = self._settings.get("ui.theme", "Dark")
        self._theme_combo.setCurrentText(theme)
        self._is_dark_theme = theme == "Dark"
        self._remote_ip.setText(self._settings.get("udp.remote_ip", "192.168.1.12"))
        self._remote_port.setValue(self._settings.get("udp.remote_port", 4004))
        self._local_port.setValue(self._settings.get("udp.local_port", 45678))

    def _setup_ui(self):
        self.setWindowTitle("Satellite Debug Tool")
        self.resize(1280, 800)
        self.setMinimumWidth(1024)
        self.setMinimumHeight(600)
        self.setStyleSheet(f"background-color: {S.BG_DARK}; color: {S.TEXT};")

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)

        self._toolbar = QToolBar()
        self._toolbar.setStyleSheet(
            f"background-color: {S.PANEL_DARK}; border: none; padding: 4px;"
        )
        self._toolbar.setMovable(False)
        self.addToolBar(self._toolbar)

        self._type_combo = QComboBox()
        self._type_combo.addItems(["Serial", "UDP"])
        self._type_combo.setFixedWidth(70)
        self._type_combo.setStyleSheet(
            f"background-color: #333; color: {S.TEXT}; border: none; padding: 4px; border-radius: 2px;"
        )
        self._type_combo.currentTextChanged.connect(self._on_type_changed)
        self._toolbar.addWidget(QLabel("Type:"))
        self._toolbar.addWidget(self._type_combo)

        self._config_stack = QStackedWidget()

        self._serial_widget = QWidget()
        serial_layout = QHBoxLayout(self._serial_widget)
        serial_layout.setContentsMargins(0, 0, 0, 0)
        serial_layout.setSpacing(4)

        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(80)
        self._port_combo.setStyleSheet(
            f"background-color: #333; color: {S.TEXT}; border: none; padding: 4px; border-radius: 2px;"
        )
        self._refresh_ports()
        serial_layout.addWidget(QLabel("Port:"))
        serial_layout.addWidget(self._port_combo)

        self._baudrate_combo = QComboBox()
        self._baudrate_combo.addItems(
            ["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"]
        )
        self._baudrate_combo.setCurrentText("115200")
        self._baudrate_combo.setStyleSheet(
            f"background-color: #333; color: {S.TEXT}; border: none; padding: 4px; border-radius: 2px;"
        )
        serial_layout.addWidget(QLabel("Baud:"))
        serial_layout.addWidget(self._baudrate_combo)

        self._config_stack.addWidget(self._serial_widget)

        self._udp_widget = QWidget()
        udp_layout = QHBoxLayout(self._udp_widget)
        udp_layout.setContentsMargins(0, 0, 0, 0)
        udp_layout.setSpacing(4)

        self._remote_ip = QLineEdit("192.168.1.12")
        self._remote_ip.setFixedWidth(100)
        self._remote_ip.setStyleSheet(
            f"background-color: #333; color: {S.TEXT}; border: none; padding: 4px; border-radius: 2px;"
        )
        udp_layout.addWidget(QLabel("Remote IP:"))
        udp_layout.addWidget(self._remote_ip)

        self._remote_port = QSpinBox()
        self._remote_port.setRange(1, 65535)
        self._remote_port.setValue(4004)
        self._remote_port.setFixedWidth(70)
        self._remote_port.setStyleSheet(
            f"background-color: #333; color: {S.TEXT}; border: none; padding: 4px; border-radius: 2px;"
        )
        udp_layout.addWidget(QLabel("Remote Port:"))
        udp_layout.addWidget(self._remote_port)

        self._local_port = QSpinBox()
        self._local_port.setRange(1, 65535)
        self._local_port.setValue(45678)
        self._local_port.setFixedWidth(70)
        self._local_port.setStyleSheet(
            f"background-color: #333; color: {S.TEXT}; border: none; padding: 4px; border-radius: 2px;"
        )
        udp_layout.addWidget(QLabel("Local Port:"))
        udp_layout.addWidget(self._local_port)

        self._config_stack.addWidget(self._udp_widget)

        self._toolbar.addWidget(self._config_stack)

        self._toolbar.addSeparator()

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setFixedSize(70, 28)
        self._connect_btn.setStyleSheet(
            f"background-color: {S.PRIMARY}; color: white; border: none; border-radius: 2px;"
        )
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        self._toolbar.addWidget(self._connect_btn)

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setFixedSize(80, 28)
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.setStyleSheet(
            f"background-color: {S.ERROR}; color: white; border: none; border-radius: 2px;"
        )
        self._disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        self._toolbar.addWidget(self._disconnect_btn)

        self._toolbar.addSeparator()

        self._debug_btn = QPushButton("Debug: OFF")
        self._debug_btn.setFixedSize(90, 28)
        self._debug_btn.setEnabled(False)
        self._debug_btn.setStyleSheet(
            f"background-color: #444; color: {S.TEXT}; border: none; border-radius: 2px;"
        )
        self._debug_btn.clicked.connect(self._on_debug_toggled)
        self._toolbar.addWidget(self._debug_btn)

        self._toolbar.addSeparator()

        self._record_btn = QPushButton("Record")
        self._record_btn.setFixedSize(70, 28)
        self._record_btn.setStyleSheet(
            f"background-color: {S.PRIMARY}; color: white; border: none; border-radius: 2px;"
        )
        self._record_btn.clicked.connect(self._on_record_clicked)
        self._toolbar.addWidget(self._record_btn)

        self._import_btn = QPushButton("Import")
        self._import_btn.setFixedSize(70, 28)
        self._import_btn.setStyleSheet(
            f"background-color: {S.PRIMARY}; color: white; border: none; border-radius: 2px;"
        )
        self._import_btn.clicked.connect(self._on_import_clicked)
        self._toolbar.addWidget(self._import_btn)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setFixedSize(60, 28)
        self._clear_btn.setStyleSheet(
            f"background-color: {S.PRIMARY}; color: white; border: none; border-radius: 2px;"
        )
        self._clear_btn.clicked.connect(self._on_clear_clicked)
        self._toolbar.addWidget(self._clear_btn)

        self._theme_combo = QComboBox()
        self._theme_combo.addItems(["Dark", "Light"])
        self._theme_combo.setFixedWidth(70)
        self._theme_combo.setStyleSheet(
            f"background-color: #333; color: {S.TEXT}; border: none; padding: 4px; border-radius: 2px;"
        )
        self._theme_combo.currentTextChanged.connect(self._on_theme_changed)
        self._toolbar.addWidget(QLabel("Theme:"))
        self._toolbar.addWidget(self._theme_combo)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._toolbar.addWidget(spacer)

        self._conn_status_label = QLabel("Disconnected")
        self._conn_status_label.setStyleSheet(
            f"color: {S.TEXT}; font-weight: bold; padding: 4px 8px;"
        )
        self._toolbar.addWidget(self._conn_status_label)

        # ==== M3: 顶部 StatusStrip（始终可见的链路/状态灯带） ====
        self._status_strip = StatusStripWidget(self._profile_store, self._state_store)
        layout.addWidget(self._status_strip)

        # ==== M3: Dashboard（KPI 卡片 + 模式按钮，profile 驱动） ====
        self._dashboard = DashboardWidget(self._profile_store, self._state_store)
        self._dashboard.mode_requested.connect(self._on_dashboard_mode_requested)
        layout.addWidget(self._dashboard)

        splitter = QSplitter(Qt.Vertical)

        # Horizontal splitter for chart + attitude side by side
        top_splitter = QSplitter(Qt.Horizontal)

        # M3: 分组曲线（按 profile.group_id 分子图）
        self._chart = GroupedChartWidget()
        self._chart.setMinimumHeight(400)
        self._chart.set_dark_theme(True)
        self._chart.set_profile_store(self._profile_store)
        top_splitter.addWidget(self._chart)

        self._attitude = AttitudeWidget()
        self._attitude.setMinimumWidth(350)
        self._attitude.set_dark_theme(True)
        self._attitude.channel_changed.connect(self._on_attitude_channel_changed)
        top_splitter.addWidget(self._attitude)

        # 右侧：状态灯板 + 事件时间线（M2 新增）
        right_panel = QSplitter(Qt.Vertical)
        right_panel.setMinimumWidth(260)
        self._state_panel = StatePanelWidget(self._profile_store, self._state_store)
        right_panel.addWidget(self._state_panel)
        self._event_timeline = EventTimelineWidget(self._event_log)
        right_panel.addWidget(self._event_timeline)
        right_panel.setStretchFactor(0, 1)
        right_panel.setStretchFactor(1, 1)
        top_splitter.addWidget(right_panel)

        top_splitter.setStretchFactor(0, 3)   # chart
        top_splitter.setStretchFactor(1, 1)   # attitude
        top_splitter.setStretchFactor(2, 1)   # state + event
        splitter.addWidget(top_splitter)

        channel_panel = QWidget()
        channel_panel.setObjectName("channel_panel")
        channel_panel.setStyleSheet(
            f"background-color: {S.PANEL_DARK}; border: 1px solid {S.BORDER}; border-radius: 2px;"
        )
        channel_layout = QVBoxLayout(channel_panel)
        channel_layout.setContentsMargins(8, 8, 8, 8)

        channel_header = QLabel("Channel Selection")
        channel_header.setStyleSheet(f"color: {S.TEXT}; font-weight: bold;")
        channel_layout.addWidget(channel_header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(f"background-color: {S.BG_DARK}; border: none;")

        self._channel_widget = QWidget()
        self._channel_widget.setStyleSheet("background-color: transparent;")
        self._channel_layout = QGridLayout(self._channel_widget)
        self._channel_layout.setContentsMargins(0, 0, 0, 0)
        self._channel_layout.setSpacing(6)
        self._channel_checks = {}
        self._channel_dots = {}
        self._channel_value_labels = {}
        self._channel_containers = {}
        self._channel_colors = {}

        self._scroll.setWidget(self._channel_widget)
        channel_layout.addWidget(self._scroll)
        channel_panel.setMinimumHeight(80)

        # M3: 控制面板（采样率 / USER_MARK / 复位统计）
        self._control_panel = ControlPanelWidget()
        self._control_panel.sample_rate_changed.connect(self._on_sample_rate_changed)
        self._control_panel.user_mark_requested.connect(self._on_user_mark_requested)
        self._control_panel.reset_stats_requested.connect(self._on_reset_stats_requested)

        splitter.addWidget(self._control_panel)
        splitter.addWidget(channel_panel)
        splitter.setStretchFactor(0, 4)   # top_splitter（曲线+姿态+状态）
        splitter.setStretchFactor(1, 0)   # control_panel（不拉伸）
        splitter.setStretchFactor(2, 1)   # channel_panel

        layout.addWidget(splitter)

        self._statusbar = QStatusBar()
        self._statusbar.setStyleSheet(
            f"background-color: {S.PANEL_DARK}; color: {S.TEXT};"
        )
        self.setStatusBar(self._statusbar)

        self._fps_label = QLabel("FPS: 0")
        self._channel_count_label = QLabel("Channels: 0")
        self._frame_count_label = QLabel("Total Frames: 0")
        self._error_count_label = QLabel("Errors: 0")

        self._statusbar.addPermanentWidget(self._fps_label)
        self._statusbar.addPermanentWidget(QLabel("  |  "))
        self._statusbar.addPermanentWidget(self._channel_count_label)
        self._statusbar.addPermanentWidget(QLabel("  |  "))
        self._statusbar.addPermanentWidget(self._frame_count_label)
        self._statusbar.addPermanentWidget(QLabel("  |  "))
        self._statusbar.addPermanentWidget(self._error_count_label)

        # 轻量 UI 刷新（FPS/计数/姿态 3D/通道数值）—— 10Hz
        self._update_timer = QTimer()
        self._update_timer.timeout.connect(self._update_display)
        self._update_timer.start(100)

        # 重绘制（分组曲线 / Dashboard 数值） —— 5Hz，拉开与 UI 事件循环的挤压，
        # 曲线 setData 只要看得清楚就够，避免每 100ms 刷 30000 点 ndarray
        # 导致拖拽/点击卡顿
        self._heavy_timer = QTimer()
        self._heavy_timer.timeout.connect(self._update_heavy)
        self._heavy_timer.start(200)

        self._frame_count = 0
        self._error_count = 0

        # Load attitude channel selections after attitude widget is created
        roll_ch = self._settings.get("attitude.roll_channel", "")
        pitch_ch = self._settings.get("attitude.pitch_channel", "")
        yaw_ch = self._settings.get("attitude.yaw_channel", "")
        if roll_ch:
            self._attitude._roll_combo.setCurrentText(roll_ch)
        if pitch_ch:
            self._attitude._pitch_combo.setCurrentText(pitch_ch)
        if yaw_ch:
            self._attitude._yaw_combo.setCurrentText(yaw_ch)

    def _on_type_changed(self, text):
        if text == "Serial":
            self._config_stack.setCurrentIndex(0)
        else:
            self._config_stack.setCurrentIndex(1)
        self._settings.set("general.connection_type", text)
        self._settings.save()

    def _refresh_ports(self):
        ports = SerialWorker.list_ports()
        self._port_combo.clear()
        if ports:
            self._port_combo.addItems(ports)
        else:
            self._port_combo.addItem("No ports")

    def _on_connect_clicked(self):
        conn_type = self._type_combo.currentText()
        if conn_type == "Serial":
            port = self._port_combo.currentText()
            if port == "No ports" or not port:
                self._statusbar.showMessage("No serial port available", 3000)
                return
            baudrate = int(self._baudrate_combo.currentText())
            config = {"type": "serial", "port": port, "baudrate": baudrate}
            self._worker = SerialWorker()
            self._conn_status_label.setText(f"{port} @ {baudrate}")
            self._settings.set(
                "serial.default_baudrate", self._baudrate_combo.currentText()
            )
            self._settings.set("serial.last_port", port)
            self._settings.save()
        else:
            config = {
                "type": "udp",
                "remote_ip": self._remote_ip.text(),
                "remote_port": self._remote_port.value(),
                "local_port": self._local_port.value(),
            }
            self._worker = UdpWorker()
            self._conn_status_label.setText(
                f"UDP {config['remote_ip']}:{config['remote_port']}"
            )
            self._settings.set("udp.remote_ip", self._remote_ip.text())
            self._settings.set("udp.remote_port", self._remote_port.value())
            self._settings.set("udp.local_port", self._local_port.value())
            self._settings.save()

        self._worker.connected.connect(self._on_connected)
        self._worker.disconnected.connect(self._on_disconnected)
        self._worker.error.connect(self._on_error)
        self._worker.data_received.connect(self._on_data_received)

        if self._worker.connect(config):
            self._is_connected = True
        else:
            self._conn_status_label.setText("Connection Failed")
            self._conn_status_label.setStyleSheet(f"color: {S.ERROR};")

    def _on_disconnect_clicked(self):
        self._debug_enabled = False
        self._debug_btn.setText("Debug: OFF")
        self._debug_btn.setEnabled(False)
        if self._worker:
            self._worker.disconnect()

    def _on_connected(self):
        self._is_connected = True
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._debug_btn.setEnabled(True)
        self._conn_status_label.setStyleSheet(f"color: {S.SUCCESS};")
        self._type_combo.setEnabled(False)

        # M3: 控制面板可用，链路指示置绿
        self._control_panel.set_enabled(True)
        self._status_strip.set_link_state(connected=True)

        # 启动握手：发 4 条 REQUEST，之后定时 tick 检查心跳/重发
        self._receiver.reset()
        if self._worker is not None:
            self._handshake = Handshake(self._profile_store, self._worker.send)
            self._handshake.ready.connect(self._on_handshake_ready)
            self._handshake.link_lost.connect(self._on_link_lost)
            self._handshake.link_restored.connect(self._on_link_restored)
            self._handshake.start()
            self._handshake_timer.start()

    def _on_handshake_tick(self):
        if self._handshake is not None:
            self._handshake.tick(self._handshake_timer.interval())

    def _on_handshake_ready(self, hw_type: str):
        self._statusbar.showMessage(f"Profile ready: {hw_type}", 3000)
        # 通知各 profile 驱动组件切换 hw_type 并按最新 profile 重建
        self._state_panel.set_hw_type(hw_type)
        self._dashboard.set_hw_type(hw_type)
        self._status_strip.set_hw_type(hw_type)
        self._chart.set_hw_type(hw_type)

    # ----- ControlPanel / Dashboard 发出的控制意图 -----

    def _send_control_frame(self, frame: bytes) -> bool:
        if self._worker is None or not self._is_connected:
            self._statusbar.showMessage("未连接，命令未发送", 3000)
            return False
        return bool(self._worker.send(frame))

    def _on_sample_rate_changed(self, hz: int) -> None:
        if self._send_control_frame(build_set_sample_rate(hz)):
            self._statusbar.showMessage(f"已请求采样率 {hz} Hz", 2000)

    def _on_user_mark_requested(self, mark_id: int, text: str) -> None:
        if self._send_control_frame(build_user_mark(mark_id, text)):
            self._statusbar.showMessage(f"Mark #{mark_id} 已发送", 2000)

    def _on_reset_stats_requested(self) -> None:
        if self._send_control_frame(build_reset_stats()):
            self._statusbar.showMessage("已请求下位机复位统计", 2000)

    def _on_dashboard_mode_requested(self, state_id: int, target_value: int) -> None:
        """Dashboard 枚举按钮：当前仅对 TRACE_MODE (afd01 state_id=0) 下发实际帧；
        其它状态字只显示意图，等协议扩展 SET_STATE 泛化子命令后再自动发。"""
        if state_id == 0:
            if self._send_control_frame(build_set_trace_mode(target_value)):
                self._statusbar.showMessage(
                    f"已请求切换模式（state_id={state_id} → {target_value}）", 2000,
                )
        else:
            self._statusbar.showMessage(
                f"state_id={state_id} 的模式切换暂未下发（协议待扩展）", 3000,
            )

    # ----- EventLog → Chart 垂直事件标记 -----

    def _on_event_added_for_chart(self, record: EventRecord) -> None:
        hw = self._profile_store.current_hw_type()
        if hw is None or record.hw_type != hw:
            return
        self._chart.add_event_marker(record.timestamp_ms, record.level)

    # ----- Channel panel / AttitudeWidget 的 profile 同步 -----

    def _channel_display_label(self, key: str) -> str:
        """把 DataStore 内部 key（`ch_00`）转成 profile 里的真名（含单位）。"""
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

    def _on_profile_changed_sync(self, _hw_type: str) -> None:
        """profile 任一表更新后，刷新 channel panel 标签 + 尝试 3D 自动绑定。

        幂等：profile 签名未变时只做 setText 无重绑定；
        （setText 相同字符串 Qt 不会触发 repaint，安全）
        """
        # 1) 刷新所有已存在 checkbox 的显示名
        for key, cb in self._channel_checks.items():
            new_label = self._channel_display_label(key)
            if cb.text() != new_label:
                cb.setText(new_label)

        # 2) 3D 自动绑定 —— 签名未变就跳过
        hw = self._profile_store.current_hw_type()
        if hw is None:
            return
        name_to_key: dict[str, str] = {}
        for ch in self._profile_store.get_channels(hw):
            name_to_key[ch.name.lower()] = f"ch_{ch.channel_id:02d}"
        if not name_to_key:
            return
        sig = tuple(sorted(name_to_key.items()))
        if getattr(self, "_attitude_bind_sig", None) == sig:
            return
        self._attitude_bind_sig = sig
        self._attitude.auto_bind_from_profile(name_to_key)

    def _on_link_lost(self):
        self._statusbar.showMessage("Heartbeat timeout (link lost)", 5000)
        self._status_strip.set_link_state(connected=False)

    def _on_link_restored(self):
        self._statusbar.showMessage("Heartbeat restored", 2000)
        self._status_strip.set_link_state(connected=True)

    def _on_disconnected(self):
        self._is_connected = False
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._debug_btn.setEnabled(False)
        self._debug_enabled = False
        self._debug_btn.setText("Debug: OFF")
        self._conn_status_label.setText("Disconnected")
        self._conn_status_label.setStyleSheet(f"color: {S.TEXT};")
        self._type_combo.setEnabled(True)

        # M3: 控制面板置灰、链路指示置红
        self._control_panel.set_enabled(False)
        self._status_strip.set_link_state(connected=False)

        # 握手停止，profile 缓存保留供下次连接复用
        self._handshake_timer.stop()
        if self._handshake is not None:
            self._handshake.stop()
            self._handshake = None

    def _on_debug_toggled(self):
        if not self._worker:
            return
        self._debug_enabled = not self._debug_enabled
        frame = build_debug_enable_v2(self._debug_enabled)
        if self._worker.send(frame):
            self._debug_btn.setText(f"Debug: {'ON' if self._debug_enabled else 'OFF'}")
            self._debug_btn.setStyleSheet(
                f"background-color: {S.SUCCESS if self._debug_enabled else '#444'}; color: white; border: none; border-radius: 2px;"
            )
        else:
            self._debug_enabled = not self._debug_enabled
            self._statusbar.showMessage("Failed to send debug command", 3000)

    def _on_error(self, msg: str):
        self._error_count += 1
        self._error_count_label.setText(f"Errors: {self._error_count}")
        self._statusbar.showMessage(f"Error: {msg}", 5000)

    def _on_data_received(self, data: bytes):
        # 录制：写原始字节流，保证 .sdb 里帧边界与协议一致
        if self._is_recording and self._recorder:
            self._recorder.write_frame(data)

        # 协议解码：v2 状态机每次吐一批 record，按类型分发
        records = self._receiver.feed(data)
        for rec in records:
            # 握手层消费 META/DEFINE/HEARTBEAT，其余类型忽略
            # 注意：META 帧处理后 current_hw_type 才有值，故分发时每次重取
            if self._handshake is not None:
                self._handshake.feed(rec)

            if isinstance(rec, DataReport):
                self._data_store.update(rec)
                self._frame_count += 1
                continue
            if isinstance(rec, Heartbeat):
                self._status_strip.pulse_heartbeat()
                continue

            hw = self._profile_store.current_hw_type()
            if hw is None:
                # META 未到，状态/事件无法归属 → 丢弃（5Hz 全量重发会补上）
                continue
            if isinstance(rec, StateReport):
                self._state_store.update(hw, rec)
            elif isinstance(rec, EventReport):
                self._event_log.add(hw, rec, self._profile_store)

    def _update_heavy(self):
        """重绘制：分组曲线 + Dashboard KPI。5Hz 足够，避免每 100ms 刷整份
        ndarray 导致 UI 响应卡顿。"""
        self._chart.refresh(self._data_store)
        self._dashboard.refresh(self._data_store)

    def _update_display(self):
        current_time = datetime.now().timestamp()
        self._frame_times.append(current_time)
        self._frame_times = [t for t in self._frame_times if current_time - t < 1.0]
        fps = len(self._frame_times)
        self._fps_label.setText(f"FPS: {fps}")

        channels = self._data_store.get_all_channels()
        self._channel_count_label.setText(f"Channels: {len(channels)}")
        self._frame_count_label.setText(f"Frames: {self._frame_count}")

        # Update attitude widget channel options when new channels appear
        self._attitude.set_channel_options(channels)

        existing_names = set(self._channel_checks.keys())
        new_names = set(channels) - existing_names

        pal = S.palette(self._is_dark_theme)
        for name in new_names:
            idx = len(self._channel_checks)
            cols = 8
            row = idx // cols
            col = idx % cols
            color = COLORS[idx % len(COLORS)]
            container = QWidget()
            container.setFixedHeight(32)
            container.setStyleSheet(
                f"background-color: {pal['panel']}; border-left: 4px solid {color}; "
                f"border-radius: 3px; padding: 4px 8px;"
            )
            container_layout = QHBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.setSpacing(4)
            dot = QLabel()
            dot.setFixedSize(10, 10)
            dot.setStyleSheet(f"background-color: {color}; border-radius: 50%;")
            cb = QCheckBox(self._channel_display_label(name))
            cb.setChecked(True)
            cb.setStyleSheet(
                f"color: {pal['text']}; border: none; padding: 0px 4px; background: transparent;"
            )
            value_label = QLabel("--")
            value_label.setStyleSheet(
                f"color: {pal['text']}; min-width: 60px; text-align: right; background: transparent;"
            )
            container_layout.addWidget(dot)
            container_layout.addWidget(cb)
            container_layout.addWidget(value_label)
            container_layout.addStretch()
            self._channel_layout.addWidget(container, row, col)
            self._channel_checks[name] = cb
            self._channel_dots[name] = dot
            self._channel_value_labels[name] = value_label
            self._channel_containers[name] = container
            self._channel_colors[name] = color

        for name in list(self._channel_checks.keys()):
            if name not in channels:
                container = self._channel_containers[name]
                self._channel_layout.removeWidget(container)
                container.deleteLater()
                del self._channel_checks[name]
                del self._channel_dots[name]
                del self._channel_value_labels[name]
                del self._channel_containers[name]

        # 通道数值标签：保留旧 UI 里"列出每通道最新数值"的功能
        for name, cb in self._channel_checks.items():
            if not cb.isChecked() or name not in channels:
                continue
            ch = self._data_store.get_channel(name)
            latest = ch.get_latest() if ch else None
            if latest:
                self._channel_value_labels[name].setText(f"{latest[1]:.2f}")

        # Update attitude widget with roll/pitch/yaw from selected channels
        roll_ch, pitch_ch, yaw_ch = self._attitude.get_channel_selections()
        roll_val = pitch_val = yaw_val = 0.0
        if roll_ch:
            ch = self._data_store.get_channel(roll_ch)
            if ch and ch.get_latest():
                roll_val = ch.get_latest()[1]
        if pitch_ch:
            ch = self._data_store.get_channel(pitch_ch)
            if ch and ch.get_latest():
                pitch_val = ch.get_latest()[1]
        if yaw_ch:
            ch = self._data_store.get_channel(yaw_ch)
            if ch and ch.get_latest():
                yaw_val = ch.get_latest()[1]
        self._attitude.update_attitude(roll_val, pitch_val, yaw_val, roll_ch, pitch_ch, yaw_ch)

    def _on_record_clicked(self):
        if self._is_recording:
            if self._recorder:
                self._recorder.stop()
                self._recorder = None
            self._is_recording = False
            self._status_strip.set_recording(False)
            self._record_btn.setText("Record")
            self._record_btn.setStyleSheet(
                f"background-color: {S.PRIMARY}; color: white; border: none; border-radius: 2px;"
            )
            self._statusbar.showMessage("Recording stopped", 3000)
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath, _ = QFileDialog.getSaveFileName(
                self,
                "Save Recording",
                f"recording_{timestamp}.sdb",
                "SDB Files (*.sdb);;All Files (*)",
            )
            if filepath:
                # M5: 录制时把当前 profile dict 嵌入 .sdb 文件头，回放时可脱机恢复 UI
                profile_dict = None
                hw = self._profile_store.current_hw_type()
                if hw is not None:
                    p = self._profile_store.get_profile(hw)
                    if p is not None:
                        from satellite_debug_tool.core.profile.cache import profile_to_dict
                        profile_dict = profile_to_dict(p)
                self._recorder = DataRecorder(filepath, profile_dict=profile_dict)
                if self._recorder.start():
                    self._is_recording = True
                    self._status_strip.set_recording(True)
                    self._record_btn.setText("Stop")
                    self._record_btn.setStyleSheet(
                        f"background-color: {S.ERROR}; color: white; border: none; border-radius: 2px;"
                    )
                    suffix = " + profile" if profile_dict else ""
                    self._statusbar.showMessage(f"Recording to {filepath}{suffix}", 3000)
                else:
                    self._recorder = None
                    self._statusbar.showMessage("Failed to start recording", 3000)

    def _on_import_clicked(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "Import Data",
            "",
            "SDB Files (*.sdb);;All Files (*)",
        )
        if not filepath:
            return
        try:
            sdb = DataImporter.open_sdb(filepath)

            # 1) 回放前先恢复 profile，让 UI 按文件内 profile 渲染
            if sdb.profile is not None:
                hw_type = self._profile_store.import_dict(sdb.profile)
                if hw_type is not None:
                    self._state_panel.set_hw_type(hw_type)
                    self._dashboard.set_hw_type(hw_type)
                    self._status_strip.set_hw_type(hw_type)
                    self._chart.set_hw_type(hw_type)

            # 2) 清空现有数据后灌入录制内容
            self._data_store.clear()
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
            self._frame_count += data_count
            self._chart.set_auto_range(True)
            self._statusbar.showMessage(
                f"Imported {data_count} DataReport(s) from {filepath}", 3000,
            )
        except Exception as exc:
            self._statusbar.showMessage(f"Import failed: {exc}", 5000)
            return

    def _on_clear_clicked(self):
        # 1) 数据/历史缓存
        self._data_store.clear()
        self._event_log.clear()
        self._frame_count = 0
        self._error_count = 0
        self._frame_times.clear()

        # 2) UI 组件
        self._chart.clear()                     # 清曲线数据 + 所有事件竖线
        self._attitude.clear()
        self._dashboard.refresh(self._data_store)   # 卡片刷新为 "—"
        # StatePanel / StatusStrip 不清（状态字保留当前值以便立即识别设备状态）

        # EventTimeline：列表视图手动清空（EventLog 已清，但 QListWidget 缓存需 rebuild）
        if hasattr(self._event_timeline, "_list"):
            self._event_timeline._list.clear()
            if hasattr(self._event_timeline, "_update_count"):
                self._event_timeline._update_count()

        # 3) 底部 Channel panel：完全重建，下一帧有数据再自动生成
        for name in list(self._channel_checks.keys()):
            cb = self._channel_checks[name]
            dot = self._channel_dots[name]
            value_label = self._channel_value_labels[name]
            container = self._channel_containers[name]
            self._channel_layout.removeWidget(container)
            cb.deleteLater()
            dot.deleteLater()
            value_label.deleteLater()
            container.deleteLater()
        self._channel_checks.clear()
        self._channel_dots.clear()
        self._channel_value_labels.clear()
        self._channel_containers.clear()
        self._channel_colors.clear()

        # 4) 状态栏计数
        self._channel_count_label.setText("Channels: 0")
        self._frame_count_label.setText("Frames: 0")
        self._error_count_label.setText("Errors: 0")
        self._statusbar.showMessage("Display cleared", 2000)

    def _on_attitude_channel_changed(self, channel_name: str, axis: str):
        """Save attitude channel selection when user changes it."""
        if axis == "roll":
            self._settings.set("attitude.roll_channel", channel_name)
        elif axis == "pitch":
            self._settings.set("attitude.pitch_channel", channel_name)
        elif axis == "yaw":
            self._settings.set("attitude.yaw_channel", channel_name)
        self._settings.save()

    def _on_theme_changed(self, theme: str):
        self._is_dark_theme = theme == "Dark"
        self._apply_stylesheet(theme)
        self._settings.set("ui.theme", theme)
        self._settings.save()

    def _apply_stylesheet(self, theme: str):
        if theme == "Dark":
            bg = S.BG_DARK
            panel = S.PANEL_DARK
            border = S.BORDER
            text = S.TEXT
            input_bg = S.INPUT_DARK
        else:
            bg = S.BG_LIGHT
            panel = S.PANEL_LIGHT
            border = S.BORDER_LIGHT
            text = S.TEXT_LIGHT
            input_bg = S.INPUT_LIGHT
        self.setStyleSheet(f"background-color: {bg}; color: {text};")
        self._chart.set_dark_theme(self._is_dark_theme)
        self._attitude.set_dark_theme(self._is_dark_theme)
        # M2/M3 新增 widget 的主题统一切换
        self._status_strip.set_dark_theme(self._is_dark_theme)
        self._dashboard.set_dark_theme(self._is_dark_theme)
        self._state_panel.set_dark_theme(self._is_dark_theme)
        self._event_timeline.set_dark_theme(self._is_dark_theme)
        self._control_panel.set_dark_theme(self._is_dark_theme)
        # StatusBar
        self._statusbar.setStyleSheet(f"background-color: {panel}; color: {text};")
        # 底部 channel panel
        if hasattr(self, "_scroll"):
            self._scroll.setStyleSheet(f"background-color: {bg}; border: none;")
        # 通道条目内的 checkbox / value_label 随主题刷新
        for name, cb in self._channel_checks.items():
            cb.setStyleSheet(f"color: {text}; border: none; padding: 0px 4px; background: transparent;")
        for lbl in self._channel_value_labels.values():
            lbl.setStyleSheet(
                f"color: {text}; min-width: 60px; text-align: right; background: transparent;"
            )
        for name, container in self._channel_containers.items():
            color = self._channel_colors.get(name, "#888888")
            container.setStyleSheet(
                f"background-color: {panel}; border-left: 4px solid {color}; "
                f"border-radius: 3px; padding: 4px 8px;"
            )
        self._toolbar.setStyleSheet(
            f"background-color: {panel}; border: none; padding: 4px;"
        )
        self._serial_widget.setStyleSheet(f"background-color: transparent;")
        self._udp_widget.setStyleSheet(f"background-color: transparent;")
        for w in self._serial_widget.findChildren(QLabel):
            w.setStyleSheet(f"color: {text}; background-color: transparent;")
        for w in self._udp_widget.findChildren(QLabel):
            w.setStyleSheet(f"color: {text}; background-color: transparent;")

        for widget in [
            self._type_combo,
            self._port_combo,
            self._baudrate_combo,
            self._remote_ip,
            self._remote_port,
            self._local_port,
        ]:
            widget.setStyleSheet(
                f"background-color: {input_bg}; "
                f"color: {text}; border: none; padding: 4px; border-radius: 2px;"
            )
        self._connect_btn.setStyleSheet(
            f"background-color: {S.PRIMARY if self._is_dark_theme else S.PRIMARY_LIGHT}; "
            "color: white; border: none; border-radius: 2px;"
        )
        self._disconnect_btn.setStyleSheet(
            f"background-color: {S.ERROR}; color: white; border: none; border-radius: 2px;"
        )
        self._debug_btn.setStyleSheet(
            f"background-color: {S.BUTTON_DARK if self._is_dark_theme else S.BUTTON_LIGHT}; "
            f"color: {text}; border: none; border-radius: 2px;"
        )
        self._record_btn.setStyleSheet(
            f"background-color: {S.PRIMARY if self._is_dark_theme else S.PRIMARY_LIGHT}; "
            "color: white; border: none; border-radius: 2px;"
        )
        self._import_btn.setStyleSheet(
            f"background-color: {S.PRIMARY if self._is_dark_theme else S.PRIMARY_LIGHT}; "
            "color: white; border: none; border-radius: 2px;"
        )
        self._clear_btn.setStyleSheet(
            f"background-color: {S.PRIMARY if self._is_dark_theme else S.PRIMARY_LIGHT}; "
            "color: white; border: none; border-radius: 2px;"
        )
        self._theme_combo.setStyleSheet(
            f"background-color: {input_bg}; "
            f"color: {text}; border: none; padding: 4px; border-radius: 2px;"
        )
        channel_panel = self.findChild(QWidget, "channel_panel")
        if channel_panel:
            channel_panel.setStyleSheet(
                f"background-color: {panel}; border: 1px solid {border}; border-radius: 2px;"
            )
        self._scroll.setStyleSheet(f"background-color: {bg}; border: none;")
        self._channel_widget.setStyleSheet(f"background-color: transparent;")
        for name, container in self._channel_containers.items():
            color = self._channel_colors.get(name, COLORS[0])
            container.setStyleSheet(
                f"background-color: {panel}; border-left: 4px solid {color}; border-radius: 3px; padding: 4px 8px;"
            )
        for cb in self._channel_checks.values():
            cb.setStyleSheet(f"color: {text}; border: none; padding: 0px 4px;")
        for label in self._channel_value_labels.values():
            label.setStyleSheet(f"color: {text}; min-width: 60px;")
        self._statusbar.setStyleSheet(f"background-color: {panel}; color: {text};")
