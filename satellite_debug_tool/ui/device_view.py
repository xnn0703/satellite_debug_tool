"""DeviceView — 设备 Tab：设备信息 + 参数管理 + OTA 固件升级。

M9 新增。通过 debug 协议远程读写设备参数、上传固件。
共享 Live Tab 的连接（worker），不新建连接。
"""

from __future__ import annotations

import time
import zlib
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QCoreApplication, Qt, QTimer, Signal, Slot
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
    CommandResponse,
    MetaInfo,
    ParaEntry,
    ParaTableReport,
    RespCode,
    build_ota_abort,
    build_ota_begin,
    build_ota_data,
    build_ota_end,
    build_para_reset,
    build_para_set,
    build_request_para_table,
    PARA_FLAG_READ_ONLY,
    PARA_FLAG_REQUIRES_REBOOT,
    ParaType,
)
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.i18n import (
    mark_raw_text,
    register_translatable,
    set_translatable_text,
    tr,
    trc,
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

# OTA 分块大小（设备 heap 有限，RX buffer 1200B，512B chunk 的帧约 530B 可靠容纳）
OTA_CHUNK_SIZE = 512
# OTA 每块超时 (ms) 和最大重试次数
OTA_CHUNK_TIMEOUT_MS = 2000
OTA_CHUNK_MAX_RETRY = 3
OTA_BEGIN_TIMEOUT_MS = 15000
OTA_END_TIMEOUT_MS = 10000
PARA_AUTO_FALLBACK_MS = 1500
# 参数写入可能触发 FDB/flash 擦写，3s 容易误报超时。
PARA_SET_TIMEOUT_MS = 10000
PARA_RESET_TIMEOUT_MS = 15000
PARA_READ_TIMEOUT_MS = 10000


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


class CapabilityUiState(Enum):
    DISCONNECTED = "disconnected"
    WAITING_PROFILE = "waiting_profile"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class DeviceView(QWidget):
    """设备 Tab：设备信息 + 参数表 + OTA。"""

    status_message = Signal(str, int)
    debug_mode_requested = Signal(bool)
    device_transaction_active_changed = Signal(bool)
    handshake_retry_pause_changed = Signal(bool)

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        settings=None,
        profile_store: Optional[ProfileStore] = None,
    ):
        super().__init__(parent)
        self._worker = None
        self._profile_store: Optional[ProfileStore] = None
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
        self._supports_parameters = False
        self._supports_ota = False
        self._last_auto_read_hw: Optional[str] = None
        self._params_loaded_hw: Optional[str] = None
        self._para_read_pending = False
        self._para_verify_retry_scheduled = False
        self._pending_para_name: Optional[str] = None
        self._pending_para_value: Optional[str] = None
        self._pending_para_type: Optional[int] = None
        self._para_status_by_name: dict[str, tuple[str, dict[str, Any]]] = {}
        self._para_capability_state = CapabilityUiState.DISCONNECTED
        self._ota_capability_state = CapabilityUiState.DISCONNECTED

        # OTA 状态机
        self._ota_active = False
        self._ota_state = "IDLE"
        self._ota_file: Optional[bytes] = None
        self._ota_filename = ""
        self._ota_seq = 0
        self._ota_total_chunks = 0
        self._ota_crc32 = 0
        self._ota_retry = 0
        self._ota_paused_debug = False
        self._ota_restore_debug = False
        self._known_debug_enabled = False
        self._ota_start_time = 0.0
        self._ota_status_source = "Idle"
        self._ota_status_values: dict[str, Any] = {}

        # OTA 升级后等待设备重启 + 新版本上线。设备 bootloader 流程
        # （Store Firmware → Load Firmware → jump → app 启动 → 发 META）
        # 整体约 45-60s，所以超时 120s、探测间隔 3s 取宽裕。
        self._ota_post_reboot_fw_before: str = ""   # 升级前的 fw_ver 快照
        self._ota_post_reboot_deadline: float = 0.0  # 超时绝对时间
        self._ota_post_reboot_started_at: float = 0.0  # 开始等待时刻（用于 UI 显示已等待秒数）
        self._ota_reboot_meta_not_before: float = 0.0  # 隔离 END ACK 前后的旧 META
        self._ota_post_reboot_timer = QTimer(self)
        self._ota_post_reboot_timer.setInterval(3000)
        self._ota_post_reboot_timer.timeout.connect(self._on_ota_post_reboot_tick)

        # 待应答请求（用于超时匹配 COMMAND_RESPONSE）
        self._pending_request: Optional[str] = None  # "para_set" / "para_reset" / "ota_begin" / "ota_data" / "ota_end"
        self._response_timer = QTimer(self)
        self._response_timer.setSingleShot(True)
        self._response_timer.timeout.connect(self._on_response_timeout)
        self._para_read_timer = QTimer(self)
        self._para_read_timer.setSingleShot(True)
        self._para_read_timer.timeout.connect(self._on_para_read_timeout)

        self._setup_ui()
        if profile_store is not None:
            self.set_profile_store(profile_store)
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
            context="DeviceView",
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
            context="DeviceView",
            **values,
        )

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
        """注入 LiveView 的 ProfileStore，用于按设备 capability 启用功能。"""
        if self._profile_store is store:
            return
        if self._profile_store is not None:
            try:
                self._profile_store.profile_changed.disconnect(self._on_profile_changed)
            except (TypeError, RuntimeError):
                pass
        self._profile_store = store
        self._profile_store.profile_changed.connect(self._on_profile_changed)
        self._refresh_capabilities()

    @Slot(str)
    def _on_profile_changed(self, _hw_type: str) -> None:
        self._refresh_capabilities()

    @Slot(object)
    def set_worker(self, worker):
        """由 MainWindow 桥接 LiveView.connected_worker_changed 调用。"""
        self._worker = worker
        connected = worker is not None
        if connected:
            self._hw_type = "—"
            self._fw_ver = "—"
            self._device_sn = "—"
            self._protocol_ver = 0
            self._update_info_labels()
            self._overlay.hide()
            self._para_capability_state = CapabilityUiState.WAITING_PROFILE
            self._ota_capability_state = CapabilityUiState.WAITING_PROFILE
            self._refresh_capabilities()
        else:
            self._hw_type = "—"
            self._fw_ver = "—"
            self._device_sn = "—"
            self._protocol_ver = 0
            self._update_info_labels()
            self._overlay.show()
            self._overlay.raise_()
            self._last_auto_read_hw = None
            self._params_loaded_hw = None
            self._para_read_pending = False
            self._para_verify_retry_scheduled = False
            self._para_read_timer.stop()
            self._supports_parameters = False
            self._supports_ota = False
            self._para_capability_state = CapabilityUiState.DISCONNECTED
            self._ota_capability_state = CapabilityUiState.DISCONNECTED
            self._clear_para_status()
            if not self._ota_active:
                self._set_ota_status("Idle")
            self._set_controls_enabled(False)
            if self._ota_active:
                self._ota_finish("Connection lost; OTA aborted")
            # 连接断开时停掉 OTA 后等待的探测，避免对断连 worker 发包
            if self._ota_post_reboot_timer.isActive():
                self._ota_post_reboot_timer.stop()

    @Slot(bool)
    def set_debug_state(self, enabled: bool) -> None:
        self._known_debug_enabled = bool(enabled)

    @Slot(bool, bool, str)
    def on_debug_request_finished(self, target: bool, ok: bool, detail: str) -> None:
        if not self._ota_active or self._ota_state != "QUIESCE" or target:
            return
        if not ok:
            self._ota_finish("Failed to stop live data: {detail}", detail=detail)
            return
        self._ota_send_begin()

    @Slot(object)
    def _on_frame_received(self, record):
        """由 MainWindow 桥接 LiveView.frame_received 调用。"""
        if isinstance(record, MetaInfo):
            self._hw_type = record.hw_type
            self._fw_ver = record.fw_ver
            self._device_sn = record.device_sn
            self._protocol_ver = record.protocol_ver
            self._update_info_labels()
            self._refresh_capabilities()
            # 同版本/降级均允许，因此 WAIT_REBOOT 收到任意有效 META 即视为重新上线。
            if (
                self._ota_active
                and self._ota_state == "WAIT_REBOOT"
                and record.fw_ver
                and time.monotonic() >= self._ota_reboot_meta_not_before
            ):
                before = self._ota_post_reboot_fw_before or "?"
                if record.fw_ver != before:
                    source = "✓ Update complete; device is online ({before} → {after})"
                    values = {"before": before, "after": record.fw_ver}
                else:
                    source = "✓ Device is back online (version {version}, unchanged)"
                    values = {"version": record.fw_ver}
                self._known_debug_enabled = False
                self._params_loaded_hw = None
                self._last_auto_read_hw = None
                self._ota_finish(source, restore_debug=False, **values)
                if self._supports_parameters:
                    self._maybe_auto_read_params()
        elif isinstance(record, ParaTableReport):
            self._on_para_table_received(record)
        elif isinstance(record, CommandResponse):
            self._on_command_response(record)

    def _on_ota_post_reboot_tick(self):
        """OTA 重启等待：周期请求 META，等设备回新版本号。

        设备 bootloader 阶段（Store/Load Firmware）共约 45s，期间发包没用，
        新 app 启动后会响应 META。这里持续探测直到收到新 fw_ver 或超时。
        """
        from satellite_debug_tool.core.protocol import build_request_meta_info
        now = time.monotonic()
        elapsed = int(now - self._ota_post_reboot_started_at)
        # 超时判断
        if now > self._ota_post_reboot_deadline:
            self._ota_finish(
                "Device did not return within 120 s; check the connection",
                restore_debug=False,
            )
            return
        # UI 显示已等待时长
        self._set_ota_status(
            "Device is rebooting ({elapsed}s elapsed; bootloader flashing usually takes about 45s)...",
            elapsed=elapsed,
        )
        # 主动请求 META，触发设备重新发送版本信息（设备未启动期间会丢弃，无副作用）
        if self._worker is not None:
            self._send(build_request_meta_info())

    def _device_hw_type(self) -> Optional[str]:
        if self._hw_type and self._hw_type != "—":
            return self._hw_type
        return None

    def _capability_supported(self, name: str) -> bool:
        hw = self._device_hw_type()
        if hw is None:
            return False
        default = hw == "afd01" and name in {"parameters", "ota"}
        if self._profile_store is None:
            return default
        return self._profile_store.has_capability(hw, name, default=default)

    def _refresh_capabilities(self) -> None:
        connected = self._worker is not None
        hw = self._device_hw_type()
        previous_para_state = self._para_capability_state
        previous_ota_state = self._ota_capability_state
        self._supports_parameters = connected and self._capability_supported("parameters")
        self._supports_ota = connected and self._capability_supported("ota")
        self._set_controls_enabled(connected)

        if not connected:
            self._para_capability_state = CapabilityUiState.DISCONNECTED
            self._ota_capability_state = CapabilityUiState.DISCONNECTED
            return

        if hw is None:
            self._para_capability_state = CapabilityUiState.WAITING_PROFILE
            self._ota_capability_state = CapabilityUiState.WAITING_PROFILE
            self._set_para_status("Waiting for device Profile and capabilities...")
            if not self._ota_active:
                self._set_ota_status("Waiting for device Profile and capabilities...")
            return

        if self._supports_parameters:
            self._para_capability_state = CapabilityUiState.SUPPORTED
            if previous_para_state in {
                CapabilityUiState.WAITING_PROFILE,
                CapabilityUiState.UNSUPPORTED,
            }:
                self._clear_para_status()
            self._maybe_auto_read_params()
        else:
            self._para_capability_state = CapabilityUiState.UNSUPPORTED
            self._params = []
            self._para_table.setRowCount(0)
            self._set_para_status("This firmware does not declare parameter-management support")

        if self._supports_ota:
            self._ota_capability_state = CapabilityUiState.SUPPORTED
            if (
                not self._ota_active
                and previous_ota_state in {
                    CapabilityUiState.WAITING_PROFILE,
                    CapabilityUiState.UNSUPPORTED,
                }
            ):
                self._set_ota_status("Idle")
        else:
            self._ota_capability_state = CapabilityUiState.UNSUPPORTED
            if not self._ota_active:
                self._set_ota_status("This firmware does not declare OTA support")

    def _maybe_auto_read_params(self) -> None:
        hw = self._device_hw_type()
        if hw is None or not self._supports_parameters:
            return
        if self._last_auto_read_hw == hw:
            return
        self._last_auto_read_hw = hw
        QTimer.singleShot(PARA_AUTO_FALLBACK_MS, lambda hw=hw: self._on_auto_read_params(hw))

    def _on_auto_read_params(self, hw: str) -> None:
        if self._device_hw_type() != hw or not self._supports_parameters:
            return
        if self._params_loaded_hw == hw:
            return
        if self._para_read_pending:
            return
        self._request_para_table()

    def _set_controls_enabled(self, connected: bool):
        available = connected and not self._ota_active
        self._refresh_info_btn.setEnabled(available)
        self._read_all_btn.setEnabled(available and self._supports_parameters)
        self._factory_reset_btn.setEnabled(available and self._supports_parameters)
        self._ota_select_btn.setEnabled(available and self._supports_ota)
        self._ota_upload_btn.setEnabled(
            available and self._supports_ota and self._ota_file is not None
        )
        self._ota_abort_btn.setEnabled(
            connected and self._ota_active and self._ota_state != "WAIT_REBOOT"
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
        if self._worker is None:
            self.status_message.emit(tr("Not connected; command was not sent"), 3000)
            return False
        return bool(self._worker.send(frame))

    # ---- 设备信息 ----

    def _update_info_labels(self):
        self._info_labels["hw_type"].setText(self._hw_type)
        self._info_labels["fw_ver"].setText(self._fw_ver)
        self._info_labels["device_sn"].setText(self._device_sn)
        self._info_labels["protocol_ver"].setText(f"v{self._protocol_ver}")

    def _on_refresh_info(self):
        from satellite_debug_tool.core.protocol import build_request_meta_info
        self._send(build_request_meta_info())

    # ---- 参数管理 ----

    def _on_read_params(self):
        self._request_para_table()

    def _request_para_table(self, *, allow_during_set: bool = False):
        if not self._supports_parameters:
            message = tr("This firmware does not declare parameter-management support")
            self._set_para_status(
                "This firmware does not declare parameter-management support"
            )
            self.status_message.emit(message, 3000)
            return
        if self._ota_active:
            self._set_para_status("OTA is active; parameter operations are paused")
            return
        if self._pending_request == "para_set" and not allow_during_set:
            message = tr("Parameter write is awaiting confirmation; read is deferred")
            self._set_para_status(
                "Parameter write is awaiting confirmation; read is deferred"
            )
            self.status_message.emit(message, 2000)
            return
        if self._para_read_pending:
            self._set_para_status("Reading parameter table...")
            return
        if self._send(build_request_para_table()):
            self._para_read_pending = True
            self._set_para_status("Reading parameter table...")
            self._para_read_timer.start(PARA_READ_TIMEOUT_MS)
        else:
            self._set_para_status("Failed to send parameter-table request")

    def _on_para_table_received(self, report: ParaTableReport):
        self._para_read_pending = False
        self._para_read_timer.stop()
        hw = self._device_hw_type()
        if hw is not None:
            self._params_loaded_hw = hw
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
        self._verify_pending_para_set(report)
        self._set_controls_enabled(self._worker is not None)

    def _on_para_apply(self, row: int):
        if not self._supports_parameters:
            self.status_message.emit(
                tr("This firmware does not declare parameter-management support"),
                3000,
            )
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
        self._pending_request = "para_set"
        self._pending_para_row = row
        self._pending_para_name = p.name
        self._pending_para_value = new_val
        self._pending_para_type = p.para_type
        self._set_para_row_status(p.name, row, "Awaiting write confirmation...")
        if self._send(build_para_set(p.name, new_val)):
            self._response_timer.start(PARA_SET_TIMEOUT_MS)
        else:
            self._set_para_row_status(p.name, row, "Send failed")
            self._clear_pending_para_set()

    def _clear_pending_para_set(self) -> None:
        self._pending_request = None
        self._pending_para_name = None
        self._pending_para_value = None
        self._pending_para_type = None
        self._para_verify_retry_scheduled = False
        self._para_read_pending = False
        self._para_read_timer.stop()

    def _values_match(self, para_type: int, actual: str, expected: str) -> bool:
        if para_type == int(ParaType.FLOAT):
            try:
                return abs(float(actual) - float(expected)) < 1e-4
            except ValueError:
                return actual.strip() == expected.strip()
        if para_type in {
            int(ParaType.INT),
            int(ParaType.UINT8),
            int(ParaType.INT8),
            int(ParaType.UINT16),
            int(ParaType.INT16),
        }:
            try:
                return int(actual, 0) == int(expected, 0)
            except ValueError:
                return actual.strip() == expected.strip()
        return actual.strip() == expected.strip()

    def _verify_pending_para_set(self, report: ParaTableReport) -> None:
        if self._pending_request != "para_set" or not self._pending_para_name:
            return

        target = next((p for p in report.params if p.name == self._pending_para_name), None)
        row = next((idx for idx, p in enumerate(report.params) if p.name == self._pending_para_name), -1)
        status_item = self._para_table.item(row, 4) if 0 <= row < self._para_table.rowCount() else None
        if target is not None and self._pending_para_value is not None:
            para_type = self._pending_para_type if self._pending_para_type is not None else target.para_type
            if self._values_match(para_type, target.value, self._pending_para_value):
                self._response_timer.stop()
                self._set_para_row_status(
                    self._pending_para_name,
                    row,
                    "✓ Success",
                )
                self._clear_pending_para_set()
                return

        if self._pending_para_name:
            self._set_para_row_status(
                self._pending_para_name,
                row,
                "Waiting for device readback...",
            )

    def _schedule_para_set_verify_read(self, delay_ms: int) -> None:
        if self._pending_request != "para_set" or self._para_verify_retry_scheduled:
            return
        self._para_verify_retry_scheduled = True
        QTimer.singleShot(delay_ms, self._run_para_set_verify_read)

    def _run_para_set_verify_read(self) -> None:
        self._para_verify_retry_scheduled = False
        if self._pending_request == "para_set":
            self._request_para_table(allow_during_set=True)

    def _on_factory_reset(self):
        if not self._supports_parameters:
            self.status_message.emit(
                tr("This firmware does not declare parameter-management support"),
                3000,
            )
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
        self._pending_request = "para_reset"
        if self._send(build_para_reset()):
            self._response_timer.start(PARA_RESET_TIMEOUT_MS)

    # ---- OTA ----

    def _on_select_firmware(self):
        if not self._supports_ota:
            self.status_message.emit(
                tr("This firmware does not declare OTA support"),
                3000,
            )
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
        self._ota_file = data
        self._ota_filename = p.name
        self._ota_crc32 = zlib.crc32(data) & 0xFFFFFFFF
        set_translatable_text(
            "{file}  ({size} bytes)",
            self._ota_file_label,
            file=p.name,
            size=len(data),
        )
        self._set_controls_enabled(self._worker is not None)

    def _on_ota_start(self):
        if self._ota_file is None or self._worker is None or not self._supports_ota:
            if not self._supports_ota:
                self.status_message.emit(
                    tr("This firmware does not declare OTA support"),
                    3000,
                )
            return
        if self._ota_active:
            return
        self._ota_active = True
        self._ota_state = "QUIESCE"
        self._ota_seq = 0
        self._ota_retry = 0
        self._ota_total_chunks = (len(self._ota_file) + OTA_CHUNK_SIZE - 1) // OTA_CHUNK_SIZE
        self._ota_start_time = time.monotonic()
        self._ota_progress.setValue(0)
        self._ota_paused_debug = self._ota_pause_debug_cb.isChecked()
        self._ota_restore_debug = self._ota_paused_debug and self._known_debug_enabled
        self.device_transaction_active_changed.emit(True)
        self.handshake_retry_pause_changed.emit(True)
        self._set_controls_enabled(True)

        if self._ota_paused_debug:
            self._set_ota_status("Stopping live data...")
            self.debug_mode_requested.emit(False)
        else:
            self._ota_send_begin()

    def _ota_send_begin(self):
        if not self._ota_active or self._ota_file is None:
            return
        self._ota_state = "BEGIN"
        self._set_ota_status("Sending OTA_BEGIN...")
        self._pending_request = "ota_begin"
        if self._send(build_ota_begin(len(self._ota_file), self._ota_filename)):
            self._response_timer.start(OTA_BEGIN_TIMEOUT_MS)
        else:
            self._ota_finish("Failed to send OTA_BEGIN")

    def _ota_send_current_chunk(self) -> None:
        if not self._ota_active or self._ota_file is None:
            return
        if self._ota_seq >= self._ota_total_chunks:
            self._ota_send_end()
            return
        offset = self._ota_seq * OTA_CHUNK_SIZE
        chunk = self._ota_file[offset:offset + OTA_CHUNK_SIZE]
        self._ota_state = "DATA"
        self._pending_request = "ota_data"
        if self._send(build_ota_data(self._ota_seq, chunk)):
            self._response_timer.start(OTA_CHUNK_TIMEOUT_MS)
        else:
            self._ota_finish("Failed to send chunk {sequence}", sequence=self._ota_seq)

    def _update_ota_progress(self) -> None:
        if self._ota_total_chunks <= 0 or self._ota_file is None:
            return
        pct = int(self._ota_seq * 100 / self._ota_total_chunks)
        self._ota_progress.setValue(pct)
        elapsed = time.monotonic() - self._ota_start_time
        transferred = min(self._ota_seq * OTA_CHUNK_SIZE, len(self._ota_file))
        if elapsed <= 0.1 or transferred <= 0:
            self._set_ota_status(
                "Transferring... {sequence}/{total} ({percent}%)",
                sequence=self._ota_seq,
                total=self._ota_total_chunks,
                percent=pct,
            )
            return
        speed_kbs = transferred / elapsed / 1024
        remaining = max(0, len(self._ota_file) - transferred)
        eta = remaining / (transferred / elapsed)
        self._set_ota_status(
            "Transferring... {sequence}/{total} ({percent}%)  "
            "{speed:.1f} KB/s  {remaining}s remaining",
            sequence=self._ota_seq,
            total=self._ota_total_chunks,
            percent=pct,
            speed=speed_kbs,
            remaining=int(eta),
        )

    def _ota_send_end(self):
        if not self._ota_active:
            return
        self._ota_state = "END"
        self._pending_request = "ota_end"
        self._set_ota_status("Verifying...")
        if self._send(build_ota_end(self._ota_crc32)):
            self._response_timer.start(OTA_END_TIMEOUT_MS)
        else:
            self._ota_finish("Failed to send OTA_END")

    def _ota_enter_wait_reboot(self) -> None:
        self._ota_state = "WAIT_REBOOT"
        self._pending_request = None
        self._response_timer.stop()
        self._ota_progress.setValue(100)
        self._ota_abort_btn.setEnabled(False)
        self.handshake_retry_pause_changed.emit(False)
        self._ota_post_reboot_fw_before = self._fw_ver
        self._fw_ver = ""
        self._device_sn = ""
        self._update_info_labels()
        self._ota_post_reboot_started_at = time.monotonic()
        self._ota_post_reboot_deadline = self._ota_post_reboot_started_at + 120.0
        self._ota_reboot_meta_not_before = self._ota_post_reboot_started_at + 1.0
        self._set_ota_status("Device is rebooting; waiting for firmware to return...")
        self._ota_post_reboot_timer.start()

    def _on_ota_abort(self):
        if not self._ota_active or self._ota_state == "WAIT_REBOOT":
            return
        self._send(build_ota_abort())
        self._ota_finish("Aborted by user")

    def _ota_finish(
        self,
        source: str,
        *,
        restore_debug: bool = True,
        **values: Any,
    ):
        should_restore = (
            restore_debug
            and self._ota_restore_debug
            and self._worker is not None
        )
        self._ota_active = False
        self._ota_state = "IDLE"
        self._response_timer.stop()
        self._ota_post_reboot_timer.stop()
        self._pending_request = None
        self.handshake_retry_pause_changed.emit(False)
        self.device_transaction_active_changed.emit(False)
        self._set_controls_enabled(self._worker is not None)
        self._set_ota_status(source, **values)
        self._ota_paused_debug = False
        self._ota_restore_debug = False
        if should_restore:
            QTimer.singleShot(0, lambda: self.debug_mode_requested.emit(True))
        self.status_message.emit(
            tr("OTA: {message}", message=tr(source, **values)),
            5000,
        )

    # ---- COMMAND_RESPONSE 处理 ----

    def _requires_response_context(self) -> bool:
        return self._capability_supported("command_response_context")

    def _success_response_matches(self, resp: CommandResponse, expected: str) -> bool:
        if int(resp.code) != int(RespCode.SUCCESS):
            return False
        if not self._requires_response_context():
            return True
        return (resp.msg or "").strip() == expected

    def _on_command_response(self, resp: CommandResponse):
        if self._pending_request is None:
            return
        req = self._pending_request

        if req == "para_set":
            row = getattr(self, "_pending_para_row", -1)
            expected = f"PARA_SET={self._pending_para_name or ''}"
            if int(resp.code) == int(RespCode.SUCCESS):
                if not self._success_response_matches(resp, expected):
                    return
                if 0 <= row < self._para_table.rowCount():
                    self._set_para_row_status(
                        self._pending_para_name or "",
                        row,
                        "Waiting for device readback...",
                    )
                if self._pending_para_name:
                    self._para_status_by_name[self._pending_para_name] = (
                        "Waiting for device readback...",
                        {},
                    )
                # 新固件会主动回表；旧固件只做一次兜底读取，不再周期轮询。
                if not self._requires_response_context():
                    self._schedule_para_set_verify_read(300)
            else:
                self._response_timer.stop()
                detail = resp.msg or tr("Error {code}", code=resp.code)
                msg = tr("✗ {detail}", detail=detail)
                if 0 <= row < self._para_table.rowCount():
                    self._para_table.item(row, 4).setText(msg)
                if self._pending_para_name:
                    self._para_status_by_name[self._pending_para_name] = (
                        "✗ {detail}",
                        {"detail": detail},
                    )
                self._clear_pending_para_set()
            return

        if req == "para_reset":
            if int(resp.code) == int(RespCode.SUCCESS) and not self._success_response_matches(
                resp, "PARA_RESET=OK"
            ):
                return
            self._response_timer.stop()
            self._pending_request = None
            if int(resp.code) == int(RespCode.SUCCESS):
                self.status_message.emit(
                    tr("Parameters restored to factory defaults; restart is recommended"),
                    5000,
                )
                if not self._requires_response_context():
                    QTimer.singleShot(500, self._on_read_params)
            else:
                self.status_message.emit(
                    tr("Factory reset failed: {detail}", detail=resp.msg),
                    5000,
                )

        elif req == "ota_begin":
            if int(resp.code) == int(RespCode.SUCCESS):
                if not self._success_response_matches(resp, "OTA_BEGIN=READY"):
                    return
                self._response_timer.stop()
                self._pending_request = None
                self._ota_retry = 0
                QTimer.singleShot(0, self._ota_send_current_chunk)
            else:
                self._ota_finish(
                    "OTA_BEGIN rejected: {detail}",
                    detail=resp.msg or resp.code,
                )

        elif req == "ota_data":
            expected = f"OTA_DATA={self._ota_seq}"
            if int(resp.code) == int(RespCode.SUCCESS):
                if not self._success_response_matches(resp, expected):
                    return
                self._response_timer.stop()
                self._pending_request = None
                self._ota_seq += 1
                self._ota_retry = 0
                self._update_ota_progress()
                QTimer.singleShot(0, self._ota_send_current_chunk)
            else:
                self._ota_finish(
                    "Chunk {sequence} rejected: {detail}",
                    sequence=self._ota_seq,
                    detail=resp.msg or resp.code,
                )

        elif req == "ota_end":
            if int(resp.code) == int(RespCode.SUCCESS):
                if not self._success_response_matches(resp, "OTA_END=VERIFIED"):
                    return
                self._ota_enter_wait_reboot()
            else:
                self._ota_finish(
                    "OTA_END verification failed: {detail}",
                    detail=resp.msg or resp.code,
                )

    def _on_response_timeout(self):
        req = self._pending_request
        if req == "ota_data" and self._ota_active:
            if self._ota_retry < OTA_CHUNK_MAX_RETRY:
                self._ota_retry += 1
                self._pending_request = None
                self._set_ota_status(
                    "Chunk {sequence} was not confirmed; retry {retry}/{maximum}",
                    sequence=self._ota_seq,
                    retry=self._ota_retry,
                    maximum=OTA_CHUNK_MAX_RETRY,
                )
                self._ota_send_current_chunk()
            else:
                self._pending_request = None
                self._ota_finish(
                    "Chunk {sequence} timed out repeatedly",
                    sequence=self._ota_seq,
                )
        elif req and req.startswith("ota_"):
            self._pending_request = None
            self._ota_finish("{request} timed out", request=req)
        elif req == "para_set":
            self._pending_request = None
            row = getattr(self, "_pending_para_row", -1)
            if self._pending_para_name:
                self._set_para_row_status(
                    self._pending_para_name,
                    row,
                    "Target value was not read back",
                )
            self._clear_pending_para_set()
        elif req == "para_reset":
            self._pending_request = None
            self.status_message.emit(tr("Factory reset timed out; try again"), 5000)

    def _on_para_read_timeout(self):
        if not self._para_read_pending:
            return
        self._para_read_pending = False
        if self._pending_request == "para_set":
            if self._pending_para_name:
                self._para_status_by_name[self._pending_para_name] = (
                    "Waiting for device readback...",
                    {},
                )
            return
        self._set_para_status("Parameter-table read timed out")
        self.status_message.emit(tr("Parameter-table read timed out; try again"), 3000)

    # ---- 主题 ----

    def retranslate_ui(self) -> None:
        self._render_para_row_statuses()
        for row, para in enumerate(self._params):
            name_item = self._para_table.item(row, 0)
            if name_item is not None and para.flags & PARA_FLAG_REQUIRES_REBOOT:
                name_item.setToolTip(
                    tr("Restart the device for this change to take effect")
                )
        if self._ota_file is None:
            set_translatable_text("No file selected", self._ota_file_label)
        else:
            set_translatable_text(
                "{file}  ({size} bytes)",
                self._ota_file_label,
                file=self._ota_filename,
                size=len(self._ota_file),
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
