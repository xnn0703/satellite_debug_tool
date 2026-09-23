"""One-time review dialog for immutable production report V2 generation."""

from __future__ import annotations

from typing import Sequence

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.i18n import tr


class ReportReviewDialog(QDialog):
    def __init__(
        self,
        serial_numbers: Sequence[str],
        *,
        default_reviewer: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Review production report"))
        self.setMinimumWidth(520)
        root = QVBoxLayout(self)
        root.addWidget(
            QLabel(
                tr(
                    "Review creates an immutable V2 report. The automatic V1 verdict remains in the audit record."
                )
            )
        )
        form = QFormLayout()
        self._serial = QComboBox()
        self._serial.addItems([str(value) for value in serial_numbers])
        self._reviewer = QLineEdit(default_reviewer)
        self._verdict = QComboBox()
        self._verdict.addItems(["PASS", "FAIL"])
        self._reason = QPlainTextEdit()
        self._reason.setMinimumHeight(110)
        form.addRow(tr("Device SN"), self._serial)
        form.addRow(tr("Reviewer"), self._reviewer)
        form.addRow(tr("Final verdict"), self._verdict)
        form.addRow(tr("Review reason"), self._reason)
        root.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @property
    def serial_number(self) -> str:
        return self._serial.currentText().strip()

    @property
    def reviewer(self) -> str:
        return self._reviewer.text().strip()

    @property
    def final_verdict(self) -> str:
        return self._verdict.currentText()

    @property
    def reason(self) -> str:
        return self._reason.toPlainText().strip()

    def _accept_validated(self) -> None:
        if not self.serial_number or not self.reviewer or not self.reason:
            QMessageBox.warning(
                self,
                tr("Review production report"),
                tr("Device, reviewer, and review reason are required."),
            )
            return
        self.accept()


__all__ = ["ReportReviewDialog"]
