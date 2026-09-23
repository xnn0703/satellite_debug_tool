"""设置弹窗：配置路径（录制/log/固件）后写入 settings.json 持久化。"""

from __future__ import annotations

import ipaddress
from typing import Callable, Optional

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
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


SETTINGS_SCOPE_CUSTOMER = "customer"
SETTINGS_SCOPE_ENGINEERING = "engineering"
SETTINGS_SCOPE_PRODUCTION = "production"
_SETTINGS_SCOPES = frozenset(
    (SETTINGS_SCOPE_CUSTOMER, SETTINGS_SCOPE_ENGINEERING, SETTINGS_SCOPE_PRODUCTION)
)

from satellite_debug_tool.core.config import Settings, SettingsSaveError
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
        device_udp_port_editable: Optional[Callable[[], bool]] = None,
        scope: str = SETTINGS_SCOPE_PRODUCTION,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._profile_store = profile_store
        self._device_udp_port_editable = device_udp_port_editable
        self._scope = str(scope)
        if self._scope not in _SETTINGS_SCOPES:
            raise ValueError(f"unknown settings scope: {scope}")
        self._recovery_active = bool(
            settings.read_only_recovery or settings.device_configuration_blocked
        )
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

        device_udp_row = QHBoxLayout()
        self._device_udp_label = QLabel(tr("Device UDP local port:"))
        self._device_udp_label.setMinimumWidth(120)
        self._device_udp_port = QSpinBox()
        self._device_udp_port.setRange(1, 65535)
        self._device_udp_port.setValue(
            int(self._settings.get("device_udp.local_port", 45678))
        )
        self._device_udp_port.setToolTip(
            tr("Customer and production sessions share this UDP socket")
        )
        self._refresh_device_udp_port_editability()
        device_udp_row.addWidget(self._device_udp_label)
        device_udp_row.addWidget(self._device_udp_port)
        device_udp_row.addStretch(1)
        outer.addLayout(device_udp_row)

        self._external_power_section = QWidget()
        external_power_layout = QVBoxLayout(self._external_power_section)
        external_power_layout.setContentsMargins(0, 0, 0, 0)
        external_power_layout.setSpacing(7)
        self._external_power_title = QLabel(tr("External power"))
        external_power_layout.addWidget(self._external_power_title)
        external_power_row = QHBoxLayout()
        self._external_power_host_label = QLabel(tr("Power supply IPv4:"))
        self._external_power_host_label.setMinimumWidth(120)
        self._external_power_host = QLineEdit(
            str(self._settings.get("external_power.host", ""))
        )
        self._external_power_host.setPlaceholderText(
            tr("Not configured; monitoring is disabled")
        )
        self._external_power_port = QLabel("2268")
        self._external_power_port.setToolTip(tr("Fixed read-only SCPI port"))
        external_power_row.addWidget(self._external_power_host_label)
        external_power_row.addWidget(self._external_power_host, 1)
        self._external_power_port_label = QLabel(tr("Port:"))
        external_power_row.addWidget(self._external_power_port_label)
        external_power_row.addWidget(self._external_power_port)
        external_power_layout.addLayout(external_power_row)
        self._external_power_hint = QLabel(
            tr("Read-only monitoring: voltage, current and status; output is never controlled.")
        )
        self._external_power_hint.setWordWrap(True)
        external_power_layout.addWidget(self._external_power_hint)
        self._external_power_section.setVisible(
            self._scope == SETTINGS_SCOPE_CUSTOMER
        )
        outer.addWidget(self._external_power_section)

        self._recovery_frame = QFrame()
        self._recovery_frame.setObjectName("settingsRecoveryFrame")
        recovery = QVBoxLayout(self._recovery_frame)
        recovery.setContentsMargins(10, 10, 10, 10)
        recovery.setSpacing(7)
        self._recovery_title = QLabel(tr("Device settings recovery required"))
        self._recovery_title.setObjectName("settingsRecoveryTitle")
        recovery.addWidget(self._recovery_title)
        self._recovery_detail = QLabel(
            str(self._settings.device_configuration_error)
            or tr("Device settings cannot be used safely.")
        )
        self._recovery_detail.setWordWrap(True)
        recovery.addWidget(self._recovery_detail)
        self._recovery_devices_label = QLabel(
            tr("Customer devices (one IPv4:port per line):")
        )
        recovery.addWidget(self._recovery_devices_label)
        self._recovery_devices = QPlainTextEdit()
        self._recovery_devices.setFixedHeight(76)
        configured = self._settings.get("customer.devices", [])
        if isinstance(configured, list):
            self._recovery_devices.setPlainText(
                "\n".join(
                    f"{item.get('ip')}:{item.get('port')}"
                    for item in configured
                    if isinstance(item, dict)
                )
            )
        recovery.addWidget(self._recovery_devices)
        confirm_row = QHBoxLayout()
        self._recovery_confirmation_label = QLabel(
            tr("Type REBUILD_DEVICE_SETTINGS to rebuild:")
        )
        self._recovery_confirmation = QLineEdit()
        confirm_row.addWidget(self._recovery_confirmation_label)
        confirm_row.addWidget(self._recovery_confirmation, 1)
        recovery.addLayout(confirm_row)
        recovery_actions = QHBoxLayout()
        self._export_recovery_btn = QPushButton(tr("Export recovery evidence..."))
        self._export_recovery_btn.clicked.connect(self._on_export_recovery)
        self._rebuild_settings_btn = QPushButton(tr("Rebuild device settings"))
        self._rebuild_settings_btn.clicked.connect(self._on_rebuild_settings)
        recovery_actions.addWidget(self._export_recovery_btn)
        recovery_actions.addStretch(1)
        recovery_actions.addWidget(self._rebuild_settings_btn)
        recovery.addLayout(recovery_actions)
        self._recovery_frame.setVisible(self._recovery_active)
        outer.addWidget(self._recovery_frame)

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

        self._production_section = QWidget()
        production_layout = QVBoxLayout(self._production_section)
        production_layout.setContentsMargins(0, 0, 0, 0)
        production_layout.setSpacing(10)
        production_row = QHBoxLayout()
        self._production_configuration_label = QLabel(tr("Production configurations:"))
        self._btn_production_configurations = QPushButton(
            tr("Manage production configurations...")
        )
        self._btn_production_configurations.clicked.connect(
            self._on_open_production_configurations
        )
        production_row.addWidget(self._production_configuration_label)
        production_row.addStretch(1)
        production_row.addWidget(self._btn_production_configurations)
        production_layout.addLayout(production_row)

        self._production_report_title = QLabel(tr("Production report"))
        production_layout.addWidget(self._production_report_title)
        self._report_root_edit = self._make_row(
            production_layout,
            tr("Report output folder:"),
            self._settings.get("paths.production_report_dir", ""),
            "report",
        )
        branding = self._settings.get("production.report_branding", {})
        if not isinstance(branding, dict):
            branding = {}
        self._report_company = QLineEdit(str(branding.get("company_name", "")))
        self._report_logo = QLineEdit(str(branding.get("logo_path", "")))
        self._report_header = QLineEdit(str(branding.get("header", "")))
        self._report_footer = QLineEdit(str(branding.get("footer", "")))
        self._report_tester_role = QLineEdit(str(branding.get("tester_role", "")))
        self._report_reviewer_role = QLineEdit(str(branding.get("reviewer_role", "")))
        for label_text, editor in (
            (tr("Company name:"), self._report_company),
            (tr("Report header:"), self._report_header),
            (tr("Report footer:"), self._report_footer),
            (tr("Tester role:"), self._report_tester_role),
            (tr("Reviewer role:"), self._report_reviewer_role),
        ):
            row = QHBoxLayout()
            label = QLabel(label_text)
            label.setMinimumWidth(120)
            row.addWidget(label)
            row.addWidget(editor, 1)
            production_layout.addLayout(row)
        logo_row = QHBoxLayout()
        logo_label = QLabel(tr("Report logo:"))
        logo_label.setMinimumWidth(120)
        logo_button = QPushButton(tr("Browse..."))
        logo_button.setFixedWidth(80)
        logo_button.clicked.connect(self._on_browse_report_logo)
        logo_row.addWidget(logo_label)
        logo_row.addWidget(self._report_logo, 1)
        logo_row.addWidget(logo_button)
        production_layout.addLayout(logo_row)
        self._production_section.setVisible(
            self._scope == SETTINGS_SCOPE_PRODUCTION
        )
        outer.addWidget(self._production_section)

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
        self._production_configuration_label.setText(
            tr("Production configurations:")
        )
        self._btn_production_configurations.setText(
            tr("Manage production configurations...")
        )
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
        self._dialog_buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self._dialog_buttons.button(QDialogButtonBox.Ok).setText(tr("OK"))
        self._dialog_buttons.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        self._dialog_buttons.accepted.connect(self._on_accept)
        self._dialog_buttons.rejected.connect(self.reject)
        outer.addWidget(self._dialog_buttons)

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
            "report": tr("Select production report folder"),
        }[title_id]

    def _on_browse_report_logo(self) -> None:
        filename, _selected = QFileDialog.getOpenFileName(
            self,
            tr("Select report logo"),
            self._report_logo.text().strip(),
            tr("Image files (*.png *.jpg *.jpeg);;All files (*)"),
        )
        if filename:
            self._report_logo.setText(filename)

    def _on_browse(self, edit: QLineEdit, title_id: str) -> None:
        current = edit.text().strip() or ""
        directory = QFileDialog.getExistingDirectory(
            self, self._browse_title(title_id), current
        )
        if directory:
            edit.setText(directory)

    def _on_accept(self) -> None:
        if self._scope == SETTINGS_SCOPE_CUSTOMER:
            external_power_host = self._external_power_host.text().strip()
            if external_power_host:
                try:
                    address = ipaddress.ip_address(external_power_host)
                except ValueError:
                    QMessageBox.warning(
                        self,
                        tr("Settings"),
                        tr("External power host must be a valid IPv4 address."),
                    )
                    return
                if address.version != 4:
                    QMessageBox.warning(
                        self,
                        tr("Settings"),
                        tr("External power host must be a valid IPv4 address."),
                    )
                    return
            self._settings.set("external_power.host", external_power_host)
        # 写入 settings（去掉首尾空格，空字符串清空配置）
        self._settings.set("paths.recording_dir", self._recording_edit.text().strip())
        self._settings.set("paths.log_dir", self._log_edit.text().strip())
        self._settings.set("paths.firmware_dir", self._firmware_edit.text().strip())
        if self._scope == SETTINGS_SCOPE_PRODUCTION:
            self._settings.set(
                "paths.production_report_dir", self._report_root_edit.text().strip()
            )
            self._settings.set(
                "production.report_branding",
                {
                    "company_name": self._report_company.text().strip(),
                    "logo_path": self._report_logo.text().strip(),
                    "header": self._report_header.text().strip(),
                    "footer": self._report_footer.text().strip(),
                    "tester_role": self._report_tester_role.text().strip(),
                    "reviewer_role": self._report_reviewer_role.text().strip(),
                },
            )
        # 地图 token
        self._settings.set("map.tianditu_token", self._tianditu_edit.text().strip())
        language = str(self._language_combo.currentData() or LANGUAGE_AUTO)
        self._settings.set("ui.language", language)
        # M11：更新设置
        self._settings.set("update.auto_check", bool(self._cb_auto_check.isChecked()))
        self._settings.set("update.check_interval_hours", int(self._spin_interval.value()))
        self._refresh_device_udp_port_editability()
        if not self._recovery_active and self._device_udp_port.isEnabled():
            self._settings.set(
                "device_udp.local_port", int(self._device_udp_port.value())
            )
        if self._recovery_active:
            self._settings.persist_preferences()
        else:
            try:
                self._settings.save()
            except SettingsSaveError as exc:
                QMessageBox.critical(self, tr("Settings"), str(exc))
                return
        manager = get_translation_manager()
        if manager is not None:
            manager.set_preference(language)
        self.accept()

    def done(self, result: int) -> None:
        super().done(result)

    def _on_reset_skip_version(self) -> None:
        self._settings.set("update.skip_version", "")
        self._settings.persist_preferences()
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
        self.setWindowTitle(tr("Settings"))
        self._device_udp_label.setText(tr("Device UDP local port:"))
        self._refresh_device_udp_port_editability()
        self._recovery_title.setText(tr("Device settings recovery required"))
        self._recovery_devices_label.setText(
            tr("Customer devices (one IPv4:port per line):")
        )
        self._recovery_confirmation_label.setText(
            tr("Type REBUILD_DEVICE_SETTINGS to rebuild:")
        )
        self._export_recovery_btn.setText(tr("Export recovery evidence..."))
        self._rebuild_settings_btn.setText(tr("Rebuild device settings"))
        self._production_configuration_label.setText(
            tr("Production configurations:")
        )
        self._btn_production_configurations.setText(
            tr("Manage production configurations...")
        )
        self._external_power_title.setText(tr("External power"))
        self._external_power_host_label.setText(tr("Power supply IPv4:"))
        self._external_power_host.setPlaceholderText(
            tr("Not configured; monitoring is disabled")
        )
        self._external_power_port.setToolTip(tr("Fixed read-only SCPI port"))
        self._external_power_port_label.setText(tr("Port:"))
        self._external_power_hint.setText(
            tr("Read-only monitoring: voltage, current and status; output is never controlled.")
        )
        self._production_report_title.setText(tr("Production report"))
        self._render_skipped_version()

    def _refresh_device_udp_port_editability(self) -> None:
        provider = self._device_udp_port_editable
        editable = True if provider is None else bool(provider())
        # Recovery rebuild is the sole device-settings transaction and must
        # remain editable while normal device paths are fail-closed.
        self._device_udp_port.setEnabled(bool(self._recovery_active or editable))
        self._device_udp_port.setToolTip(
            tr("Customer and production sessions share this UDP socket")
            if editable or self._recovery_active
            else tr("Disconnect all UDP device sessions before changing this port")
        )

    def _parse_recovery_devices(self) -> list[dict[str, object]]:
        devices: list[dict[str, object]] = []
        for line in self._recovery_devices.toPlainText().splitlines():
            value = line.strip()
            if not value:
                continue
            try:
                ip_text, port_text = value.rsplit(":", 1)
                port = int(port_text, 10)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    tr("Invalid customer device endpoint: {endpoint}", endpoint=value)
                ) from exc
            devices.append({"ip": ip_text.strip(), "port": port})
        return devices

    def _on_export_recovery(self) -> None:
        default_path = str(
            self._settings.config_directory / "settings_recovery_evidence.json"
        )
        filename, _selected = QFileDialog.getSaveFileName(
            self,
            tr("Export recovery evidence"),
            default_path,
            tr("JSON files (*.json);;All files (*)"),
        )
        if not filename:
            return
        try:
            self._settings.export_recovery_evidence(filename)
        except SettingsSaveError as exc:
            QMessageBox.critical(self, tr("Settings"), str(exc))
            return
        QMessageBox.information(
            self,
            tr("Settings"),
            tr("Recovery evidence exported."),
        )

    def _on_rebuild_settings(self) -> None:
        try:
            devices = self._parse_recovery_devices()
            current_active = self._settings.get("customer.active_endpoint")
            active = current_active if current_active in devices else (
                devices[0] if devices else None
            )
            self._settings.rebuild_device_settings(
                local_port=int(self._device_udp_port.value()),
                devices=devices,
                active_endpoint=active,
                confirmation=self._recovery_confirmation.text(),
            )
        except (SettingsSaveError, ValueError) as exc:
            QMessageBox.critical(self, tr("Settings"), str(exc))
            return
        self._recovery_active = False
        QMessageBox.information(
            self,
            tr("Settings"),
            tr("Device settings rebuilt. Restart the application before connecting devices."),
        )
        self.accept()

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

    def _on_open_production_configurations(self) -> None:
        from satellite_debug_tool.ui.production_configuration_dialog import (
            ProductionConfigurationDialog,
        )

        dialog = ProductionConfigurationDialog(self)
        dialog.exec()
