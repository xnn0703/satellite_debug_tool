"""设置弹窗：配置路径（录制/log/固件）后写入 settings.json 持久化。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
from satellite_debug_tool.i18n import (
    LANGUAGE_AUTO,
    LANGUAGE_EN_US,
    LANGUAGE_ZH_CN,
    get_translation_manager,
    register_translatable,
    set_translatable_text,
    tr,
)


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
        self.setWindowTitle(tr("Settings"))
        self.setMinimumWidth(520)
        self._setup_ui()
        register_translatable(self)

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        language_row = QHBoxLayout()
        language_label = QLabel(tr("Language:"))
        language_label.setMinimumWidth(120)
        self._language_combo = QComboBox()
        self._language_combo.addItem(tr("System default"), LANGUAGE_AUTO)
        self._language_combo.addItem(tr("Simplified Chinese"), LANGUAGE_ZH_CN)
        self._language_combo.addItem("English", LANGUAGE_EN_US)
        current_language = str(self._settings.get("ui.language", LANGUAGE_AUTO))
        current_index = self._language_combo.findData(current_language)
        self._language_combo.setCurrentIndex(max(0, current_index))
        language_row.addWidget(language_label)
        language_row.addWidget(self._language_combo, 1)
        outer.addLayout(language_row)

        outer.addWidget(
            QLabel(tr("Default folders used by file selection dialogs"))
        )

        # 3 行路径配置
        self._recording_edit = self._make_row(
            outer, tr("Recording / playback folder:"),
            self._settings.get("paths.recording_dir", ""),
            "recording",
        )
        self._log_edit = self._make_row(
            outer, tr("Log import folder:"),
            self._settings.get("paths.log_dir", ""),
            "log",
        )
        self._firmware_edit = self._make_row(
            outer, tr("Firmware folder:"),
            self._settings.get("paths.firmware_dir", ""),
            "firmware",
        )

        # 地图：天地图 token（在线地图 + GPS 轨迹，坐标准）
        td_row = QHBoxLayout()
        td_lbl = QLabel(tr("Tianditu token:"))
        td_lbl.setMinimumWidth(120)
        self._tianditu_edit = QLineEdit(self._settings.get("map.tianditu_token", ""))
        self._tianditu_edit.setPlaceholderText(
            tr(
                "Application key from lbs.tianditu.gov.cn "
                "(leave blank to use offline OSM)"
            )
        )
        self._tianditu_edit.setToolTip(
            tr(
                "Online Tianditu tile key (tk). Playback and Log maps use "
                "Tianditu with WGS-84 coordinates when configured; otherwise "
                "the app falls back to the offline OSM cache."
            )
        )
        td_row.addWidget(td_lbl)
        td_row.addWidget(self._tianditu_edit, 1)
        outer.addLayout(td_row)

        # M10 F2：图表分组管理入口（profile_store 提供时启用）
        chart_row = QHBoxLayout()
        chart_row.addWidget(QLabel(tr("Chart groups:")))
        chart_row.addStretch(1)
        self._btn_chart_groups = QPushButton(tr("Manage chart groups..."))
        self._btn_chart_groups.setToolTip(
            tr(
                "Choose which channels share each subplot in grouped mode "
                "(saved separately for each hardware type)"
            )
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
        outer.addWidget(QLabel(tr("Automatic updates")))

        self._cb_auto_check = QCheckBox(tr("Check for updates at startup"))
        self._cb_auto_check.setChecked(bool(self._settings.get("update.auto_check", True)))
        self._cb_auto_check.setToolTip(
            tr("When disabled, updates are checked only when requested manually")
        )
        outer.addWidget(self._cb_auto_check)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel(tr("Check interval (hours):")))
        self._spin_interval = QSpinBox()
        self._spin_interval.setRange(1, 168)   # 1h ~ 7d
        self._spin_interval.setValue(int(self._settings.get("update.check_interval_hours", 24)))
        self._spin_interval.setToolTip(
            tr("Do not check again until this interval has elapsed")
        )
        interval_row.addWidget(self._spin_interval)
        interval_row.addStretch(1)
        outer.addLayout(interval_row)

        skip_row = QHBoxLayout()
        self._lbl_skipped = QLabel()
        self._render_skipped_version()
        skip_row.addWidget(self._lbl_skipped)
        skip_row.addStretch(1)
        self._btn_reset_skip = QPushButton(tr("Reset skipped version"))
        self._btn_reset_skip.setEnabled(bool(self._settings.get("update.skip_version", "")))
        self._btn_reset_skip.clicked.connect(self._on_reset_skip_version)
        skip_row.addWidget(self._btn_reset_skip)
        outer.addLayout(skip_row)

        outer.addStretch()

        # 提示
        hint = QLabel(
            tr(
                "Leave a folder blank to use the system default "
                "(the last opened location).\n"
                "Configuration file: ~/.satellite_debug_tool/settings.json"
            )
        )
        hint.setStyleSheet("color: #888; font-size: 11px;")
        outer.addWidget(hint)

        # 按钮
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(tr("OK"))
        buttons.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _make_row(self, layout: QVBoxLayout, label_text: str,
                  initial: str, browse_title_id: str) -> QLineEdit:
        """构造一行 [Label] [LineEdit] [浏览...] 并加到 layout，返回 LineEdit。"""
        row = QHBoxLayout()
        lbl = QLabel(label_text)
        lbl.setMinimumWidth(120)
        edit = QLineEdit(initial)
        edit.setPlaceholderText(tr("Not set; use the system default"))
        browse_btn = QPushButton(tr("Browse..."))
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(
            lambda: self._on_browse(edit, browse_title_id)
        )
        row.addWidget(lbl)
        row.addWidget(edit, 1)
        row.addWidget(browse_btn)
        layout.addLayout(row)
        return edit

    @staticmethod
    def _browse_title(title_id: str) -> str:
        return {
            "recording": tr("Select recording / playback folder"),
            "log": tr("Select Log import folder"),
            "firmware": tr("Select firmware folder"),
        }[title_id]

    def _on_browse(self, edit: QLineEdit, title_id: str) -> None:
        current = edit.text().strip() or ""
        directory = QFileDialog.getExistingDirectory(
            self, self._browse_title(title_id), current
        )
        if directory:
            edit.setText(directory)

    def _on_accept(self) -> None:
        # 写入 settings（去掉首尾空格，空字符串清空配置）
        self._settings.set("paths.recording_dir", self._recording_edit.text().strip())
        self._settings.set("paths.log_dir", self._log_edit.text().strip())
        self._settings.set("paths.firmware_dir", self._firmware_edit.text().strip())
        # 地图 token
        self._settings.set("map.tianditu_token", self._tianditu_edit.text().strip())
        language = str(self._language_combo.currentData() or LANGUAGE_AUTO)
        self._settings.set("ui.language", language)
        # M11：更新设置
        self._settings.set("update.auto_check", bool(self._cb_auto_check.isChecked()))
        self._settings.set("update.check_interval_hours", int(self._spin_interval.value()))
        self._settings.save()
        manager = get_translation_manager()
        if manager is not None:
            manager.set_preference(language)
        self.accept()

    def _on_reset_skip_version(self) -> None:
        self._settings.set("update.skip_version", "")
        self._settings.save()
        self._render_skipped_version()
        self._btn_reset_skip.setEnabled(False)

    def _render_skipped_version(self) -> None:
        skipped = self._settings.get("update.skip_version", "") or tr("None")
        set_translatable_text(
            "Skipped version: {version}",
            self._lbl_skipped,
            version=skipped,
        )

    def retranslate_ui(self) -> None:
        self._render_skipped_version()

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
