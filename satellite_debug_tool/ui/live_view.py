"""LiveView —— 实时连接 / 数据采集 / 显示 Tab。

从 MainWindow 抽出（M7-S4）。原 1189 行 MainWindow 的所有业务逻辑（worker /
receiver / handshake / data_store / state_store / event_log / 各 widget
持有 / 录制 / 主题切换分发）全部内化到此类。

MainWindow 现在只剩 QTabWidget 壳：顶部全局主题切换 + 四个 Tab(Live /
Playback / Log / Device) + 共享 statusbar。LiveView 通过 ``status_message`` 信号把短
消息上报到 MainWindow statusbar；主题由 MainWindow 调 ``set_theme(theme)``
广播下来。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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

from satellite_debug_tool.core.comm import SerialWorker, UdpWorker
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.data import DataStore, EventLog, EventRecord, StateStore
from satellite_debug_tool.core.link_trace import trace_message
from satellite_debug_tool.core.profile import (
    CHANNEL_ROLE_ANTENNA_AZ,
    CHANNEL_ROLE_ANTENNA_EL,
    CHANNEL_ROLE_PITCH,
    CHANNEL_ROLE_ROLL,
    CHANNEL_ROLE_YAW,
    CONTROL_SUBCMD_SET_TRACE_MODE,
    CONTROL_VALUE_FROM_ENUM_VALUE,
    ProfileCache,
    ProfileStore,
)
from satellite_debug_tool.core.protocol import (
    DataReport,
    EventReport,
    FrameReceiverV2,
    Heartbeat,
    CommandResponse,
    RespCode,
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
from satellite_debug_tool.ui.channel_panel import ChannelPanel
from satellite_debug_tool.ui.chart_widget import COLORS
from satellite_debug_tool.ui.control_panel_widget import ControlPanelWidget
from satellite_debug_tool.ui.dashboard_widget import DashboardWidget
from satellite_debug_tool.ui.event_timeline_widget import EventTimelineWidget
from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget
from satellite_debug_tool.ui.state_panel_widget import StatePanelWidget
from satellite_debug_tool.ui.status_strip_widget import StatusStripWidget
from satellite_debug_tool.ui.simulation_panel_widget import SimulationPanelWidget


DEBUG_ACK_TIMEOUT_MS = 3000
DEBUG_ACK_MAX_RETRIES = 0
DEBUG_LATE_ACK_WINDOW_S = 3.0


def _debug_ctrl_log(message: str) -> None:
    trace_message("DBG_CTRL", message)


def _parse_debug_ack_target(message: str) -> bool | None:
    text = (message or "").strip()
    if text == "DEBUG_ENABLE=1":
        return True
    if text == "DEBUG_ENABLE=0":
        return False
    return None


class LiveView(QWidget):
    """实时模式主视图。"""

    # 短消息上报到 MainWindow statusbar：(message, timeout_ms)
    status_message = Signal(str, int)
    # M9: 连接共享 — DeviceView 通过这些信号接入同一条链路
    connected_worker_changed = Signal(object)  # emit worker 或 None
    frame_received = Signal(object)            # emit 每个 parsed FrameV2Record
    debug_request_finished = Signal(bool, bool, str)  # target, ok, detail
    debug_state_changed = Signal(bool)

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
        self._debug_pending_target: bool | None = None
        self._debug_retry_count = 0
        self._debug_data_seen_after_request = False
        self._debug_last_requested_target: bool | None = None
        self._debug_last_request_at = 0.0
        self._external_control_locked = False
        self._debug_ack_timer = QTimer(self)
        self._debug_ack_timer.setSingleShot(True)
        self._debug_ack_timer.timeout.connect(self._on_debug_ack_timeout)
        self._recorder = None
        self._is_recording = False
        # 仿真模式
        self._mock_modem: object | None = None  # MockModem 实例（延迟导入）
        self._sim_active = False
        self._frame_times: list[float] = []
        self._theme = "dark"
        self._is_dark_theme = True

        self._setup_ui()
        self._load_settings()

    def profile_store(self) -> ProfileStore:
        """供 DeviceView 复用 Live 页当前连接的 profile/capability。"""
        return self._profile_store

    def is_debug_enabled(self) -> bool:
        return self._debug_enabled

    @Slot(bool)
    def request_debug_mode(self, target: bool) -> None:
        """Live/Device 共用的严格 Debug 控制入口。"""
        target = bool(target)
        if not self._is_connected or self._worker is None:
            self.debug_request_finished.emit(target, False, "设备未连接")
            return
        if self._debug_pending_target is not None:
            self.debug_request_finished.emit(target, False, "已有 Debug 命令等待确认")
            return
        if self._debug_enabled == target:
            self.debug_request_finished.emit(target, True, "already_confirmed")
            return
        if not self._send_debug_enable(target):
            self.debug_request_finished.emit(target, False, "Debug 命令发送失败")

    @Slot(bool)
    def set_device_transaction_active(self, active: bool) -> None:
        """设备事务期间锁住 Live 页 Debug 按钮。"""
        self._external_control_locked = bool(active)
        self._update_debug_button_enabled()

    @Slot(bool)
    def set_handshake_retries_paused(self, paused: bool) -> None:
        if self._handshake is not None:
            self._handshake.set_retry_paused(bool(paused))

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
        self._remote_ip.setMinimumWidth(124)   # 容下完整 IP（等宽字体）
        udp_layout.addWidget(QLabel("Remote IP:"))
        udp_layout.addWidget(self._remote_ip)

        self._remote_port = QSpinBox()
        self._remote_port.setRange(1, 65535)
        self._remote_port.setValue(4004)
        self._remote_port.setMinimumWidth(78)
        udp_layout.addWidget(QLabel("Remote Port:"))
        udp_layout.addWidget(self._remote_port)

        self._local_port = QSpinBox()
        self._local_port.setRange(1, 65535)
        self._local_port.setValue(45678)
        self._local_port.setMinimumWidth(82)
        udp_layout.addWidget(QLabel("Local Port:"))
        udp_layout.addWidget(self._local_port)
        self._config_stack.addWidget(self._udp_widget)

        self._toolbar.addWidget(self._config_stack)
        self._toolbar.addSeparator()

        # Connect = 主按钮(accent + plug)；Disconnect = 危险图标按钮(连接后才有意义)
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setMinimumSize(92, 29)
        self._connect_btn.setToolTip("打开串口 / 绑定 UDP 端口并启动握手")
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        self._toolbar.addWidget(self._connect_btn)

        self._disconnect_btn = QPushButton("")   # icon-only（设计）
        self._disconnect_btn.setFixedSize(30, 29)
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.setToolTip("断开连接（不清空已接收的数据/Profile）")
        self._disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        self._toolbar.addWidget(self._disconnect_btn)

        self._toolbar.addSeparator()

        self._debug_btn = QPushButton("Debug: OFF")
        self._debug_btn.setMinimumSize(100, 29)
        self._debug_btn.setEnabled(False)
        self._debug_btn.setToolTip("下发 CONTROL.DEBUG_ENABLE，开启/关闭下位机数据上报")
        self._debug_btn.clicked.connect(self._on_debug_toggled)
        self._toolbar.addWidget(self._debug_btn)

        self._record_btn = QPushButton("REC")
        self._record_btn.setMinimumSize(74, 29)
        self._record_btn.setToolTip("开始/停止录制 .sdb v2（含 profile 快照）")
        self._record_btn.clicked.connect(self._on_record_clicked)
        self._toolbar.addWidget(self._record_btn)

        self._import_btn = QPushButton("Import")
        self._import_btn.setMinimumSize(84, 29)
        self._import_btn.setToolTip("（M7：建议改用回放 Tab）离线导入 .sdb v2 到 Live 视图")
        self._import_btn.clicked.connect(self._on_import_clicked)
        self._toolbar.addWidget(self._import_btn)

        self._clear_btn = QPushButton("")   # icon-only ghost（设计）
        self._clear_btn.setFixedSize(30, 29)
        self._clear_btn.setToolTip(
            "清空曲线/Dashboard/事件/计数（保留 Profile 与 StatePanel 当前状态）"
        )
        self._clear_btn.clicked.connect(self._on_clear_clicked)
        self._toolbar.addWidget(self._clear_btn)

        self._toolbar.addSeparator()

        self._sim_btn = QPushButton("仿真")
        self._sim_btn.setMinimumSize(74, 29)
        self._sim_btn.setCheckable(True)
        self._sim_btn.setToolTip("切换仿真模式（模拟对星，无需真实设备）")
        self._sim_btn.clicked.connect(self._on_sim_toggled)
        self._toolbar.addWidget(self._sim_btn)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._toolbar.addWidget(spacer)

        # 隐藏的兼容 label（旧逻辑仍引用 _hw_label / _conn_status_label）
        self._hw_label = QLabel("设备: —")
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
        right_panel.setObjectName("rpanel")
        right_panel.setMinimumWidth(300)

        self._attitude = AttitudeWidget()
        self._attitude.setMinimumHeight(300)
        self._attitude.set_dark_theme(True)
        right_panel.addWidget(self._attitude)

        self._state_panel = StatePanelWidget(
            self._profile_store, self._state_store, data_store=self._data_store
        )
        right_panel.addWidget(self._state_panel)
        self._event_timeline = EventTimelineWidget(self._event_log)
        self._event_timeline.jump_requested.connect(self._chart.jump_to_timestamp)
        right_panel.addWidget(self._event_timeline)
        right_panel.setStretchFactor(0, 1)   # 姿态可随窗口增高（波束不被裁）
        right_panel.setStretchFactor(1, 0)   # 状态紧凑
        right_panel.setStretchFactor(2, 1)   # 事件填充
        right_panel.setSizes([340, 200, 300])
        top_splitter.addWidget(right_panel)

        # 仿真面板（默认隐藏，仿真模式时作为第 4 列显示）
        self._sim_panel = SimulationPanelWidget()
        self._sim_panel.setVisible(False)
        self._sim_panel.setMinimumWidth(240)
        self._sim_panel.setMaximumWidth(300)
        top_splitter.addWidget(self._sim_panel)

        # 列：通道 / chart / 右栏 / 仿真面板(隐藏)
        top_splitter.setStretchFactor(0, 0)
        top_splitter.setStretchFactor(1, 1)
        top_splitter.setStretchFactor(2, 0)
        top_splitter.setStretchFactor(3, 0)
        top_splitter.setSizes([212, 868, 320, 0])
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
            self._sim_btn.setIcon(_ic.icon("satellite", color=t2, size=14))
        except Exception:
            pass
        self._set_variant(self._connect_btn, "primary")
        self._set_variant(self._disconnect_btn, "danger")
        self._set_variant(self._clear_btn, "ghost")
        for btn in (self._debug_btn, self._record_btn, self._import_btn, self._sim_btn):
            self._set_variant(btn, "")
        # 输入控件清内联样式 → 全局 QSS 接管
        for w in (self._type_combo, self._port_combo, self._baudrate_combo,
                  self._remote_ip, self._remote_port, self._local_port):
            w.setStyleSheet("")

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
        trace_message("DBG_UI", f"CLICK CONNECT type={conn_type}")
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
            self._set_conn_state(False, dev="—", detail="Connection Failed")

    def _on_disconnect_clicked(self):
        trace_message("DBG_UI", "CLICK DISCONNECT")
        self._debug_enabled = False
        self._debug_pending_target = None
        self._debug_last_requested_target = None
        self._debug_last_request_at = 0.0
        self._debug_ack_timer.stop()
        self._debug_btn.setText("Debug: OFF")
        self._debug_btn.setEnabled(False)
        if self._worker:
            self._worker.disconnect()

    @property
    def worker(self):
        """当前 BaseWorker 实例，未连接时为 None。供 DeviceView 共享连接。"""
        return self._worker if self._is_connected else None

    def _on_connected(self):
        self._is_connected = True
        # 新连接必须丢弃上一轮运行态；profile 可以复用，但 state 必须等设备重新上报。
        self._state_store.clear()
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._update_debug_button_enabled()
        self._set_conn_state(True, dev="—", detail=self._conn_status_label.text())
        self._type_combo.setEnabled(False)
        self._control_panel.set_enabled(True)
        self._status_strip.set_link_state(connected=True)
        self.connected_worker_changed.emit(self._worker)

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
        self._set_conn_state(True, dev=hw_type, detail=self._conn_status_label.text())
        self._state_panel.set_hw_type(hw_type)
        self._dashboard.set_hw_type(hw_type)
        self._status_strip.set_hw_type(hw_type)
        self._chart.set_hw_type(hw_type)
        self._control_panel.set_hw_type(hw_type)
        # 有设备专属 STL 模型（~/.satellite_debug_tool/models/<hw_type>.stl）则替换默认占位
        self._attitude.try_load_device_model(hw_type)

    # ----- 命令下发 -----

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
        hw = self._profile_store.current_hw_type()
        if hw is None:
            self.status_message.emit("尚未完成 profile 握手，模式切换未发送", 3000)
            return
        binding = self._profile_store.get_state_control_binding(hw, state_id)
        if binding is None:
            self.status_message.emit(
                f"state_id={state_id} 未声明 control_binding，模式切换未发送", 3000,
            )
            return
        if (
            binding.subcmd == CONTROL_SUBCMD_SET_TRACE_MODE
            and binding.value_from == CONTROL_VALUE_FROM_ENUM_VALUE
        ):
            if self._send_control_frame(build_set_trace_mode(target_value)):
                self.status_message.emit(
                    f"已请求切换模式（state_id={state_id} → {target_value}）", 2000,
                )
        else:
            self.status_message.emit(
                f"control_binding={binding.subcmd}/{binding.value_from} 暂未支持", 3000,
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

    def _channel_group_title(self, key: str) -> str:
        """通道 key → 分组标题（用 profile group_id；无则"其它"）。"""
        hw = self._profile_store.current_hw_type()
        if hw is None or not key.startswith("ch_"):
            return "其它"
        try:
            cid = int(key.split("_", 1)[1])
        except (IndexError, ValueError):
            return "其它"
        entry = self._profile_store.get_channel(hw, cid)
        if entry is None:
            return "其它"
        from satellite_debug_tool.ui.grouped_chart_widget import _group_title
        return _group_title(entry.group_id)

    def _on_channel_visibility_changed(self, name: str, checked: bool) -> None:
        """D6 P0：ChannelPanel 勾选 → 控制 chart 该曲线显隐。"""
        self._chart.set_channel_visible(name, checked)

    def _resync_channel_visibility(self) -> None:
        """曲线重建后把 ChannelPanel 当前勾选态重新套到曲线上。"""
        for name in self._channel_panel.channel_names():
            self._chart.set_channel_visible(name, self._channel_panel.is_checked(name))

    def _on_profile_changed_sync(self, hw_type: str) -> None:
        if hw_type:
            self._hw_label.setText(f"设备: {hw_type}")
        # Profile 变化时刷新每条通道在 ChannelPanel 里显示的名字（带 unit）
        for key in self._channel_panel.channel_names():
            self._channel_panel.set_label(key, self._channel_display_label(key))
        hw = self._profile_store.current_hw_type()
        if hw is None:
            return
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
        sig = (
            tuple(sorted(name_to_key.items())),
            tuple(sorted(semantic_picks.items())),
        )
        if getattr(self, "_attitude_bind_sig", None) == sig:
            return
        self._attitude_bind_sig = sig
        self._attitude.auto_bind_from_profile(name_to_key)
        fallback_roll, fallback_pitch, fallback_yaw = self._attitude.current_attitude_bindings()
        _, _, fallback_ant_az, fallback_ant_el = self._attitude.current_pointing_bindings()
        self._attitude.set_auto_bindings(
            roll=semantic_picks.get("roll", fallback_roll),
            pitch=semantic_picks.get("pitch", fallback_pitch),
            yaw=semantic_picks.get("yaw", fallback_yaw),
            ant_az=semantic_picks.get("ant_az", fallback_ant_az),
            ant_el=semantic_picks.get("ant_el", fallback_ant_el),
        )

    def _on_link_lost(self):
        self.status_message.emit("Heartbeat timeout (link lost)", 5000)
        self._status_strip.set_link_state(connected=False)

    def _on_link_restored(self):
        self.status_message.emit("Heartbeat restored", 2000)
        self._status_strip.set_link_state(connected=True)

    def _on_disconnected(self):
        # M9: 断开前通知设备关闭数据上报
        if self._debug_enabled and self._worker is not None:
            self._worker.send(build_debug_enable_v2(False))
        self._is_connected = False
        self.connected_worker_changed.emit(None)
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._debug_btn.setEnabled(False)
        self._debug_enabled = False
        self._debug_pending_target = None
        self._debug_last_requested_target = None
        self._debug_last_request_at = 0.0
        self._debug_ack_timer.stop()
        self._debug_btn.setText("Debug: OFF")
        self.debug_state_changed.emit(False)
        self._conn_status_label.setText("Disconnected")
        self._hw_label.setText("设备: —")
        self._set_conn_state(False, dev="—", detail="Disconnected")
        self._type_combo.setEnabled(True)
        self._control_panel.set_enabled(False)
        self._status_strip.set_link_state(connected=False)
        self._handshake_timer.stop()
        if self._handshake is not None:
            self._handshake.stop()
            self._handshake = None

    def _on_debug_toggled(self):
        target = not self._debug_enabled
        trace_message(
            "DBG_UI",
            f"CLICK DEBUG target={1 if target else 0} "
            f"connected={int(self._is_connected)} "
            f"pending={self._debug_pending_target!r}",
        )
        if not self._worker:
            trace_message("DBG_UI", "CLICK DEBUG ignored reason=no_worker")
            return
        if self._debug_pending_target is not None:
            trace_message("DBG_UI", "CLICK DEBUG ignored reason=request_pending")
            return
        self.request_debug_mode(target)

    def _send_debug_enable(self, target: bool, *, retry: bool = False) -> bool:
        if self._worker is None:
            return False
        if self._worker.send(build_debug_enable_v2(target)):
            self._debug_pending_target = target
            self._debug_last_requested_target = target
            self._debug_last_request_at = time.monotonic()
            self._debug_data_seen_after_request = False
            if retry:
                self._debug_retry_count += 1
            else:
                self._debug_retry_count = 0
            label = "ON" if target else "OFF"
            _debug_ctrl_log(
                f"send DEBUG_ENABLE target={1 if target else 0} "
                f"retry={self._debug_retry_count}"
            )
            self._debug_btn.setText(f"Debug: {label}...")
            self._debug_btn.setEnabled(False)
            self._debug_btn.setCheckable(True)
            self._debug_btn.setChecked(target)
            self._debug_ack_timer.start(DEBUG_ACK_TIMEOUT_MS)
            return True
        else:
            _debug_ctrl_log(f"send DEBUG_ENABLE target={1 if target else 0} failed")
            self.status_message.emit("Failed to send debug command", 3000)
            return False

    def _on_debug_command_response(self, resp: CommandResponse) -> None:
        target = self._debug_pending_target
        ack_target = _parse_debug_ack_target(resp.msg or "")
        if target is None:
            if ack_target is not None:
                age = time.monotonic() - self._debug_last_request_at
                _debug_ctrl_log(
                    f"rx late debug ack target={1 if ack_target else 0} "
                    f"code={resp.code} msg={resp.msg!r} age={age:.3f}s"
                )
                if (
                    int(resp.code) == int(RespCode.SUCCESS)
                    and self._debug_last_requested_target == ack_target
                    and 0.0 <= age <= DEBUG_LATE_ACK_WINDOW_S
                ):
                    self._apply_debug_state(ack_target, source="late_ack")
            return
        _debug_ctrl_log(
            f"rx command_response while pending target={1 if target else 0}: "
            f"code={resp.code} msg={resp.msg!r}"
        )
        if ack_target is None:
            debug_related_error = (
                "DEBUG_ENABLE" in (resp.msg or "").upper()
                and int(resp.code) != int(RespCode.SUCCESS)
            )
            if debug_related_error:
                self._finish_debug_request(False, f"Debug command failed: {resp.msg or resp.code}")
                return
            _debug_ctrl_log(
                f"rx non-debug response ignored while pending target={1 if target else 0}: "
                f"code={resp.code} msg={resp.msg!r}"
            )
            return
        if ack_target != target:
            _debug_ctrl_log(
                f"rx stale debug ack target={1 if ack_target else 0} ignored, "
                f"pending target={1 if target else 0}"
            )
            return
        if int(resp.code) != int(RespCode.SUCCESS):
            self._finish_debug_request(False, f"Debug command failed: {resp.msg or resp.code}")
            return
        self._finish_debug_request(True, source="ack")

    def _on_debug_ack_timeout(self) -> None:
        target = self._debug_pending_target
        if target is None:
            return
        if self._debug_retry_count < DEBUG_ACK_MAX_RETRIES:
            label = "ON" if target else "OFF"
            _debug_ctrl_log(
                f"ack timeout target={1 if target else 0}, "
                f"retry_next={self._debug_retry_count + 1}, "
                f"data_seen={self._debug_data_seen_after_request}"
            )
            self.status_message.emit(f"Debug {label} 未确认，重试", 2000)
            self._send_debug_enable(target, retry=True)
            return
        _debug_ctrl_log(
            f"ack timeout final target={1 if target else 0}, "
            f"data_seen={self._debug_data_seen_after_request}"
        )
        if target and self._debug_data_seen_after_request:
            self._finish_debug_request(True, source="data_report_timeout")
            return
        self._finish_debug_request(False, "Debug command not confirmed")

    def _finish_debug_request(
        self,
        ok: bool,
        error: str = "",
        *,
        source: str = "ack",
    ) -> None:
        target = self._debug_pending_target
        self._debug_pending_target = None
        self._debug_ack_timer.stop()
        self._update_debug_button_enabled()
        if ok and target is not None:
            self._apply_debug_state(target, source=source)
            self.debug_request_finished.emit(target, True, source)
            return
        self._render_debug_button()
        if error:
            _debug_ctrl_log(f"debug request failed: {error}")
            self.status_message.emit(error, 3000)
        if target is not None:
            self.debug_request_finished.emit(target, False, error or "Debug command not confirmed")

    def _apply_debug_state(self, target: bool, *, source: str) -> None:
        self._debug_enabled = target
        self._update_debug_button_enabled()
        _debug_ctrl_log(
            f"debug state confirmed target={1 if target else 0}, "
            f"source={source}, data_seen={self._debug_data_seen_after_request}"
        )
        if self._debug_enabled:
            hw = self._profile_store.current_hw_type()
            self._state_store.clear(hw)
        self._render_debug_button()
        self.debug_state_changed.emit(target)

    def _update_debug_button_enabled(self) -> None:
        self._debug_btn.setEnabled(
            self._is_connected
            and not self._external_control_locked
            and self._debug_pending_target is None
        )

    def _render_debug_button(self) -> None:
        self._debug_btn.setText(f"Debug: {'ON' if self._debug_enabled else 'OFF'}")
        self._debug_btn.setCheckable(True)
        self._debug_btn.setChecked(self._debug_enabled)

    def _on_error(self, msg: str):
        self._error_count += 1
        self._error_count_label.setText(f"ERR {self._error_count}")
        self.status_message.emit(f"Error: {msg}", 5000)

    def _on_data_received(self, data: bytes):
        if self._is_recording and self._recorder:
            self._recorder.write_frame(data)

        records = self._receiver.feed(data)
        for rec in records:
            # M9: 广播给 DeviceView 等外部消费者
            self.frame_received.emit(rec)

            if self._handshake is not None:
                self._handshake.feed(rec)

            if isinstance(rec, DataReport):
                if self._debug_pending_target is not None and not self._debug_data_seen_after_request:
                    pending_target = self._debug_pending_target
                    self._debug_data_seen_after_request = True
                    _debug_ctrl_log(
                        f"rx first DATA_REPORT while pending target="
                        f"{1 if pending_target else 0}, samples={len(rec.samples)}"
                    )
                    if pending_target:
                        self._finish_debug_request(True, source="data_report")
                elif self._debug_enabled and not self._debug_data_seen_after_request:
                    self._debug_data_seen_after_request = True
                    _debug_ctrl_log(f"rx first DATA_REPORT after debug on, samples={len(rec.samples)}")
                self._data_store.update(rec)
                self._frame_count += 1
                self._frame_times.append(datetime.now().timestamp())
                continue
            if isinstance(rec, CommandResponse):
                self._on_debug_command_response(rec)
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
        self._frame_times = [t for t in self._frame_times if current_time - t < 1.0]
        fps = len(self._frame_times)
        self._fps_label.setText(f"FPS {fps}")

        channels = self._data_store.get_all_channels()
        self._channel_count_label.setText(f"CH {len(channels)}")
        self._frame_count_label.setText(f"FRM {self._frame_count}")

        hw = self._profile_store.current_hw_type()
        if hw is not None:
            attitude_options = [
                f"ch_{c.channel_id:02d}"
                for c in self._profile_store.get_channels(hw)
            ]
        else:
            attitude_options = channels
        self._attitude.set_channel_options(attitude_options)

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
            # 恢复次按钮样式（红色录制图标 → 灰）
            try:
                from satellite_debug_tool.ui import icons as _ic
                self._record_btn.setIcon(_ic.icon("record", color=S.palette(self._theme)["text_2"], size=14))
            except Exception:
                pass
            self._record_btn.setStyleSheet("")
            self.status_message.emit("Recording stopped", 3000)
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            default_name = f"recording_{timestamp}.sdb"
            rec_dir = self._settings.get("paths.recording_dir", "") or ""
            if rec_dir:
                from pathlib import Path as _Path
                initial = str(_Path(rec_dir) / default_name)
            else:
                initial = default_name
            filepath, _ = QFileDialog.getSaveFileName(
                self,
                "Save Recording",
                initial,
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
                    # 录制中 = 危险态（红底）+ 红录制图标
                    pal = S.palette(self._theme)
                    try:
                        from satellite_debug_tool.ui import icons as _ic
                        self._record_btn.setIcon(_ic.icon("record", color=pal["err"], size=14))
                    except Exception:
                        pass
                    self._record_btn.setStyleSheet(
                        f"QPushButton {{ background-color: {pal['card_2']}; color: {pal['err']}; "
                        f"border: 1px solid {pal['err']}; border-radius: 5px; padding: 4px 11px; "
                        f"font-weight: 600; }}"
                    )
                    suffix = " + profile" if profile_dict else ""
                    self.status_message.emit(f"Recording to {filepath}{suffix}", 3000)
                else:
                    self._recorder = None
                    self.status_message.emit("Failed to start recording", 3000)

    def _on_import_clicked(self):
        """M7：此入口保留向后兼容；新建议用回放 Tab 独立 DataStore。"""
        last_dir = self._settings.get("paths.recording_dir", "") or ""
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "Import Data (Live)",
            last_dir,
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

        for name in list(self._channel_panel.channel_names()):
            self._channel_panel.remove_channel(name)

        self._channel_count_label.setText("CH 0")
        self._frame_count_label.setText("FRM 0")
        self._error_count_label.setText("ERR 0")
        self.status_message.emit("Display cleared", 2000)

    # ============================ 仿真模式 ============================

    def _on_sim_toggled(self, checked: bool) -> None:
        if checked:
            self._start_simulation()
        else:
            self._stop_simulation()

    def _start_simulation(self) -> None:
        from satellite_debug_tool.core.simulation.mock_modem import MockModem
        self._mock_modem = MockModem(self)
        self._mock_modem.snr_updated.connect(self._sim_panel.update_snr)
        self._mock_modem.report_received.connect(self._sim_panel.update_report)
        self._mock_modem.metrics_updated.connect(self._sim_panel.update_metrics)

        # 连接面板信号
        self._sim_panel.satellite_changed.connect(self._on_sim_sat_changed)
        self._sim_panel.blockage_requested.connect(self._on_sim_blockage)
        self._sim_panel.rain_fade_changed.connect(self._on_sim_rain_changed)
        if hasattr(self._sim_panel, 'snr_baseline_changed'):
            self._sim_panel.snr_baseline_changed.connect(self._on_sim_baseline)
        if hasattr(self._sim_panel, 'heading_changed'):
            self._sim_panel.heading_changed.connect(self._on_sim_heading)

        self._mock_modem.start(port=45679, remote_addr=("127.0.0.1", 5004),
                               sat_lon=134.0, band="Ka")

        self._sim_active = True
        self._sim_panel.setVisible(True)
        # 给仿真面板分配宽度（第4列）
        sizes = self._top_splitter.sizes()
        if len(sizes) == 4:
            total = sum(sizes[:3])
            sim_w = 280
            chart_w = max(400, total - sizes[0] - sizes[2] - sim_w)
            self._top_splitter.setSizes([sizes[0], chart_w, sizes[2], sim_w])
        self._sim_btn.setText("停止仿真")
        self._set_variant(self._sim_btn, "danger")
        self._control_panel.set_enabled(True)
        self.status_message.emit("仿真模式已启动 (MockModem UDP 45679)", 3000)

    def _stop_simulation(self) -> None:
        if self._mock_modem:
            self._mock_modem.stop()
            self._mock_modem = None

        self._sim_active = False
        self._sim_panel.setVisible(False)
        # 收回仿真面板宽度
        sizes = self._top_splitter.sizes()
        if len(sizes) == 4:
            sim_w = sizes[3]
            self._top_splitter.setSizes([sizes[0], sizes[1] + sim_w, sizes[2], 0])
        self._sim_btn.setText("仿真")
        self._set_variant(self._sim_btn, "")
        self._control_panel.set_enabled(False)
        self.status_message.emit("仿真模式已停止", 2000)

    def _on_sim_sat_changed(self, lon: float, freq: float) -> None:
        if self._mock_modem:
            band = "Ka" if freq > 15.0 else "Ku"
            self._mock_modem.set_satellite(lon, band)

    def _on_sim_blockage(self, duration_s: float) -> None:
        if self._mock_modem:
            self._mock_modem.inject_blockage(duration_s)
            self.status_message.emit(f"遮挡注入 ({duration_s}s)", 2000)

    def _on_sim_rain_changed(self, db: float) -> None:
        if self._mock_modem:
            self._mock_modem.set_rain_fade(db)

    def _on_sim_baseline(self, db: float) -> None:
        if self._mock_modem:
            self._mock_modem.set_snr_baseline(db)

    def _on_sim_heading(self, heading: float) -> None:
        if self._mock_modem:
            self._mock_modem.set_heading(heading)

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

    def _set_conn_state(self, connected: bool, dev: str = "—", detail: str = "") -> None:
        """更新设备状态卡显示（dev 名 + 状态文案）+ 重新着色。"""
        self._cs_dev.setText(dev or "—")
        self._cs_stat.setText(detail or ("LINK OK" if connected else "Disconnected"))
        self._style_conn_card(S.palette(self._theme), "small")
