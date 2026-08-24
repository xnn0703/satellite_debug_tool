from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QComboBox,
    QLineEdit,
    QSpinBox,
    QDialogButtonBox,
    QTabWidget,
    QWidget,
)
from satellite_debug_tool.core.comm import SerialWorker
from satellite_debug_tool.i18n import register_translatable, tr


class ConnectionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Connection settings"))
        self.resize(400, 200)
        self._setup_ui()
        register_translatable(self)

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self.serial_tab = self._create_serial_tab()
        self.udp_tab = self._create_udp_tab()
        self.tabs.addTab(self.serial_tab, tr("Serial"))
        self.tabs.addTab(self.udp_tab, "UDP")

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self._buttons.accepted.connect(self._on_ok)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    def _create_serial_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        port_layout = QHBoxLayout()
        self._serial_port_label = QLabel(tr("Port:"))
        port_layout.addWidget(self._serial_port_label)
        self.serial_port_combo = QComboBox()
        self.serial_port_combo.addItems(SerialWorker.list_ports())
        port_layout.addWidget(self.serial_port_combo)
        layout.addLayout(port_layout)

        baud_layout = QHBoxLayout()
        self._baudrate_label = QLabel(tr("Baud rate:"))
        baud_layout.addWidget(self._baudrate_label)
        self.baudrate_combo = QComboBox()
        baudrates = [
            "9600",
            "19200",
            "38400",
            "57600",
            "115200",
            "230400",
            "460800",
            "921600",
        ]
        self.baudrate_combo.addItems(baudrates)
        self.baudrate_combo.setCurrentText("115200")
        baud_layout.addWidget(self.baudrate_combo)
        layout.addLayout(baud_layout)

        layout.addStretch()
        return widget

    def _create_udp_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        remote_layout = QHBoxLayout()
        self._remote_ip_label = QLabel(tr("Remote IP:"))
        remote_layout.addWidget(self._remote_ip_label)
        self.udp_remote_ip = QLineEdit("192.168.1.12")
        remote_layout.addWidget(self.udp_remote_ip)
        layout.addLayout(remote_layout)

        remote_port_layout = QHBoxLayout()
        self._remote_port_label = QLabel(tr("Remote port:"))
        remote_port_layout.addWidget(self._remote_port_label)
        self.udp_remote_port = QSpinBox()
        self.udp_remote_port.setRange(1, 65535)
        self.udp_remote_port.setValue(4004)
        remote_port_layout.addWidget(self.udp_remote_port)
        layout.addLayout(remote_port_layout)

        local_port_layout = QHBoxLayout()
        self._local_port_label = QLabel(tr("Local port:"))
        local_port_layout.addWidget(self._local_port_label)
        self.udp_local_port = QSpinBox()
        self.udp_local_port.setRange(1, 65535)
        self.udp_local_port.setValue(45678)
        local_port_layout.addWidget(self.udp_local_port)
        layout.addLayout(local_port_layout)

        layout.addStretch()
        return widget

    def _on_ok(self):
        self.accept()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(tr("Connection settings"))
        self.tabs.setTabText(0, tr("Serial"))
        self.tabs.setTabText(1, "UDP")
        self._serial_port_label.setText(tr("Port:"))
        self._baudrate_label.setText(tr("Baud rate:"))
        self._remote_ip_label.setText(tr("Remote IP:"))
        self._remote_port_label.setText(tr("Remote port:"))
        self._local_port_label.setText(tr("Local port:"))
        self._buttons.button(QDialogButtonBox.Ok).setText(tr("OK"))
        self._buttons.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))

    def get_config(self) -> dict:
        if self.tabs.currentIndex() == 0:
            return {
                "type": "serial",
                "port": self.serial_port_combo.currentText(),
                "baudrate": int(self.baudrate_combo.currentText()),
            }
        else:
            return {
                "type": "udp",
                "remote_ip": self.udp_remote_ip.text(),
                "remote_port": self.udp_remote_port.value(),
                "local_port": self.udp_local_port.value(),
            }
