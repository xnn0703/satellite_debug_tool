"""Customer-facing fixture control dialog backed by the shared runtime."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QVBoxLayout

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.production import FixtureRuntime
from satellite_debug_tool.i18n import tr
from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace


class CustomerFixtureDialog(QDialog):
    def __init__(
        self,
        settings: Settings,
        runtime: FixtureRuntime,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Motion platform control"))
        self.resize(1180, 760)
        self.setMinimumSize(960, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.workspace = FixtureDebugWorkspace(
            settings,
            runtime.fixture_lease,
            profile_store=runtime.profile_store,
            calibration_store=runtime.calibration_store,
            reference_lease=runtime.reference_lease,
            telemetry=runtime.telemetry,
            customer_mode=True,
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


__all__ = ["CustomerFixtureDialog"]
