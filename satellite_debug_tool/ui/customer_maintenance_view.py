"""Customer maintenance view with signed-package OTA and no parameter editor."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.product import Availability, ProductSnapshot, ProductValue
from satellite_debug_tool.core.security import (
    FirmwarePackage,
    FirmwarePackageError,
    FirmwarePackageErrorCode,
    TrustedFirmwareKey,
    load_bundled_trusted_keys,
    verify_firmware_package,
)
from satellite_debug_tool.i18n import register_translatable, tr, trc
from satellite_debug_tool.ui import styles as S


class CustomerMaintenanceView(QWidget):
    """Read-only device inventory plus the guarded customer OTA entry."""

    def __init__(
        self,
        live_view,
        device_view,
        settings,
        parent: Optional[QWidget] = None,
        *,
        trusted_keys: Optional[Mapping[str, TrustedFirmwareKey]] = None,
    ) -> None:
        super().__init__(parent)
        self._live = live_view
        self._device = device_view
        self._settings = settings
        self._store = live_view.product_store()
        self._theme = "dark"
        self._package: Optional[FirmwarePackage] = None
        self._package_path: Optional[Path] = None
        self._package_error: Optional[FirmwarePackageError] = None
        self._trust_error = ""
        try:
            self._trusted_keys = dict(
                load_bundled_trusted_keys() if trusted_keys is None else trusted_keys
            )
        except FirmwarePackageError as exc:
            self._trusted_keys = {}
            self._trust_error = exc.detail
        self._ota_status_source = "Idle"
        self._ota_status_values: dict = {}
        self._local_status_source = ""
        self._build_ui()
        self._store.updated.connect(self.refresh)
        self._live.connection_state_changed.connect(self._on_connection_changed)
        self._device.ota_status_changed.connect(self._on_ota_status)
        self.refresh()
        register_translatable(self)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 18)
        root.setSpacing(12)

        header = QVBoxLayout()
        header.setSpacing(2)
        self._title = QLabel(tr("Maintenance"))
        self._title.setObjectName("customerPageTitle")
        self._subtitle = QLabel(tr("Device inventory and authenticated firmware updates."))
        self._subtitle.setObjectName("customerPageSubtitle")
        header.addWidget(self._title)
        header.addWidget(self._subtitle)
        root.addLayout(header)

        identity = QFrame()
        identity.setObjectName("customerMaintenanceSection")
        identity_layout = QGridLayout(identity)
        identity_layout.setContentsMargins(14, 12, 14, 12)
        identity_layout.setHorizontalSpacing(18)
        identity_layout.setVerticalSpacing(7)
        self._identity_labels: dict[str, QLabel] = {}
        self._identity_title_labels: dict[str, QLabel] = {}
        for row, key in enumerate(
            (
                "model",
                "serial",
                "main_fw",
                "boot_fw",
                "protocol",
            )
        ):
            title = QLabel(self._identity_title(key))
            title.setObjectName("customerMaintenanceLabel")
            value = QLabel("—")
            value.setObjectName("customerMaintenanceValue")
            identity_layout.addWidget(title, row, 0)
            identity_layout.addWidget(value, row, 1)
            self._identity_labels[key] = value
            self._identity_title_labels[key] = title
        identity_layout.setColumnStretch(1, 1)
        root.addWidget(identity)

        components = QFrame()
        components.setObjectName("customerMaintenanceSection")
        components_layout = QVBoxLayout(components)
        components_layout.setContentsMargins(14, 10, 14, 12)
        self._component_title = QLabel(tr("Device components"))
        self._component_title.setObjectName("customerControlTitle")
        components_layout.addWidget(self._component_title)
        self._component_table = QTableWidget(3, 5)
        self._component_table.setHorizontalHeaderLabels(
            [tr("Component"), tr("Status"), tr("Temperature"), tr("Voltage"), tr("Version")]
        )
        self._component_table.verticalHeader().hide()
        self._component_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._component_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._component_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._component_table.horizontalHeader().setStretchLastSection(True)
        self._component_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._component_table.horizontalHeader().setFixedHeight(28)
        for row in range(self._component_table.rowCount()):
            self._component_table.setRowHeight(row, 26)
        self._component_table.setFixedHeight(
            28 + self._component_table.rowCount() * 26 + 4
        )
        components_layout.addWidget(self._component_table)
        root.addWidget(components)

        ota = QFrame()
        ota.setObjectName("customerMaintenanceSection")
        ota_layout = QVBoxLayout(ota)
        ota_layout.setContentsMargins(14, 10, 14, 13)
        ota_layout.setSpacing(9)
        self._ota_title = QLabel(tr("Signed firmware update"))
        self._ota_title.setObjectName("customerControlTitle")
        ota_layout.addWidget(self._ota_title)
        package_row = QHBoxLayout()
        self._package_label = QLabel(tr("No signed package selected"))
        self._package_label.setWordWrap(True)
        package_row.addWidget(self._package_label, 1)
        self._select_btn = QPushButton(tr("Select package..."))
        self._select_btn.clicked.connect(self._choose_package)
        package_row.addWidget(self._select_btn)
        ota_layout.addLayout(package_row)
        self._package_detail = QLabel("—")
        self._package_detail.setObjectName("customerPackageDetail")
        self._package_detail.setWordWrap(True)
        ota_layout.addWidget(self._package_detail)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        ota_layout.addWidget(self._progress)
        action_row = QHBoxLayout()
        self._ota_status = QLabel(tr("Idle"))
        self._ota_status.setWordWrap(True)
        action_row.addWidget(self._ota_status, 1)
        self._upload_btn = QPushButton(tr("Install update"))
        self._upload_btn.clicked.connect(self._start_ota)
        action_row.addWidget(self._upload_btn)
        self._abort_btn = QPushButton(tr("Abort"))
        self._abort_btn.clicked.connect(self._device.abort_customer_ota)
        action_row.addWidget(self._abort_btn)
        ota_layout.addLayout(action_row)
        root.addWidget(ota)
        root.addStretch(1)

    @staticmethod
    def _text(value: ProductValue) -> str:
        if value.value is None:
            return "—"
        text = str(value.value)
        return tr("{value} (stale)", value=text) if value.availability == Availability.STALE else text

    @staticmethod
    def _identity_title(key: str) -> str:
        if key == "model":
            return tr("Model")
        if key == "serial":
            return tr("Serial number")
        if key == "main_fw":
            return tr("Main firmware")
        if key == "boot_fw":
            return tr("Boot firmware")
        return tr("Service protocol")

    @staticmethod
    def _component_value(value: ProductValue, unit: str = "") -> str:
        if value.value is None:
            return "—"
        if isinstance(value.value, float):
            text = f"{value.value:.1f}"
        else:
            text = str(value.value)
        if unit:
            text = f"{text} {unit}"
        return tr("{value} (stale)", value=text) if value.availability == Availability.STALE else text

    def refresh(self) -> None:
        snapshot = self._store.snapshot()
        identity = snapshot.identity
        self._identity_labels["model"].setText(self._text(identity.model))
        self._identity_labels["serial"].setText(self._text(identity.serial_number))
        self._identity_labels["main_fw"].setText(self._text(identity.main_firmware))
        self._identity_labels["boot_fw"].setText(self._text(identity.boot_firmware))
        self._identity_labels["protocol"].setText(self._text(identity.protocol_version))
        self._refresh_components(snapshot)
        self._refresh_actions()

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
                self._component_value(component.temperature_c, "°C"),
                self._component_value(component.voltage_v, "V"),
                self._text(component.version),
            )
            for column, value in enumerate(values):
                self._component_table.setItem(row, column, QTableWidgetItem(value))

    def _choose_package(self) -> None:
        last_dir = self._settings.get("paths.firmware_dir", "") or ""
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Select signed firmware package"),
            last_dir,
            tr("Signed firmware package (*.sfpkg)"),
        )
        if path:
            self.load_package(Path(path))

    def load_package(self, path: Path) -> bool:
        snapshot = self._store.snapshot()
        hardware = self._live.profile_store().current_hw_type() or ""
        current_version = snapshot.identity.main_firmware.value or ""
        if not self._live.is_connected() or not hardware or not current_version:
            self._set_local_status("Wait for authoritative AFD01 identity before selecting a package")
            return False
        if not self._trusted_keys:
            self._set_local_status("No trusted firmware signing key is installed")
            return False
        try:
            package = verify_firmware_package(
                path,
                expected_product="AFD01",
                expected_hardware=hardware,
                current_version=str(current_version),
                trusted_keys=self._trusted_keys,
            )
        except FirmwarePackageError as exc:
            self._package = None
            self._package_path = None
            self._package_error = exc
            self._package_label.setText(tr("Package rejected"))
            self._package_detail.setText(self._package_error_text(exc))
            self._refresh_actions()
            return False
        if not self._device.load_customer_ota_image(package.firmware, package.firmware_name):
            self._set_local_status("Device OTA service is not ready")
            return False
        self._package = package
        self._package_path = path
        self._package_error = None
        self._package_label.setText(path.name)
        self._render_package_detail()
        self._set_local_status("Package verified and ready")
        self._refresh_actions()
        return True

    @staticmethod
    def _package_error_text(error: FirmwarePackageError) -> str:
        code = error.code
        if code == FirmwarePackageErrorCode.UNSIGNED:
            return tr("The package is unsigned or its signature is malformed")
        if code in {FirmwarePackageErrorCode.UNKNOWN_KEY, FirmwarePackageErrorCode.SIGNATURE_INVALID}:
            return tr("The package signature is not trusted")
        if code in {FirmwarePackageErrorCode.HASH_MISMATCH, FirmwarePackageErrorCode.SIZE_MISMATCH}:
            return tr("The firmware payload is corrupt")
        if code == FirmwarePackageErrorCode.PRODUCT_MISMATCH:
            return tr("The package is for a different product")
        if code == FirmwarePackageErrorCode.HARDWARE_MISMATCH:
            return tr("The package does not support this hardware")
        if code in {FirmwarePackageErrorCode.VERSION_INVALID, FirmwarePackageErrorCode.VERSION_NOT_ALLOWED}:
            return tr("The package version is not allowed for this device")
        if code in {FirmwarePackageErrorCode.CRYPTO_UNAVAILABLE, FirmwarePackageErrorCode.TRUST_STORE_INVALID}:
            return tr("Firmware signature verification is unavailable")
        return tr("The firmware package format is invalid")

    def _start_ota(self) -> None:
        if self._package is None:
            return
        snapshot = self._store.snapshot()
        current = self._text(snapshot.identity.main_firmware)
        answer = QMessageBox.warning(
            self,
            tr("Install firmware update"),
            tr(
                "Install signed firmware {target} over {current}? The device will reboot during the update.",
                target=self._package.version,
                current=current,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not self._device.start_customer_ota():
            self._set_local_status("Device OTA service is not ready")
        self._refresh_actions()

    @Slot(str, object, int, bool)
    def _on_ota_status(self, source: str, values: object, progress: int, active: bool) -> None:
        self._ota_status_source = source
        self._ota_status_values = dict(values) if isinstance(values, dict) else {}
        self._local_status_source = ""
        self._ota_status.setText(trc("DeviceView", source, **self._ota_status_values))
        self._progress.setValue(int(progress))
        self._abort_btn.setEnabled(bool(active))
        self._refresh_actions()

    def _set_local_status(self, source: str) -> None:
        self._ota_status_source = ""
        self._ota_status_values = {}
        self._local_status_source = source
        self._ota_status.setText(tr(source))

    def _render_package_detail(self) -> None:
        if self._package is not None:
            package = self._package
            self._package_detail.setText(
                tr(
                    "Verified {product} {version} · signer {signer} · SHA-256 {digest}",
                    product=package.product,
                    version=package.version,
                    signer=package.key_label,
                    digest=package.firmware_sha256[:12],
                )
            )
        elif self._package_error is not None:
            self._package_detail.setText(self._package_error_text(self._package_error))

    @Slot(bool)
    def _on_connection_changed(self, connected: bool) -> None:
        if not connected:
            self._package = None
            self._package_path = None
            self._package_error = None
            self._package_label.setText(tr("No signed package selected"))
            self._package_detail.setText("—")
            self._progress.setValue(0)
        self.refresh()

    def _refresh_actions(self) -> None:
        available = self._device.customer_ota_available()
        trusted = bool(self._trusted_keys) and not self._trust_error
        self._select_btn.setEnabled(available and trusted)
        self._upload_btn.setEnabled(available and self._package is not None)
        if not trusted:
            self._package_detail.setText(
                tr("No trusted firmware signing key is installed")
                if not self._trust_error
                else tr("Firmware signature verification is unavailable")
            )

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = theme
        pal = S.palette(theme)
        self.setStyleSheet(
            f"CustomerMaintenanceView {{ background: {pal['bg']}; color: {pal['text']}; }}"
            f"#customerMaintenanceSection {{ background: {pal['panel']}; border: 1px solid {pal['border']}; "
            f"border-radius: 6px; }}"
            f"#customerMaintenanceLabel, #customerPackageDetail {{ color: {pal['text_2']}; }}"
            f"#customerMaintenanceValue {{ color: {pal['text']}; font-weight: 600; }}"
        )

    def retranslate_ui(self) -> None:
        self._title.setText(tr("Maintenance"))
        self._subtitle.setText(tr("Device inventory and authenticated firmware updates."))
        self._component_title.setText(tr("Device components"))
        self._component_table.setHorizontalHeaderLabels(
            [tr("Component"), tr("Status"), tr("Temperature"), tr("Voltage"), tr("Version")]
        )
        self._ota_title.setText(tr("Signed firmware update"))
        self._select_btn.setText(tr("Select package..."))
        self._upload_btn.setText(tr("Install update"))
        self._abort_btn.setText(tr("Abort"))
        for key, label in self._identity_title_labels.items():
            label.setText(self._identity_title(key))
        if self._package_path is None:
            self._package_label.setText(tr("No signed package selected"))
        self._render_package_detail()
        if self._ota_status_source:
            self._ota_status.setText(
                trc("DeviceView", self._ota_status_source, **self._ota_status_values)
            )
        elif self._local_status_source:
            self._ota_status.setText(tr(self._local_status_source))
        self.refresh()
