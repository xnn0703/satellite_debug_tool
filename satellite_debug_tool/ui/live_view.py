"""LiveView —— 实时连接 / 数据采集 / 显示 Tab。

从 MainWindow 抽出（M7-S4）。原 1189 行 MainWindow 的所有业务逻辑（worker /
receiver / handshake / data_store / state_store / event_log / 各 widget
持有 / 录制 / 主题切换分发）全部内化到此类。

MainWindow 现在只剩 QTabWidget 壳：顶部全局主题切换 + 三个 Tab(Live /
Playback / Log) + 共享 statusbar。LiveView 通过 ``status_message`` 信号把短
消息上报到 MainWindow statusbar；主题由 MainWindow 调 ``set_theme(theme)``
广播下来。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
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

from satellite_debug_tool.core.comm import SerialWorker, UdpWorker
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.data import DataStore, EventLog, EventRecord, StateStore
from satellite_debug_tool.core.profile import ProfileCache, ProfileStore
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
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.io.data_recorder import DataRecorder
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.attitude_widget import AttitudeWidget
from satellite_debug_tool.ui.chart_widget import COLORS
from satellite_debug_tool.ui.control_panel_widget import ControlPanelWidget
from satellite_debug_tool.ui.dashboard_widget import DashboardWidget
from satellite_debug_tool.ui.event_timeline_widget import EventTimelineWidget
from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget
from satellite_debug_tool.ui.state_panel_widget import StatePanelWidget
from satellite_debug_tool.ui.status_strip_widget import StatusStripWidget


class LiveView(QWidget):
    """实时模式主视图。"""

    # 短消息上报到 MainWindow statusbar：(message, timeout_ms)
    status_message = Signal(str, int)

    def __init__(self, settings: Settings, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._settings = settings

        # ---------- 业务状态（原 MainWindow.__init__）----------
        self._worker = None
        self._receiver = FrameReceiverV2()
        self._data_store = DataStore()
        self._profile_store = ProfileStore(cache=ProfileCache())
        self._state_store = StateStore()
        self._event_log = EventLog()
        self._event_log.event_added.connect(self._on_event_added_for_chart)
        self._profile_store.profile_changed.connect(self._on_profile_changed_sync)
        self._handshake: Handshake | None = None
        self._handshake_timer = QTimer(self)
        self._handshake_timer.setInterval(100)
        self._handshake_timer.timeout.connect(self._on_handshake_tick)
        self._is_connected = False
        self._debug_enabled = False
        self._recorder = None
        self._is_recording = False
        self._frame_times: list[float] = []
        self._theme = "dark"
        self._is_dark_theme = True

        self._setup_ui()
        self._load_settings()

    # ============================ 主题 / 字号 ============================

    def set_theme(self, theme: str, scale: str = "small") -> None:
        """由 MainWindow 广播：切换主题。M7 字号已固化 small，scale 参数忽略。"""
        self._theme = theme
        self._is_dark_theme = theme != "light"
        self._apply_theme(theme)

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light")

    # ============================ 初始化 ============================

    def _load_settings(self):
        conn_type = self._settings.get("general.connection_type", "Serial")
        self._type_combo.setCurrentText(conn_type)
        self._on_type_changed(conn_type)
        self._baudrate_combo.setCurrentText(
            self._settings.get("serial.default_baudrate", "115200")
        )
        # 主题由 MainWindow 应用到本 view；这里只更新 UDP 字段
        self._remote_ip.setText(self._settings.get("udp.remote_ip", "192.168.1.12"))
        self._remote_port.setValue(self._settings.get("udp.remote_port", 4004))
        self._local_port.setValue(self._settings.get("udp.local_port", 45678))

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

        self._type_combo = QComboBox()
        self._type_combo.addItems(["Serial", "UDP"])
        self._type_combo.setFixedWidth(70)
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
        self._refresh_ports()
        serial_layout.addWidget(QLabel("Port:"))
        serial_layout.addWidget(self._port_combo)

        self._baudrate_combo = QComboBox()
        self._baudrate_combo.addItems(
            ["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"]
        )
        self._baudrate_combo.setCurrentText("115200")
        serial_layout.addWidget(QLabel("Baud:"))
        serial_layout.addWidget(self._baudrate_combo)
        self._config_stack.addWidget(self._serial_widget)

        self._udp_widget = QWidget()
        udp_layout = QHBoxLayout(self._udp_widget)
        udp_layout.setContentsMargins(0, 0, 0, 0)
        udp_layout.setSpacing(4)

        self._remote_ip = QLineEdit("192.168.1.12")
        self._remote_ip.setFixedWidth(100)
        udp_layout.addWidget(QLabel("Remote IP:"))
        udp_layout.addWidget(self._remote_ip)

        self._remote_port = QSpinBox()
        self._remote_port.setRange(1, 65535)
        self._remote_port.setValue(4004)
        self._remote_port.setFixedWidth(70)
        udp_layout.addWidget(QLabel("Remote Port:"))
        udp_layout.addWidget(self._remote_port)

        self._local_port = QSpinBox()
        self._local_port.setRange(1, 65535)
        self._local_port.setValue(45678)
        self._local_port.setFixedWidth(70)
        udp_layout.addWidget(QLabel("Local Port:"))
        udp_layout.addWidget(self._local_port)
        self._config_stack.addWidget(self._udp_widget)

        self._toolbar.addWidget(self._config_stack)
        self._toolbar.addSeparator()

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setFixedSize(70, 28)
        self._connect_btn.setToolTip("打开串口 / 绑定 UDP 端口并启动握手")
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        self._toolbar.addWidget(self._connect_btn)

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setFixedSize(80, 28)
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.setToolTip("断开连接（不清空已接收的数据/Profile）")
        self._disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        self._toolbar.addWidget(self._disconnect_btn)

        self._toolbar.addSeparator()

        self._debug_btn = QPushButton("Debug: OFF")
        self._debug_btn.setFixedSize(90, 28)
        self._debug_btn.setEnabled(False)
        self._debug_btn.setToolTip("下发 CONTROL.DEBUG_ENABLE，开启/关闭下位机数据上报")
        self._debug_btn.clicked.connect(self._on_debug_toggled)
        self._toolbar.addWidget(self._debug_btn)

        self._toolbar.addSeparator()

        self._record_btn = QPushButton("Record")
        self._record_btn.setFixedSize(70, 28)
        self._record_btn.setToolTip("开始/停止录制 .sdb v2（含 profile 快照）")
        self._record_btn.clicked.connect(self._on_record_clicked)
        self._toolbar.addWidget(self._record_btn)

        self._import_btn = QPushButton("Import")
        self._import_btn.setFixedSize(70, 28)
        self._import_btn.setToolTip("（M7：建议改用回放 Tab）离线导入 .sdb v2 到 Live 视图")
        self._import_btn.clicked.connect(self._on_import_clicked)
        self._toolbar.addWidget(self._import_btn)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setFixedSize(60, 28)
        self._clear_btn.setToolTip(
            "清空曲线/Dashboard/事件/计数（保留 Profile 与 StatePanel 当前状态）"
        )
        self._clear_btn.clicked.connect(self._on_clear_clicked)
        self._toolbar.addWidget(self._clear_btn)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._toolbar.addWidget(spacer)

        self._hw_label = QLabel("设备: —")
        self._hw_label.setToolTip("当前连接设备的 hw_type（来自下位机 META_INFO）")
        self._hw_label.setStyleSheet(
            f"color: {S.TEXT}; font-weight: bold; padding: 4px 8px; "
            f"border-left: 1px solid {S.BORDER};"
        )
        self._toolbar.addWidget(self._hw_label)

        self._conn_status_label = QLabel("Disconnected")
        self._conn_status_label.setStyleSheet(
            f"color: {S.TEXT}; font-weight: bold; padding: 4px 8px;"
        )
        self._toolbar.addWidget(self._conn_status_label)

        # ---------- StatusStrip / Dashboard ----------
        self._status_strip = StatusStripWidget(self._profile_store, self._state_store)
        root.addWidget(self._status_strip)

        self._dashboard = DashboardWidget(self._profile_store, self._state_store)
        self._dashboard.mode_requested.connect(self._on_dashboard_mode_requested)
        root.addWidget(self._dashboard)

        # ---------- 主区：splitter ----------
        splitter = QSplitter(Qt.Vertical)
        top_splitter = QSplitter(Qt.Horizontal)

        self._chart = GroupedChartWidget()
        self._chart.setMinimumHeight(400)
        self._chart.set_dark_theme(True)
        self._chart.set_profile_store(self._profile_store)
        top_splitter.addWidget(self._chart)

        self._attitude = AttitudeWidget()
        self._attitude.setMinimumWidth(350)
        self._attitude.set_dark_theme(True)
        top_splitter.addWidget(self._attitude)

        right_panel = QSplitter(Qt.Vertical)
        right_panel.setMinimumWidth(260)
        self._state_panel = StatePanelWidget(
            self._profile_store, self._state_store, data_store=self._data_store
        )
        right_panel.addWidget(self._state_panel)
        self._event_timeline = EventTimelineWidget(self._event_log)
        self._event_timeline.jump_requested.connect(self._chart.jump_to_timestamp)
        right_panel.addWidget(self._event_timeline)
        right_panel.setStretchFactor(0, 1)
        right_panel.setStretchFactor(1, 1)
        top_splitter.addWidget(right_panel)

        top_splitter.setStretchFactor(0, 3)
        top_splitter.setStretchFactor(1, 1)
        top_splitter.setStretchFactor(2, 1)
        splitter.addWidget(top_splitter)

        # ---------- Channel 勾选 panel ----------
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
        self._channel_layout.setSpacing(4)   # M7：紧凑
        self._channel_checks: dict = {}
        self._channel_dots: dict = {}
        self._channel_value_labels: dict = {}
        self._channel_containers: dict = {}
        self._channel_colors: dict = {}

        self._scroll.setWidget(self._channel_widget)
        channel_layout.addWidget(self._scroll)
        channel_panel.setMinimumHeight(80)

        self._control_panel = ControlPanelWidget()
        self._control_panel.setMaximumHeight(80)   # M7：防 splitter 拖动吞掉曲线区
        self._control_panel.set_profile_store(self._profile_store)
        self._control_panel.sample_rate_changed.connect(self._on_sample_rate_changed)
        self._control_panel.user_mark_requested.connect(self._on_user_mark_requested)
        self._control_panel.reset_stats_requested.connect(self._on_reset_stats_requested)
        self._control_panel.channel_enable_changed.connect(self._on_channel_enable_changed)

        splitter.addWidget(self._control_panel)
        splitter.addWidget(channel_panel)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 1)
        root.addWidget(splitter)

        # ---------- 底部统计行（替代原 QStatusBar 的 4 个 permanent widget） ----------
        stats_row = QWidget()
        stats_row.setMaximumHeight(22)
        stats_row.setStyleSheet(
            f"background-color: {S.PANEL_DARK}; color: {S.TEXT};"
        )
        stats_layout = QHBoxLayout(stats_row)
        stats_layout.setContentsMargins(8, 2, 8, 2)
        stats_layout.setSpacing(8)
        self._fps_label = QLabel("FPS: 0")
        self._channel_count_label = QLabel("Channels: 0")
        self._frame_count_label = QLabel("Total Frames: 0")
        self._error_count_label = QLabel("Errors: 0")
        stats_layout.addStretch(1)
        for w in (self._fps_label, QLabel("|"), self._channel_count_label,
                  QLabel("|"), self._frame_count_label, QLabel("|"),
                  self._error_count_label):
            stats_layout.addWidget(w)
        root.addWidget(stats_row)

        # ---------- timers ----------
        self._update_timer = QTimer()
        self._update_timer.timeout.connect(self._update_display)
        self._update_timer.start(100)

        self._heavy_timer = QTimer()
        self._heavy_timer.timeout.connect(self._update_heavy)
        self._heavy_timer.start(200)

        self._frame_count = 0
        self._error_count = 0

        # 应用初始按钮 / 输入框 stylesheet（_apply_theme 会做完整刷新）
        self._apply_button_styles_initial()

    def _apply_button_styles_initial(self):
        """setup_ui 期间应用一次按钮 / 输入框样式，避免 _apply_theme 调用前显示裸样式。"""
        pal = S.palette(self._theme)
        self._connect_btn.setStyleSheet(
            f"background-color: {pal['primary']}; color: white; border: none; border-radius: 2px;"
        )
        self._disconnect_btn.setStyleSheet(
            f"background-color: {pal['error']}; color: white; border: none; border-radius: 2px;"
        )
        self._debug_btn.setStyleSheet(
            f"background-color: {pal['button_bg']}; color: {pal['text']}; border: none; border-radius: 2px;"
        )
        for btn in (self._record_btn, self._import_btn, self._clear_btn):
            btn.setStyleSheet(
                f"background-color: {pal['primary']}; color: white; border: none; border-radius: 2px;"
            )
        common_input = (
            f"background-color: {pal['input_bg']}; color: {pal['text']}; "
            f"border: none; padding: 4px; border-radius: 2px;"
        )
        for w in (self._type_combo, self._port_combo, self._baudrate_combo,
                  self._remote_ip, self._remote_port, self._local_port):
            w.setStyleSheet(common_input)

    # ============================ 连接 / 工作流 ============================

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
                self.status_message.emit("No serial port available", 3000)
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
        self._control_panel.set_enabled(True)
        self._status_strip.set_link_state(connected=True)

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

    def _on_channel_enable_changed(self, mask: int):
        from satellite_debug_tool.core.protocol import build_channel_enable_mask
        if self._send_control_frame(build_channel_enable_mask(mask)):
            self.status_message.emit(f"通道使能 mask → 0x{mask:016X}", 3000)

    def _on_handshake_ready(self, hw_type: str):
        self.status_message.emit(f"Profile ready: {hw_type}", 3000)
        self._hw_label.setText(f"设备: {hw_type}")
        self._state_panel.set_hw_type(hw_type)
        self._dashboard.set_hw_type(hw_type)
        self._status_strip.set_hw_type(hw_type)
        self._chart.set_hw_type(hw_type)
        self._control_panel.set_hw_type(hw_type)

    # ----- 命令下发 -----

    def _send_control_frame(self, frame: bytes) -> bool:
        if self._worker is None or not self._is_connected:
            self.status_message.emit("未连接，命令未发送", 3000)
            return False
        return bool(self._worker.send(frame))

    def _on_sample_rate_changed(self, hz: int) -> None:
        if self._send_control_frame(build_set_sample_rate(hz)):
            self.status_message.emit(f"已请求采样率 {hz} Hz", 2000)

    def _on_user_mark_requested(self, mark_id: int, text: str) -> None:
        if self._send_control_frame(build_user_mark(mark_id, text)):
            self.status_message.emit(f"Mark #{mark_id} 已发送", 2000)

    def _on_reset_stats_requested(self) -> None:
        if self._send_control_frame(build_reset_stats()):
            self.status_message.emit("已请求下位机复位统计", 2000)

    def _on_dashboard_mode_requested(self, state_id: int, target_value: int) -> None:
        if state_id == 0:
            if self._send_control_frame(build_set_trace_mode(target_value)):
                self.status_message.emit(
                    f"已请求切换模式（state_id={state_id} → {target_value}）", 2000,
                )
        else:
            self.status_message.emit(
                f"state_id={state_id} 的模式切换暂未下发（协议待扩展）", 3000,
            )

    # ----- EventLog → Chart -----

    def _on_event_added_for_chart(self, record: EventRecord) -> None:
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

    def _on_profile_changed_sync(self, hw_type: str) -> None:
        if hw_type:
            self._hw_label.setText(f"设备: {hw_type}")
        for key, cb in self._channel_checks.items():
            new_label = self._channel_display_label(key)
            if cb.text() != new_label:
                cb.setText(new_label)
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
        self.status_message.emit("Heartbeat timeout (link lost)", 5000)
        self._status_strip.set_link_state(connected=False)

    def _on_link_restored(self):
        self.status_message.emit("Heartbeat restored", 2000)
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
        self._hw_label.setText("设备: —")
        self._type_combo.setEnabled(True)
        self._control_panel.set_enabled(False)
        self._status_strip.set_link_state(connected=False)
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
            pal = S.palette(self._theme)
            self._debug_btn.setStyleSheet(
                f"background-color: {pal['success'] if self._debug_enabled else pal['button_bg']}; "
                f"color: white; border: none; border-radius: 2px;"
            )
        else:
            self._debug_enabled = not self._debug_enabled
            self.status_message.emit("Failed to send debug command", 3000)

    def _on_error(self, msg: str):
        self._error_count += 1
        self._error_count_label.setText(f"Errors: {self._error_count}")
        self.status_message.emit(f"Error: {msg}", 5000)

    def _on_data_received(self, data: bytes):
        if self._is_recording and self._recorder:
            self._recorder.write_frame(data)

        records = self._receiver.feed(data)
        for rec in records:
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
                continue
            if isinstance(rec, StateReport):
                self._state_store.update(hw, rec)
            elif isinstance(rec, EventReport):
                self._event_log.add(hw, rec, self._profile_store)

    # ============================ Timers ============================

    def _update_heavy(self):
        self._chart.refresh(self._data_store)
        self._dashboard.refresh(self._data_store)
        self._state_panel.refresh_channel_values()

    def _update_display(self):
        current_time = datetime.now().timestamp()
        self._frame_times.append(current_time)
        self._frame_times = [t for t in self._frame_times if current_time - t < 1.0]
        fps = len(self._frame_times)
        self._fps_label.setText(f"FPS: {fps}")

        channels = self._data_store.get_all_channels()
        self._channel_count_label.setText(f"Channels: {len(channels)}")
        self._frame_count_label.setText(f"Frames: {self._frame_count}")

        hw = self._profile_store.current_hw_type()
        if hw is not None:
            attitude_options = [
                f"ch_{c.channel_id:02d}"
                for c in self._profile_store.get_channels(hw)
            ]
        else:
            attitude_options = channels
        self._attitude.set_channel_options(attitude_options)

        existing_names = set(self._channel_checks.keys())
        new_names = set(channels) - existing_names

        pal = S.palette(self._theme)
        row_h = 28
        dot_sz = 10
        for name in new_names:
            idx = len(self._channel_checks)
            cols = 8
            row = idx // cols
            col = idx % cols
            color = COLORS[idx % len(COLORS)]
            container = QWidget()
            container.setFixedHeight(row_h)
            container.setStyleSheet(
                f"background-color: {pal['panel']}; border-left: 4px solid {color}; "
                f"border-radius: 3px; padding: 4px 8px;"
            )
            container_layout = QHBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.setSpacing(4)
            dot = QLabel()
            dot.setFixedSize(dot_sz, dot_sz)
            dot.setStyleSheet(
                f"background-color: {color}; border-radius: {dot_sz // 2}px;"
            )
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

        for name, cb in self._channel_checks.items():
            if not cb.isChecked() or name not in channels:
                continue
            ch = self._data_store.get_channel(name)
            latest = ch.get_latest() if ch else None
            if latest:
                self._channel_value_labels[name].setText(f"{latest[1]:.2f}")

        def _latest(ch_name: str):
            if not ch_name:
                return None
            ch = self._data_store.get_channel(ch_name)
            if ch is None:
                return None
            latest = ch.get_latest()
            return latest[1] if latest else None

        roll_ch, pitch_ch, yaw_ch = self._attitude.get_channel_selections()
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
            if self._recorder:
                self._recorder.stop()
                self._recorder = None
            self._is_recording = False
            self._status_strip.set_recording(False)
            self._record_btn.setText("Record")
            pal = S.palette(self._theme)
            self._record_btn.setStyleSheet(
                f"background-color: {pal['primary']}; color: white; border: none; border-radius: 2px;"
            )
            self.status_message.emit("Recording stopped", 3000)
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath, _ = QFileDialog.getSaveFileName(
                self,
                "Save Recording",
                f"recording_{timestamp}.sdb",
                "SDB Files (*.sdb);;All Files (*)",
            )
            if filepath:
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
                    pal = S.palette(self._theme)
                    self._record_btn.setStyleSheet(
                        f"background-color: {pal['error']}; color: white; border: none; border-radius: 2px;"
                    )
                    suffix = " + profile" if profile_dict else ""
                    self.status_message.emit(f"Recording to {filepath}{suffix}", 3000)
                else:
                    self._recorder = None
                    self.status_message.emit("Failed to start recording", 3000)

    def _on_import_clicked(self):
        """M7：此入口保留向后兼容；新建议用回放 Tab 独立 DataStore。"""
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "Import Data (Live)",
            "",
            "SDB Files (*.sdb);;All Files (*)",
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
            self.status_message.emit(
                f"Imported {data_count} DataReport(s) from {filepath}", 3000,
            )
        except Exception as exc:
            self.status_message.emit(f"Import failed: {exc}", 5000)
            return

    def _on_clear_clicked(self):
        self._data_store.clear()
        self._event_log.clear()
        self._frame_count = 0
        self._error_count = 0
        self._frame_times.clear()

        self._chart.clear()
        self._attitude.clear()
        self._dashboard.refresh(self._data_store)

        if hasattr(self._event_timeline, "_list"):
            self._event_timeline._list.clear()
            if hasattr(self._event_timeline, "_update_count"):
                self._event_timeline._update_count()

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

        self._channel_count_label.setText("Channels: 0")
        self._frame_count_label.setText("Frames: 0")
        self._error_count_label.setText("Errors: 0")
        self.status_message.emit("Display cleared", 2000)

    # ============================ 主题应用 ============================

    def _apply_theme(self, theme: str):
        """统一主题分发（M7：字号固化 small）。"""
        scale = "small"
        pal = S.palette(theme)
        bg, panel, border, text, input_bg = (
            pal["bg"], pal["panel"], pal["border"], pal["text"], pal["input_bg"]
        )
        primary = pal["primary"]
        error = pal["error"]

        self.setStyleSheet(f"background-color: {bg}; color: {text};")

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

        # 工具栏
        self._toolbar.setStyleSheet(
            f"background-color: {panel}; border: none; padding: 4px;"
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
        self._toolbar_scroll.setFixedHeight(36)
        self._toolbar_scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {panel}; border: none; }}"
        )
        self._serial_widget.setStyleSheet("background-color: transparent;")
        self._udp_widget.setStyleSheet("background-color: transparent;")
        for w in self._serial_widget.findChildren(QLabel):
            w.setStyleSheet(f"color: {text}; background-color: transparent;")
        for w in self._udp_widget.findChildren(QLabel):
            w.setStyleSheet(f"color: {text}; background-color: transparent;")

        for widget in (self._type_combo, self._port_combo, self._baudrate_combo,
                       self._remote_ip, self._remote_port, self._local_port):
            widget.setStyleSheet(
                f"background-color: {input_bg}; color: {text}; "
                f"border: none; padding: 4px; border-radius: 2px;"
            )
        self._connect_btn.setStyleSheet(
            f"background-color: {primary}; color: white; border: none; border-radius: 2px;"
        )
        self._disconnect_btn.setStyleSheet(
            f"background-color: {error}; color: white; border: none; border-radius: 2px;"
        )
        self._debug_btn.setStyleSheet(
            f"background-color: {pal['button_bg']}; color: {text}; border: none; border-radius: 2px;"
        )
        for btn in (self._record_btn, self._import_btn, self._clear_btn):
            btn.setStyleSheet(
                f"background-color: {primary}; color: white; border: none; border-radius: 2px;"
            )

        # Channel panel
        channel_panel = self.findChild(QWidget, "channel_panel")
        if channel_panel:
            channel_panel.setStyleSheet(
                f"background-color: {panel}; border: 1px solid {border}; border-radius: 2px;"
            )
        self._scroll.setStyleSheet(f"background-color: {bg}; border: none;")
        self._channel_widget.setStyleSheet("background-color: transparent;")
        row_h = 28
        dot_sz = 10
        for name, container in self._channel_containers.items():
            color = self._channel_colors.get(name, "#888888")
            container.setFixedHeight(row_h)
            container.setStyleSheet(
                f"background-color: {panel}; border-left: 4px solid {color}; "
                f"border-radius: 3px; padding: 4px 8px;"
            )
        for name, dot in self._channel_dots.items():
            color = self._channel_colors.get(name, "#888888")
            dot.setFixedSize(dot_sz, dot_sz)
            dot.setStyleSheet(
                f"background-color: {color}; border-radius: {dot_sz // 2}px;"
            )
        for cb in self._channel_checks.values():
            cb.setStyleSheet(
                f"color: {text}; border: none; padding: 0px 4px; background: transparent;"
            )
        for lbl in self._channel_value_labels.values():
            lbl.setStyleSheet(
                f"color: {text}; min-width: 60px; text-align: right; background: transparent;"
            )
