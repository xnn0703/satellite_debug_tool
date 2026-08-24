"""Device page bound to typed parameter and OTA session controllers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QCoreApplication, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.protocol import (
    MetaInfo,
    ParaEntry,
    ParaTableReport,
    PARA_FLAG_READ_ONLY,
    PARA_FLAG_REQUIRES_REBOOT,
    ParaType,
)
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.session import (
    DeviceSessionCore,
    OtaCapabilityState,
    OtaController,
    OtaState,
    OtaStatus,
    ParameterCapabilityState,
    ParameterController,
    ParameterOperation,
    ParameterStatus,
)
from satellite_debug_tool.i18n import (
    mark_raw_text,
    register_translatable,
    set_translatable_text,
    tr,
    trc,
    tr_source,
)
from satellite_debug_tool.ui import styles as S


_PARA_TYPE_NAMES = {
    int(ParaType.INT): "INT",
    int(ParaType.FLOAT): "FLOAT",
    int(ParaType.STRING): "STRING",
    int(ParaType.IP): "IP",
    int(ParaType.UINT8): "UINT8",
    int(ParaType.INT8): "INT8",
    int(ParaType.UINT16): "UINT16",
    int(ParaType.INT16): "INT16",
}

if False:  # Translation extraction declarations for indirect status templates.
    QCoreApplication.translate("DeviceView", "Unchanged")
    QCoreApplication.translate("DeviceView", "Awaiting write confirmation...")
    QCoreApplication.translate("DeviceView", "Send failed")
    QCoreApplication.translate("DeviceView", "✓ Success")
    QCoreApplication.translate("DeviceView", "Waiting for device readback...")
    QCoreApplication.translate("DeviceView", "Target value was not read back")
    QCoreApplication.translate("DeviceView", "✗ {detail}")
    QCoreApplication.translate(
        "DeviceView",
        "✓ Update complete; device is online ({before} → {after})",
    )
    QCoreApplication.translate(
        "DeviceView",
        "✓ Device is back online (version {version}, unchanged)",
    )


CapabilityUiState = ParameterCapabilityState


class DeviceView(QWidget):
    """设备 Tab：设备信息 + 参数表 + OTA。"""

    status_message = Signal(str, int)
    debug_mode_requested = Signal(bool)
    device_transaction_active_changed = Signal(bool)
    handshake_retry_pause_changed = Signal(bool)
    ota_status_changed = Signal(str, object, int, bool)

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        settings=None,
        profile_store: Optional[ProfileStore] = None,
        session_core: Optional[DeviceSessionCore] = None,
    ):
        super().__init__(parent)
        if session_core is not None and profile_store is not None:
            if session_core.profile_store is not profile_store:
                raise ValueError("session_core and profile_store must share one authority")
        self._session_core = session_core or DeviceSessionCore(
            profile_store=profile_store,
            parent=self,
        )
        self._profile_store = self._session_core.profile_store
        self._parameter_controller = ParameterController(
            self._session_core,
            parent=self,
        )
        self._ota_controller = OtaController(
            self._session_core,
            parent=self,
        )
        self._theme = "dark"
        self._scale = "small"
        self._settings = settings   # 可选；用于读取 paths.firmware_dir 作为打开默认目录

        # 设备信息缓存（来自 MetaInfo）
        self._hw_type = "—"
        self._fw_ver = "—"
        self._device_sn = "—"
        self._protocol_ver = 0

        # 参数表
        self._params: list[ParaEntry] = []
        self._para_status_by_name: dict[str, tuple[str, dict[str, Any]]] = {}
        self._para_capability_state = CapabilityUiState.DISCONNECTED
        self._ota_capability_state = CapabilityUiState.DISCONNECTED

        # UI only keeps stable translation keys; transfer state belongs to OtaController.
        self._ota_status_source = "Idle"
        self._ota_status_values: dict[str, Any] = {}

        self._setup_ui()
        self._bind_controllers()
        self._apply_connection_state(self._session_core.connected)
        if self._session_core.meta_info is not None:
            self._apply_session_record(self._session_core.meta_info)
        register_translatable(self)

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        # ---- 设备信息卡片 ----
        info_group = QGroupBox(tr("Device information"))
        info_layout = QGridLayout(info_group)
        info_layout.setSpacing(6)

        self._info_labels = {}
        for row, (key, label) in enumerate([
            ("hw_type", tr("Device type:")),
            ("fw_ver", tr("Firmware version:")),
            ("device_sn", tr("Serial number:")),
            ("protocol_ver", tr("Protocol version:")),
        ]):
            info_layout.addWidget(QLabel(label), row, 0)
            val = QLabel("—")
            mark_raw_text(val)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            info_layout.addWidget(val, row, 1)
            self._info_labels[key] = val

        self._refresh_info_btn = QPushButton(tr("Refresh"))
        self._refresh_info_btn.setFixedWidth(80)
        self._refresh_info_btn.clicked.connect(self._on_refresh_info)
        info_layout.addWidget(self._refresh_info_btn, 0, 2, 2, 1)

        outer.addWidget(info_group)

        # ---- 参数表 ----
        para_group = QGroupBox(tr("Parameter management"))
        para_layout = QVBoxLayout(para_group)

        btn_row = QHBoxLayout()
        self._read_all_btn = QPushButton(tr("Read all"))
        self._read_all_btn.clicked.connect(self._on_read_params)
        self._factory_reset_btn = QPushButton(tr("Factory reset"))
        self._factory_reset_btn.clicked.connect(self._on_factory_reset)
        btn_row.addWidget(self._read_all_btn)
        btn_row.addWidget(self._factory_reset_btn)
        btn_row.addStretch()
        para_layout.addLayout(btn_row)

        self._para_status_label = QLabel("")
        para_layout.addWidget(self._para_status_label)

        self._para_table = QTableWidget(0, 5)
        self._para_table.setHorizontalHeaderLabels([
            tr("Name"),
            tr("Type"),
            tr("Current value"),
            tr("Action"),
            tr("Status"),
        ])
        self._para_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._para_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._para_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._para_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._para_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self._para_table.verticalHeader().setVisible(False)
        self._para_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._para_table.setSelectionBehavior(QTableWidget.SelectRows)
        para_layout.addWidget(self._para_table)

        outer.addWidget(para_group, stretch=1)

        # ---- OTA ----
        ota_group = QGroupBox(tr("Firmware update (OTA)"))
        ota_layout = QVBoxLayout(ota_group)

        file_row = QHBoxLayout()
        self._ota_file_label = QLabel(tr("No file selected"))
        self._ota_select_btn = QPushButton(tr("Select firmware..."))
        self._ota_select_btn.clicked.connect(self._on_select_firmware)
        file_row.addWidget(self._ota_file_label, 1)
        file_row.addWidget(self._ota_select_btn)
        ota_layout.addLayout(file_row)

        ctrl_row = QHBoxLayout()
        self._ota_pause_debug_cb = QCheckBox(tr("Pause live data for full-speed transfer"))
        self._ota_pause_debug_cb.setChecked(True)
        self._ota_upload_btn = QPushButton(tr("Upload and update"))
        self._ota_upload_btn.setEnabled(False)
        self._ota_upload_btn.clicked.connect(self._on_ota_start)
        self._ota_abort_btn = QPushButton(tr("Abort"))
        self._ota_abort_btn.setEnabled(False)
        self._ota_abort_btn.clicked.connect(self._on_ota_abort)
        ctrl_row.addWidget(self._ota_pause_debug_cb)
        ctrl_row.addStretch()
        ctrl_row.addWidget(self._ota_upload_btn)
        ctrl_row.addWidget(self._ota_abort_btn)
        ota_layout.addLayout(ctrl_row)

        self._ota_progress = QProgressBar()
        self._ota_progress.setRange(0, 100)
        self._ota_progress.setValue(0)
        ota_layout.addWidget(self._ota_progress)

        self._ota_status_label = QLabel(tr("Idle"))
        ota_layout.addWidget(self._ota_status_label)

        outer.addWidget(ota_group)

        # ---- 未连接遮罩 ----
        self._overlay = QLabel(tr("Connect a device on the Live tab first"))
        self._overlay.setAlignment(Qt.AlignCenter)
        self._overlay.setStyleSheet(
            "background-color: rgba(30,30,30,200); color: #888; font-size: 16px;"
        )
        self._overlay.setParent(self)
        self._overlay.raise_()

        self._set_controls_enabled(False)

    def _set_para_status(self, source: str, **values: Any) -> None:
        set_translatable_text(
            source,
            self._para_status_label,
            **values,
        )

    def _clear_para_status(self) -> None:
        self._para_status_label.clear()
        if hasattr(self._para_status_label, "_i18n_state_text"):
            delattr(self._para_status_label, "_i18n_state_text")

    def _set_ota_status(self, source: str, **values: Any) -> None:
        self._ota_status_source = source
        self._ota_status_values = dict(values)
        set_translatable_text(
            source,
            self._ota_status_label,
            **values,
        )
        progress = self._ota_progress.value() if hasattr(self, "_ota_progress") else 0
        self.ota_status_changed.emit(
            source,
            dict(values),
            int(progress),
            self._ota_controller.active,
        )

    def customer_ota_available(self) -> bool:
        """Whether the shared engineering OTA transport can accept a verified image."""
        return bool(
            self._session_core.connected
            and self._ota_controller.supported
            and not self._ota_controller.active
        )

    def load_customer_ota_image(self, data: bytes, filename: str) -> bool:
        """Load an image already authenticated by the customer package verifier."""
        if not self.customer_ota_available() or not data or not filename:
            return False
        self._ota_controller.configure_file(bytes(data), Path(filename).name)
        set_translatable_text(
            "{file}  ({size} bytes)",
            self._ota_file_label,
            file=self._ota_controller.filename,
            size=self._ota_controller.file_size,
        )
        self._set_controls_enabled(True)
        return True

    def start_customer_ota(self) -> bool:
        if not self.customer_ota_available() or not self._ota_controller.has_file:
            return False
        self._ota_pause_debug_cb.setChecked(True)
        self._on_ota_start()
        return self._ota_controller.active

    def abort_customer_ota(self) -> None:
        self._on_ota_abort()

    def _set_para_row_status(
        self,
        name: str,
        row: int,
        source: str,
        **values: Any,
    ) -> None:
        self._para_status_by_name[name] = (source, dict(values))
        if 0 <= row < self._para_table.rowCount():
            item = self._para_table.item(row, 4)
            if item is not None:
                item.setText(trc("DeviceView", source, **values))

    def _render_para_row_statuses(self) -> None:
        for row, para in enumerate(self._params):
            status = self._para_status_by_name.get(para.name)
            item = self._para_table.item(row, 4)
            if item is None:
                continue
            if status is None:
                item.setText("")
            else:
                source, values = status
                item.setText(trc("DeviceView", source, **values))

    # ---- 连接共享 ----

    def set_profile_store(self, store: ProfileStore) -> None:
        """Validate that callers use the session's canonical ProfileStore."""
        if store is not self._profile_store:
            raise ValueError("DeviceView profile store belongs to its DeviceSessionCore")

    def _bind_controllers(self) -> None:
        self._session_core.connection_changed.connect(self._apply_connection_state)
        self._session_core.record_received.connect(self._apply_session_record)

        parameter = self._parameter_controller
        parameter.capability_changed.connect(self._on_parameter_capability_changed)
        parameter.table_received.connect(self._on_para_table_received)
        parameter.status_changed.connect(self._on_parameter_status_changed)
        parameter.parameter_status_changed.connect(self._on_parameter_row_status_changed)

        ota = self._ota_controller
        ota.capability_changed.connect(self._on_ota_capability_changed)
        ota.state_changed.connect(self._on_ota_state_changed)
        ota.status_changed.connect(self._on_ota_controller_status)
        ota.progress_changed.connect(self._ota_progress.setValue)
        ota.transaction_active_changed.connect(self._on_ota_transaction_changed)
        ota.debug_mode_requested.connect(self.debug_mode_requested)

    @Slot(object)
    def set_worker(self, worker):
        """Compatibility hook for isolated tests and legacy embedders."""
        if worker is None:
            self._session_core.end_connection()
            return
        self._session_core.attach_transport(worker, worker.send)

    @Slot(bool)
    def _apply_connection_state(self, connected: bool) -> None:
        self._hw_type = "—"
        self._fw_ver = "—"
        self._device_sn = "—"
        self._protocol_ver = 0
        self._update_info_labels()
        self._overlay.setVisible(not connected)
        if not connected:
            self._overlay.raise_()
            self._params = []
            self._para_table.setRowCount(0)
            self._clear_para_status()
        self._set_controls_enabled(connected)

    @Slot(bool)
    def set_debug_state(self, enabled: bool) -> None:
        self._ota_controller.set_debug_state(enabled)

    @Slot(bool, bool, str)
    def on_debug_request_finished(self, target: bool, ok: bool, detail: str) -> None:
        self._ota_controller.on_debug_request_finished(target, ok, detail)

    @Slot(object)
    def _on_frame_received(self, record):
        """Compatibility entry point for tests that inject decoded records."""
        self._apply_session_record(record)
        self._parameter_controller.feed_record(record)
        self._ota_controller.feed_record(record)

    @Slot(object)
    def _apply_session_record(self, record: object) -> None:
        if isinstance(record, MetaInfo):
            self._hw_type = record.hw_type
            self._fw_ver = record.fw_ver
            self._device_sn = record.device_sn
            self._protocol_ver = record.protocol_ver
            self._update_info_labels()

    @Slot(object)
    def _on_parameter_capability_changed(
        self,
        state: ParameterCapabilityState,
    ) -> None:
        self._para_capability_state = state
        if state is not ParameterCapabilityState.SUPPORTED:
            self._params = []
            self._para_table.setRowCount(0)
        self._set_controls_enabled(self._session_core.connected)

    @Slot(object)
    def _on_ota_capability_changed(self, state: OtaCapabilityState) -> None:
        self._ota_capability_state = CapabilityUiState(state.value)
        self._set_controls_enabled(self._session_core.connected)

    @Slot(object, object)
    def _on_parameter_status_changed(
        self,
        status: ParameterStatus,
        values: dict[str, Any],
    ) -> None:
        sources = {
            ParameterStatus.IDLE: "",
            ParameterStatus.WAITING_PROFILE: tr_source("Waiting for device Profile and capabilities..."),
            ParameterStatus.WAITING_CAPABILITY: tr_source("Waiting for device capability declaration..."),
            ParameterStatus.UNSUPPORTED: tr_source("Parameter management is unavailable in this firmware"),
            ParameterStatus.TRANSACTION_ACTIVE: tr_source("OTA is active; parameter operations are paused"),
            ParameterStatus.READING: tr_source("Reading parameter table..."),
            ParameterStatus.READ_SEND_FAILED: tr_source("Failed to send parameter-table request"),
            ParameterStatus.READ_TIMEOUT: tr_source("Parameter-table read timed out"),
            ParameterStatus.READ_DEFERRED: tr_source("Parameter write is awaiting confirmation; read is deferred"),
            ParameterStatus.RESET_SUCCESS: tr_source("Parameters restored to factory defaults; restart is recommended"),
            ParameterStatus.RESET_FAILED: tr_source("Factory reset failed: {detail}"),
            ParameterStatus.RESET_TIMEOUT: tr_source("Factory reset timed out; try again"),
        }
        source = sources.get(status)
        if source is None:
            return
        if source:
            self._set_para_status(source, **values)
        elif self._parameter_controller.operation is ParameterOperation.IDLE:
            self._clear_para_status()
        if status in {
            ParameterStatus.READ_TIMEOUT,
            ParameterStatus.READ_DEFERRED,
            ParameterStatus.RESET_SUCCESS,
            ParameterStatus.RESET_FAILED,
            ParameterStatus.RESET_TIMEOUT,
        }:
            self.status_message.emit(tr(source, **values), 5000)

    @Slot(str, object, object)
    def _on_parameter_row_status_changed(
        self,
        name: str,
        status: ParameterStatus,
        values: dict[str, Any],
    ) -> None:
        sources = {
            ParameterStatus.WRITE_AWAITING: tr_source("Awaiting write confirmation..."),
            ParameterStatus.WRITE_SEND_FAILED: tr_source("Send failed"),
            ParameterStatus.WRITE_WAITING_READBACK: tr_source("Waiting for device readback..."),
            ParameterStatus.WRITE_SUCCESS: tr_source("✓ Success"),
            ParameterStatus.WRITE_NOT_READ_BACK: tr_source("Target value was not read back"),
            ParameterStatus.WRITE_ERROR: tr_source("✗ {detail}"),
        }
        source = sources.get(status)
        if source is None:
            return
        row = next((index for index, para in enumerate(self._params) if para.name == name), -1)
        self._set_para_row_status(name, row, source, **values)

    @Slot(object)
    def _on_ota_state_changed(self, _state: OtaState) -> None:
        self._set_controls_enabled(self._session_core.connected)

    @Slot(bool)
    def _on_ota_transaction_changed(self, active: bool) -> None:
        self._parameter_controller.set_transaction_active(active)
        self.device_transaction_active_changed.emit(active)
        self._set_controls_enabled(self._session_core.connected)

    @Slot(object, object)
    def _on_ota_controller_status(
        self,
        status: OtaStatus,
        values: dict[str, Any],
    ) -> None:
        if status is OtaStatus.TRANSFERRING:
            source = (
                tr_source(
                    "Transferring... {sequence}/{total} ({percent}%)  "
                    "{speed:.1f} KB/s  {remaining}s remaining"
                )
                if "speed" in values
                else tr_source("Transferring... {sequence}/{total} ({percent}%)")
            )
        else:
            source = {
                OtaStatus.IDLE: tr_source("Idle"),
                OtaStatus.WAITING_PROFILE: tr_source("Waiting for device Profile and capabilities..."),
                OtaStatus.WAITING_CAPABILITY: tr_source("Waiting for device capability declaration..."),
                OtaStatus.UNSUPPORTED: tr_source("OTA is unavailable in this firmware"),
                OtaStatus.NO_FILE: tr_source("No file selected"),
                OtaStatus.STOPPING_LIVE_DATA: tr_source("Stopping live data..."),
                OtaStatus.STOP_LIVE_DATA_FAILED: tr_source("Failed to stop live data: {detail}"),
                OtaStatus.SENDING_BEGIN: tr_source("Sending OTA_BEGIN..."),
                OtaStatus.BEGIN_SEND_FAILED: tr_source("Failed to send OTA_BEGIN"),
                OtaStatus.BEGIN_REJECTED: tr_source("OTA_BEGIN rejected: {detail}"),
                OtaStatus.BEGIN_TIMEOUT: tr_source("ota_begin timed out"),
                OtaStatus.CHUNK_SEND_FAILED: tr_source("Failed to send chunk {sequence}"),
                OtaStatus.CHUNK_RETRY: tr_source("Chunk {sequence} was not confirmed; retry {retry}/{maximum}"),
                OtaStatus.CHUNK_REJECTED: tr_source("Chunk {sequence} rejected: {detail}"),
                OtaStatus.CHUNK_TIMEOUT: tr_source("Chunk {sequence} timed out repeatedly"),
                OtaStatus.VERIFYING: tr_source("Verifying..."),
                OtaStatus.END_SEND_FAILED: tr_source("Failed to send OTA_END"),
                OtaStatus.END_REJECTED: tr_source("OTA_END verification failed: {detail}"),
                OtaStatus.END_TIMEOUT: tr_source("ota_end timed out"),
                OtaStatus.REBOOTING: tr_source("Device is rebooting ({elapsed}s elapsed; bootloader flashing usually takes about 45s)..."),
                OtaStatus.REBOOT_TIMEOUT: tr_source("Device did not return within 120 s; check the connection"),
                OtaStatus.DEVICE_RETURNED_CHANGED: tr_source("✓ Update complete; device is online ({before} → {after})"),
                OtaStatus.DEVICE_RETURNED_UNCHANGED: tr_source("✓ Device is back online (version {version}, unchanged)"),
                OtaStatus.CONNECTION_LOST: tr_source("Connection lost; OTA aborted"),
                OtaStatus.ABORTED: tr_source("Aborted by user"),
            }[status]
        self._set_ota_status(source, **values)
        final_statuses = {
            OtaStatus.STOP_LIVE_DATA_FAILED,
            OtaStatus.BEGIN_SEND_FAILED,
            OtaStatus.BEGIN_REJECTED,
            OtaStatus.BEGIN_TIMEOUT,
            OtaStatus.CHUNK_SEND_FAILED,
            OtaStatus.CHUNK_REJECTED,
            OtaStatus.CHUNK_TIMEOUT,
            OtaStatus.END_SEND_FAILED,
            OtaStatus.END_REJECTED,
            OtaStatus.END_TIMEOUT,
            OtaStatus.REBOOT_TIMEOUT,
            OtaStatus.DEVICE_RETURNED_CHANGED,
            OtaStatus.DEVICE_RETURNED_UNCHANGED,
            OtaStatus.CONNECTION_LOST,
            OtaStatus.ABORTED,
        }
        if status in final_statuses:
            self.status_message.emit(
                tr("OTA: {message}", message=tr(source, **values)),
                5000,
            )

    def _set_controls_enabled(self, connected: bool):
        available = connected and not self._ota_controller.active
        self._refresh_info_btn.setEnabled(available)
        self._read_all_btn.setEnabled(available and self._parameter_controller.supported)
        self._factory_reset_btn.setEnabled(
            available and self._parameter_controller.supported
        )
        self._ota_select_btn.setEnabled(available and self._ota_controller.supported)
        self._ota_upload_btn.setEnabled(
            available and self._ota_controller.supported and self._ota_controller.has_file
        )
        self._ota_abort_btn.setEnabled(
            connected
            and self._ota_controller.active
            and self._ota_controller.state is not OtaState.WAIT_REBOOT
        )
        for row, para in enumerate(self._params):
            edit = self._para_table.cellWidget(row, 2)
            apply_btn = self._para_table.cellWidget(row, 3)
            writable = not bool(para.flags & PARA_FLAG_READ_ONLY)
            if edit is not None:
                edit.setEnabled(available)
            if apply_btn is not None:
                apply_btn.setEnabled(available and writable)

    def _send(self, frame: bytes) -> bool:
        if not self._session_core.connected:
            self.status_message.emit(tr("Not connected; command was not sent"), 3000)
            return False
        return self._session_core.send(frame)

    # ---- 设备信息 ----

    def _update_info_labels(self):
        self._info_labels["hw_type"].setText(self._hw_type)
        self._info_labels["fw_ver"].setText(self._fw_ver)
        self._info_labels["device_sn"].setText(self._device_sn)
        self._info_labels["protocol_ver"].setText(f"v{self._protocol_ver}")

    def _on_refresh_info(self):
        if not self._session_core.request_meta_info():
            self.status_message.emit(tr("Not connected; command was not sent"), 3000)

    # ---- 参数管理 ----

    def _on_read_params(self):
        self._parameter_controller.request_table()

    def _parameter_availability_message(self) -> str:
        if self._para_capability_state is CapabilityUiState.WAITING_PROFILE:
            return "Waiting for device capability declaration..."
        return "Parameter management is unavailable in this firmware"

    def _ota_availability_message(self) -> str:
        if self._ota_capability_state is CapabilityUiState.WAITING_PROFILE:
            return "Waiting for device capability declaration..."
        return "OTA is unavailable in this firmware"

    def _request_para_table(self, *, allow_during_set: bool = False):
        return self._parameter_controller.request_table(
            allow_during_write=allow_during_set
        )

    def _on_auto_read_params(self, hardware: str) -> None:
        self._parameter_controller._run_auto_read(hardware)

    def _on_para_table_received(self, report: ParaTableReport):
        self._clear_para_status()
        self._params = report.params
        self._para_table.setRowCount(len(report.params))
        for row, p in enumerate(report.params):
            # 名称
            name_item = QTableWidgetItem(p.name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            if p.flags & PARA_FLAG_REQUIRES_REBOOT:
                name_item.setToolTip(tr("Restart the device for this change to take effect"))
                name_item.setText(f"⚠ {p.name}")
            self._para_table.setItem(row, 0, name_item)

            # 类型
            type_item = QTableWidgetItem(_PARA_TYPE_NAMES.get(p.para_type, str(p.para_type)))
            type_item.setFlags(type_item.flags() & ~Qt.ItemIsEditable)
            self._para_table.setItem(row, 1, type_item)

            # 当前值（可编辑的 QLineEdit）
            edit = QLineEdit(p.value)
            if p.flags & PARA_FLAG_READ_ONLY:
                edit.setReadOnly(True)
            self._para_table.setCellWidget(row, 2, edit)

            # 操作按钮
            apply_btn = QPushButton(tr("Apply"))
            if p.flags & PARA_FLAG_READ_ONLY:
                apply_btn.setEnabled(False)
            apply_btn.clicked.connect(lambda _checked=False, r=row: self._on_para_apply(r))
            self._para_table.setCellWidget(row, 3, apply_btn)

            # 状态
            status = self._para_status_by_name.get(p.name)
            status_item = QTableWidgetItem(
                trc("DeviceView", status[0], **status[1])
                if status is not None
                else ""
            )
            status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
            self._para_table.setItem(row, 4, status_item)

        self.status_message.emit(
            tr("Read {count} parameter(s)", count=len(report.params)),
            2000,
        )
        self._set_controls_enabled(self._session_core.connected)

    def _on_para_apply(self, row: int):
        if not self._parameter_controller.supported:
            self.status_message.emit(tr(self._parameter_availability_message()), 3000)
            return
        if row >= len(self._params):
            return
        p = self._params[row]
        edit = self._para_table.cellWidget(row, 2)
        if edit is None:
            return
        new_val = edit.text().strip()
        if new_val == p.value:
            self._set_para_row_status(p.name, row, "Unchanged")
            return
        self._parameter_controller.write(p.name, p.para_type, new_val)

    def _run_para_set_verify_read(self) -> None:
        self._parameter_controller._request_verify_table()

    def _on_factory_reset(self):
        if not self._parameter_controller.supported:
            self.status_message.emit(tr(self._parameter_availability_message()), 3000)
            return
        ret = QMessageBox.warning(
            self,
            tr("Factory reset"),
            tr(
                "Restore all parameters to factory defaults?\n"
                "This cannot be undone, and some parameters require a restart."
            ),
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if ret != QMessageBox.Yes:
            return
        self._parameter_controller.reset_parameters()

    # ---- OTA ----

    def _on_select_firmware(self):
        if not self._ota_controller.supported:
            self.status_message.emit(tr(self._ota_availability_message()), 3000)
            return
        last_dir = ""
        if self._settings is not None:
            last_dir = self._settings.get("paths.firmware_dir", "") or ""
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Select firmware file"),
            last_dir,
            tr("Firmware (*.bin);;All files (*)"),
        )
        if not path:
            return
        p = Path(path)
        data = p.read_bytes()
        self._ota_controller.configure_file(data, p.name)
        set_translatable_text(
            "{file}  ({size} bytes)",
            self._ota_file_label,
            file=p.name,
            size=len(data),
        )
        self._set_controls_enabled(self._session_core.connected)

    def _on_ota_start(self):
        if not self._ota_controller.has_file or not self._ota_controller.supported:
            if not self._ota_controller.supported:
                self.status_message.emit(
                    tr(self._ota_availability_message()),
                    3000,
                )
            return
        self._ota_controller.start(
            pause_debug=self._ota_pause_debug_cb.isChecked()
        )

    def _on_ota_abort(self):
        self._ota_controller.abort()

    def _on_command_response(self, response) -> None:
        """Compatibility entry point for decoded-response unit tests."""
        self._parameter_controller.feed_record(response)
        self._ota_controller.feed_record(response)

    def _on_response_timeout(self) -> None:
        """Compatibility hook; active controller timers own production timeouts."""
        if self._ota_controller.active:
            self._ota_controller._on_response_timeout()
        else:
            self._parameter_controller._on_response_timeout()

    # ---- 主题 ----

    def retranslate_ui(self) -> None:
        self._render_para_row_statuses()
        for row, para in enumerate(self._params):
            name_item = self._para_table.item(row, 0)
            if name_item is not None and para.flags & PARA_FLAG_REQUIRES_REBOOT:
                name_item.setToolTip(
                    tr("Restart the device for this change to take effect")
                )
        if not self._ota_controller.has_file:
            set_translatable_text("No file selected", self._ota_file_label)
        else:
            set_translatable_text(
                "{file}  ({size} bytes)",
                self._ota_file_label,
                file=self._ota_controller.filename,
                size=self._ota_controller.file_size,
            )
        self._set_ota_status(self._ota_status_source, **self._ota_status_values)

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = S._normalize_theme(theme)
        self._scale = scale
        p = S.palette(self._theme)
        px = S.font_px(12, scale)
        self.setStyleSheet(
            f"QGroupBox {{ color: {p['text']}; border: 1px solid {p['border']}; "
            f"border-radius: 4px; margin-top: 8px; padding-top: 12px; font-size: {px}px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 10px; "
            f"padding: 0 4px; color: {p['text_muted']}; }}"
            f"QLabel {{ color: {p['text']}; font-size: {px}px; }}"
            f"QLineEdit {{ background-color: {p['input_bg']}; color: {p['text']}; "
            f"border: 1px solid {p['input_border']}; border-radius: 2px; "
            f"padding: 2px 4px; font-size: {px}px; }}"
            f"QPushButton {{ background-color: {p['input_bg']}; color: {p['text']}; "
            f"border: 1px solid {p['input_border']}; border-radius: 2px; "
            f"padding: 4px 10px; font-size: {px}px; }}"
            f"QPushButton:hover {{ background-color: {p['card_alt']}; }}"
            f"QPushButton:disabled {{ color: {p['text_faint']}; }}"
            f"QTableWidget {{ background-color: {p['card']}; color: {p['text']}; "
            f"border: 1px solid {p['border']}; font-size: {px}px; "
            f"gridline-color: {p['border']}; }}"
            f"QHeaderView::section {{ background-color: {p['panel']}; color: {p['text_muted']}; "
            f"border: 1px solid {p['border']}; padding: 4px; font-size: {px}px; }}"
            f"QProgressBar {{ border: 1px solid {p['border']}; border-radius: 2px; "
            f"background-color: {p['input_bg']}; text-align: center; color: {p['text']}; "
            f"font-size: {px}px; }}"
            f"QProgressBar::chunk {{ background-color: {p['primary']}; }}"
            f"QCheckBox {{ color: {p['text']}; font-size: {px}px; }}"
        )
        self._overlay.setStyleSheet(
            f"background-color: rgba(30,30,30,200); color: {p['text_faint']}; font-size: 16px;"
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._overlay.setGeometry(self.rect())
