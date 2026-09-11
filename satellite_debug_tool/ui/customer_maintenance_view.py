"""Customer maintenance view with mount configuration and signed-package OTA."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, Mapping, Optional

from PySide6.QtCore import QSignalBlocker, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QDoubleSpinBox,
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

from satellite_debug_tool.core.product import (
    Availability,
    ProductSnapshot,
    ProductValue,
    customer_product_policy,
    product_identity_matches,
)
from satellite_debug_tool.core.protocol import (
    MOUNT_CONTRACT_FRD1,
    MOUNT_STATUS_VALID_ANGLES,
    MOUNT_STATUS_VALID_CONTRACT,
    MOUNT_STATUS_VALID_EXPECTED_RBV,
    MOUNT_STATUS_VALID_RBV_VERIFIED,
    MOUNT_STATUS_VALID_READBACK_RBV,
    MOUNT_STATUS_VALID_RESTART_REQUIRED,
    SERVICE_FEATURE_DEVICE_MOUNT,
)
from satellite_debug_tool.core.session import (
    DeviceSessionScope,
    MountConfigurationController,
    MountConfigurationStatus,
    OtaArtifactToken,
    SessionOperationGateway,
)
from satellite_debug_tool.core.security import (
    FirmwarePackage,
    FirmwarePackageError,
    FirmwarePackageErrorCode,
    TrustedFirmwareKey,
    load_bundled_trusted_keys,
    verify_firmware_package,
)
from satellite_debug_tool.i18n import register_translatable, tr, trc, tr_source
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.attitude_widget import AttitudeWidget
from satellite_debug_tool.ui.semantic_style import set_semantic_property


class CustomerMaintenanceView(QWidget):
    """Device inventory, atomic mount setup, and guarded customer OTA."""

    status_message = Signal(str, int)

    def __init__(
        self,
        live_view,
        device_view,
        settings,
        parent: Optional[QWidget] = None,
        *,
        trusted_keys: Optional[Mapping[str, TrustedFirmwareKey]] = None,
        operation_gateway_factory: Optional[
            Callable[[], SessionOperationGateway]
        ] = None,
    ) -> None:
        super().__init__(parent)
        if operation_gateway_factory is not None and not callable(
            operation_gateway_factory
        ):
            raise TypeError("operation_gateway_factory must be callable")
        self._live = live_view
        self._device = device_view
        self._settings = settings
        self._store = live_view.product_store()
        operation_gateway = (
            operation_gateway_factory()
            if operation_gateway_factory is not None
            else None
        )
        if operation_gateway_factory is not None and operation_gateway is None:
            raise ValueError("operation_gateway_factory must return a gateway")
        self._mount_controller = MountConfigurationController(
            live_view.session_core(),
            parent=self,
            operation_gateway=operation_gateway,
        )
        self._theme = "dark"
        self._mount_authoritative: Optional[tuple[float, float, float]] = None
        self._mount_dirty = False
        self._mount_draft_scope: Optional[tuple[object, str, str]] = None
        self._mount_preview_model_hw = ""
        self._mount_preview_angles: Optional[tuple[float, float, float]] = None
        self._view_active = False
        self._mount_status_source = "No pending mount change"
        self._mount_status_values: dict = {}
        self._package: Optional[FirmwarePackage] = None
        self._package_path: Optional[Path] = None
        self._package_scope: Optional[DeviceSessionScope] = None
        self._package_artifact: Optional[OtaArtifactToken] = None
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
        self._store.updated.connect(self._on_store_updated)
        self._mount_controller.pending_changed.connect(
            self._on_mount_pending_changed
        )
        self._mount_controller.status_changed.connect(self._on_mount_status)
        live_view.session_core().device_transaction_changed.connect(
            lambda _active: self._refresh_mount()
        )
        self._live.connection_state_changed.connect(self._on_connection_changed)
        phase_signal = getattr(
            self._live, "device_connection_phase_changed", None
        )
        if phase_signal is not None:
            phase_signal.connect(lambda _phase: self.refresh())
        self._device.ota_status_changed.connect(self._on_ota_status)
        self._device.ota_artifact_changed.connect(self._on_ota_artifact_changed)
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
        self._subtitle = QLabel(
            tr("Device inventory, installation attitude, and authenticated firmware updates.")
        )
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
                "device_uid",
                "mac_address",
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

        mount = QFrame()
        mount.setObjectName("customerMaintenanceSection")
        mount_layout = QHBoxLayout(mount)
        mount_layout.setContentsMargins(14, 10, 14, 13)
        mount_layout.setSpacing(16)
        mount_controls = QVBoxLayout()
        mount_controls.setSpacing(8)
        self._mount_title = QLabel(tr("Device installation attitude"))
        self._mount_title.setObjectName("customerControlTitle")
        mount_controls.addWidget(self._mount_title)
        self._mount_contract = QLabel(
            tr("FRD carrier: +X forward / +Y right / +Z down · ZYX · degrees")
        )
        self._mount_contract.setObjectName("customerPackageDetail")
        self._mount_contract.setWordWrap(True)
        mount_controls.addWidget(self._mount_contract)
        mount_grid = QGridLayout()
        mount_grid.setHorizontalSpacing(10)
        mount_grid.setVerticalSpacing(7)
        self._mount_yaw_label = QLabel(tr("Yaw (+ right)"))
        self._mount_pitch_label = QLabel(tr("Pitch (+ nose up)"))
        self._mount_roll_label = QLabel(tr("Roll (+ left side up)"))
        self._mount_yaw = self._mount_input(-180.0, 180.0)
        self._mount_pitch = self._mount_input(-90.0, 90.0)
        self._mount_roll = self._mount_input(-180.0, 180.0)
        for row, (label, field) in enumerate(
            (
                (self._mount_yaw_label, self._mount_yaw),
                (self._mount_pitch_label, self._mount_pitch),
                (self._mount_roll_label, self._mount_roll),
            )
        ):
            mount_grid.addWidget(label, row, 0)
            mount_grid.addWidget(field, row, 1)
        mount_grid.setColumnStretch(1, 1)
        mount_controls.addLayout(mount_grid)
        self._mount_readback = QLabel(tr("Waiting for device mount readback"))
        self._mount_readback.setObjectName("customerPackageDetail")
        self._mount_readback.setWordWrap(True)
        mount_controls.addWidget(self._mount_readback)
        self._mount_transaction_state = QLabel(tr("No pending mount change"))
        self._mount_transaction_state.setObjectName("customerMountTransactionState")
        self._mount_transaction_state.setWordWrap(True)
        mount_controls.addWidget(self._mount_transaction_state)
        self._save_mount_btn = QPushButton(tr("Save and restart"))
        self._save_mount_btn.setMinimumHeight(34)
        self._save_mount_btn.clicked.connect(self._request_mount)
        mount_controls.addWidget(self._save_mount_btn)
        mount_controls.addStretch(1)
        mount_layout.addLayout(mount_controls, 2)

        self._mount_preview = AttitudeWidget()
        self._mount_preview.setMinimumSize(420, 280)
        self._mount_preview.set_yaw_reference("legacy")
        self._mount_preview.setUpdatesEnabled(False)
        mount_layout.addWidget(self._mount_preview, 3)
        root.addWidget(mount)

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

    def _mount_input(self, minimum: float, maximum: float) -> QDoubleSpinBox:
        field = QDoubleSpinBox()
        field.setDecimals(1)
        field.setSingleStep(0.1)
        field.setRange(minimum, maximum)
        field.setSuffix(" °")
        field.setKeyboardTracking(False)
        field.valueChanged.connect(self._mark_mount_dirty)
        return field

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
        if key == "device_uid":
            return tr("MCU UID")
        if key == "mac_address":
            return tr("MAC address")
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
        self._identity_labels["device_uid"].setText(self._text(identity.device_uid))
        self._identity_labels["mac_address"].setText(self._text(identity.mac_address))
        self._identity_labels["main_fw"].setText(self._text(identity.main_firmware))
        self._identity_labels["boot_fw"].setText(self._text(identity.boot_firmware))
        self._identity_labels["protocol"].setText(self._text(identity.protocol_version))
        self._refresh_components(snapshot)
        self._refresh_mount()
        self._refresh_actions()

    @Slot()
    def _on_store_updated(self) -> None:
        if self._view_active:
            self.refresh()

    def _refresh_mount(self) -> None:
        service_state = self._live.customer_service_state()
        device_online = self._device_online()
        status = self._store.mount_status_record if device_online else None
        authoritative = self._mount_angles(status)
        draft_match = self._mount_draft_matches_current()
        if self._mount_dirty and draft_match is False:
            self._mount_dirty = False
            self._mount_draft_scope = None
        if authoritative is not None:
            self._mount_authoritative = authoritative
            if self._mount_controller.pending is None:
                if not self._mount_dirty:
                    self._load_mount_inputs(authoritative)
                elif all(
                    abs(value - actual) <= 0.051
                    for value, actual in zip(self._mount_target(), authoritative)
                ):
                    self._mount_dirty = False
                    self._mount_draft_scope = None

        if self._view_active:
            hardware = str(
                self._live.profile_store().current_hw_type() or ""
            ).strip().lower()
            if hardware != self._mount_preview_model_hw:
                if hardware:
                    self._mount_preview.try_load_device_model(hardware)
                else:
                    self._mount_preview.clear_device_model()
                self._mount_preview_model_hw = hardware
            self._update_mount_preview()

        protocol = self._store.service_protocol
        capabilities = self._store.capabilities_record
        identity_ready = self._live.session_core().device_scope().has_product_identity
        if not device_online:
            self._mount_readback.setText(tr("Offline"))
        elif self._store.product_identity is None or protocol is None:
            self._mount_readback.setText(tr("Waiting for product service"))
        elif not service_state.customer_service_supported:
            self._mount_readback.setText(
                tr("Installation attitude is not supported by this firmware")
            )
        elif int(protocol) < 8:
            self._mount_readback.setText(
                tr("Installation attitude is not supported by this firmware")
            )
        elif capabilities is None:
            self._mount_readback.setText(tr("Waiting for product service"))
        elif not (
            capabilities.feature_flags & SERVICE_FEATURE_DEVICE_MOUNT
        ):
            self._mount_readback.setText(
                tr("Installation attitude is not supported by this firmware")
            )
        elif not service_state.mount_configuration_ready:
            self._mount_readback.setText(tr("Waiting for product service"))
        elif not identity_ready:
            self._mount_readback.setText(
                tr("Waiting for device serial number or unique identifier")
            )
        elif status is None:
            self._mount_readback.setText(tr("Waiting for device mount readback"))
        elif authoritative is None:
            self._mount_readback.setText(
                tr("Device mount contract is unavailable or incompatible")
            )
        else:
            detail = tr(
                "Readback yaw {yaw:.1f}°, pitch {pitch:.1f}°, roll {roll:.1f}°",
                yaw=authoritative[0],
                pitch=authoritative[1],
                roll=authoritative[2],
            )
            rbv = self._mount_rbv_text(status)
            if not (status.valid_mask & MOUNT_STATUS_VALID_RESTART_REQUIRED):
                restart = tr("restart state unavailable")
            elif status.restart_required:
                restart = tr("restart required")
            else:
                restart = tr("active")
            self._mount_readback.setText(f"{detail} · {rbv} · {restart}")

        status_ready = (
            status is not None
            and authoritative is not None
            and bool(status.valid_mask & MOUNT_STATUS_VALID_RESTART_REQUIRED)
        )
        can_edit = (
            device_online
            and service_state.mount_configuration_ready
            and identity_ready
            and status_ready
            and self._mount_controller.pending is None
            and self._mount_controller.transaction_available
            and (not self._mount_dirty or draft_match is True)
        )
        for field in (self._mount_yaw, self._mount_pitch, self._mount_roll):
            field.setEnabled(can_edit)
        self._save_mount_btn.setEnabled(can_edit and self._mount_dirty)

    @staticmethod
    def _mount_angles(status) -> Optional[tuple[float, float, float]]:
        if status is None:
            return None
        required = MOUNT_STATUS_VALID_CONTRACT | MOUNT_STATUS_VALID_ANGLES
        if status.valid_mask & required != required:
            return None
        if status.mount_contract_id != MOUNT_CONTRACT_FRD1:
            return None
        angles = (
            float(status.mount_yaw_deg),
            float(status.mount_pitch_deg),
            float(status.mount_roll_deg),
        )
        return angles if all(math.isfinite(value) for value in angles) else None

    @staticmethod
    def _mount_rbv_text(status) -> str:
        """Render device-authoritative external-INS RBV expectation and readback.

        The maintenance page intentionally does not recompute RBV from the
        three editable device-to-carrier angles.  The device owns that result
        through ``ext_ins_rot + mount_yaw/pitch/roll``; the legacy
        ``imu_mount_rotation`` diagnostic is unrelated to this display.
        """

        expected_valid = bool(status.valid_mask & MOUNT_STATUS_VALID_EXPECTED_RBV)
        readback_valid = bool(status.valid_mask & MOUNT_STATUS_VALID_READBACK_RBV)
        verified_valid = bool(status.valid_mask & MOUNT_STATUS_VALID_RBV_VERIFIED)
        if not expected_valid:
            return tr("RBV expectation unavailable")
        expected_values = (
            status.expected_rbv_x_deg,
            status.expected_rbv_y_deg,
            status.expected_rbv_z_deg,
        )
        if not all(math.isfinite(value) for value in expected_values):
            return tr("Device readback unavailable")
        expected = tr(
            "expected RBV {x:.1f}/{y:.1f}/{z:.1f}°",
            x=status.expected_rbv_x_deg,
            y=status.expected_rbv_y_deg,
            z=status.expected_rbv_z_deg,
        )
        if not readback_valid:
            return f"{expected}; {tr('Device readback unavailable')}"
        readback_values = (
            status.readback_rbv_x_deg,
            status.readback_rbv_y_deg,
            status.readback_rbv_z_deg,
        )
        if not all(math.isfinite(value) for value in readback_values):
            return f"{expected}; {tr('Device readback unavailable')}"
        if not verified_valid:
            verified = tr("verification unavailable")
        elif status.rbv_verified:
            verified = tr("verified")
        else:
            verified = tr("not verified")
        return tr(
            "{expected}; readback {x:.1f}/{y:.1f}/{z:.1f}° ({verified})",
            expected=expected,
            x=status.readback_rbv_x_deg,
            y=status.readback_rbv_y_deg,
            z=status.readback_rbv_z_deg,
            verified=verified,
        )

    def _load_mount_inputs(self, angles: tuple[float, float, float]) -> None:
        with QSignalBlocker(self._mount_yaw), QSignalBlocker(
            self._mount_pitch
        ), QSignalBlocker(self._mount_roll):
            self._mount_yaw.setValue(angles[0])
            self._mount_pitch.setValue(angles[1])
            self._mount_roll.setValue(angles[2])
        self._update_mount_preview()

    def _mark_mount_dirty(self, *_args) -> None:
        was_dirty = self._mount_dirty
        target = self._mount_target()
        self._mount_dirty = (
            self._mount_authoritative is None
            or any(
                abs(value - actual) > 0.051
                for value, actual in zip(target, self._mount_authoritative)
            )
        )
        if self._mount_dirty and not was_dirty:
            self._mount_draft_scope = self._mount_scope()
        elif not self._mount_dirty:
            self._mount_draft_scope = None
        self._update_mount_preview()
        self._refresh_mount()

    def _mount_scope(self) -> tuple[object, str, str]:
        """Return endpoint plus any authoritative device identity currently known."""

        session = self._live.session_core()
        facts = dict(session.device_scope().product_identity_facts)
        uid = facts.get("product_device_uid", "")
        serial = facts.get("product_serial_number", "")
        return session.endpoint, uid, serial

    def _mount_draft_matches_current(self) -> Optional[bool]:
        """Resolve draft ownership; None waits for identity facts after reconnect."""

        scope = self._mount_draft_scope
        if not self._mount_dirty or scope is None:
            return True
        endpoint, uid, serial = self._mount_scope()
        draft_endpoint, draft_uid, draft_serial = scope
        if (
            draft_endpoint is not None
            and endpoint is not None
            and endpoint != draft_endpoint
        ):
            return False
        for expected, actual in ((draft_uid, uid), (draft_serial, serial)):
            if not expected:
                continue
            if not actual:
                return None
            if actual != expected:
                return False
        return True

    def _mount_target(self) -> tuple[float, float, float]:
        return (
            self._mount_yaw.value(),
            self._mount_pitch.value(),
            self._mount_roll.value(),
        )

    def _update_mount_preview(self) -> None:
        if not self._view_active:
            return
        yaw, pitch, roll = self._mount_target()
        angles = (yaw, pitch, roll)
        if angles == self._mount_preview_angles:
            return
        self._mount_preview_angles = angles
        self._mount_preview.update_attitude(
            roll,
            pitch,
            yaw,
            "mount_roll",
            "mount_pitch",
            "mount_yaw",
        )

    def _request_mount(self) -> None:
        if not self._mount_dirty or self._mount_controller.pending is not None:
            return
        yaw, pitch, roll = self._mount_target()
        answer = QMessageBox.warning(
            self,
            tr("Save installation attitude"),
            tr(
                "Save yaw {yaw:.1f}°, pitch {pitch:.1f}°, roll {roll:.1f}° and restart the device?",
                yaw=yaw,
                pitch=pitch,
                roll=roll,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._mount_controller.request_mount(yaw, pitch, roll)

    @Slot(bool)
    def _on_mount_pending_changed(self, pending: bool) -> None:
        _ = pending
        self._refresh_mount()

    @Slot(object, object)
    def _on_mount_status(
        self,
        status: MountConfigurationStatus,
        values: dict,
    ) -> None:
        sources = {
            MountConfigurationStatus.IDLE: tr_source("No pending mount change"),
            MountConfigurationStatus.SEND_FAILED: tr_source("Mount command could not be sent"),
            MountConfigurationStatus.WAITING_RESPONSE: tr_source("Waiting for mount response (request {request_id})"),
            MountConfigurationStatus.WAITING_READBACK: tr_source("Mount accepted; waiting for authoritative readback"),
            MountConfigurationStatus.WAITING_RESTART: tr_source("Mount saved; waiting for device restart and final readback"),
            MountConfigurationStatus.APPLIED: tr_source("Installation attitude applied and verified"),
            MountConfigurationStatus.INVALID_REQUEST: tr_source("Invalid mount request"),
            MountConfigurationStatus.OUT_OF_RANGE: tr_source("Installation angle is outside the supported range"),
            MountConfigurationStatus.STATE_NOT_ALLOWED: tr_source("Mount configuration is not allowed in the current state"),
            MountConfigurationStatus.NOT_SUPPORTED: tr_source("Installation attitude is not supported by this firmware"),
            MountConfigurationStatus.BUSY: tr_source("Device is busy"),
            MountConfigurationStatus.IDENTITY_UNAVAILABLE: tr_source("Waiting for device serial number or unique identifier"),
            MountConfigurationStatus.INTERNAL_ERROR: tr_source("Device did not confirm the mount transaction"),
            MountConfigurationStatus.RESPONSE_TIMEOUT: tr_source("Mount response timed out"),
            MountConfigurationStatus.READBACK_TIMEOUT: tr_source("Mount readback timed out"),
            MountConfigurationStatus.RESTART_SEND_FAILED: tr_source("Device restart command could not be sent"),
            MountConfigurationStatus.RESTART_TIMEOUT: tr_source("Device did not return with the requested mount"),
            MountConfigurationStatus.SESSION_CHANGED: tr_source("Mount operation cancelled because the device changed"),
        }
        self._set_mount_status(sources[status], **values)
        if status is MountConfigurationStatus.APPLIED:
            self._mount_dirty = False
            self._mount_draft_scope = None
        pending = status in {
            MountConfigurationStatus.WAITING_RESPONSE,
            MountConfigurationStatus.WAITING_READBACK,
            MountConfigurationStatus.WAITING_RESTART,
        }
        set_semantic_property(
            self._mount_transaction_state,
            "result",
            "" if pending else ("ok" if status is MountConfigurationStatus.APPLIED else "error"),
        )
        if not pending:
            self.status_message.emit(
                tr(self._mount_status_source, **self._mount_status_values),
                5000,
            )
        self._refresh_mount()

    def _set_mount_status(self, source: str, **values) -> None:
        self._mount_status_source = source
        self._mount_status_values = dict(values)
        self._mount_transaction_state.setText(tr(source, **values))

    def _refresh_components(self, snapshot: ProductSnapshot) -> None:
        rows = (
            (tr("Converter"), snapshot.converter),
            (tr("TX array"), snapshot.tx_array),
            (tr("RX array"), snapshot.rx_array),
        )
        for row, (name, component) in enumerate(rows):
            online = (
                "—"
                if component.online.value is None
                else tr("Online") if component.online.value else tr("Offline")
            )
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
        ota_identity = self._ota_identity_scope()
        if ota_identity is None:
            self._set_local_status("Wait for authoritative product identity before selecting a package")
            return False
        scope, expected_product = ota_identity
        hardware = scope.hardware_type
        current_version = scope.product_firmware
        if not self._trusted_keys:
            self._set_local_status("No trusted firmware signing key is installed")
            return False
        try:
            package = verify_firmware_package(
                path,
                expected_product=expected_product,
                expected_hardware=hardware,
                current_version=str(current_version),
                trusted_keys=self._trusted_keys,
            )
        except FirmwarePackageError as exc:
            self._clear_package_selection()
            self._package_error = exc
            self._package_label.setText(tr("Package rejected"))
            self._package_detail.setText(self._package_error_text(exc))
            self._refresh_actions()
            return False
        artifact = self._device.load_customer_ota_package(package)
        if artifact is None:
            self._set_local_status("Device OTA service is not ready")
            return False
        self._package = package
        self._package_path = path
        self._package_scope = scope
        self._package_artifact = artifact
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
        package = self._package
        package_scope = self._package_scope
        package_artifact = self._package_artifact
        if package is None or package_scope is None or package_artifact is None:
            return
        snapshot = self._store.snapshot()
        current = self._text(snapshot.identity.main_firmware)
        answer = QMessageBox.warning(
            self,
            tr("Install firmware update"),
            tr(
                "Install signed firmware {target} over {current}? The device will reboot during the update.",
                target=package.version,
                current=current,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        current_identity = self._ota_identity_scope()
        if (
            self._package is not package
            or self._package_scope != package_scope
            or self._package_artifact != package_artifact
            or current_identity is None
            or current_identity[0] != package_scope
        ):
            self._set_local_status(
                "Device session changed; select the firmware again"
            )
            self._refresh_actions()
            return
        if not self._device.start_customer_ota(package_artifact):
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
            self._clear_package_selection()
            self._progress.setValue(0)
            self._mount_authoritative = None
        self.refresh()

    @Slot(object)
    def _on_ota_artifact_changed(self, artifact: object) -> None:
        if self._package_artifact is not None and artifact != self._package_artifact:
            self._clear_package_selection()
            self._set_local_status(
                "Firmware selection changed; verify the signed package again"
            )
        self._refresh_actions()

    def _clear_package_selection(self) -> None:
        self._package = None
        self._package_path = None
        self._package_scope = None
        self._package_artifact = None
        self._package_error = None
        if hasattr(self, "_package_label"):
            self._package_label.setText(tr("No signed package selected"))
            self._package_detail.setText("—")

    def _refresh_actions(self) -> None:
        ota_identity = self._ota_identity_scope()
        if (
            self._package is not None
            and (
                ota_identity is None
                or self._package_scope != ota_identity[0]
            )
        ):
            self._clear_package_selection()
        available = (
            ota_identity is not None
            and self._device_online()
            and self._device.customer_ota_available()
        )
        trusted = bool(self._trusted_keys) and not self._trust_error
        self._select_btn.setEnabled(available and trusted)
        self._upload_btn.setEnabled(
            available
            and self._package is not None
            and self._package_artifact is not None
            and self._device.customer_ota_available(self._package_artifact)
        )
        if not trusted:
            self._package_detail.setText(
                tr("No trusted firmware signing key is installed")
                if not self._trust_error
                else tr("Firmware signature verification is unavailable")
            )

    def _ota_identity_scope(
        self,
    ) -> Optional[tuple[DeviceSessionScope, str]]:
        """Return the exact current-session OTA identity and signed product name."""

        if not self._device_online():
            return None
        service = self._live.session_core().customer_service_state()
        if not service.customer_service_supported:
            return None
        hardware_value = self._live.profile_store().current_hw_type()
        hardware = str(hardware_value or "").strip().lower()
        policy = customer_product_policy(hardware)
        if policy is None or not policy.customer_ota_product:
            return None
        snapshot = self._store.snapshot()
        model_value = snapshot.identity.model
        version_value = snapshot.identity.main_firmware
        if (
            model_value.availability != Availability.VALID
            or version_value.availability != Availability.VALID
            or not model_value.value
            or not version_value.value
            or not product_identity_matches(policy, str(model_value.value))
        ):
            return None
        model = str(model_value.value).strip()
        version = str(version_value.value).strip()
        if not model or not version:
            return None
        scope = self._live.session_core().device_scope()
        if (
            scope.hardware_type != hardware
            or scope.product_identity != model.lower()
            or scope.product_firmware != version
            or not scope.has_immutable_identity
            or not scope.identity_facts_consistent
            or not scope.firmware_facts_consistent
        ):
            return None
        return scope, policy.customer_ota_product

    def _device_online(self) -> bool:
        checker = getattr(self._live, "is_device_online", None)
        return bool(checker()) if checker is not None else bool(self._live.is_connected())

    def activate_view(self) -> None:
        """Refresh and enable the installation 3D preview while visible."""

        if self._view_active:
            return
        self._view_active = True
        self._mount_preview.setUpdatesEnabled(True)
        self._mount_preview_angles = None
        self.refresh()

    def deactivate_view(self) -> None:
        """Suspend hidden OpenGL preview updates while retaining edit state."""

        if not self._view_active:
            return
        self._view_active = False
        self._mount_preview.setUpdatesEnabled(False)

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = theme
        pal = S.palette(theme)
        self.setStyleSheet(
            f"CustomerMaintenanceView {{ background: {pal['bg']}; color: {pal['text']}; }}"
            f"#customerMaintenanceSection {{ background: {pal['panel']}; border: 1px solid {pal['border']}; "
            f"border-radius: 6px; }}"
            f"#customerMaintenanceLabel, #customerPackageDetail {{ color: {pal['text_2']}; }}"
            f"#customerMaintenanceValue {{ color: {pal['text']}; font-weight: 600; }}"
            f"#customerMountTransactionState {{ color: {pal['text_2']}; }}"
            f"#customerMountTransactionState[result='ok'] {{ color: {pal['ok']}; }}"
            f"#customerMountTransactionState[result='error'] {{ color: {pal['err']}; }}"
        )
        self._mount_preview.set_theme(theme, _scale)

    def retranslate_ui(self) -> None:
        self._title.setText(tr("Maintenance"))
        self._subtitle.setText(
            tr("Device inventory, installation attitude, and authenticated firmware updates.")
        )
        self._component_title.setText(tr("Device components"))
        self._component_table.setHorizontalHeaderLabels(
            [tr("Component"), tr("Status"), tr("Temperature"), tr("Voltage"), tr("Version")]
        )
        self._ota_title.setText(tr("Signed firmware update"))
        self._mount_title.setText(tr("Device installation attitude"))
        self._mount_contract.setText(
            tr("FRD carrier: +X forward / +Y right / +Z down · ZYX · degrees")
        )
        self._mount_yaw_label.setText(tr("Yaw (+ right)"))
        self._mount_pitch_label.setText(tr("Pitch (+ nose up)"))
        self._mount_roll_label.setText(tr("Roll (+ left side up)"))
        self._save_mount_btn.setText(tr("Save and restart"))
        self._mount_transaction_state.setText(
            tr(self._mount_status_source, **self._mount_status_values)
        )
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
