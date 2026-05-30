"""设置弹窗：配置路径（录制/log/固件）后写入 settings.json 持久化。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.profile import ProfileStore


class SettingsDialog(QDialog):
    """全局路径配置弹窗。

    3 行：录制/回放目录、Log 导入目录、固件导入目录。
    每行 LineEdit + 浏览按钮。点确定写入 Settings 并保存到 settings.json。

    M10：增加"管理图表分组..."二级入口（profile_store 提供时启用）。
    """

    def __init__(
        self,
        settings: Settings,
        parent: Optional[QWidget] = None,
        profile_store: Optional[ProfileStore] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._profile_store = profile_store
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

        # M10 F2：图表分组管理入口（profile_store 提供时启用）
        chart_row = QHBoxLayout()
        chart_row.addWidget(QLabel("图表分组:"))
        chart_row.addStretch(1)
        self._btn_chart_groups = QPushButton("管理图表分组...")
        self._btn_chart_groups.setToolTip(
            "自定义 chart 分组模式下哪些通道在同一子图（按 hw_type 隔离配置）"
        )
        self._btn_chart_groups.clicked.connect(self._on_open_chart_groups)
        self._btn_chart_groups.setEnabled(self._profile_store is not None)
        chart_row.addWidget(self._btn_chart_groups)
        outer.addLayout(chart_row)

        # M11：更新设置 section
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep)
        outer.addWidget(QLabel("自动更新"))

        self._cb_auto_check = QCheckBox("启动时后台检查更新")
        self._cb_auto_check.setChecked(bool(self._settings.get("update.auto_check", True)))
        self._cb_auto_check.setToolTip("关闭后仅手动点工具栏「检查更新」时才查")
        outer.addWidget(self._cb_auto_check)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("检查间隔（小时）:"))
        self._spin_interval = QSpinBox()
        self._spin_interval.setRange(1, 168)   # 1h ~ 7d
        self._spin_interval.setValue(int(self._settings.get("update.check_interval_hours", 24)))
        self._spin_interval.setToolTip("距上次检查不足此时长不会重复查")
        interval_row.addWidget(self._spin_interval)
        interval_row.addStretch(1)
        outer.addLayout(interval_row)

        skip_row = QHBoxLayout()
        self._lbl_skipped = QLabel(
            f"已跳过版本: {self._settings.get('update.skip_version', '') or '（无）'}"
        )
        skip_row.addWidget(self._lbl_skipped)
        skip_row.addStretch(1)
        self._btn_reset_skip = QPushButton("重置跳过版本")
        self._btn_reset_skip.setEnabled(bool(self._settings.get("update.skip_version", "")))
        self._btn_reset_skip.clicked.connect(self._on_reset_skip_version)
        skip_row.addWidget(self._btn_reset_skip)
        outer.addLayout(skip_row)

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
        # M11：更新设置
        self._settings.set("update.auto_check", bool(self._cb_auto_check.isChecked()))
        self._settings.set("update.check_interval_hours", int(self._spin_interval.value()))
        self._settings.save()
        self.accept()

    def _on_reset_skip_version(self) -> None:
        self._settings.set("update.skip_version", "")
        self._settings.save()
        self._lbl_skipped.setText("已跳过版本: （无）")
        self._btn_reset_skip.setEnabled(False)

    def _on_open_chart_groups(self) -> None:
        """打开 ChartGroupDialog（modal，关闭后回到 SettingsDialog）。

        Accept 返回时通过 profile_store.profile_changed 通知所有 chart 重建子图，
        这样新分组立即生效不用重启或重连。
        """
        if self._profile_store is None:
            return
        # 延迟 import 避免 settings_dialog → chart_group_dialog → settings_dialog 循环
        from satellite_debug_tool.ui.chart_group_dialog import ChartGroupDialog
        from PySide6.QtWidgets import QDialog as _QD
        hw = self._profile_store.current_hw_type()
        dlg = ChartGroupDialog(self._profile_store, self._settings, hw, parent=self)
        if dlg.exec() == _QD.DialogCode.Accepted and hw is not None:
            # 触发 profile_changed → GroupedChart._on_profile_changed → _rebuild
            self._profile_store.profile_changed.emit(hw)
