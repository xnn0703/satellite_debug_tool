"""Shared TCP shell dialog opened from all three workspaces (M37)."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QVBoxLayout

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.i18n import tr
from satellite_debug_tool.ui.tcp_shell_workspace import TcpShellWorkspace


class CustomerTcpShellDialog(QDialog):
    def __init__(
        self,
        settings: Settings,
        *,
        device_directory=None,
        session_directory=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("TCP shell"))
        self.resize(1040, 720)
        self.setMinimumSize(880, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.workspace = TcpShellWorkspace(
            settings,
            device_directory=device_directory,
            session_directory=session_directory,
            parent=self,
        )
        layout.addWidget(self.workspace)

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self.workspace.confirm_shutdown():
            event.ignore()
            return
        self.workspace.deactivate_view()
        self.workspace.shutdown()
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.workspace.activate_view()


__all__ = ["CustomerTcpShellDialog"]
