"""用户产物装配：客户会话与日常页面，共用领域模型和更新事务。"""

from PySide6.QtCore import QSize, QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QStatusBar, QToolBar, QWidget
from satellite_debug_tool import __version__
from satellite_debug_tool.edition import Edition, edition_of
from satellite_debug_tool.core.comm import UdpEndpointBroker
from satellite_debug_tool.core.customer import CustomerDeviceDirectory
from satellite_debug_tool.core.session import EndpointSessionDirectory
from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.ui.application_icon import application_icon
from satellite_debug_tool.ui.application_window_actions import ApplicationWindowActions
from satellite_debug_tool.ui.customer_session_bundle import CustomerEndpointSessionBundleFactory
from satellite_debug_tool.ui.customer_workspace import CustomerWorkspace
from satellite_debug_tool.ui.settings_dialog import SettingsDialog, SETTINGS_SCOPE_CUSTOMER
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.qss import build


class UserWindow(ApplicationWindowActions, QMainWindow):
    def __init__(self, settings):
        super().__init__()
        if edition_of(settings) is not Edition.USER:
            raise ValueError("UserWindow requires user edition settings")
        self._settings = settings
        self._pending_update_request = None
        self._bg_check_thread = None
        self.resize(1280, 800)
        self.setMinimumSize(1024, 600)
        self._udp_broker = UdpEndpointBroker(local_port=int(settings.get("device_udp.local_port", 45678)), parent=self)
        self._endpoint_directory = EndpointSessionDirectory(self._udp_broker, parent=self)
        self._customer_bundle_factory = CustomerEndpointSessionBundleFactory(
            settings=settings, session_directory=self._endpoint_directory, status_sink=self._on_status_message, parent=self)
        self._customer_devices = CustomerDeviceDirectory(settings, self._endpoint_directory,
            supplemental_facts_provider=self._customer_bundle_factory, parent=self)
        self._customer_bundle_factory.bind_device_directory(self._customer_devices)
        self._customer = CustomerWorkspace(None, settings, None, device_directory=self._customer_devices,
                                         page_bundle_factory=self._customer_bundle_factory,
                                         tcp_shell_session_directory=self._endpoint_directory)
        self._customer.add_requested.connect(self._on_add_customer_device)
        self._customer.edit_requested.connect(self._on_edit_customer_device)
        self._customer.delete_requested.connect(self._on_delete_customer_device)
        self._customer.status_message.connect(self._on_status_message)
        self.setCentralWidget(self._customer)
        bar = QToolBar()
        bar.setMovable(False)
        row = QWidget()
        layout = QHBoxLayout(row)
        mark = QLabel()
        mark.setPixmap(application_icon().pixmap(QSize(26, 26), self.devicePixelRatioF()))
        layout.addWidget(mark)
        self._name = QLabel()
        layout.addWidget(self._name)
        layout.addStretch(1)
        self._theme_button = QPushButton()
        self._theme_button.clicked.connect(self._cycle_theme)
        layout.addWidget(self._theme_button)
        self._update_button = QPushButton()
        self._update_button.clicked.connect(self._on_check_update_clicked)
        layout.addWidget(self._update_button)
        self._settings_button = QPushButton()
        self._settings_button.clicked.connect(self._on_open_settings)
        layout.addWidget(self._settings_button)
        bar.addWidget(row)
        self.addToolBar(bar)
        self.setStatusBar(QStatusBar())
        self._version_label = QLabel(f"v{__version__}")
        self.statusBar().addPermanentWidget(self._version_label)
        self._apply_theme(settings.get("ui.theme", "dark"))
        register_translatable(self)
        self._customer.activate_view()
        QTimer.singleShot(2000, self._kick_silent_update_check)

    def _on_status_message(self, text, timeout=5000):
        self.statusBar().showMessage(text, timeout)

    def _cycle_theme(self):
        self._apply_theme(S.THEMES[(S.THEMES.index(self._theme) + 1) % len(S.THEMES)])
        self._settings.set("ui.theme", self._theme)
        self._settings.persist_preferences()

    def _apply_theme(self, theme):
        from PySide6.QtWidgets import QApplication
        self._theme = S._normalize_theme(theme)
        app = QApplication.instance()
        app.setStyleSheet(build(self._theme))
        S.apply_application_palette(app, self._theme)
        self._customer.set_theme(self._theme)

    def _on_open_settings(self):
        dialog = SettingsDialog(self._settings, self, scope=SETTINGS_SCOPE_CUSTOMER,
                                device_udp_port_editable=lambda: not self._udp_broker.has_active_demand)
        dialog.exec()

    def retranslate_ui(self):
        self.setWindowTitle(tr("Satellite Terminal Tool"))
        self._name.setText(tr("Terminal Tool"))
        self._theme_button.setText(tr("Theme"))
        self._update_button.setText(tr("Check for updates"))
        self._settings_button.setText(tr("Settings"))
        self._version_label.setText(f"v{__version__} · " + tr("User edition"))

    def closeEvent(self, event):
        if not self._shutdown_background_update_check():
            self._reject_close(event, "background update check did not stop")
            return
        if not self._customer.shutdown():
            QMessageBox.warning(self, tr("Device operation in progress"), tr("Finish the active device operation before closing."))
            self._reject_close(event, "customer operation is active")
            return
        if not self._shutdown_customer_resources(event):
            return
        self._launch_pending_update()
        super().closeEvent(event)
