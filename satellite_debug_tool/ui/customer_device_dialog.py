"""Small editor for one explicitly configured customer UDP endpoint."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.i18n import register_translatable, tr


Endpoint = tuple[str, int]


class CustomerDeviceDialog(QDialog):
    """Collect endpoint intent; the customer directory remains the validator."""

    def __init__(
        self,
        endpoint: Optional[Endpoint] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._editing = endpoint is not None
        self.setWindowTitle(tr("Edit customer device") if self._editing else tr("Add customer device"))
        self.setMinimumWidth(360)

        root = QVBoxLayout(self)
        form = QFormLayout()
        self._ip_edit = QLineEdit(endpoint[0] if endpoint is not None else "")
        self._ip_edit.setPlaceholderText("192.168.1.13")
        self._port_edit = QSpinBox()
        self._port_edit.setRange(1, 65535)
        self._port_edit.setValue(endpoint[1] if endpoint is not None else 4004)
        form.addRow(tr("Device IPv4 address:"), self._ip_edit)
        form.addRow(tr("Device UDP port:"), self._port_edit)
        root.addLayout(form)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)
        register_translatable(self)

    def endpoint(self) -> Endpoint:
        return self._ip_edit.text().strip(), int(self._port_edit.value())

    def retranslate_ui(self) -> None:
        self.setWindowTitle(
            tr("Edit customer device") if self._editing else tr("Add customer device")
        )


__all__ = ["CustomerDeviceDialog"]
