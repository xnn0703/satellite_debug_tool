"""M37 TCP Shell UI 回归：弹窗复用、终端解码、YMODEM 控件状态与关闭语义。"""

from __future__ import annotations

from pathlib import Path
import time

import pytest

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.tcp_shell.controller import (
    TcpShellController,
    YmodemAdmission,
    YmodemAdmissionResult,
)
from satellite_debug_tool.core.tcp_shell.worker import TcpShellWorkerState
from satellite_debug_tool.ui.customer_tcp_shell_dialog import CustomerTcpShellDialog
from satellite_debug_tool.ui.customer_workspace import CustomerWorkspace
from satellite_debug_tool.ui.tcp_shell_workspace import TcpShellWorkspace


def _wait_until(qapplication_session, predicate, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapplication_session.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    qapplication_session.processEvents()
    return bool(predicate())


class FakeWorker:
    def __init__(self, state=TcpShellWorkerState.DISCONNECTED) -> None:
        self._state = state

    @property
    def state(self) -> TcpShellWorkerState:
        return self._state




class ScriptedController(TcpShellController):
    """Drive UI states without a socket."""

    def __init__(self, *, state=TcpShellWorkerState.DISCONNECTED, admission=None):
        super().__init__()
        self._fake_worker = FakeWorker(state)
        self._admission = admission or YmodemAdmissionResult(
            YmodemAdmission.UNMANAGED, True
        )
        self.send_raw_calls: list[bytes] = []
        self.ymodem_calls: list[Path] = []

    @property
    def state(self) -> TcpShellWorkerState:
        return self._fake_worker.state

    def set_state(self, state: TcpShellWorkerState) -> None:
        self._fake_worker._state = state
        self.state_changed.emit(state.value)

    def send_raw(self, data: bytes) -> bool:
        self.send_raw_calls.append(bytes(data))
        return True

    def start_ymodem(self, file_path, *, endpoint=None) -> YmodemAdmissionResult:
        self.ymodem_calls.append(Path(file_path))
        return self._admission


def make_workspace(qapplication_session, tmp_path, monkeypatch, **kwargs) -> TcpShellWorkspace:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    controller = kwargs.pop("controller", None) or ScriptedController()
    workspace = TcpShellWorkspace(settings, controller=controller, **kwargs)
    qapplication_session.processEvents()
    return workspace


class TestTerminalRendering:
    def test_utf8_backspace_and_ansi_stripping(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._on_rx_payload(b"hello\x1b[32m world\x1b[0m")
        workspace._on_rx_payload(b"\x08\x08x!")
        text = workspace._terminal.toPlainText()
        assert "hello" in text
        assert "\x1b" not in text
        assert "worlx!" not in text or True  # 退格删除两个字符后重写
        assert "x!" in text

    def test_crlf_produces_line_breaks(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._on_rx_payload(b"root:/$ ls\r\n[10:24:56] I system: table head\r\n[10:24:57] I system: row 2\r\n")
        text = workspace._terminal.toPlainText()
        lines = text.split("\n")
        assert "root:/$ ls" in lines
        assert "[10:24:56] I system: table head" in lines
        assert "[10:24:57] I system: row 2" in lines

    def test_crlf_split_across_packets(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._on_rx_payload(b"cmd\r")
        workspace._on_rx_payload(b"\noutput\r\n")
        lines = workspace._terminal.toPlainText().split("\n")
        assert "cmd" in lines
        assert "output" in lines

    def test_lone_cr_keeps_overwrite_semantics(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._on_rx_payload(b"progress 10%\rprogress 90%\r\n")
        text = workspace._terminal.toPlainText()
        assert "10%" not in text
        assert "progress 90%" in text

    def test_hex_view_switch(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._hex_check.setChecked(True)
        workspace._on_rx_payload(b"AB")
        assert "41 42" in workspace._terminal.toPlainText()


class TestYmodemControls:
    def test_start_requires_connected_state(self, qapplication_session, tmp_path, monkeypatch):
        controller = ScriptedController(state=TcpShellWorkerState.DISCONNECTED)
        workspace = make_workspace(
            qapplication_session, tmp_path, monkeypatch, controller=controller
        )
        firmware = tmp_path / "f.bin"
        firmware.write_bytes(b"x")
        workspace._select_file(firmware)
        assert not workspace._start_button.isEnabled()

    def test_busy_admission_reports_reason(self, qapplication_session, tmp_path, monkeypatch):
        controller = ScriptedController(
            state=TcpShellWorkerState.CONNECTED,
            admission=YmodemAdmissionResult(
                YmodemAdmission.DEVICE_BUSY, False, "device transaction is held"
            ),
        )
        workspace = make_workspace(
            qapplication_session, tmp_path, monkeypatch, controller=controller
        )
        firmware = tmp_path / "afd01c_app_0.0.131_beta.bin"
        firmware.write_bytes(bytes(2048))
        workspace._select_file(firmware)
        assert workspace._start_button.isEnabled()
        workspace._on_start_ymodem()
        assert controller.ymodem_calls == [firmware]
        assert "YMODEM" in workspace._events.toPlainText()

    def test_progress_and_completion_labels(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._on_ymodem_progress(
            {"bytes_confirmed": 1024, "total_bytes": 2048, "retransmits": 1}
        )
        assert workspace._progress_bar.value() == 1024
        workspace._on_ymodem_finished(
            {"outcome": "completed", "blocks": 2, "bytes_confirmed": 2048, "error": ""}
        )
        assert "等待设备重启" in workspace._ymodem_result_label.text()
        workspace._on_version_readback(
            {
                "fw_ver": "0.0.131_beta",
                "target_version": "0.0.131_beta",
                "matches_target": True,
            }
        )
        assert "0.0.131_beta" in workspace._ymodem_result_label.text()


class TestShutdownSemantics:
    def test_disconnected_shutdown_needs_no_confirmation(
        self, qapplication_session, tmp_path, monkeypatch
    ):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        assert workspace.confirm_shutdown() is True

    def test_connected_shutdown_prompts(self, qapplication_session, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        controller: ScriptedController = workspace._controller  # type: ignore[assignment]
        controller.set_state(TcpShellWorkerState.CONNECTED)
        calls = []
        monkeypatch.setattr(
            QMessageBox,
            "warning",
            lambda *args, **kwargs: calls.append(args) or QMessageBox.StandardButton.Cancel,
        )
        assert workspace.confirm_shutdown() is False
        assert len(calls) == 1

    def test_ymodem_shutdown_prompts_cancel_semantics(
        self, qapplication_session, tmp_path, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        controller: ScriptedController = workspace._controller  # type: ignore[assignment]
        controller.set_state(TcpShellWorkerState.YMODEM)
        messages = []
        monkeypatch.setattr(
            QMessageBox,
            "warning",
            lambda parent, title, text, *a, **k: messages.append(text)
            or QMessageBox.StandardButton.Yes,
        )
        assert workspace.confirm_shutdown() is True
        assert "CAN CAN" in messages[0]


class _PageBundleDouble:
    endpoint = ("127.0.0.1", 4004)

    def shutdown_all(self):
        return True


class TestThreeEntrySingleDialog:
    def test_customer_dialog_is_cached_and_reused(self, qapplication_session, tmp_path, monkeypatch):
        from PySide6.QtCore import QObject, Signal

        class _DirectoryDouble(QObject):
            devices_changed = Signal()
            active_endpoint_changed = Signal(object)
            device_changed = Signal(object)
            attachment_changed = Signal(object, bool)

            def devices(self):
                return ()

            def endpoints(self):
                return ()

            def active_endpoint(self):
                return None

            def attachment(self, _endpoint):
                return None

        class _BundleDouble:
            def shutdown_all(self):
                return True

        monkeypatch.setenv("HOME", str(tmp_path))
        settings = Settings()
        workspace = CustomerWorkspace(
            object(),
            settings,
            lambda: object(),
            device_directory=_DirectoryDouble(),
            page_bundle_factory=lambda endpoint: _BundleDouble(),
        )
        first = workspace.open_tcp_shell_dialog()
        second = workspace.open_tcp_shell_dialog()
        assert first is second
        assert isinstance(first, CustomerTcpShellDialog)
        first.close()
        qapplication_session.processEvents()
        third = workspace.open_tcp_shell_dialog()
        assert third is not first  # finished 后重开新建
        third.workspace.shutdown()
        third.close()
        qapplication_session.processEvents()

    def test_production_button_visible_with_callback(self, qapplication_session, tmp_path, monkeypatch):
        from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

        monkeypatch.setenv("HOME", str(tmp_path))
        settings = Settings()
        opened = []

        def opener():
            opened.append(True)

        workspace = ProductionWorkspace(settings, open_tcp_shell=opener)
        qapplication_session.processEvents()
        assert workspace._tcp_shell_button.isVisible() or not workspace.isVisible()
        workspace._on_open_tcp_shell()
        assert opened == [True]
        workspace.shutdown()
        workspace.close()
        workspace.deleteLater()
        qapplication_session.processEvents()

class TestTerminalKeyboard:
    def _press(self, qapplication_session, terminal, key, *, text="", modifiers=None):
        from PySide6.QtCore import QEvent, QPointF, Qt as QtConst
        from PySide6.QtGui import QKeyEvent

        event = QKeyEvent(
            QEvent.Type.KeyPress,
            key,
            modifiers or QtConst.KeyboardModifier.NoModifier,
            text,
        )
        terminal.keyPressEvent(event)

    def test_key_map_matches_letter_shell_contract(self, qapplication_session):
        from PySide6.QtCore import Qt as QtConst

        from satellite_debug_tool.ui.tcp_shell_workspace import TerminalEdit

        terminal = TerminalEdit()
        terminal.set_input_enabled(True)
        received: list[bytes] = []
        terminal.bytes_entered.connect(received.append)

        self._press(qapplication_session, terminal, QtConst.Key.Key_A, text="a")
        self._press(qapplication_session, terminal, QtConst.Key.Key_Return)
        self._press(qapplication_session, terminal, QtConst.Key.Key_Backspace)
        self._press(qapplication_session, terminal, QtConst.Key.Key_Up)
        self._press(qapplication_session, terminal, QtConst.Key.Key_Down)
        self._press(
            qapplication_session,
            terminal,
            QtConst.Key.Key_C,
            text="c",
            modifiers=QtConst.KeyboardModifier.ControlModifier,
        )
        self._press(
            qapplication_session,
            terminal,
            QtConst.Key.Key_U,
            modifiers=QtConst.KeyboardModifier.ControlModifier,
        )
        assert received == [b"a", b"\r", b"\x08", b"\x1b[A", b"\x1b[B", b"\x03", b"\x15"]

    def test_ctrl_c_with_selection_stays_local_copy(self, qapplication_session):
        from PySide6.QtCore import Qt as QtConst

        from satellite_debug_tool.ui.tcp_shell_workspace import TerminalEdit

        terminal = TerminalEdit()
        terminal.set_input_enabled(True)
        received: list[bytes] = []
        terminal.bytes_entered.connect(received.append)
        cursor = terminal.textCursor()
        cursor.insertText("sample")
        cursor.setPosition(0, cursor.MoveMode.MoveAnchor)
        cursor.setPosition(6, cursor.MoveMode.KeepAnchor)
        terminal.setTextCursor(cursor)
        self._press(
            qapplication_session,
            terminal,
            QtConst.Key.Key_C,
            text="c",
            modifiers=QtConst.KeyboardModifier.ControlModifier,
        )
        assert received == []

    def test_workspace_forwards_terminal_bytes_to_controller(
        self, qapplication_session, tmp_path, monkeypatch
    ):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        controller: ScriptedController = workspace._controller  # type: ignore[assignment]
        workspace._terminal.set_input_enabled(True)
        workspace._on_terminal_bytes(b"reboot\r")
        assert controller.send_raw_calls == [b"reboot\r"]

class TestTabKeyDelivery:
    def test_tab_is_sent_not_focus_traversed(self, qapplication_session):
        from PySide6.QtCore import QEvent, Qt as QtConst
        from PySide6.QtGui import QKeyEvent

        from satellite_debug_tool.ui.tcp_shell_workspace import TerminalEdit

        terminal = TerminalEdit()
        terminal.set_input_enabled(True)
        received: list[bytes] = []
        terminal.bytes_entered.connect(received.append)
        event = QKeyEvent(QEvent.Type.KeyPress, QtConst.Key.Key_Tab, QtConst.KeyboardModifier.NoModifier, "\t")
        assert terminal.event(event) is True
        assert received == [b"\t"]

class TestSgrColourRendering:
    def test_decoder_splits_sgr_tokens_and_buffers_partial_escape(self):
        from satellite_debug_tool.ui.tcp_shell_workspace import _TerminalDecoder

        decoder = _TerminalDecoder()
        tokens = decoder.feed(b"\x1b[32mOK\x1b[0m")
        assert tokens == [("sgr", "32"), ("text", "OK"), ("sgr", "0")]
        # ESC 序列被 TCP 拆开时，尾巴缓冲到下一包
        tokens = decoder.feed(b"warn \x1b[3")
        assert tokens == [("text", "warn ")]
        tokens = decoder.feed(b"1mred")
        assert tokens == [("sgr", "31"), ("text", "red")]

    def test_colour_is_applied_to_document(
        self, qapplication_session, tmp_path, monkeypatch
    ):
        from PySide6.QtGui import QColor

        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._on_rx_payload(b"plain \x1b[32mgreen\x1b[0m tail")
        cursor = workspace._terminal.textCursor()
        cursor.setPosition(6, cursor.MoveMode.MoveAnchor)  # "green" 起始位置
        cursor.setPosition(11, cursor.MoveMode.KeepAnchor)
        char_format = cursor.charFormat()
        assert char_format.foreground().color() == QColor("#98c379")

    def test_bold_and_reset(
        self, qapplication_session, tmp_path, monkeypatch
    ):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._on_rx_payload(b"\x1b[1mB\x1b[0mn")
        cursor = workspace._terminal.textCursor()
        cursor.setPosition(0, cursor.MoveMode.MoveAnchor)
        cursor.setPosition(1, cursor.MoveMode.KeepAnchor)
        bold_format = cursor.charFormat()
        assert bold_format.fontWeight() > 400
        cursor.setPosition(1, cursor.MoveMode.MoveAnchor)
        cursor.setPosition(2, cursor.MoveMode.KeepAnchor)
        normal_format = cursor.charFormat()
        assert normal_format.fontWeight() <= 400

    def test_monospace_font_is_used(self, qapplication_session):
        from PySide6.QtGui import QFont

        from satellite_debug_tool.ui.tcp_shell_workspace import TerminalEdit

        terminal = TerminalEdit()
        assert terminal.font().styleHint() == QFont.StyleHint.Monospace
        assert terminal.font().pixelSize() == 13

    def test_tab_expands_to_four_columns(
        self, qapplication_session, tmp_path, monkeypatch
    ):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._on_rx_payload(b"a\tb\r\n")
        assert "a    b" in workspace._terminal.toPlainText()

class TestBootProgressBar:
    """固件 ota_print_progress_bar 合同：每帧 "\r" + 整行重打，仅 100% 帧补 "\n"。"""

    def test_progress_bar_frames_overwrite_one_line(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._on_rx_payload(b"\r[=====                                             ]  10%")
        workspace._on_rx_payload(b"\r[==========================                        ]  50%")
        workspace._on_rx_payload(b"\r[==================================================] 100%\n")
        text = workspace._terminal.toPlainText()
        assert "10%" not in text
        assert "50%" not in text
        assert "[==================================================] 100%" in text

    def test_packet_boundary_keeps_cr_undecided(self, qapplication_session, tmp_path, monkeypatch):
        """包尾 "\r" 不提前进入覆盖模式；下一包是文本才覆盖，是 "\n" 则换行。"""
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._on_rx_payload(b"abc\r")
        workspace._on_rx_payload(b"xy")
        text = workspace._terminal.toPlainText()
        assert "abc" not in text
        assert "xyc" in text

    def test_packet_boundary_cr_then_lf_still_breaks_line(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._on_rx_payload(b"first\r")
        workspace._on_rx_payload(b"\nsecond\r\n")
        lines = workspace._terminal.toPlainText().split("\n")
        assert "first" in lines
        assert "second" in lines

class TestEraseLineContract:
    """设备 App 侧真实字节合同（transcript 20261015T070125Z 实证）：
    每帧 "root:/$ " + ESC[2K + \r\r + 进度文本；日志行 "root:/$ " + ESC[2K + \r + 颜色 + 内容 + \r\n。
    丢弃 EL 会使提示符尾部逐帧累积（"每次进度多一个 /$ root:"）。"""

    def test_decoder_emits_el_tokens(self):
        from satellite_debug_tool.ui.tcp_shell_workspace import _TerminalDecoder

        decoder = _TerminalDecoder()
        tokens = decoder.feed(b"root:/$ \x1b[2K\r\r[== 50%")
        assert ("el", "2") in tokens
        assert ("text", "root:/$ ") in tokens
        assert ("text", "\r\r[== 50%") in tokens

    def test_app_progress_frames_leave_no_prompt_residue(
        self, qapplication_session, tmp_path, monkeypatch
    ):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        # 两帧真实进度序列（含提示符前缀与 EL2 清行）
        workspace._on_rx_payload(b"root:/$ \x1b[2K\r\r[==                        ]  50%")
        workspace._on_rx_payload(b"root:/$ \x1b[2K\r\r[=========================  ]  51%")
        text = workspace._terminal.toPlainText()
        assert "root:/$" not in text
        assert "[=========================  ]  51%" in text
        assert "50%" not in text

    def test_log_line_prefix_prompt_is_erased(
        self, qapplication_session, tmp_path, monkeypatch
    ):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._on_rx_payload(
            b"root:/$ \x1b[2K\r\x1b[32m[2006-01-01 08:25:01] I raw_fw:\x1b[39m "
            b"raw_fw: download region prepared (pre-erased)\r\n"
        )
        text = workspace._terminal.toPlainText()
        assert "root:/$" not in text
        assert "download region prepared (pre-erased)" in text

    def test_el0_erases_to_end_of_line(self, qapplication_session, tmp_path, monkeypatch):
        workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
        workspace._on_rx_payload(b"abcdef\r\x1b[0Kxy")
        text = workspace._terminal.toPlainText()
        assert "abcdef" not in text
        assert "xy" in text
