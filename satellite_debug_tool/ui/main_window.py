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
from satellite_debug_tool.core.protocol import FrameReceiver, build_debug_control_frame
from satellite_debug_tool.core.data import DataStore
from satellite_debug_tool.io.data_recorder import DataRecorder
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.ui.chart_widget import ChartWidget, COLORS
from satellite_debug_tool.ui.attitude_widget import AttitudeWidget
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.core.config import Settings


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._worker = None
        self._receiver = FrameReceiver()
        self._data_store = DataStore()
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

        splitter = QSplitter(Qt.Vertical)

        # Horizontal splitter for chart + attitude side by side
        top_splitter = QSplitter(Qt.Horizontal)

        self._chart = ChartWidget()
        self._chart.setMinimumHeight(400)
        self._chart.set_dark_theme(True)
        top_splitter.addWidget(self._chart)

        self._attitude = AttitudeWidget()
        self._attitude.setMinimumWidth(350)
        self._attitude.set_dark_theme(True)
        self._attitude.channel_changed.connect(self._on_attitude_channel_changed)
        top_splitter.addWidget(self._attitude)

        top_splitter.setStretchFactor(0, 3)
        top_splitter.setStretchFactor(1, 1)
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

        splitter.addWidget(channel_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

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

        self._update_timer = QTimer()
        self._update_timer.timeout.connect(self._update_display)
        self._update_timer.start(100)

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

    def _on_debug_toggled(self):
        if not self._worker:
            return
        self._debug_enabled = not self._debug_enabled
        frame = build_debug_control_frame(self._debug_enabled)
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
        frames = self._receiver.feed(data)
        for frame in frames:
            self._data_store.update(frame)
            self._frame_count += 1
            if self._is_recording and self._recorder:
                self._recorder.write_frame(data)

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

        for name in new_names:
            idx = len(self._channel_checks)
            cols = 8
            row = idx // cols
            col = idx % cols
            color = COLORS[idx % len(COLORS)]
            container = QWidget()
            container.setFixedHeight(32)
            container.setStyleSheet(
                f"background-color: {S.PANEL_DARK}; border-left: 4px solid {color}; border-radius: 3px; padding: 4px 8px;"
            )
            container_layout = QHBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.setSpacing(4)
            dot = QLabel()
            dot.setFixedSize(10, 10)
            dot.setStyleSheet(f"background-color: {color}; border-radius: 50%;")
            cb = QCheckBox(name)
            cb.setChecked(True)
            cb.setStyleSheet(f"color: {S.TEXT}; border: none; padding: 0px 4px;")
            value_label = QLabel("--")
            value_label.setStyleSheet(
                f"color: {S.TEXT}; min-width: 60px; text-align: right;"
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

        visible_names = [
            name
            for name, cb in self._channel_checks.items()
            if cb.isChecked() and name in channels
        ]
        if visible_names:
            self._chart.set_channels(visible_names)
            values = {}
            for name in visible_names:
                ch = self._data_store.get_channel(name)
                latest = ch.get_latest() if ch else None
                if latest:
                    values[name] = latest[1]
                    self._channel_value_labels[name].setText(f"{latest[1]:.2f}")
            if values:
                latest_ts = next(
                    (
                        self._data_store.get_channel(name).get_latest()[0]
                        for name in visible_names
                        if self._data_store.get_channel(name).get_latest()
                    ),
                    0,
                )
                self._chart.update_data(latest_ts / 1000.0, values)

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
                self._recorder = DataRecorder(filepath)
                if self._recorder.start():
                    self._is_recording = True
                    self._record_btn.setText("Stop")
                    self._record_btn.setStyleSheet(
                        f"background-color: {S.ERROR}; color: white; border: none; border-radius: 2px;"
                    )
                    self._statusbar.showMessage(f"Recording to {filepath}", 3000)
                else:
                    self._recorder = None
                    self._statusbar.showMessage("Failed to start recording", 3000)

    def _on_import_clicked(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "Import Data",
            "",
            "Data Files (*.sdb *.csv);;SDB Files (*.sdb);;CSV Files (*.csv);;All Files (*)",
        )
        if filepath:
            try:
                if filepath.endswith(".csv"):
                    frames = DataImporter.read_csv(filepath)
                else:
                    frames = DataImporter.read_sdb(filepath)
                for frame in frames:
                    self._data_store.update(frame)
                    self._frame_count += 1
                self._chart.set_auto_time_range(True)
                all_timestamps = []
                for ch in self._data_store.get_all_channels().values():
                    for ts, _ in ch.get_values():
                        all_timestamps.append(ts / 1000.0)
                if all_timestamps:
                    self._chart.set_time_range(min(all_timestamps), max(all_timestamps))
                self._statusbar.showMessage(f"Imported data from {filepath}", 3000)
            except Exception as e:
                self._statusbar.showMessage(f"Import failed: {e}", 5000)

    def _on_clear_clicked(self):
        self._chart.clear()
        self._attitude.clear()
        self._data_store.clear()
        self._frame_count = 0
        self._frame_times.clear()
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
