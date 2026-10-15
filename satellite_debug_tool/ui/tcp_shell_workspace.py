"""TCP Shell + YMODEM workspace widget shared by all three workspaces (M37)."""

from __future__ import annotations

import codecs
from datetime import datetime
import hashlib
import re
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QTextCharFormat
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.tcp_shell.controller import (
    TcpShellController,
    parse_target_version,
    ymodem_route_hint,
)
from satellite_debug_tool.core.tcp_shell.worker import TcpShellWorkerState
from satellite_debug_tool.i18n import register_translatable, tr

_ANSI_TOKEN = re.compile(
    rb"\x1b(?:\[([0-9;?]*)([A-Za-z])|\][^\x07\x1b]*(?:\x07|\x1b\\))"
)
_DEFAULT_TCP_PORT = 1235

# Letter Shell 常用 SGR 前景色（one-dark 系，dark 主题下可读；亮色 90-97 加亮）。
_SGR_FOREGROUND = {
    30: "#5c6370",
    31: "#e06c75",
    32: "#98c379",
    33: "#e5c07b",
    34: "#61afef",
    35: "#c678dd",
    36: "#56b6c2",
    37: "#d7dae0",
    90: "#7f848e",
    91: "#ff7b86",
    92: "#b5e890",
    93: "#ffd479",
    94: "#84c1ff",
    95: "#e2a2ff",
    96: "#8ce0e8",
    97: "#ffffff",
}


class _TerminalDecoder:
    """Decode UTF-8 incrementally and split ANSI sequences from text.

    Returns a token list: ``("text", str)`` chunks, ``("sgr", params)`` for
    colour changes and ``("el", mode)`` for Erase-in-Line. Other control
    sequences (cursor moves, OSC) are dropped. A partial escape sequence
    split across TCP packets is buffered.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending_ansi = b""

    def feed(self, data: bytes) -> list[tuple[str, str]]:
        data = self._pending_ansi + data
        self._pending_ansi = b""
        tokens: list[tuple[str, str]] = []
        position = 0
        for match in _ANSI_TOKEN.finditer(data):
            if match.start() > position:
                tokens.append(
                    ("text", self._decoder.decode(data[position : match.start()]))
                )
            final = match.group(2)
            if final == b"m":
                tokens.append(("sgr", (match.group(1) or b"0").decode("ascii")))
            elif final == b"K":
                tokens.append(("el", (match.group(1) or b"0").decode("ascii")))
            position = match.end()
        tail = data[position:]
        keep = 0
        escape = tail.rfind(b"\x1b")
        if escape != -1 and _ANSI_TOKEN.match(tail, escape) is None:
            keep = len(tail) - escape
        if keep < len(tail):
            tokens.append(("text", self._decoder.decode(tail[: len(tail) - keep])))
        self._pending_ansi = tail[len(tail) - keep :] if keep else b""
        return tokens

    def reset(self) -> None:
        self._decoder.reset()
        self._pending_ansi = b""


class TerminalEdit(QTextEdit):
    """Dumb terminal: keystrokes become device bytes; the device echoes back.

    Line editing, history and echo live in the firmware Letter Shell, exactly
    like a physical debug serial line in WindTerm. Local text stays read-only.
    """

    bytes_entered = Signal(bytes)

    # 类级默认值：构造期间的 Qt 事件就可能进入 event() 覆写。
    _input_enabled = False

    _CTRL_KEYS = {
        Qt.Key.Key_D: b"\x04",
        Qt.Key.Key_E: b"\x05",
        Qt.Key.Key_K: b"\x0b",
        Qt.Key.Key_L: b"\x0c",
        Qt.Key.Key_U: b"\x15",
    }
    _SPECIAL_KEYS = {
        Qt.Key.Key_Return: b"\r",
        Qt.Key.Key_Enter: b"\r",
        Qt.Key.Key_Backspace: b"\x08",
        Qt.Key.Key_Up: b"\x1b[A",
        Qt.Key.Key_Down: b"\x1b[B",
        Qt.Key.Key_Right: b"\x1b[C",
        Qt.Key.Key_Left: b"\x1b[D",
        Qt.Key.Key_Delete: b"\x1b[3~",
        Qt.Key.Key_Home: b"\x01",
        Qt.Key.Key_End: b"\x05",
        Qt.Key.Key_Escape: b"\x1b",
        Qt.Key.Key_Tab: b"\t",
        Qt.Key.Key_Backtab: b"\x1b[Z",
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        # Letter Shell 用空格对齐表格与日志列，必须等宽字体；显式声明
        # styleHint/fixedPitch，无系统固定字体别名时 Qt 仍会解析到等宽族。
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        fixed.setPixelSize(13)
        fixed.setStyleHint(QFont.StyleHint.Monospace)
        fixed.setFixedPitch(True)
        self.setFont(fixed)
        self._input_enabled = False

    def set_input_enabled(self, enabled: bool) -> None:
        self._input_enabled = bool(enabled)

    def event(self, event):  # noqa: N802
        # Qt 在 event() 层把 Tab/Backtab 截走做焦点切换，keyPressEvent 收不到；
        # 终端输入启用时在更早一层拦截并作为键值发送。
        from PySide6.QtCore import QEvent

        if (
            self._input_enabled
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)
        ):
            self.bytes_entered.emit(b"\t" if event.key() == Qt.Key.Key_Tab else b"\x1b[Z")
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._input_enabled:
            data = self._key_bytes(event)
            if data:
                self.bytes_entered.emit(data)
                return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        if self._input_enabled and source.hasText():
            self.bytes_entered.emit(str(source.text()).encode("utf-8"))
            return
        super().insertFromMimeData(source)

    def _key_bytes(self, event) -> bytes:
        key = event.key()
        modifiers = event.modifiers()
        text = event.text()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_C:
                # 有选中文本时保留 Ctrl+C 复制，否则发送设备端中断。
                return b"" if self.textCursor().hasSelection() else b"\x03"
            return self._CTRL_KEYS.get(key, b"")
        if modifiers & (Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
            return b""
        if key in self._SPECIAL_KEYS:
            return self._SPECIAL_KEYS[key]
        if text:
            return text.encode("utf-8")
        return b""


class TcpShellWorkspace(QWidget):
    """Terminal, YMODEM sender and session evidence for one raw TCP shell."""

    def __init__(
        self,
        settings: Settings,
        *,
        device_directory=None,
        session_directory=None,
        controller: TcpShellController | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._device_directory = device_directory
        self._controller = controller or TcpShellController(
            device_directory=device_directory,
            session_directory=session_directory,
        )
        self._decoder = _TerminalDecoder()
        self._pending_cr = False
        self._overwrite_mode = False
        self._sgr_foreground: str | None = None
        self._sgr_bold = False
        self._hex_view = False
        self._selected_file: Optional[Path] = None
        self._build_ui()
        self._bind_controller()
        self._restore_last_target()
        self._sync_controls()
        register_translatable(self)

    # -- UI construction ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        target_box = QGroupBox(tr("TCP shell target"))
        target_layout = QHBoxLayout(target_box)
        self._device_combo = QComboBox()
        self._device_combo.setEditable(True)
        self._device_combo.setMinimumWidth(240)
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(_DEFAULT_TCP_PORT)
        self._connect_button = QPushButton(tr("Connect"))
        self._connect_button.clicked.connect(self._on_connect_clicked)
        self._disconnect_button = QPushButton(tr("Disconnect"))
        self._disconnect_button.clicked.connect(self._on_disconnect_clicked)
        self._state_label = QLabel(tr("Disconnected"))
        target_layout.addWidget(self._device_combo, 1)
        target_layout.addWidget(QLabel(tr("Port")))
        target_layout.addWidget(self._port_spin)
        target_layout.addWidget(self._connect_button)
        target_layout.addWidget(self._disconnect_button)
        target_layout.addWidget(self._state_label)
        root.addWidget(target_box)

        splitter = QSplitter(Qt.Orientation.Vertical)

        terminal_box = QGroupBox(tr("Shell terminal"))
        terminal_layout = QVBoxLayout(terminal_box)
        self._terminal = TerminalEdit()
        self._terminal.document().setMaximumBlockCount(5000)
        self._terminal.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._terminal.bytes_entered.connect(self._on_terminal_bytes)
        self._hex_check = QCheckBox(tr("Show raw hex"))
        self._hex_check.toggled.connect(self._on_hex_toggled)
        header_row = QHBoxLayout()
        header_row.addWidget(self._hex_check)
        header_row.addStretch(1)
        terminal_layout.addLayout(header_row)
        terminal_layout.addWidget(self._terminal, 1)
        splitter.addWidget(terminal_box)

        ymodem_box = QGroupBox(tr("YMODEM firmware transfer"))
        ymodem_layout = QVBoxLayout(ymodem_box)
        file_row = QHBoxLayout()
        self._file_edit = QLineEdit()
        self._file_edit.setReadOnly(True)
        self._file_edit.setPlaceholderText(tr("Select a firmware .bin file"))
        self._browse_button = QPushButton(tr("Browse..."))
        self._browse_button.clicked.connect(self._on_browse_clicked)
        self._start_button = QPushButton(tr("Start transfer"))
        self._start_button.clicked.connect(self._on_start_ymodem)
        self._cancel_button = QPushButton(tr("Cancel transfer"))
        self._cancel_button.clicked.connect(self._on_cancel_ymodem)
        file_row.addWidget(self._file_edit, 1)
        file_row.addWidget(self._browse_button)
        file_row.addWidget(self._start_button)
        file_row.addWidget(self._cancel_button)
        self._file_info_label = QLabel(tr("No firmware file selected"))
        self._file_info_label.setWordWrap(True)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 1)
        self._progress_bar.setValue(0)
        self._ymodem_result_label = QLabel("")
        self._ymodem_result_label.setWordWrap(True)
        ymodem_layout.addLayout(file_row)
        ymodem_layout.addWidget(self._file_info_label)
        ymodem_layout.addWidget(self._progress_bar)
        ymodem_layout.addWidget(self._ymodem_result_label)
        splitter.addWidget(ymodem_box)

        self._events_box = events_box = QGroupBox(tr("Session events"))
        events_layout = QVBoxLayout(events_box)
        self._events = QPlainTextEdit()
        self._events.setReadOnly(True)
        self._events.setMaximumBlockCount(500)
        events_layout.addWidget(self._events)
        splitter.addWidget(events_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 1)
        root.addWidget(splitter, 1)

    def _bind_controller(self) -> None:
        self._controller.connected.connect(self._on_connected)
        self._controller.connect_failed.connect(self._on_connect_failed)
        self._controller.disconnected.connect(self._on_disconnected)
        self._controller.state_changed.connect(self._on_state_changed)
        self._controller.rx_payload.connect(self._on_rx_payload)
        self._controller.ymodem_progress.connect(self._on_ymodem_progress)
        self._controller.ymodem_finished.connect(self._on_ymodem_finished)
        self._controller.session_started.connect(self._on_session_started)
        self._controller.version_readback.connect(self._on_version_readback)

    # -- lifecycle -----------------------------------------------------------------

    def activate_view(self) -> None:
        self._refresh_device_choices()

    def deactivate_view(self) -> None:
        return

    def confirm_shutdown(self) -> bool:
        state = self._controller.state
        if state is TcpShellWorkerState.DISCONNECTED:
            return True
        if state is TcpShellWorkerState.YMODEM:
            message = tr(
                "A YMODEM transfer is running. Closing sends CAN CAN to cancel "
                "the transfer and saves the session as incomplete. Close anyway?"
            )
        else:
            message = tr(
                "The TCP shell is connected. Closing disconnects the session. "
                "Close anyway?"
            )
        answer = QMessageBox.warning(
            self,
            tr("Close TCP shell"),
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer is QMessageBox.StandardButton.Yes

    def shutdown(self) -> None:
        self._controller.shutdown()

    def retranslate_ui(self) -> None:
        self._connect_button.setText(tr("Connect"))
        self._disconnect_button.setText(tr("Disconnect"))
        self._browse_button.setText(tr("Browse..."))
        self._start_button.setText(tr("Start transfer"))
        self._cancel_button.setText(tr("Cancel transfer"))
        self._hex_check.setText(tr("Show raw hex"))
        self._file_edit.setPlaceholderText(tr("Select a firmware .bin file"))
        self._file_info_label.setText(
            self._file_info_label.text()
            if self._selected_file is not None
            else tr("No firmware file selected")
        )
        self._events_box.setTitle(tr("Session events"))
        self._refresh_state_label()

    # -- target resolution -----------------------------------------------------------

    def _refresh_device_choices(self) -> None:
        combo = self._device_combo
        current = combo.currentText().strip()
        combo.blockSignals(True)
        combo.clear()
        if self._device_directory is not None:
            for endpoint in self._device_directory.endpoints():
                combo.addItem(f"{endpoint[0]}:{endpoint[1]}")
        if current:
            combo.setEditText(current)
        combo.blockSignals(False)

    def _current_host(self) -> str:
        text = self._device_combo.currentText().strip()
        if ":" in text:
            text = text.rsplit(":", 1)[0]
        return text

    def _current_endpoint(self):
        """Resolve the target against managed endpoints by host, else None."""

        host = self._current_host()
        if not host or self._device_directory is None:
            return None
        for endpoint in self._device_directory.endpoints():
            if endpoint[0] == host:
                return endpoint
        return None

    def _remember_target(self) -> None:
        self._settings.set("tcp_shell.last_host", self._current_host())
        self._settings.set("tcp_shell.last_port", int(self._port_spin.value()))
        self._settings.persist_preferences()

    def _restore_last_target(self) -> None:
        host = str(self._settings.get("tcp_shell.last_host", "") or "")
        port = int(self._settings.get("tcp_shell.last_port", _DEFAULT_TCP_PORT) or _DEFAULT_TCP_PORT)
        if host:
            self._device_combo.setEditText(host)
        if 1 <= port <= 65535:
            self._port_spin.setValue(port)

    # -- connection --------------------------------------------------------------------

    def _on_connect_clicked(self) -> None:
        host = self._current_host()
        if not host:
            self._append_event(tr("Enter a device IPv4 address first."))
            return
        self._remember_target()
        self._append_event(tr("Connecting to {host}:{port}...").format(host=host, port=self._port_spin.value()))
        self._controller.connect_device(
            host,
            int(self._port_spin.value()),
            endpoint=self._current_endpoint(),
        )
        self._sync_controls()

    def _on_disconnect_clicked(self) -> None:
        self._controller.disconnect_device("operator")
        self._sync_controls()

    def _on_connected(self, host: str, port: int) -> None:
        self._append_event(tr("Connected to {host}:{port}").format(host=host, port=port))
        self._sync_controls()

    def _on_connect_failed(self, reason: str) -> None:
        self._append_event(tr("Connect failed: {reason}").format(reason=reason))
        self._sync_controls()

    def _on_disconnected(self, reason: str) -> None:
        self._append_event(tr("Disconnected: {reason}").format(reason=reason))
        self._sync_controls()

    def _on_state_changed(self, _state: str) -> None:
        self._sync_controls()

    def _on_session_started(self, session_id: str) -> None:
        self._append_event(tr("Evidence session: {id}").format(id=session_id))

    # -- shell terminal ------------------------------------------------------------------

    def _on_terminal_bytes(self, data: bytes) -> None:
        self._controller.send_raw(data)

    def _on_rx_payload(self, data: bytes) -> None:
        if self._hex_view:
            self._append_terminal(data.hex(" ") + "\n")
            return
        cursor = self._terminal.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        pending: list[str] = []

        def flush() -> None:
            if pending:
                chunk = "".join(pending)
                if self._overwrite_mode:
                    cursor.movePosition(
                        cursor.MoveOperation.Right,
                        cursor.MoveMode.KeepAnchor,
                        len(chunk),
                    )
                cursor.insertText(chunk, self._current_char_format())
                pending.clear()
                self._overwrite_mode = False

        def resolve_pending_cr() -> None:
            # "\r\n" 是一次换行；单独 "\r" 回到行首进入覆盖模式。
            if self._pending_cr:
                self._pending_cr = False
                cursor.movePosition(cursor.MoveOperation.StartOfLine)
                self._overwrite_mode = True

        def erase_line(mode: str) -> None:
            # 设备日志/进度合同：先打提示符，再 ESC[2K 清整行、\r 回行首重绘。
            # 丢弃 EL 会残留提示符尾部并逐帧累积（"每次进度多一个 /$ root:"）。
            flush()
            resolve_pending_cr()
            if mode == "1":
                cursor.movePosition(cursor.MoveOperation.StartOfLine, cursor.MoveMode.KeepAnchor)
            elif mode == "2":
                cursor.movePosition(cursor.MoveOperation.StartOfLine)
                cursor.movePosition(cursor.MoveOperation.EndOfLine, cursor.MoveMode.KeepAnchor)
            else:
                cursor.movePosition(cursor.MoveOperation.EndOfLine, cursor.MoveMode.KeepAnchor)
            if cursor.hasSelection():
                cursor.removeSelectedText()

        def render_text(text: str) -> None:
            for char in text:
                if char == "\r":
                    flush()
                    self._pending_cr = True
                    continue
                if char == "\n":
                    flush()
                    if self._pending_cr:
                        self._pending_cr = False
                    cursor.insertText("\n")
                    continue
                resolve_pending_cr()
                if char == "\b":
                    flush()
                    cursor.deletePreviousChar()
                elif char == "\t":
                    # 等宽字体下按固定 4 列展开，保持设备空格对齐。
                    pending.append("    ")
                else:
                    pending.append(char)

        for kind, value in self._decoder.feed(data):
            if kind == "sgr":
                flush()
                self._apply_sgr(value)
            elif kind == "el":
                erase_line(value)
            else:
                render_text(value)
        # 包尾的 "\r" 不提前决断：TCP 包边界不得改变语义——下一字符是 "\n"
        # 则换行，是其他字符才进入行首覆盖（boot 进度条与 shell 提示符并发
        # 交错时，提前决断会把覆盖意图泄漏到无关文本上产生半替换碎片）。
        flush()
        self._terminal.setTextCursor(cursor)
        scrollbar = self._terminal.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _current_char_format(self) -> QTextCharFormat:
        if self._sgr_foreground is None and not self._sgr_bold:
            return QTextCharFormat()
        char_format = QTextCharFormat()
        if self._sgr_foreground is not None:
            char_format.setForeground(QColor(self._sgr_foreground))
        if self._sgr_bold:
            char_format.setFontWeight(QFont.Weight.Bold)
        return char_format

    def _apply_sgr(self, params: str) -> None:
        codes: list[int] = []
        for part in params.split(";"):
            codes.append(int(part) if part.isdigit() else 0)
        if not codes:
            codes = [0]
        index = 0
        while index < len(codes):
            code = codes[index]
            if code == 0:
                self._sgr_foreground = None
                self._sgr_bold = False
            elif code == 1:
                self._sgr_bold = True
            elif code == 22:
                self._sgr_bold = False
            elif code == 39:
                self._sgr_foreground = None
            elif code in _SGR_FOREGROUND:
                self._sgr_foreground = _SGR_FOREGROUND[code]
            elif code in (38, 48):
                # 256/RGB 扩展色：跳过其后参数（38;5;n / 38;2;r;g;b），保持当前色。
                if index + 1 < len(codes) and codes[index + 1] == 5:
                    index += 2
                elif index + 1 < len(codes) and codes[index + 1] == 2:
                    index += 4
            index += 1

    def _append_terminal(self, text: str) -> None:
        cursor = self._terminal.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        scrollbar = self._terminal.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_hex_toggled(self, checked: bool) -> None:
        self._hex_view = bool(checked)

    # -- YMODEM -----------------------------------------------------------------------------

    def _on_browse_clicked(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, tr("Select firmware file"), "", "Binary (*.bin);;All files (*)"
        )
        if not path:
            return
        self._select_file(Path(path))

    def _select_file(self, path: Path) -> None:
        data = path.read_bytes()
        target_version = parse_target_version(path.name)
        info = tr("File: {name} | {size} bytes | SHA-256 {digest}").format(
            name=path.name, size=len(data), digest=hashlib.sha256(data).hexdigest()[:16]
        )
        route = ymodem_route_hint(path.name)
        info += " | " + tr("Device route: {route}").format(route=route)
        if target_version:
            info += " | " + tr("Target version: {version}").format(version=target_version)
        if not target_version and route == "download":
            info += " | " + tr("Filename does not encode a version")
        self._file_edit.setText(str(path))
        self._file_info_label.setText(info)
        self._selected_file = path
        self._ymodem_result_label.setText("")
        self._sync_controls()

    def _on_start_ymodem(self) -> None:
        if self._selected_file is None:
            return
        endpoint = self._current_endpoint()
        admission = self._controller.start_ymodem(self._selected_file, endpoint=endpoint)
        if not admission.allowed:
            self._append_event(
                tr("YMODEM rejected: {detail}").format(detail=admission.detail)
            )
            return
        self._append_event(
            tr("YMODEM admission: {mode}").format(mode=admission.admission.value)
        )
        self._progress_bar.setRange(0, 1)
        self._progress_bar.setValue(0)
        self._ymodem_result_label.setText("")
        self._sync_controls()

    def _on_cancel_ymodem(self) -> None:
        self._controller.cancel_ymodem()

    def _on_ymodem_progress(self, payload: dict) -> None:
        total = int(payload.get("total_bytes", 0))
        confirmed = int(payload.get("bytes_confirmed", 0))
        retransmits = int(payload.get("retransmits", 0))
        self._progress_bar.setRange(0, max(total, 1))
        self._progress_bar.setValue(confirmed)
        self._ymodem_result_label.setText(
            tr("{confirmed}/{total} bytes | retransmits: {retransmits}").format(
                confirmed=confirmed, total=total, retransmits=retransmits
            )
        )

    def _on_ymodem_finished(self, payload: dict) -> None:
        outcome = str(payload.get("outcome", ""))
        self._progress_bar.setValue(self._progress_bar.maximum())
        if outcome == "completed":
            self._ymodem_result_label.setText(
                tr(
                    "Transfer completed; waiting for the device to reboot. "
                    "Version readback appears after the session returns."
                )
            )
        else:
            self._ymodem_result_label.setText(
                tr("Transfer {outcome}: {error}").format(
                    outcome=outcome, error=payload.get("error", "")
                )
            )
        self._append_event(
            tr("YMODEM {outcome}: {blocks} blocks, {bytes} bytes").format(
                outcome=outcome,
                blocks=payload.get("blocks", 0),
                bytes=payload.get("bytes_confirmed", 0),
            )
        )
        self._sync_controls()

    def _on_version_readback(self, payload: dict) -> None:
        target = str(payload.get("target_version", ""))
        current = str(payload.get("fw_ver", ""))
        if payload.get("matches_target"):
            text = tr("Readback version {version} matches the target.").format(
                version=current
            )
        else:
            text = tr(
                "Readback version {current}; target {target}. "
                "Apply confirmation is a separate fact."
            ).format(current=current, target=target or "-")
        self._ymodem_result_label.setText(text)
        self._append_event(text)

    # -- shared helpers -----------------------------------------------------------------------

    def _append_event(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._events.appendPlainText(f"[{stamp}] {text}")

    def _refresh_state_label(self) -> None:
        state = self._controller.state
        labels = {
            TcpShellWorkerState.DISCONNECTED: tr("Disconnected"),
            TcpShellWorkerState.CONNECTING: tr("Connecting..."),
            TcpShellWorkerState.CONNECTED: tr("Connected"),
            TcpShellWorkerState.YMODEM: tr("YMODEM transfer"),
        }
        self._state_label.setText(labels.get(state, tr("Disconnected")))

    def _sync_controls(self) -> None:
        state = self._controller.state
        connected = state in (
            TcpShellWorkerState.CONNECTED,
            TcpShellWorkerState.YMODEM,
        )
        transferring = state is TcpShellWorkerState.YMODEM
        self._connect_button.setEnabled(not connected)
        self._disconnect_button.setEnabled(connected)
        self._terminal.set_input_enabled(connected and not transferring)
        self._start_button.setEnabled(
            connected and not transferring and self._selected_file is not None
        )
        self._cancel_button.setEnabled(transferring)
        self._browse_button.setEnabled(not transferring)
        self._refresh_state_label()


__all__ = ["TcpShellWorkspace"]
