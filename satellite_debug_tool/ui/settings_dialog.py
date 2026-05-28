"""设置弹窗：配置路径（录制/log/固件）后写入 settings.json 持久化。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.config import Settings


class SettingsDialog(QDialog):
    """全局路径配置弹窗。

    3 行：录制/回放目录、Log 导入目录、固件导入目录。
    每行 LineEdit + 浏览按钮。点确定写入 Settings 并保存到 settings.json。
    """

    def __init__(self, settings: Settings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("设置")
        self.setMinimumWidth(520)
        self._setup_ui()

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        outer.addWidget(QLabel("目录配置（用于文件对话框的默认起始位置）"))

        # 3 行路径配置
        self._recording_edit = self._make_row(
            outer, "录制 / 回放目录:",
            self._settings.get("paths.recording_dir", ""),
            "选择录制 / 回放目录"
        )
        self._log_edit = self._make_row(
            outer, "Log 导入目录:",
            self._settings.get("paths.log_dir", ""),
            "选择 Log 导入目录"
        )
        self._firmware_edit = self._make_row(
            outer, "固件导入目录:",
            self._settings.get("paths.firmware_dir", ""),
            "选择固件导入目录"
        )

        outer.addStretch()

        # 提示
        hint = QLabel(
            "留空则使用系统默认（上次打开的位置）。\n"
            f"配置文件: ~/.satellite_debug_tool/settings.json"
        )
        hint.setStyleSheet("color: #888; font-size: 11px;")
        outer.addWidget(hint)

        # 按钮
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _make_row(self, layout: QVBoxLayout, label_text: str,
                  initial: str, browse_title: str) -> QLineEdit:
        """构造一行 [Label] [LineEdit] [浏览...] 并加到 layout，返回 LineEdit。"""
        row = QHBoxLayout()
        lbl = QLabel(label_text)
        lbl.setMinimumWidth(120)
        edit = QLineEdit(initial)
        edit.setPlaceholderText("（未设置，使用系统默认）")
        browse_btn = QPushButton("浏览...")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(lambda: self._on_browse(edit, browse_title))
        row.addWidget(lbl)
        row.addWidget(edit, 1)
        row.addWidget(browse_btn)
        layout.addLayout(row)
        return edit

    def _on_browse(self, edit: QLineEdit, title: str) -> None:
        current = edit.text().strip() or ""
        directory = QFileDialog.getExistingDirectory(self, title, current)
        if directory:
            edit.setText(directory)

    def _on_accept(self) -> None:
        # 写入 settings（去掉首尾空格，空字符串清空配置）
        self._settings.set("paths.recording_dir", self._recording_edit.text().strip())
        self._settings.set("paths.log_dir", self._log_edit.text().strip())
        self._settings.set("paths.firmware_dir", self._firmware_edit.text().strip())
        self._settings.save()
        self.accept()
