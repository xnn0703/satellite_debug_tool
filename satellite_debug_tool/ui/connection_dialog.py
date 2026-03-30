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


class ConnectionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connection Settings")
        self.resize(400, 200)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self.serial_tab = self._create_serial_tab()
        self.udp_tab = self._create_udp_tab()
        self.tabs.addTab(self.serial_tab, "Serial")
        self.tabs.addTab(self.udp_tab, "UDP")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _create_serial_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        port_layout = QHBoxLayout()
        port_layout.addWidget(QLabel("Port:"))
        self.serial_port_combo = QComboBox()
        self.serial_port_combo.addItems(SerialWorker.list_ports())
        port_layout.addWidget(self.serial_port_combo)
        layout.addLayout(port_layout)

        baud_layout = QHBoxLayout()
        baud_layout.addWidget(QLabel("Baudrate:"))
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
        remote_layout.addWidget(QLabel("Remote IP:"))
        self.udp_remote_ip = QLineEdit("192.168.1.12")
        remote_layout.addWidget(self.udp_remote_ip)
        layout.addLayout(remote_layout)

        remote_port_layout = QHBoxLayout()
        remote_port_layout.addWidget(QLabel("Remote Port:"))
        self.udp_remote_port = QSpinBox()
        self.udp_remote_port.setRange(1, 65535)
        self.udp_remote_port.setValue(4004)
        remote_port_layout.addWidget(self.udp_remote_port)
        layout.addLayout(remote_port_layout)

        local_port_layout = QHBoxLayout()
        local_port_layout.addWidget(QLabel("Local Port:"))
        self.udp_local_port = QSpinBox()
        self.udp_local_port.setRange(1, 65535)
        self.udp_local_port.setValue(45678)
        local_port_layout.addWidget(self.udp_local_port)
        layout.addLayout(local_port_layout)

        layout.addStretch()
        return widget

    def _on_ok(self):
        self.accept()

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
