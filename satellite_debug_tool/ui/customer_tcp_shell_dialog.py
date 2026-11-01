"""Shared Shell dialog for user and operations editions (TCP/serial + YMODEM)."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Slot, Signal
from PySide6.QtWidgets import QDialog, QVBoxLayout, QMessageBox

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.i18n import tr
from satellite_debug_tool.ui.tcp_shell_workspace import TcpShellWorkspace


class CustomerTcpShellDialog(QDialog):
    close_failed = Signal(str)

    def __init__(
        self,
        settings: Settings,
        *,
        device_directory=None,
        session_directory=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Shell"))
        self.resize(1040, 720)
        # 侧栏（连接/YMODEM/事件）有 300px 内容下限，终端区随窗口收缩。
        self.setMinimumSize(960, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.workspace = TcpShellWorkspace(
            settings,
            device_directory=device_directory,
            session_directory=session_directory,
            parent=self,
        )
        layout.addWidget(self.workspace)
        self._close_authorized = False
        self._close_event_active = False
        self.workspace.shutdown_finished.connect(self._on_shutdown_finished)
        self.workspace.shutdown_failed.connect(self._on_shutdown_failed)

    @property
    def close_requested(self) -> bool:
        return self._close_authorized

    @property
    def closing(self) -> bool:
        return self._close_authorized and not self.workspace.shutdown_complete

    @Slot(str)
    def _on_shutdown_failed(self, reason: str) -> None:
        self._close_authorized = False
        self.close_failed.emit(reason)
        QMessageBox.warning(self, tr("Shell"), tr("Shell shutdown failed: {reason}", reason=reason))

    @Slot()
    def _on_shutdown_finished(self) -> None:
        if not self._close_event_active:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._close_authorized:
            if not self.workspace.confirm_shutdown():
                event.ignore()
                return
            self._close_authorized = True
        if not self.workspace.shutdown_complete:
            self._close_event_active = True
            try:
                self.workspace.request_shutdown()
            finally:
                self._close_event_active = False
            if not self.workspace.shutdown_complete:
                event.ignore()
                return
        self.workspace.deactivate_view()
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.workspace.activate_view()


__all__ = ["CustomerTcpShellDialog"]
