"""DeviceView — 设备 Tab：设备信息 + 参数管理 + OTA 固件升级。

M9 新增。通过 debug 协议远程读写设备参数、上传固件。
共享 Live Tab 的连接（worker），不新建连接。
"""

from __future__ import annotations

import socket
import time
import zlib
from pathlib import Path
from typing import Optional

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
    build_debug_enable_v2,
    build_device_reboot,
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


class DeviceView(QWidget):
    """设备 Tab：设备信息 + 参数表 + OTA。"""

    status_message = Signal(str, int)

    def __init__(self, parent: Optional[QWidget] = None, settings=None):
        super().__init__(parent)
        self._worker = None
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

        # OTA 状态机
        self._ota_active = False
        self._ota_file: Optional[bytes] = None
        self._ota_filename = ""
        self._ota_seq = 0
        self._ota_total_chunks = 0
        self._ota_crc32 = 0
        self._ota_retry = 0
        self._ota_paused_debug = False
        self._ota_start_time = 0.0

        # OTA 升级后等待设备重启 + 新版本上线。设备 bootloader 流程
        # （Store Firmware → Load Firmware → jump → app 启动 → 发 META）
        # 整体约 45-60s，所以超时 120s、探测间隔 3s 取宽裕。
        self._ota_post_reboot_fw_before: str = ""   # 升级前的 fw_ver 快照
        self._ota_post_reboot_deadline: float = 0.0  # 超时绝对时间
        self._ota_post_reboot_started_at: float = 0.0  # 开始等待时刻（用于 UI 显示已等待秒数）
        self._ota_post_reboot_timer = QTimer(self)
        self._ota_post_reboot_timer.setInterval(3000)
        self._ota_post_reboot_timer.timeout.connect(self._on_ota_post_reboot_tick)

        # 待应答请求（用于超时匹配 COMMAND_RESPONSE）
        self._pending_request: Optional[str] = None  # "para_set" / "para_reset" / "ota_begin" / "ota_data" / "ota_end"
        self._response_timer = QTimer(self)
        self._response_timer.setSingleShot(True)
        self._response_timer.timeout.connect(self._on_response_timeout)

        self._setup_ui()

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        # ---- 设备信息卡片 ----
        info_group = QGroupBox("设备信息")
        info_layout = QGridLayout(info_group)
        info_layout.setSpacing(6)

        self._info_labels = {}
        for row, (key, label) in enumerate([
            ("hw_type", "设备类型:"),
            ("fw_ver", "固件版本:"),
            ("device_sn", "序列号:"),
            ("protocol_ver", "协议版本:"),
        ]):
            info_layout.addWidget(QLabel(label), row, 0)
            val = QLabel("—")
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            info_layout.addWidget(val, row, 1)
            self._info_labels[key] = val

        self._refresh_info_btn = QPushButton("刷新")
        self._refresh_info_btn.setFixedWidth(80)
        self._refresh_info_btn.clicked.connect(self._on_refresh_info)
        info_layout.addWidget(self._refresh_info_btn, 0, 2, 2, 1)

        outer.addWidget(info_group)

        # ---- 参数表 ----
        para_group = QGroupBox("参数管理")
        para_layout = QVBoxLayout(para_group)

        btn_row = QHBoxLayout()
        self._read_all_btn = QPushButton("读取全部")
        self._read_all_btn.clicked.connect(self._on_read_params)
        self._factory_reset_btn = QPushButton("恢复出厂")
        self._factory_reset_btn.clicked.connect(self._on_factory_reset)
        btn_row.addWidget(self._read_all_btn)
        btn_row.addWidget(self._factory_reset_btn)
        btn_row.addStretch()
        para_layout.addLayout(btn_row)

        self._para_table = QTableWidget(0, 5)
        self._para_table.setHorizontalHeaderLabels(["名称", "类型", "当前值", "操作", "状态"])
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
        ota_group = QGroupBox("固件升级 (OTA)")
        ota_layout = QVBoxLayout(ota_group)

        file_row = QHBoxLayout()
        self._ota_file_label = QLabel("未选择文件")
        self._ota_select_btn = QPushButton("选择固件...")
        self._ota_select_btn.clicked.connect(self._on_select_firmware)
        file_row.addWidget(self._ota_file_label, 1)
        file_row.addWidget(self._ota_select_btn)
        ota_layout.addLayout(file_row)

        ctrl_row = QHBoxLayout()
        self._ota_pause_debug_cb = QCheckBox("暂停实时数据（全力传输）")
        self._ota_pause_debug_cb.setChecked(True)
        self._ota_upload_btn = QPushButton("上传并升级")
        self._ota_upload_btn.setEnabled(False)
        self._ota_upload_btn.clicked.connect(self._on_ota_start)
        self._ota_abort_btn = QPushButton("中止")
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

        self._ota_status_label = QLabel("空闲")
        ota_layout.addWidget(self._ota_status_label)

        outer.addWidget(ota_group)

        # ---- 未连接遮罩 ----
        self._overlay = QLabel("请先在实时页面连接设备")
        self._overlay.setAlignment(Qt.AlignCenter)
        self._overlay.setStyleSheet(
            "background-color: rgba(30,30,30,200); color: #888; font-size: 16px;"
        )
        self._overlay.setParent(self)
        self._overlay.raise_()

        self._set_controls_enabled(False)

    # ---- 连接共享 ----

    @Slot(object)
    def set_worker(self, worker):
        """由 MainWindow 桥接 LiveView.connected_worker_changed 调用。"""
        self._worker = worker
        connected = worker is not None
        self._set_controls_enabled(connected)
        if connected:
            self._overlay.hide()
            # 自动读取参数表
            QTimer.singleShot(500, self._on_read_params)
        else:
            self._overlay.show()
            self._overlay.raise_()
            if self._ota_active:
                self._ota_finish("连接断开，OTA 中止")
            # 连接断开时停掉 OTA 后等待的探测，避免对断连 worker 发包
            if self._ota_post_reboot_timer.isActive():
                self._ota_post_reboot_timer.stop()

    @Slot(object)
    def _on_frame_received(self, record):
        """由 MainWindow 桥接 LiveView.frame_received 调用。"""
        if isinstance(record, MetaInfo):
            self._hw_type = record.hw_type
            self._fw_ver = record.fw_ver
            self._device_sn = record.device_sn
            self._protocol_ver = record.protocol_ver
            self._update_info_labels()
            # OTA 后等待新版本上线：清空过 fw_ver 之后，收到任何非空 fw_ver 都算重新上线
            if self._ota_post_reboot_timer.isActive() and record.fw_ver:
                self._ota_post_reboot_timer.stop()
                before = self._ota_post_reboot_fw_before or "?"
                if record.fw_ver != before:
                    msg = f"✓ 升级成功，设备已上线（{before} → {record.fw_ver}）"
                else:
                    msg = f"✓ 设备已重新上线（版本 {record.fw_ver}，与升级前相同）"
                self._ota_status_label.setText(msg)
                self.status_message.emit(f"OTA 完成，设备运行 {record.fw_ver}", 5000)
                # 升级后参数表可能变化，重新读取
                QTimer.singleShot(300, self._on_read_params)
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
            self._ota_post_reboot_timer.stop()
            self._ota_status_label.setText("设备未在 120s 内重新上线，请检查连接")
            self.status_message.emit("OTA 后设备未自动上线，请手动重连", 8000)
            return
        # UI 显示已等待时长
        self._ota_status_label.setText(
            f"设备重启中（已等 {elapsed}s，bootloader 刷写通常约 45s）..."
        )
        # 主动请求 META，触发设备重新发送版本信息（设备未启动期间会丢弃，无副作用）
        if self._worker is not None:
            self._send(build_request_meta_info())

    def _set_controls_enabled(self, enabled: bool):
        for w in (self._read_all_btn, self._factory_reset_btn,
                  self._refresh_info_btn, self._ota_select_btn):
            w.setEnabled(enabled)
        self._ota_upload_btn.setEnabled(enabled and self._ota_file is not None)

    def _send(self, frame: bytes) -> bool:
        if self._worker is None:
            self.status_message.emit("未连接，命令未发送", 3000)
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
        self._send(build_request_para_table())

    def _on_para_table_received(self, report: ParaTableReport):
        self._params = report.params
        self._para_table.setRowCount(len(report.params))
        for row, p in enumerate(report.params):
            # 名称
            name_item = QTableWidgetItem(p.name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            if p.flags & PARA_FLAG_REQUIRES_REBOOT:
                name_item.setToolTip("修改后需要重启设备才能生效")
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
            apply_btn = QPushButton("应用")
            if p.flags & PARA_FLAG_READ_ONLY:
                apply_btn.setEnabled(False)
            apply_btn.clicked.connect(lambda _checked=False, r=row: self._on_para_apply(r))
            self._para_table.setCellWidget(row, 3, apply_btn)

            # 状态
            status_item = QTableWidgetItem("")
            status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
            self._para_table.setItem(row, 4, status_item)

        self.status_message.emit(f"已读取 {len(report.params)} 个参数", 2000)

    def _on_para_apply(self, row: int):
        if row >= len(self._params):
            return
        p = self._params[row]
        edit = self._para_table.cellWidget(row, 2)
        if edit is None:
            return
        new_val = edit.text().strip()
        if new_val == p.value:
            self._para_table.item(row, 4).setText("未修改")
            return
        self._pending_request = "para_set"
        self._pending_para_row = row
        self._para_table.item(row, 4).setText("发送中...")
        if self._send(build_para_set(p.name, new_val)):
            self._response_timer.start(3000)
        else:
            self._para_table.item(row, 4).setText("发送失败")
            self._pending_request = None

    def _on_factory_reset(self):
        ret = QMessageBox.warning(
            self, "恢复出厂",
            "确定恢复所有参数为出厂默认值？\n此操作不可撤销，部分参数需重启生效。",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if ret != QMessageBox.Yes:
            return
        self._pending_request = "para_reset"
        if self._send(build_para_reset()):
            self._response_timer.start(5000)

    # ---- OTA ----

    def _on_select_firmware(self):
        last_dir = ""
        if self._settings is not None:
            last_dir = self._settings.get("paths.firmware_dir", "") or ""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择固件文件", last_dir, "Firmware (*.bin);;All Files (*)"
        )
        if not path:
            return
        p = Path(path)
        data = p.read_bytes()
        self._ota_file = data
        self._ota_filename = p.name
        self._ota_crc32 = zlib.crc32(data) & 0xFFFFFFFF
        self._ota_file_label.setText(f"{p.name}  ({len(data)} 字节)")
        self._ota_upload_btn.setEnabled(self._worker is not None)

    def _on_ota_start(self):
        if self._ota_file is None or self._worker is None:
            return
        self._ota_active = True
        self._ota_seq = 0
        self._ota_retry = 0
        self._ota_total_chunks = (len(self._ota_file) + OTA_CHUNK_SIZE - 1) // OTA_CHUNK_SIZE
        self._ota_start_time = time.monotonic()
        self._ota_upload_btn.setEnabled(False)
        self._ota_select_btn.setEnabled(False)
        self._ota_abort_btn.setEnabled(True)
        self._ota_progress.setValue(0)

        # 直接进入 OTA 传输循环（内部用独立 socket 处理所有通信）
        self._ota_paused_debug = self._ota_pause_debug_cb.isChecked()
        self._ota_transfer_loop()

    def _ota_send_begin(self):
        """发送 OTA_BEGIN（在 DEBUG_ENABLE 响应确认后调用）。"""
        self._ota_status_label.setText("正在发送 OTA_BEGIN...")
        self._pending_request = "ota_begin"
        if self._send(build_ota_begin(len(self._ota_file), self._ota_filename)):
            self._response_timer.start(OTA_CHUNK_TIMEOUT_MS)
        else:
            self._ota_finish("发送 OTA_BEGIN 失败")

    def _ota_transfer_loop(self):
        """OTA 全流程 — 暂停 worker 线程，直接用 worker 的 socket 收发。

        避免 OTA socket 和 worker socket 竞争 W5500 的 last_remote_port。
        """
        sock = getattr(self._worker, '_sock', None)
        remote_addr = getattr(self._worker, '_remote_addr', None)
        if sock is None or remote_addr is None:
            self._ota_finish("无法获取 worker socket")
            return

        # 暂停 worker 线程：设 _running=False 让其 recvfrom 循环退出
        # worker timeout 100ms，给 5x 余量等它退出再独占 socket
        self._worker._running = False
        time.sleep(0.5)

        old_timeout = sock.gettimeout()
        ota_sock = sock  # 复用 worker socket，独占发送/接收

        def send_and_wait_ack(frame: bytes, timeout_s: float = 1.0,
                              retries: int = 3, label: str = "") -> bool:
            """发送帧并等待 COMMAND_RESPONSE(SUCCESS)。"""
            ota_sock.settimeout(timeout_s)
            for attempt in range(retries + 1):
                t_send = time.monotonic()
                ota_sock.sendto(frame, remote_addr)
                try:
                    while True:
                        resp_data, _ = ota_sock.recvfrom(4096)
                        idx = resp_data.find(b"\xaa\x55")
                        if idx >= 0 and len(resp_data) >= idx + 9:
                            cmd = resp_data[idx + 3]
                            if cmd == 0x02:  # COMMAND_RESPONSE
                                data_len = int.from_bytes(resp_data[idx+4:idx+6], "little")
                                payload = resp_data[idx+6:idx+6+data_len]
                                if len(payload) >= 1:
                                    return payload[0] == 0  # SUCCESS=0
                            # 非 COMMAND_RESPONSE（HEARTBEAT 等）继续收
                except socket.timeout:
                    pass
            return False

        try:
            # 1. 暂停实时数据
            if self._ota_paused_debug:
                self._ota_status_label.setText("正在关闭实时数据...")
                QCoreApplication.processEvents()
                if not send_and_wait_ack(build_debug_enable_v2(False), label="DEBUG_OFF"):
                    self._ota_finish("DEBUG_ENABLE(false) 失败")
                    return

            # 2. OTA_BEGIN
            self._ota_status_label.setText("正在发送 OTA_BEGIN...")
            QCoreApplication.processEvents()
            if not send_and_wait_ack(build_ota_begin(len(self._ota_file), self._ota_filename), label="BEGIN"):
                self._ota_finish("OTA_BEGIN 失败")
                return

            # 3. 逐块传输
            for seq in range(self._ota_total_chunks):
                if not self._ota_active:
                    return

                offset = seq * OTA_CHUNK_SIZE
                chunk = self._ota_file[offset:offset + OTA_CHUNK_SIZE]

                if not send_and_wait_ack(build_ota_data(seq, chunk), timeout_s=1.0,
                                        retries=3, label=f"DATA[{seq}]"):
                    self._ota_finish(f"块 {seq} 传输失败")
                    return

                self._ota_seq = seq + 1

                # 每 20 块更新 UI
                if self._ota_seq % 20 == 0 or self._ota_seq >= self._ota_total_chunks:
                    pct = int(self._ota_seq * 100 / self._ota_total_chunks)
                    self._ota_progress.setValue(pct)
                    elapsed = time.monotonic() - self._ota_start_time
                    transferred = self._ota_seq * OTA_CHUNK_SIZE
                    if elapsed > 0.1 and transferred > 0:
                        speed_kbs = transferred / elapsed / 1024
                        eta = (len(self._ota_file) - transferred) / (transferred / elapsed)
                        self._ota_status_label.setText(
                            f"传输中... {self._ota_seq}/{self._ota_total_chunks} ({pct}%)  "
                            f"{speed_kbs:.1f} KB/s  剩余 {int(eta)}s"
                        )
                    QCoreApplication.processEvents()

            # 4. OTA_END（设备 ACK 后会立即自动 reboot，无需再发 DEVICE_REBOOT）
            self._ota_progress.setValue(100)
            self._ota_status_label.setText("校验中...")
            QCoreApplication.processEvents()
            if not send_and_wait_ack(build_ota_end(self._ota_crc32), timeout_s=5.0, label="END"):
                self._ota_finish("OTA_END 校验失败")
                return

            # 5. 启动等待重启 + 新版本上线的探测循环
            # 清空当前 fw_ver 让 UI 显示 "—"，下次收到任何 META 就视为设备重新上线
            # （不要求 fw_ver 必须变化，覆盖"刷同版本固件"场景）
            self._ota_post_reboot_fw_before = self._fw_ver
            self._fw_ver = ""
            self._device_sn = ""
            self._update_info_labels()
            self._ota_post_reboot_started_at = time.monotonic()
            self._ota_post_reboot_deadline = self._ota_post_reboot_started_at + 120.0
            self._ota_status_label.setText("设备重启中（bootloader 刷写约 45s），等待新版本上线...")
            self._ota_post_reboot_timer.start()
            self._ota_finish("固件上传完成，设备重启中")

        except Exception as e:
            self._ota_finish(f"OTA 异常: {e}")
        finally:
            # 恢复 socket timeout 并重启 worker 线程
            try:
                sock.settimeout(old_timeout)
            except Exception:
                pass
            self._worker._running = True
            self._worker.start()  # 重启 QThread

    def _ota_send_end(self):
        self._pending_request = "ota_end"
        self._ota_status_label.setText("校验中...")
        if self._send(build_ota_end(self._ota_crc32)):
            self._response_timer.start(5000)
        else:
            self._ota_finish("发送 OTA_END 失败")

    def _on_ota_abort(self):
        self._send(build_ota_abort())
        self._ota_finish("用户中止")

    def _ota_finish(self, msg: str):
        self._ota_active = False
        self._response_timer.stop()
        self._pending_request = None
        self._ota_abort_btn.setEnabled(False)
        self._ota_upload_btn.setEnabled(self._worker is not None and self._ota_file is not None)
        self._ota_select_btn.setEnabled(self._worker is not None)
        self._ota_status_label.setText(msg)
        if self._ota_paused_debug:
            self._send(build_debug_enable_v2(True))
            self._ota_paused_debug = False
        self.status_message.emit(f"OTA: {msg}", 5000)

    # ---- COMMAND_RESPONSE 处理 ----

    def _on_command_response(self, resp: CommandResponse):
        if self._pending_request is None:
            return
        self._response_timer.stop()
        req = self._pending_request
        self._pending_request = None

        if req == "para_set":
            row = getattr(self, "_pending_para_row", -1)
            if row >= 0 and row < self._para_table.rowCount():
                if resp.code == RespCode.SUCCESS:
                    self._para_table.item(row, 4).setText("✓ 成功")
                    # 重新读取参数表确认
                    QTimer.singleShot(200, self._on_read_params)
                else:
                    self._para_table.item(row, 4).setText(f"✗ {resp.msg or f'错误 {resp.code}'}")

        elif req == "para_reset":
            if resp.code == RespCode.SUCCESS:
                self.status_message.emit("参数已恢复出厂默认，建议重启设备", 5000)
                QTimer.singleShot(500, self._on_read_params)
            else:
                self.status_message.emit(f"恢复出厂失败: {resp.msg}", 5000)

        elif req == "ota_debug_off":
            # DEBUG_ENABLE(false) 响应到了，现在安全发 OTA_BEGIN
            self._ota_send_begin()
            return

        elif req == "ota_begin":
            if resp.code == RespCode.SUCCESS:
                # 进入同步传输循环（不再用 timer 异步等待）
                self._ota_transfer_loop()
            else:
                self._ota_finish(f"OTA_BEGIN 被拒绝: {resp.msg or resp.code}")

        elif req == "ota_data":
            # _ota_transfer_loop 中 processEvents 触发到这里，
            # pending_request 已被清空，loop 的 spin 检测到后推进
            pass

        elif req == "ota_end":
            if resp.code == RespCode.SUCCESS:
                self._ota_progress.setValue(100)
                self._ota_status_label.setText("设备重启中...")
                self._send(build_device_reboot())
                self._ota_finish("固件上传完成，设备正在重启")
            else:
                self._ota_finish(f"OTA_END 校验失败: {resp.msg or resp.code}")

    def _on_response_timeout(self):
        req = self._pending_request
        self._pending_request = None
        if req and req.startswith("ota_"):
            # ota_data 超时由 _ota_transfer_loop 内部处理，这里只处理 begin/end/debug_off
            self._ota_finish(f"{req} 超时")
        elif req == "para_set":
            row = getattr(self, "_pending_para_row", -1)
            if 0 <= row < self._para_table.rowCount():
                self._para_table.item(row, 4).setText("超时")
        elif req == "para_reset":
            self.status_message.emit("恢复出厂超时，请重试", 5000)

    # ---- 主题 ----

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
