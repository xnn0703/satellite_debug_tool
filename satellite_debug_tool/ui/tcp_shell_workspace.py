"""Shell + YMODEM widget shared by user and operations editions (M37)."""

from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path
import time
from typing import Optional

from PySide6.QtCore import QCoreApplication, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPalette, QGuiApplication, QKeySequence, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat
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
    QStackedWidget,
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
from satellite_debug_tool.core.tcp_shell.rx_bridge import PushStatus, RxRing
from satellite_debug_tool.core.tcp_shell.worker import TcpShellWorkerState
from satellite_debug_tool.core.tcp_shell.terminal import (
    ShellInputError, TerminalScreen, TerminalStreamDecoder, TerminalStyle, TerminalUpdate, Token,
    command_text_bytes,
)
from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.ui import styles as S

_DEFAULT_TCP_PORT = 1235

# M45 帧合同：帧起点间隔 33 ms；每帧字节预算 16 KiB（对 92,160 B/s 目标留
# 5 倍余量，同时把单帧回调压在 ~50 ms 内，键事件不被长帧阻塞）；drain 按
# 4 KiB 分段取用，8 ms 是分段之间检查的软预算（不切断跨段 UTF-8/CSI），
# 不约束文档提交本身。
_FRAME_INTERVAL_MS = 33
_FRAME_BYTE_BUDGET = 16 * 1024
_DRAIN_SEGMENT_BYTES = 4096
_PARSE_SOFT_BUDGET_S = 0.008
_BACKLOG_VISIBLE_BYTES = 64 * 1024
_SHUTDOWN_DRAIN_TIMEOUT_S = 10.0
# 文档块上限跟随 TerminalScreen.max_rows（投影每次同步设置）。

# 每行行首的接收时刻时间戳（WindTerm 风格）：灰色、固定宽度，覆盖/清行/退格
# 等设备侧行编辑动作只作用于时间戳之后的设备文本。
_COLOR_SOURCE_PROPERTY = int(QTextFormat.Property.UserProperty) + 38
_TIMESTAMP_LEN = len("[00:00:00.000] ")


def _timestamp_text() -> str:
    now = datetime.now()
    return f"[{now:%H:%M:%S}.{now.microsecond // 1000:03d}] "


def _timestamp_format(theme: str = "dark") -> QTextCharFormat:
    char_format = QTextCharFormat()
    char_format.setProperty(_COLOR_SOURCE_PROPERTY, "timestamp")
    char_format.setForeground(QColor(S.palette(theme)["text_muted"]))
    return char_format

_SGR_FOREGROUND = S.ANSI_FOREGROUND

def _log_text(tokens: list[Token]) -> str:
    """Normalise decoded shell text for the .log file: CR becomes LF, drop BS."""

    chunks: list[str] = []
    for kind, value in tokens:
        if kind != "text":
            continue
        chunks.append(value.replace("\r", "\n"))
    text = "".join(chunks)
    while "\b" in text:
        text = text.replace("\b", "")
    return text


class ShellLogWriter:
    """Append device shell text to <IP>_<YYYYMMDD_HHMMSS>.log in one directory."""

    def __init__(self, directory: Path, host: str) -> None:
        safe_host = "".join(
            char if char.isalnum() or char in ".-_" else "_" for char in host
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = directory / f"{safe_host}_{stamp}.log"
        directory.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")

    def write(self, text: str) -> None:
        self._file.write(text)
        self._file.flush()

    def close(self) -> None:
        self._file.flush()
        self._file.close()


class TerminalEdit(QTextEdit):
    """普通命令文字按 ASCII 合同提交，控制键由设备处理并回显。

    命令编辑与历史属于设备；本地文档只投影接收终端模型。
    """

    bytes_entered = Signal(bytes)
    input_rejected = Signal(str)
    text_accepted = Signal()
    clear_requested = Signal()

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
        # Qt 只读 QTextEdit 不绘制 text caret；哑终端在 viewport 自绘闪烁光标，
        # 位置跟随设备回显的写入位置（渲染后 textCursor 停在末尾）。颜色取主题
        # 文字色，深浅背景都与底色形成反差。
        self.setCursorWidth(2)
        self._caret_on = False
        self._output_cursor: QTextCursor | None = None
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(530)
        self._blink_timer.timeout.connect(self._toggle_caret)
        self._input_enabled = False

    def _toggle_caret(self) -> None:
        self._caret_on = not self._caret_on
        self.viewport().update()

    def focusInEvent(self, event) -> None:  # noqa: N802
        self._blink_timer.start()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._blink_timer.stop()
        self._caret_on = False
        self.viewport().update()
        super().focusOutEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if not (self._caret_on and self.hasFocus()):
            return
        caret = self.cursorRect(self._output_cursor) if self._output_cursor is not None else self.cursorRect()
        if caret.isValid():
            painter = QPainter(self.viewport())
            painter.fillRect(
                caret.adjusted(0, 1, 0, -1),
                self.palette().color(QPalette.ColorRole.Text),
            )

    def set_output_cursor(self, cursor: QTextCursor) -> None:
        self._output_cursor = QTextCursor(cursor)
        if not self.textCursor().hasSelection():
            self.setTextCursor(cursor)
        self.viewport().update()

    def set_input_enabled(self, enabled: bool) -> None:
        self._input_enabled = bool(enabled)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, self._input_enabled)

    def inputMethodQuery(self, query):  # noqa: N802
        if query == Qt.InputMethodQuery.ImEnabled:
            return self._input_enabled
        return super().inputMethodQuery(query)

    def inputMethodEvent(self, event) -> None:  # noqa: N802
        if self._input_enabled:
            data = self._command_text(event.commitString())
            if data:
                self.bytes_entered.emit(data)
            event.accept()
            return
        super().inputMethodEvent(event)

    def _command_text(self, text: str) -> bytes:
        try:
            data = command_text_bytes(text)
        except ShellInputError as error:
            self.input_rejected.emit(str(error))
            return b""
        if data:
            self.text_accepted.emit()
        return data

    def _build_context_menu(self):
        # 只读默认菜单（全选/复制）追加清屏；WindTerm 终端右键同级操作。
        from PySide6.QtWidgets import QMenu

        menu = self.createStandardContextMenu()
        menu.addSeparator()
        paste = menu.addAction(tr("Paste"), self._paste_clipboard)
        paste.setEnabled(self._input_enabled)
        menu.addAction(tr("Clear screen"), self.clear_requested.emit)
        return menu

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self._build_context_menu().exec(event.globalPos())

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

    @Slot()
    def _paste_clipboard(self) -> None:
        self.insertFromMimeData(QGuiApplication.clipboard().mimeData())

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._input_enabled and event.matches(QKeySequence.StandardKey.Paste):
            self._paste_clipboard()
            event.accept()
            return
        if self._input_enabled:
            data = self._key_bytes(event)
            if data:
                self.bytes_entered.emit(data)
                return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        if self._input_enabled and source.hasText():
            data = self._command_text(str(source.text()))
            if data:
                self.bytes_entered.emit(data)
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
            return self._command_text(text)
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
        self._theme = S._normalize_theme(settings.get("ui.theme", "dark"))
        self._settings = settings
        self._device_directory = device_directory
        self._controller = controller or TcpShellController(
            device_directory=device_directory,
            session_directory=session_directory,
            session_root=Path(settings.config_directory) / "tcp_shell_sessions",
        )
        self._decoder = TerminalStreamDecoder()
        self._screen = TerminalScreen(_timestamp_text)
        self._input_issue = ""
        self._rx_issue = ""
        self._log_writer: ShellLogWriter | None = None
        self._hex_view = False
        # 每帧消费的活动接收环：连接建立时绑定 worker 的 ring，断开终排空后
        # 重建本地空环。代际来自 ring 的清屏计数，帧内观察到推进即重置解码/模型。
        self._active_ring = RxRing()
        self._rx_epoch = self._active_ring.epoch
        self._final_drain_pending = False
        self._shutdown_draining = False
        self._frame_timer = QTimer(self)
        self._frame_timer.setSingleShot(True)
        self._frame_timer.setInterval(_FRAME_INTERVAL_MS)
        self._frame_timer.timeout.connect(self._flush_frame)
        self._selected_file: Optional[Path] = None
        self._build_ui()
        self._bind_controller()
        self._restore_last_target()
        self._sync_controls()
        self.set_theme(self._theme)
        register_translatable(self)

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = S._normalize_theme(theme)
        palette = S.palette(self._theme)
        self.setStyleSheet(
            f"TcpShellWorkspace {{ background: {palette['bg']}; color: {palette['text']}; }}"
            f"TerminalEdit {{ background: {palette['card']}; color: {palette['text']}; "
            f"selection-background-color: {palette['selected_bg']}; selection-color: {palette['selected_text']}; }}"
        )
        # 每段保存其 ANSI 源色或时间戳角色；主题切换只更新格式，保留原文、游标与解析状态。
        for document in (self._output_document, self._hex_document):
            self._recolor_document(document, palette)

    def _recolor_document(self, document: QTextDocument, palette: dict) -> None:
        edits = []
        block = document.begin()
        while block.isValid():
            fragments = block.begin()
            while not fragments.atEnd():
                fragment = fragments.fragment()
                if fragment.isValid():
                    char_format = fragment.charFormat()
                    source = char_format.property(_COLOR_SOURCE_PROPERTY)
                    if source:
                        color = palette["text_muted"] if source == "timestamp" else S.chart_color(source, self._theme)
                        char_format.setForeground(QColor(color))
                        edits.append((fragment.position(), fragment.length(), char_format))
                fragments += 1
            block = block.next()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        for position, length, char_format in edits:
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.setCharFormat(char_format)
        cursor.endEditBlock()

    # -- UI construction ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # 水平主分裂器：终端占主区，连接/YMODEM/事件收进右侧栏。
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._terminal_box = terminal_box = QGroupBox(tr("Shell terminal"))
        terminal_layout = QVBoxLayout(terminal_box)
        self._terminal = TerminalEdit()
        self._output_document = QTextDocument(self)
        self._hex_document = QTextDocument(self)
        for document in (self._output_document, self._hex_document):
            document.setDefaultFont(self._terminal.font())
            # 文档级关闭：widget 级开关对 setDocument() 后换入的文档无效（M45 缺陷 1）。
            document.setUndoRedoEnabled(False)
        self._hex_document.setMaximumBlockCount(5000)
        self._terminal.setDocument(self._output_document)
        self._terminal.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._terminal.bytes_entered.connect(self._on_terminal_bytes)
        self._terminal.input_rejected.connect(self._on_input_rejected)
        self._terminal.text_accepted.connect(self._on_text_accepted)
        self._terminal.clear_requested.connect(self._on_clear_terminal)
        self._hex_check = QCheckBox(tr("Show raw hex"))
        self._hex_check.toggled.connect(self._on_hex_toggled)
        self._log_check = QCheckBox(tr("Auto-save session log"))
        self._log_check.toggled.connect(self._on_log_toggled)
        self._log_path_label = QLabel("")
        self._log_path_label.setWordWrap(False)
        self._backlog_label = QLabel("")
        self._backlog_label.setVisible(False)
        header_row = QHBoxLayout()
        header_row.addWidget(self._hex_check)
        header_row.addWidget(self._log_check)
        header_row.addWidget(self._backlog_label)
        header_row.addWidget(self._log_path_label, 1)
        terminal_layout.addLayout(header_row)
        self._input_feedback = QLabel("")
        self._input_feedback.setWordWrap(True)
        self._input_feedback.setVisible(False)
        self._rx_feedback = QLabel("")
        self._rx_feedback.setWordWrap(True)
        self._rx_feedback.setVisible(False)
        terminal_layout.addWidget(self._input_feedback)
        terminal_layout.addWidget(self._rx_feedback)
        terminal_layout.addWidget(self._terminal, 1)
        splitter.addWidget(terminal_box)

        sidebar = QWidget()
        # 侧栏内容下限：传输按钮并排与端口行不被压缩换行。
        sidebar.setMinimumWidth(300)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)

        self._target_box = target_box = QGroupBox(tr("Shell target"))
        target_layout = QVBoxLayout(target_box)
        self._type_combo = QComboBox()
        self._type_combo.addItem(tr("TCP"))
        self._type_combo.addItem(tr("Serial"))
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        type_row = QHBoxLayout()
        self._type_label = QLabel(tr("Type"))
        type_row.addWidget(self._type_label)
        type_row.addWidget(self._type_combo, 1)
        target_layout.addLayout(type_row)

        tcp_page = QWidget()
        tcp_row = QHBoxLayout(tcp_page)
        tcp_row.setContentsMargins(0, 0, 0, 0)
        self._device_combo = QComboBox()
        self._device_combo.setEditable(True)
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(_DEFAULT_TCP_PORT)
        tcp_row.addWidget(self._device_combo, 1)
        tcp_row.addWidget(QLabel(tr("Port")))
        tcp_row.addWidget(self._port_spin)

        serial_page = QWidget()
        serial_row = QHBoxLayout(serial_page)
        serial_row.setContentsMargins(0, 0, 0, 0)
        self._serial_combo = QComboBox()
        self._serial_combo.setEditable(True)
        self._baud_combo = QComboBox()
        self._baud_combo.setEditable(True)
        for baud in ("9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"):
            self._baud_combo.addItem(baud)
        self._baud_combo.setCurrentText("115200")
        serial_row.addWidget(self._serial_combo, 1)
        self._baud_label = QLabel(tr("Baudrate"))
        serial_row.addWidget(self._baud_label)
        serial_row.addWidget(self._baud_combo)

        self._config_stack = QStackedWidget()
        self._config_stack.addWidget(tcp_page)
        self._config_stack.addWidget(serial_page)
        target_layout.addWidget(self._config_stack)

        self._connect_button = QPushButton(tr("Connect"))
        self._connect_button.clicked.connect(self._on_connect_clicked)
        self._disconnect_button = QPushButton(tr("Disconnect"))
        self._disconnect_button.clicked.connect(self._on_disconnect_clicked)
        self._state_label = QLabel(tr("Disconnected"))
        action_row = QHBoxLayout()
        action_row.addWidget(self._connect_button, 1)
        action_row.addWidget(self._disconnect_button)
        target_layout.addLayout(action_row)
        target_layout.addWidget(self._state_label)
        sidebar_layout.addWidget(target_box)

        self._ymodem_box = ymodem_box = QGroupBox(tr("YMODEM firmware transfer"))
        ymodem_layout = QVBoxLayout(ymodem_box)
        self._file_edit = QLineEdit()
        self._file_edit.setReadOnly(True)
        self._file_edit.setPlaceholderText(tr("Select a firmware .bin file"))
        self._browse_button = QPushButton(tr("Browse..."))
        self._browse_button.clicked.connect(self._on_browse_clicked)
        file_row = QHBoxLayout()
        file_row.addWidget(self._file_edit, 1)
        file_row.addWidget(self._browse_button)
        self._start_button = QPushButton(tr("Start transfer"))
        self._start_button.clicked.connect(self._on_start_ymodem)
        self._cancel_button = QPushButton(tr("Cancel transfer"))
        self._cancel_button.clicked.connect(self._on_cancel_ymodem)
        action_row = QHBoxLayout()
        action_row.addWidget(self._start_button, 1)
        action_row.addWidget(self._cancel_button, 1)
        self._file_info_label = QLabel(tr("No firmware file selected"))
        self._file_info_label.setWordWrap(True)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 1)
        self._progress_bar.setValue(0)
        self._ymodem_result_label = QLabel("")
        self._ymodem_result_label.setWordWrap(True)
        ymodem_layout.addLayout(file_row)
        ymodem_layout.addLayout(action_row)
        ymodem_layout.addWidget(self._file_info_label)
        ymodem_layout.addWidget(self._progress_bar)
        ymodem_layout.addWidget(self._ymodem_result_label)
        sidebar_layout.addWidget(ymodem_box)

        self._events_box = events_box = QGroupBox(tr("Session events"))
        events_layout = QVBoxLayout(events_box)
        self._events = QPlainTextEdit()
        self._events.setReadOnly(True)
        self._events.setMaximumBlockCount(500)
        events_layout.addWidget(self._events)
        sidebar_layout.addWidget(events_box, 1)

        splitter.addWidget(sidebar)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([720, 320])
        root.addWidget(splitter, 1)

    def _bind_controller(self) -> None:
        self._controller.connected.connect(self._on_connected)
        self._controller.connect_failed.connect(self._on_connect_failed)
        self._controller.disconnected.connect(self._on_disconnected)
        self._controller.state_changed.connect(self._on_state_changed)
        self._controller.rx_pending.connect(self._on_rx_pending)
        self._controller.ymodem_progress.connect(self._on_ymodem_progress)
        self._controller.ymodem_finished.connect(self._on_ymodem_finished)
        self._controller.session_started.connect(self._on_session_started)
        self._controller.version_readback.connect(self._on_version_readback)

    # -- lifecycle -----------------------------------------------------------------

    def activate_view(self) -> None:
        self._refresh_device_choices()
        self._refresh_serial_choices()

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
        # M45 关停顺序：controller 先收尾（worker join，已读残余已入环），
        # 终排空按帧泵事件完成（禁止单回调清空），文本日志最后关闭。
        self._controller.shutdown()
        self._shutdown_draining = True
        self._final_drain_pending = True
        deadline = time.monotonic() + _SHUTDOWN_DRAIN_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._active_ring.backlog_bytes == 0 and not self._final_drain_pending:
                break
            self._flush_frame()
            QCoreApplication.processEvents()
        self._frame_timer.stop()
        self._shutdown_draining = False
        writer, self._log_writer = self._log_writer, None
        if writer is not None:
            writer.close()

    def retranslate_ui(self) -> None:
        self._target_box.setTitle(tr("Shell target"))
        self._type_label.setText(tr("Type"))
        self._baud_label.setText(tr("Baudrate"))
        self._terminal_box.setTitle(tr("Shell terminal"))
        self._ymodem_box.setTitle(tr("YMODEM firmware transfer"))
        self._connect_button.setText(tr("Connect"))
        self._disconnect_button.setText(tr("Disconnect"))
        self._browse_button.setText(tr("Browse..."))
        self._start_button.setText(tr("Start transfer"))
        self._cancel_button.setText(tr("Cancel transfer"))
        self._hex_check.setText(tr("Show raw hex"))
        self._log_check.setText(tr("Auto-save session log"))
        self._file_edit.setPlaceholderText(tr("Select a firmware .bin file"))
        self._file_info_label.setText(
            self._file_info_label.text()
            if self._selected_file is not None
            else tr("No firmware file selected")
        )
        self._events_box.setTitle(tr("Session events"))
        self._refresh_input_feedback()
        self._refresh_state_label()

    # -- target resolution -----------------------------------------------------------

    def _on_type_changed(self, index: int) -> None:
        self._config_stack.setCurrentIndex(index)
        if index == 1:
            self._refresh_serial_choices()

    def _refresh_serial_choices(self) -> None:
        from satellite_debug_tool.core.tcp_shell.transport import list_serial_ports

        combo = self._serial_combo
        current = combo.currentText().strip()
        combo.blockSignals(True)
        combo.clear()
        try:
            for port in list_serial_ports():
                combo.addItem(port)
        except Exception:  # noqa: BLE001 - 枚举失败不阻断手填
            pass
        if current:
            combo.setEditText(current)
        combo.blockSignals(False)

    def _is_serial(self) -> bool:
        return self._type_combo.currentIndex() == 1

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

    def _current_target(self) -> dict:
        if self._is_serial():
            return {
                "transport": "serial",
                "port": self._serial_combo.currentText().strip(),
                "baudrate": int(self._baud_combo.currentText() or "115200"),
            }
        return {
            "transport": "tcp",
            "host": self._current_host(),
            "port": int(self._port_spin.value()),
        }

    def _target_label(self, target: dict) -> str:
        if target["transport"] == "serial":
            return f"{target['port']}@{target['baudrate']}"
        return f"{target['host']}:{target['port']}"

    def _log_file_stem(self) -> str:
        if self._is_serial():
            return self._serial_combo.currentText().strip() or "shell"
        return self._current_host() or "shell"

    def _current_endpoint(self):
        """Resolve the TCP target against managed endpoints by host, else None."""

        if self._is_serial():
            return None
        host = self._current_host()
        if not host or self._device_directory is None:
            return None
        for endpoint in self._device_directory.endpoints():
            if endpoint[0] == host:
                return endpoint
        return None

    def _remember_target(self) -> None:
        self._settings.set(
            "tcp_shell.last_transport", "serial" if self._is_serial() else "tcp"
        )
        self._settings.set("tcp_shell.last_host", self._current_host())
        self._settings.set("tcp_shell.last_port", int(self._port_spin.value()))
        self._settings.set(
            "tcp_shell.serial_last_port", self._serial_combo.currentText().strip()
        )
        self._settings.set("tcp_shell.serial_baudrate", self._baud_combo.currentText())
        self._settings.persist_preferences()

    def _restore_last_target(self) -> None:
        host = str(self._settings.get("tcp_shell.last_host", "") or "")
        port = int(self._settings.get("tcp_shell.last_port", _DEFAULT_TCP_PORT) or _DEFAULT_TCP_PORT)
        if host:
            self._device_combo.setEditText(host)
        if 1 <= port <= 65535:
            self._port_spin.setValue(port)
        serial_port = str(self._settings.get("tcp_shell.serial_last_port", "") or "")
        if serial_port:
            self._serial_combo.setEditText(serial_port)
        self._baud_combo.setCurrentText(
            str(self._settings.get("tcp_shell.serial_baudrate", "115200") or "115200")
        )
        if str(self._settings.get("tcp_shell.last_transport", "tcp") or "tcp") == "serial":
            self._type_combo.setCurrentIndex(1)

    # -- connection --------------------------------------------------------------------

    def _on_connect_clicked(self) -> None:
        target = self._current_target()
        if target["transport"] == "tcp" and not target["host"]:
            self._append_event(tr("Enter a device IPv4 address first."))
            return
        if target["transport"] == "serial" and not target["port"]:
            self._append_event(tr("Enter a serial port first."))
            return
        self._remember_target()
        self._append_event(
            tr("Connecting to {label}...").format(label=self._target_label(target))
        )
        self._controller.connect_device(target, endpoint=self._current_endpoint())
        self._sync_controls()

    def _on_disconnect_clicked(self) -> None:
        self._controller.disconnect_device("operator")
        self._sync_controls()

    def _on_connected(self, label: str) -> None:
        ring = self._controller.rx_ring
        if ring is not None:
            self._active_ring = ring
        self._rx_epoch = self._active_ring.epoch
        self._final_drain_pending = False
        self._decoder.reset()
        self._input_issue = ""
        self._rx_issue = ""
        self._refresh_input_feedback()
        boundary = "\r\n" if self._screen.row.cells else ""
        rows_before = len(self._screen.rows)
        self._project_updates(
            [self._ingest_tokens([("text", boundary), ("sgr", "0")])], rows_before=rows_before
        )
        self._sync_terminal_cursor()
        self._append_event(tr("Connected to {label}").format(label=label))
        # 连接即聚焦终端：键盘输入与自绘光标立即可用。
        self._terminal.setFocus(Qt.FocusReason.OtherFocusReason)
        self._sync_controls()

    def _on_connect_failed(self, reason: str) -> None:
        self._append_event(tr("Connect failed: {reason}").format(reason=reason))
        self._sync_controls()

    def _on_disconnected(self, reason: str) -> None:
        # worker 已退出且残余已入环：终排空按帧完成，最后一帧执行 decoder.finish。
        self._final_drain_pending = True
        if not self._frame_timer.isActive():
            self._frame_timer.start()
        self._append_event(tr("Disconnected: {reason}").format(reason=reason))
        self._sync_controls()

    def _on_state_changed(self, _state: str) -> None:
        self._sync_controls()

    def _on_session_started(self, session_id: str) -> None:
        self._append_event(tr("Evidence session: {id}").format(id=session_id))

    # -- shell terminal ------------------------------------------------------------------

    def _on_terminal_bytes(self, data: bytes) -> None:
        self._controller.send_raw(data)

    @Slot(str)
    def _on_input_rejected(self, characters: str) -> None:
        self._input_issue = characters
        self._refresh_input_feedback()

    @Slot()
    def _on_text_accepted(self) -> None:
        self._input_issue = ""
        self._refresh_input_feedback()

    def _refresh_input_feedback(self) -> None:
        self._input_feedback.setText(
            tr("Input was not sent. Use English/ABC for ASCII shell commands. Characters: {characters}")
            .format(characters=self._input_issue)
        )
        self._input_feedback.setVisible(bool(self._input_issue))
        self._rx_feedback.setText(
            tr("The received stream contains invalid UTF-8 or control sequences ({details}). "
               "Inspect raw hex or the session transcript.")
            .format(details=self._rx_issue)
        )
        self._rx_feedback.setVisible(bool(self._rx_issue))

    def _on_clear_terminal(self) -> None:
        # UI 立即清呈现；worker 按命令序划界（丢弃在途旧字节并推进 ring 代际），
        # 下一帧观察到代际推进时重置解码与模型——边界前分片不进入新屏幕。
        self._decoder.reset()
        self._screen.reset()
        self._output_document.clear()
        self._hex_document.clear()
        self._input_issue = ""
        self._rx_issue = ""
        self._refresh_input_feedback()
        self._sync_terminal_cursor()
        self._controller.clear_display()
        self._append_event(tr("Terminal cleared"))

    def _on_rx_pending(self) -> None:
        if not self._frame_timer.isActive():
            self._frame_timer.start()

    def feed_rx(self, data: bytes) -> None:
        """确定性同步入口：接纳并完成给定字节的消费与投影，与真实路径共用实现。

        分段接纳；接纳遇容量先执行一帧消费再继续，UI 线程不对满环做无界等待。
        """

        remaining = bytes(data)
        while remaining:
            result = self._active_ring.push(remaining)
            remaining = remaining[result.accepted:]
            if remaining or result.status is not PushStatus.ACCEPTED:
                self._flush_frame()
        while self._active_ring.backlog_bytes:
            self._flush_frame()

    def _flush_frame(self) -> None:
        """一帧：预算内排空接收环，帧内合并更新，单 edit block 投影，末尾一次同步。"""

        ring = self._active_ring
        updates: list[TerminalUpdate] = []
        log_buffer: list[str] = []
        hex_segments: list[bytes] = []
        rows_before = len(self._screen.rows)
        budget = _FRAME_BYTE_BUDGET
        deadline = time.perf_counter() + _PARSE_SOFT_BUDGET_S
        while budget > 0:
            data, epoch = ring.drain(min(budget, _DRAIN_SEGMENT_BYTES))
            if epoch != self._rx_epoch:
                self._rx_epoch = epoch
                self._decoder.reset()
                self._screen.reset()
                self._output_document.clear()
            if not data:
                break
            budget -= len(data)
            updates.append(self._ingest_segment(data, log_buffer))
            if self._hex_view:
                hex_segments.append(data)
            if time.perf_counter() >= deadline:
                break
        if log_buffer and self._log_writer is not None:
            self._log_writer.write("".join(log_buffer))
        if hex_segments:
            cursor = QTextCursor(self._hex_document)
            cursor.movePosition(QTextCursor.MoveOperation.End)
            for segment in hex_segments:
                cursor.insertText(_timestamp_text(), _timestamp_format(self._theme))
                cursor.insertText(segment.hex(" ") + "\n", QTextCharFormat())
        if updates:
            self._project_updates(updates, rows_before=rows_before)
        self._sync_terminal_cursor()
        self._refresh_backlog_indicator(ring.backlog_bytes)
        if self._final_drain_pending and ring.backlog_bytes == 0:
            self._complete_final_drain()
            return
        if ring.backlog_bytes and not self._shutdown_draining and not self._frame_timer.isActive():
            self._frame_timer.start()

    def _ingest_segment(self, data: bytes, log_buffer: list[str]) -> TerminalUpdate:
        tokens = self._decoder.feed(data)
        log_buffer.append(_log_text(tokens))
        self._report_rx_issues(tokens)
        return self._screen.feed(tokens)

    def _ingest_tokens(self, tokens: list[Token]) -> TerminalUpdate:
        if self._log_writer is not None:
            self._log_writer.write(_log_text(tokens))
        self._report_rx_issues(tokens)
        return self._screen.feed(tokens)

    def _report_rx_issues(self, tokens: list[Token]) -> None:
        issues = [value for kind, value in tokens if kind in ("invalid_utf8", "invalid_escape")]
        if issues:
            self._rx_issue = ", ".join(dict.fromkeys(issues))
            self._refresh_input_feedback()

    def _complete_final_drain(self) -> None:
        tokens = self._decoder.finish()
        rows_before = len(self._screen.rows)
        updates: list[TerminalUpdate] = []
        if tokens:
            updates.append(self._ingest_tokens(tokens))
        if updates:
            self._project_updates(updates, rows_before=rows_before)
        self._sync_terminal_cursor()
        self._final_drain_pending = False
        self._active_ring = RxRing()
        self._rx_epoch = self._active_ring.epoch

    def _refresh_backlog_indicator(self, backlog: int) -> None:
        if backlog > _BACKLOG_VISIBLE_BYTES:
            self._backlog_label.setText(
                tr("RX backlog: {kib} KiB").format(kib=backlog // 1024)
            )
            self._backlog_label.setVisible(True)
        else:
            self._backlog_label.setVisible(False)

    def _project_updates(self, updates: list[TerminalUpdate], *, rows_before: int) -> None:
        """帧内合并投影：先平移既有脏行过本次淘汰，再吸收新更新；单 edit block。

        文档物理淘汰由 maximumBlockCount(=模型 max_rows) 拥有（Qt 内部按块摊销，
        满载 5000 块实测 ~4-5 ms/帧；手动从头删除是 O(文档) 且与块数无关的
        ~160ms，已删除该路径）。语义淘汰 owner 仍是 TerminalScreen；寻址偏移只
        计帧前已存在的被淘汰行——帧内"先创建后被淘汰"的行从未进入文档。
        """

        dirty_first: int | None = None
        removed_total = 0
        for update in updates:
            if dirty_first is None:
                dirty_first = update.first_row
            else:
                dirty_first = min(max(0, dirty_first - update.removed_rows), update.first_row)
            removed_total += update.removed_rows
        if dirty_first is None:
            return
        document = self._output_document
        document.setMaximumBlockCount(self._screen.max_rows)
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        offset = min(removed_total, rows_before)
        block = document.findBlockByNumber(dirty_first + offset)
        if block.isValid():
            cursor.setPosition(block.position())
            cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        else:
            # 帧内淘汰超过旧文档内容：脏行起点已在文档尾，追加新块而非并入末块。
            cursor.movePosition(QTextCursor.MoveOperation.End)
            if document.lastBlock().length() > 1:
                cursor.insertBlock()
        for index, row in enumerate(self._screen.rows[dirty_first:]):
            if index:
                cursor.insertBlock()
            if row.timestamp:
                cursor.insertText(row.timestamp, _timestamp_format(self._theme))
            run: list[str] = []
            style = TerminalStyle()
            for cell in row.cells:
                if cell.style != style:
                    if run:
                        cursor.insertText("".join(run), self._cell_format(style))
                        run.clear()
                    style = cell.style
                run.append(cell.text)
            if run:
                cursor.insertText("".join(run), self._cell_format(style))
        cursor.endEditBlock()

    def _cell_format(self, style: TerminalStyle) -> QTextCharFormat:
        char_format = QTextCharFormat()
        source = _SGR_FOREGROUND.get(style.foreground)
        if source is not None:
            char_format.setProperty(_COLOR_SOURCE_PROPERTY, source)
            char_format.setForeground(QColor(S.chart_color(source, self._theme)))
        if style.bold:
            char_format.setFontWeight(QFont.Weight.Bold)
        return char_format

    def _sync_terminal_cursor(self) -> None:
        if self._hex_view:
            cursor = QTextCursor(self._hex_document)
            cursor.movePosition(QTextCursor.MoveOperation.End)
        else:
            row = self._screen.row
            cursor = QTextCursor(self._output_document.lastBlock())
            cursor.setPosition(cursor.position() + len(row.timestamp)
                               + row.text_offset(self._screen.column))
        self._terminal.set_output_cursor(cursor)
        scrollbar = self._terminal.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_hex_toggled(self, checked: bool) -> None:
        self._hex_view = bool(checked)
        self._terminal.setDocument(self._hex_document if checked else self._output_document)
        self._sync_terminal_cursor()

    def _on_log_toggled(self, checked: bool) -> None:
        if checked:
            directory = self._resolve_log_directory()
            if directory is None:
                self._log_check.setChecked(False)
                return
            host = self._log_file_stem()
            self._log_writer = ShellLogWriter(directory, host)
            self._log_path_label.setText(str(self._log_writer.path))
            self._append_event(
                tr("Session log: {path}").format(path=self._log_writer.path)
            )
        else:
            writer, self._log_writer = self._log_writer, None
            if writer is not None:
                writer.close()
                self._append_event(
                    tr("Session log saved: {path}").format(path=writer.path)
                )
            self._log_path_label.setText("")

    def _resolve_log_directory(self):
        # 已配置过的目录直接使用（不存在时由 writer 重建）；仅首次使用弹选择框。
        configured = str(self._settings.get("tcp_shell.log_dir", "") or "")
        if configured:
            return Path(configured)
        directory = QFileDialog.getExistingDirectory(
            self, tr("Choose the shell log directory"), str(Path.home())
        )
        if not directory:
            return None
        self._settings.set("tcp_shell.log_dir", directory)
        self._settings.persist_preferences()
        return Path(directory)

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
        self._file_edit.setToolTip(str(path))
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
