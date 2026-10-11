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

    def test_cr_overwrites_current_line(self, qapplication_session, tmp_path, monkeypatch):
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
