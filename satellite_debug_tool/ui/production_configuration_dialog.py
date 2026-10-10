"""Visual editor and import/export UI for production configuration profiles."""

from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path
from typing import Optional
from serial.tools import list_ports

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.production import (
    PowerSupplyProfile,
    PowerSupplyConfig,
    PowerSupplyDebugWorker,
    PowerDebugOperation,
    PowerIdentity,
    ProductTestTemplate,
    ProductionConfigurationError,
    ProductionConfigurationStore,
    ReferenceSensorProfile,
    REGISTERED_POWER_DRIVERS,
    StationProfile,
    fixture_model_preset,
    fixture_model_presets,
    FixtureProfileError,
    psw80_27_validation_policy,
)
from satellite_debug_tool.core.product import production_product_policies
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.i18n import register_translatable, tr, tr_source


_TEST_IDS = (
    "firmware_verification",
    "parameter_verification",
    "static_acquisition",
    "locked_rocking",
    "power_on_rocking",
    "locked_drive",
    "power_on_drive",
    "gnss",
    "imu_static",
    "external_ins",
    "whole_navigation",
)
_TEST_LABELS = {
    "firmware_verification": tr_source("Firmware verification"),
    "parameter_verification": tr_source("Parameter verification"),
    "static_acquisition": tr_source("Static acquisition"),
    "locked_rocking": tr_source("Locked rocking tracking"),
    "power_on_rocking": tr_source("Power-on while rocking"),
    "locked_drive": tr_source("Locked driving tracking"),
    "power_on_drive": tr_source("Power-on while driving"),
    "gnss": tr_source("GNSS performance summary"),
    "imu_static": tr_source("Raw IMU static performance summary"),
    "external_ins": tr_source("External INS performance summary"),
    "whole_navigation": tr_source("Whole-unit navigation summary"),
}

# Extraction markers for labels rendered through the shared form-row helper.
_FORM_LABEL_SOURCES = (
    tr_source("Template ID"),
    tr_source("Revision"),
    tr_source("Display name"),
    tr_source("Product model"),
    tr_source("Per-device voltage"),
    tr_source("Per-device current limit"),
    tr_source("Expected firmware (optional)"),
    tr_source("Observation duration (s)"),
    tr_source("Profile ID"),
    tr_source("Power model"),
    tr_source("IPv4 address"),
    tr_source("TCP port"),
    tr_source("Expected manufacturer"),
    tr_source("Expected model"),
    tr_source("Expected serial number"),
    tr_source("Rated voltage"),
    tr_source("Rated current"),
    tr_source("Rated power"),
    tr_source("Output settle timeout"),
    tr_source("Station profile ID"),
    tr_source("Power profile"),
    tr_source("Motion profile ID"),
    tr_source("Reference profile ID"),
    tr_source("Power branch count"),
    tr_source("Per-branch current"),
)


class ProductionConfigurationDialog(QDialog):
    """Edit the complete catalog; each save commits one atomic catalog file."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        store: Optional[ProductionConfigurationStore] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        super().__init__(parent)
        self._form_labels: list[tuple[QLabel, str]] = []
        self._translated_buttons: list[tuple[QPushButton, str]] = []
        self._store = store or ProductionConfigurationStore()
        self._settings = settings
        self._products: list[ProductTestTemplate] = []
        self._powers: list[PowerSupplyProfile] = []
        self._stations: list[StationProfile] = []
        self._references: list[ReferenceSensorProfile] = []
        self._motions = []
        self.changed = False
        self._power_test_worker: Optional[PowerSupplyDebugWorker] = None
        self.setWindowTitle(tr("Production configurations"))
        self.setMinimumSize(720, 610)
        self._build_ui()
        self._migrate_legacy_ms6222_port()
        self._reload_catalog()
        register_translatable(self)

    def _migrate_legacy_ms6222_port(self) -> None:
        settings = self._settings
        if settings is None:
            return
        if settings.get("config_migrations.production_fixture_profiles_v2", False) is True:
            return
        legacy_port = str(settings.get("production.ms6222_port", "") or "").strip()
        try:
            products, powers, references, stations = self._store.load_full_catalog()
            motions = self._store.fixture_profile_store.list_profiles()
            eligible = [
                item for item in motions if not item.calibration_id.startswith("PENDING-")
            ]
            if legacy_port and not references and len(eligible) == 1:
                motion = eligible[0]
                self._store.calibration_store.load(motion.calibration_id)
                reference = ReferenceSensorProfile(
                    profile_id="ms6222-main",
                    revision=1,
                    display_name="MS-6222",
                    driver_id="ms6222_serial_v1",
                    serial_port=legacy_port,
                    baud_rate=460800,
                    installation_id="ms6222-main-installation",
                    calibration_id=motion.calibration_id,
                )
                self._store.save_catalog(products, powers, stations, [reference])
            settings.set("config_migrations.production_fixture_profiles_v2", True)
            settings.persist_preferences()
        except (ProductionConfigurationError, FixtureProfileError, OSError, ValueError):
            return

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_product_tab(), tr("Product templates"))
        self._tabs.addTab(self._build_power_tab(), tr("Power profiles"))
        self._tabs.addTab(self._build_motion_tab(), tr("Motion platform profiles"))
        self._tabs.addTab(self._build_reference_tab(), tr("MS-6222 profiles"))
        self._tabs.addTab(self._build_station_tab(), tr("Station profiles"))
        root.addWidget(self._tabs, 1)

        actions = QHBoxLayout()
        self._import_button = QPushButton(tr("Import configuration package..."))
        self._import_button.clicked.connect(self._import_bundle)
        self._export_button = QPushButton(tr("Export configuration package..."))
        self._export_button.clicked.connect(self._export_bundle)
        actions.addWidget(self._import_button)
        actions.addWidget(self._export_button)
        actions.addStretch(1)
        root.addLayout(actions)

        self._dialog_buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self._dialog_buttons.button(QDialogButtonBox.Close).setText(tr("Close"))
        self._dialog_buttons.rejected.connect(self.accept)
        root.addWidget(self._dialog_buttons)

    def _build_product_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        self._product_select = QComboBox()
        self._product_select.currentIndexChanged.connect(self._load_product_form)
        root.addWidget(self._product_select)
        form = QFormLayout()
        self._product_id = QLineEdit()
        self._product_revision = QSpinBox()
        self._product_revision.setRange(1, 1_000_000)
        self._product_name = QLineEdit()
        self._product_type = QComboBox()
        for policy in production_product_policies():
            self._product_type.addItem(
                policy.product_identity, policy.production_recipe_product
            )
        self._product_voltage = self._double_spin(0.001, 80.0, " V")
        self._product_current = self._double_spin(0.001, 100.0, " A")
        self._firmware_value = QLineEdit()
        self._duration = QSpinBox()
        self._duration.setRange(1, 7 * 24 * 3600)
        self._duration.setValue(3600)
        self._add_form_row(form, "Template ID", self._product_id)
        self._add_form_row(form, "Revision", self._product_revision)
        self._add_form_row(form, "Display name", self._product_name)
        self._add_form_row(form, "Product model", self._product_type)
        self._add_form_row(form, "Per-device voltage", self._product_voltage)
        self._add_form_row(form, "Per-device current limit", self._product_current)
        self._add_form_row(form, "Expected firmware (optional)", self._firmware_value)
        self._add_form_row(form, "Observation duration (s)", self._duration)
        root.addLayout(form)
        self._test_checks: dict[str, QCheckBox] = {}
        tests = QGridLayout()
        for index, test_id in enumerate(_TEST_IDS):
            checkbox = QCheckBox(tr(_TEST_LABELS[test_id]))
            checkbox.setChecked(test_id in {"firmware_verification", "static_acquisition"})
            self._test_checks[test_id] = checkbox
            tests.addWidget(checkbox, index // 3, index % 3)
        self._enabled_tests_label = QLabel(tr("Enabled tests"))
        root.addWidget(self._enabled_tests_label)
        root.addLayout(tests)
        root.addStretch(1)
        actions = QHBoxLayout()
        new_button = QPushButton(tr("New"))
        self._translated_buttons.append((new_button, "New"))
        new_button.clicked.connect(self._new_product)
        save_button = QPushButton(tr("Save"))
        self._translated_buttons.append((save_button, "Save"))
        save_button.clicked.connect(self._save_product)
        delete_button = QPushButton(tr("Delete"))
        self._translated_buttons.append((delete_button, "Delete"))
        delete_button.clicked.connect(self._delete_product)
        actions.addWidget(new_button)
        actions.addWidget(save_button)
        actions.addWidget(delete_button)
        actions.addStretch(1)
        root.addLayout(actions)
        return page

    def _build_power_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        self._power_select = QComboBox()
        self._power_select.currentIndexChanged.connect(self._load_power_form)
        root.addWidget(self._power_select)
        form = QFormLayout()
        self._power_id = QLineEdit()
        self._power_revision = QSpinBox()
        self._power_revision.setRange(1, 1_000_000)
        self._power_name = QLineEdit()
        self._power_driver = QComboBox()
        for driver_id, display_name in REGISTERED_POWER_DRIVERS.items():
            self._power_driver.addItem(display_name, driver_id)
        self._power_host = QLineEdit()
        self._power_port = QSpinBox()
        self._power_port.setRange(1, 65535)
        self._power_port.setValue(2268)
        self._power_manufacturer = QLineEdit("GW-INSTEK")
        self._power_model = QLineEdit("PSW 80-27")
        self._power_serial = QLineEdit()
        self._rated_voltage = self._double_spin(0.001, 80.0, " V")
        self._rated_voltage.setValue(80.0)
        self._rated_current = self._double_spin(0.001, 27.0, " A")
        self._rated_current.setValue(27.0)
        self._rated_power = self._double_spin(0.001, 720.0, " W")
        self._rated_power.setValue(720.0)
        self._settle_timeout = self._double_spin(0.1, 60.0, " s")
        self._settle_timeout.setValue(3.0)
        for label, widget in (
            ("Profile ID", self._power_id),
            ("Revision", self._power_revision),
            ("Display name", self._power_name),
            ("Power model", self._power_driver),
            ("IPv4 address", self._power_host),
            ("TCP port", self._power_port),
            ("Expected manufacturer", self._power_manufacturer),
            ("Expected model", self._power_model),
            ("Expected serial number", self._power_serial),
            ("Rated voltage", self._rated_voltage),
            ("Rated current", self._rated_current),
            ("Rated power", self._rated_power),
            ("Output settle timeout", self._settle_timeout),
        ):
            self._add_form_row(form, label, widget)
        root.addLayout(form)
        power_test_row = QHBoxLayout()
        self._test_power_button = QPushButton(tr("Connect and identify"))
        self._test_power_button.clicked.connect(self._test_power_identity)
        self._power_test_status = QLabel("-")
        power_test_row.addWidget(self._test_power_button)
        power_test_row.addWidget(self._power_test_status, 1)
        root.addLayout(power_test_row)
        root.addStretch(1)
        actions = QHBoxLayout()
        new_button = QPushButton(tr("New"))
        self._translated_buttons.append((new_button, "New"))
        new_button.clicked.connect(self._new_power)
        save_button = QPushButton(tr("Save"))
        self._translated_buttons.append((save_button, "Save"))
        save_button.clicked.connect(self._save_power)
        delete_button = QPushButton(tr("Delete"))
        self._translated_buttons.append((delete_button, "Delete"))
        delete_button.clicked.connect(self._delete_power)
        actions.addWidget(new_button)
        actions.addWidget(save_button)
        actions.addWidget(delete_button)
        actions.addStretch(1)
        root.addLayout(actions)
        return page

    def _build_station_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        self._station_select = QComboBox()
        self._station_select.currentIndexChanged.connect(self._load_station_form)
        root.addWidget(self._station_select)
        form = QFormLayout()
        self._station_id = QLineEdit()
        self._station_revision = QSpinBox()
        self._station_revision.setRange(1, 1_000_000)
        self._station_name = QLineEdit()
        self._station_power = QComboBox()
        self._station_motion = QComboBox()
        self._station_reference = QComboBox()
        self._branch_count = QSpinBox()
        self._branch_count.setRange(1, 4)
        self._branch_current = self._double_spin(0.001, 100.0, " A")
        self._report_company = QLineEdit()
        self._report_logo = QLineEdit()
        self._report_logo.setReadOnly(True)
        self._report_logo_button = QPushButton(tr("Browse..."))
        self._report_logo_button.clicked.connect(self._choose_station_logo)
        self._report_header = QLineEdit()
        self._report_footer = QLineEdit()
        self._report_tester_role = QLineEdit()
        self._report_reviewer_role = QLineEdit()
        self._station_logo_base64 = ""
        for label, widget in (
            ("Station profile ID", self._station_id),
            ("Revision", self._station_revision),
            ("Display name", self._station_name),
            ("Power profile", self._station_power),
            ("Motion profile ID", self._station_motion),
            ("Reference profile ID", self._station_reference),
            ("Power branch count", self._branch_count),
            ("Per-branch current", self._branch_current),
            ("Report company", self._report_company),
            ("Report header", self._report_header),
            ("Report footer", self._report_footer),
            ("Tester role", self._report_tester_role),
            ("Reviewer role", self._report_reviewer_role),
        ):
            self._add_form_row(form, label, widget)
        logo_row = QHBoxLayout()
        logo_row.addWidget(self._report_logo, 1)
        logo_row.addWidget(self._report_logo_button)
        logo_widget = QWidget()
        logo_widget.setLayout(logo_row)
        self._add_form_row(form, "Report logo", logo_widget)
        root.addLayout(form)
        root.addStretch(1)
        actions = QHBoxLayout()
        new_button = QPushButton(tr("New"))
        self._translated_buttons.append((new_button, "New"))
        new_button.clicked.connect(self._new_station)
        save_button = QPushButton(tr("Save"))
        self._translated_buttons.append((save_button, "Save"))
        save_button.clicked.connect(self._save_station)
        delete_button = QPushButton(tr("Delete"))
        self._translated_buttons.append((delete_button, "Delete"))
        delete_button.clicked.connect(self._delete_station)
        actions.addWidget(new_button)
        actions.addWidget(save_button)
        actions.addWidget(delete_button)
        actions.addStretch(1)
        root.addLayout(actions)
        return page

    def _build_motion_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        self._motion_select = QComboBox()
        self._motion_select.currentIndexChanged.connect(self._load_motion_form)
        root.addWidget(self._motion_select)
        form = QFormLayout()
        self._motion_id = QLineEdit()
        self._motion_revision = QSpinBox()
        self._motion_revision.setRange(1, 1_000_000)
        self._motion_model = QComboBox()
        for preset in fixture_model_presets():
            self._motion_model.addItem(preset.display_name, preset.model_id)
        self._motion_local_host = QLineEdit()
        self._motion_feedback_port = QSpinBox()
        self._motion_feedback_port.setRange(1, 65535)
        self._motion_controller_host = QLineEdit()
        self._motion_command_port = QSpinBox()
        self._motion_command_port.setRange(1, 65535)
        self._motion_calibration = QComboBox()
        for label, widget in (
            ("Profile ID", self._motion_id),
            ("Revision", self._motion_revision),
            ("Motion platform model", self._motion_model),
            ("Local feedback IPv4", self._motion_local_host),
            ("Feedback UDP port", self._motion_feedback_port),
            ("Controller IPv4", self._motion_controller_host),
            ("Command UDP port", self._motion_command_port),
            ("Coordinate calibration", self._motion_calibration),
        ):
            self._add_form_row(form, label, widget)
        root.addLayout(form)
        root.addStretch(1)
        actions = QHBoxLayout()
        new_button = QPushButton(tr("New"))
        self._translated_buttons.append((new_button, "New"))
        new_button.clicked.connect(self._new_motion)
        save_button = QPushButton(tr("Save"))
        self._translated_buttons.append((save_button, "Save"))
        save_button.clicked.connect(self._save_motion)
        actions.addWidget(new_button)
        actions.addWidget(save_button)
        actions.addStretch(1)
        root.addLayout(actions)
        return page

    def _build_reference_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        self._reference_select = QComboBox()
        self._reference_select.currentIndexChanged.connect(self._load_reference_form)
        root.addWidget(self._reference_select)
        form = QFormLayout()
        self._reference_id = QLineEdit()
        self._reference_revision = QSpinBox()
        self._reference_revision.setRange(1, 1_000_000)
        self._reference_name = QLineEdit()
        self._reference_port = QComboBox()
        self._reference_port.setEditable(True)
        self._reference_baud = QSpinBox()
        self._reference_baud.setRange(460800, 460800)
        self._reference_installation = QLineEdit()
        self._reference_serial = QLineEdit()
        self._reference_calibration = QComboBox()
        self._reference_freshness = self._double_spin(0.05, 60.0, " s")
        self._reference_std = self._double_spin(0.001, 30.0, " deg")
        self._reference_stability = self._double_spin(0.1, 60.0, " s")
        for port in list_ports.comports():
            self._reference_port.addItem(port.device, port.device)
        for label, widget in (
            ("Profile ID", self._reference_id),
            ("Revision", self._reference_revision),
            ("Display name", self._reference_name),
            ("Serial port", self._reference_port),
            ("Baud rate", self._reference_baud),
            ("Installation ID", self._reference_installation),
            ("Expected serial number", self._reference_serial),
            ("Coordinate calibration", self._reference_calibration),
            ("Freshness timeout", self._reference_freshness),
            ("Maximum attitude standard deviation", self._reference_std),
            ("Stability window", self._reference_stability),
        ):
            self._add_form_row(form, label, widget)
        root.addLayout(form)
        root.addStretch(1)
        actions = QHBoxLayout()
        new_button = QPushButton(tr("New"))
        self._translated_buttons.append((new_button, "New"))
        new_button.clicked.connect(self._new_reference)
        save_button = QPushButton(tr("Save"))
        self._translated_buttons.append((save_button, "Save"))
        save_button.clicked.connect(self._save_reference)
        delete_button = QPushButton(tr("Delete"))
        self._translated_buttons.append((delete_button, "Delete"))
        delete_button.clicked.connect(self._delete_reference)
        actions.addWidget(new_button)
        actions.addWidget(save_button)
        actions.addWidget(delete_button)
        actions.addStretch(1)
        root.addLayout(actions)
        return page

    @staticmethod
    def _double_spin(minimum: float, maximum: float, suffix: str) -> QDoubleSpinBox:
        editor = QDoubleSpinBox()
        editor.setDecimals(3)
        editor.setRange(minimum, maximum)
        editor.setSuffix(suffix)
        return editor

    def _add_form_row(self, form: QFormLayout, source: str, widget: QWidget) -> None:
        label = QLabel(tr(source))
        self._form_labels.append((label, source))
        form.addRow(label, widget)

    def retranslate_ui(self) -> None:
        self.setWindowTitle(tr("Production configurations"))
        self._tabs.setTabText(0, tr("Product templates"))
        self._tabs.setTabText(1, tr("Power profiles"))
        self._tabs.setTabText(2, tr("Motion platform profiles"))
        self._tabs.setTabText(3, tr("MS-6222 profiles"))
        self._tabs.setTabText(4, tr("Station profiles"))
        self._import_button.setText(tr("Import configuration package..."))
        self._export_button.setText(tr("Export configuration package..."))
        self._dialog_buttons.button(QDialogButtonBox.Close).setText(tr("Close"))
        self._enabled_tests_label.setText(tr("Enabled tests"))
        self._test_power_button.setText(tr("Connect and identify"))
        for label, source in self._form_labels:
            label.setText(tr(source))
        for button, source in self._translated_buttons:
            button.setText(tr(source))
        for test_id, checkbox in self._test_checks.items():
            checkbox.setText(tr(_TEST_LABELS[test_id]))

    def _reload_catalog(self) -> None:
        try:
            products, powers, references, stations = self._store.load_full_catalog()
        except ProductionConfigurationError as exc:
            QMessageBox.critical(self, tr("Production configurations"), str(exc))
            return
        self._products = list(products)
        self._powers = list(powers)
        self._references = list(references)
        self._stations = list(stations)
        self._motions = list(self._store.fixture_profile_store.list_profiles())
        self._refresh_selectors()

    def _refresh_selectors(self) -> None:
        selections = (
            (self._product_select, self._products, "template_id", "display_name"),
            (self._power_select, self._powers, "profile_id", "display_name"),
            (self._motion_select, self._motions, "profile_id", "profile_id"),
            (self._reference_select, self._references, "profile_id", "display_name"),
            (self._station_select, self._stations, "station_profile_id", "display_name"),
        )
        for combo, values, id_name, display_name in selections:
            selected = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for value in values:
                combo.addItem(
                    f"{getattr(value, display_name)} (R{value.revision})",
                    getattr(value, id_name),
                )
            index = combo.findData(selected)
            combo.setCurrentIndex(index if index >= 0 else (0 if combo.count() else -1))
            combo.blockSignals(False)
        selected_power = self._station_power.currentData()
        self._station_power.clear()
        for power in self._powers:
            self._station_power.addItem(power.display_name, power.profile_id)
        index = self._station_power.findData(selected_power)
        if index >= 0:
            self._station_power.setCurrentIndex(index)
        for combo, values, id_name, label_name in (
            (self._station_motion, self._motions, "profile_id", "profile_id"),
            (self._station_reference, self._references, "profile_id", "display_name"),
        ):
            selected = combo.currentData()
            combo.clear()
            for value in values:
                combo.addItem(getattr(value, label_name), getattr(value, id_name))
            index = combo.findData(selected)
            if index >= 0:
                combo.setCurrentIndex(index)
        self._load_product_form()
        self._load_power_form()
        self._load_motion_form()
        self._load_reference_form()
        self._load_station_form()

    def _persist(self) -> bool:
        try:
            self._store.save_catalog(
                self._products,
                self._powers,
                self._stations,
                self._references,
            )
        except ProductionConfigurationError as exc:
            QMessageBox.critical(self, tr("Production configurations"), str(exc))
            return False
        self.changed = True
        self._refresh_selectors()
        return True

    def _load_product_form(self, *_args) -> None:
        item = next(
            (value for value in self._products if value.template_id == self._product_select.currentData()),
            None,
        )
        if item is None:
            return
        self._product_id.setText(item.template_id)
        self._product_revision.setValue(item.revision)
        self._product_name.setText(item.display_name)
        self._product_type.setCurrentIndex(max(0, self._product_type.findData(item.product)))
        self._product_voltage.setValue(item.supply_voltage_v)
        self._product_current.setValue(item.per_device_current_a)
        firmware = item.expected.get("main_firmware", {})
        self._firmware_value.setText(str(firmware.get("value", "")))
        self._duration.setValue(int(item.duration_policy["minimum_effective_observation_s"]))
        for test_id, checkbox in self._test_checks.items():
            checkbox.setChecked(bool(item.tests.get(test_id, {}).get("enabled", False)))

    def _new_product(self) -> None:
        self._product_select.setCurrentIndex(-1)
        self._product_id.clear()
        self._product_revision.setValue(1)
        self._product_name.clear()
        self._product_type.setCurrentIndex(0)
        self._product_voltage.setValue(12.0)
        self._product_current.setValue(1.0)
        self._firmware_value.clear()
        self._duration.setValue(3600)

    def _save_product(self) -> None:
        firmware = self._firmware_value.text().strip()
        item = ProductTestTemplate(
            template_id=self._product_id.text().strip(),
            revision=self._product_revision.value(),
            display_name=self._product_name.text().strip(),
            product=str(self._product_type.currentData()),
            supply_voltage_v=self._product_voltage.value(),
            per_device_current_a=self._product_current.value(),
            expected={
                "main_firmware": {
                    "match": "exact" if firmware else "optional",
                    "value": firmware,
                },
                "parameters": {},
            },
            tests={
                test_id: {"enabled": checkbox.isChecked()}
                for test_id, checkbox in self._test_checks.items()
            },
            duration_policy={
                "minimum_effective_observation_s": self._duration.value(),
                "convergence_is_outside_observation": True,
                "report_first_last_window_s": min(300, self._duration.value()),
            },
        )
        self._replace(self._products, item, "template_id")

    def _delete_product(self) -> None:
        selected = self._product_select.currentData()
        self._products = [item for item in self._products if item.template_id != selected]
        self._persist()

    def _load_power_form(self, *_args) -> None:
        item = next(
            (value for value in self._powers if value.profile_id == self._power_select.currentData()),
            None,
        )
        if item is None:
            return
        self._power_id.setText(item.profile_id)
        self._power_revision.setValue(item.revision)
        self._power_name.setText(item.display_name)
        self._power_driver.setCurrentIndex(max(0, self._power_driver.findData(item.driver_id)))
        self._power_host.setText(item.host)
        self._power_port.setValue(item.port)
        self._power_manufacturer.setText(item.manufacturer)
        self._power_model.setText(item.model)
        self._power_serial.setText(item.serial_number)
        self._rated_voltage.setValue(item.rated_voltage_v)
        self._rated_current.setValue(item.rated_current_a)
        self._rated_power.setValue(item.rated_power_w)
        self._settle_timeout.setValue(item.output_settle_timeout_s)

    def _new_power(self) -> None:
        self._power_select.setCurrentIndex(-1)
        self._power_id.clear()
        self._power_revision.setValue(1)
        self._power_name.clear()
        self._power_driver.setCurrentIndex(0)
        self._power_host.clear()
        self._power_port.setValue(2268)
        self._power_manufacturer.setText("GW-INSTEK")
        self._power_model.setText("PSW 80-27")
        self._power_serial.clear()

    def _save_power(self) -> None:
        self._replace(self._powers, self._power_from_form(), "profile_id")

    def _power_from_form(self) -> PowerSupplyProfile:
        return PowerSupplyProfile(
            profile_id=self._power_id.text().strip(),
            revision=self._power_revision.value(),
            display_name=self._power_name.text().strip(),
            driver_id=str(self._power_driver.currentData()),
            host=self._power_host.text().strip(),
            port=self._power_port.value(),
            manufacturer=self._power_manufacturer.text().strip(),
            model=self._power_model.text().strip(),
            serial_number=self._power_serial.text().strip(),
            rated_voltage_v=self._rated_voltage.value(),
            rated_current_a=self._rated_current.value(),
            rated_power_w=self._rated_power.value(),
            output_settle_timeout_s=self._settle_timeout.value(),
        )

    def _test_power_identity(self) -> None:
        if self._power_test_worker is not None:
            return
        try:
            profile = self._power_from_form()
            profile.validate()
            policy = psw80_27_validation_policy(1.0, 1.0)
            config = PowerSupplyConfig(
                host=profile.host,
                port=profile.port,
                expected_manufacturer=profile.manufacturer,
                expected_model=profile.model,
                expected_serial=profile.serial_number,
                voltage_set_v=1.0,
                current_set_a=1.0,
                voltage_setpoint_tolerance_v=policy.voltage_setpoint_tolerance_v,
                current_setpoint_tolerance_a=policy.current_setpoint_tolerance_a,
                output_voltage_min_v=policy.output_voltage_min_v,
                output_voltage_max_v=policy.output_voltage_max_v,
                off_voltage_max_v=policy.off_voltage_max_v,
                output_settle_timeout_s=profile.output_settle_timeout_s,
            )
        except ProductionConfigurationError as exc:
            QMessageBox.critical(self, tr("Production configurations"), str(exc))
            return
        worker = PowerSupplyDebugWorker(config, parent=self)
        worker.operation_succeeded.connect(self._on_power_test_succeeded)
        worker.operation_failed.connect(self._on_power_test_failed)
        worker.finished.connect(self._on_power_test_finished)
        self._power_test_worker = worker
        self._test_power_button.setEnabled(False)
        self._power_test_status.setText(tr("Connecting..."))
        worker.start()
        worker.submit(PowerDebugOperation.CONNECT)

    def _on_power_test_succeeded(
        self,
        operation: str,
        payload,
        _state_evidence,
        _records,
    ) -> None:
        if operation != PowerDebugOperation.CONNECT.value:
            return
        if isinstance(payload, PowerIdentity):
            self._power_test_status.setText(
                f"{payload.manufacturer} | {payload.model} | "
                f"{payload.serial_number} | FW {payload.firmware}"
            )
        worker = self._power_test_worker
        if worker is not None:
            worker.stop()

    def _on_power_test_failed(
        self,
        _operation: str,
        details: str,
        _state_evidence,
        _records,
    ) -> None:
        self._power_test_status.setText(details)
        worker = self._power_test_worker
        if worker is not None:
            worker.stop()

    def _on_power_test_finished(self) -> None:
        worker = self._power_test_worker
        self._power_test_worker = None
        self._test_power_button.setEnabled(True)
        if worker is not None:
            worker.deleteLater()

    def _delete_power(self) -> None:
        selected = self._power_select.currentData()
        if any(item.power_profile_id == selected for item in self._stations):
            QMessageBox.critical(
                self,
                tr("Production configurations"),
                tr("The power profile is referenced by a station profile."),
            )
            return
        self._powers = [item for item in self._powers if item.profile_id != selected]
        self._persist()

    def _calibrations(self):
        values = []
        seen = set()
        for motion in self._motions:
            for calibration in self._store.calibration_store.list_for_profile(
                motion.profile_id
            ):
                if calibration.confirmed and calibration.calibration_id not in seen:
                    seen.add(calibration.calibration_id)
                    values.append(calibration)
        return values

    def _refresh_calibration_selectors(self, selected: str = "") -> None:
        calibrations = self._calibrations()
        for combo in (self._motion_calibration, self._reference_calibration):
            current = selected or str(combo.currentData() or "")
            combo.clear()
            for calibration in calibrations:
                combo.addItem(calibration.calibration_id, calibration.calibration_id)
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else (0 if combo.count() else -1))

    def _load_motion_form(self, *_args) -> None:
        item = next(
            (value for value in self._motions if value.profile_id == self._motion_select.currentData()),
            None,
        )
        if item is None:
            return
        self._motion_id.setText(item.profile_id)
        self._motion_revision.setValue(item.revision)
        self._motion_model.setCurrentIndex(max(0, self._motion_model.findData(item.model_id)))
        self._motion_local_host.setText(item.local_ipv4)
        self._motion_feedback_port.setValue(item.feedback_port)
        self._motion_controller_host.setText(item.controller_ipv4)
        self._motion_command_port.setValue(item.command_port)
        self._refresh_calibration_selectors(item.calibration_id)

    def _new_motion(self) -> None:
        preset = fixture_model_preset(str(self._motion_model.currentData()))
        item = preset.create_profile()
        self._motion_select.setCurrentIndex(-1)
        self._motion_id.setText(item.profile_id)
        self._motion_revision.setValue(1)
        self._motion_local_host.setText(item.local_ipv4)
        self._motion_feedback_port.setValue(item.feedback_port)
        self._motion_controller_host.setText(item.controller_ipv4)
        self._motion_command_port.setValue(item.command_port)
        self._refresh_calibration_selectors()

    def _save_motion(self) -> None:
        try:
            preset = fixture_model_preset(str(self._motion_model.currentData()))
            current = next(
                (item for item in self._motions if item.profile_id == self._motion_id.text().strip()),
                None,
            )
            base = preset.create_profile(self._motion_id.text().strip())
            item = replace(
                base,
                revision=self._motion_revision.value(),
                local_ipv4=self._motion_local_host.text().strip(),
                feedback_port=self._motion_feedback_port.value(),
                controller_ipv4=self._motion_controller_host.text().strip(),
                command_port=self._motion_command_port.value(),
                calibration_id=str(self._motion_calibration.currentData() or base.calibration_id),
            )
            item.validate()
            self._store.fixture_profile_store.save(
                item,
                expected_revision=(current.revision if current is not None else None),
            )
        except (FixtureProfileError, ProductionConfigurationError) as exc:
            QMessageBox.critical(self, tr("Production configurations"), str(exc))
            return
        self.changed = True
        self._reload_catalog()

    def _load_reference_form(self, *_args) -> None:
        item = next(
            (value for value in self._references if value.profile_id == self._reference_select.currentData()),
            None,
        )
        if item is None:
            return
        self._reference_id.setText(item.profile_id)
        self._reference_revision.setValue(item.revision)
        self._reference_name.setText(item.display_name)
        index = self._reference_port.findData(item.serial_port)
        if index < 0:
            self._reference_port.addItem(item.serial_port, item.serial_port)
            index = self._reference_port.findData(item.serial_port)
        self._reference_port.setCurrentIndex(index)
        self._reference_baud.setValue(item.baud_rate)
        self._reference_installation.setText(item.installation_id)
        self._reference_serial.setText(item.serial_number)
        self._refresh_calibration_selectors(item.calibration_id)
        self._reference_freshness.setValue(item.freshness_timeout_s)
        self._reference_std.setValue(item.max_attitude_std_deg)
        self._reference_stability.setValue(item.stability_window_s)

    def _new_reference(self) -> None:
        self._reference_select.setCurrentIndex(-1)
        self._reference_id.setText("ms6222-main")
        self._reference_revision.setValue(1)
        self._reference_name.setText("MS-6222")
        self._reference_installation.setText("ms6222-main-installation")
        self._reference_serial.clear()
        self._reference_freshness.setValue(1.0)
        self._reference_std.setValue(1.0)
        self._reference_stability.setValue(2.0)
        self._refresh_calibration_selectors()

    def _save_reference(self) -> None:
        item = ReferenceSensorProfile(
            profile_id=self._reference_id.text().strip(),
            revision=self._reference_revision.value(),
            display_name=self._reference_name.text().strip(),
            driver_id="ms6222_serial_v1",
            serial_port=self._reference_port.currentText().strip(),
            baud_rate=self._reference_baud.value(),
            installation_id=self._reference_installation.text().strip(),
            serial_number=self._reference_serial.text().strip(),
            calibration_id=str(self._reference_calibration.currentData() or ""),
            freshness_timeout_s=self._reference_freshness.value(),
            max_attitude_std_deg=self._reference_std.value(),
            stability_window_s=self._reference_stability.value(),
        )
        self._replace(self._references, item, "profile_id")

    def _delete_reference(self) -> None:
        selected = self._reference_select.currentData()
        if any(item.reference_profile_id == selected for item in self._stations):
            QMessageBox.critical(
                self,
                tr("Production configurations"),
                tr("The reference profile is referenced by a station profile."),
            )
            return
        self._references = [item for item in self._references if item.profile_id != selected]
        self._persist()

    def _load_station_form(self, *_args) -> None:
        item = next(
            (
                value
                for value in self._stations
                if value.station_profile_id == self._station_select.currentData()
            ),
            None,
        )
        if item is None:
            return
        self._station_id.setText(item.station_profile_id)
        self._station_revision.setValue(item.revision)
        self._station_name.setText(item.display_name)
        self._station_power.setCurrentIndex(
            max(0, self._station_power.findData(item.power_profile_id))
        )
        self._station_motion.setCurrentIndex(
            max(0, self._station_motion.findData(item.motion_profile_id))
        )
        self._station_reference.setCurrentIndex(
            max(0, self._station_reference.findData(item.reference_profile_id))
        )
        self._branch_count.setValue(item.branch_count)
        self._branch_current.setValue(item.branch_current_a)
        branding = item.report_branding
        self._report_company.setText(branding.get("company_name", ""))
        self._report_logo.setText(branding.get("logo_filename", ""))
        self._station_logo_base64 = branding.get("logo_base64", "")
        self._report_header.setText(branding.get("header", ""))
        self._report_footer.setText(branding.get("footer", ""))
        self._report_tester_role.setText(branding.get("tester_role", ""))
        self._report_reviewer_role.setText(branding.get("reviewer_role", ""))

    def _new_station(self) -> None:
        self._station_select.setCurrentIndex(-1)
        self._station_id.clear()
        self._station_revision.setValue(1)
        self._station_name.clear()
        self._station_power.setCurrentIndex(0)
        self._station_motion.setCurrentIndex(0)
        self._station_reference.setCurrentIndex(0)
        self._branch_count.setValue(1)
        self._branch_current.setValue(1.0)
        self._report_company.clear()
        self._report_logo.clear()
        self._station_logo_base64 = ""
        self._report_header.clear()
        self._report_footer.clear()
        self._report_tester_role.clear()
        self._report_reviewer_role.clear()

    def _choose_station_logo(self) -> None:
        filename, _selected = QFileDialog.getOpenFileName(
            self,
            tr("Select report logo"),
            "",
            tr("Image files (*.png *.jpg *.jpeg);;All files (*)"),
        )
        if not filename:
            return
        path = Path(filename)
        try:
            data = path.read_bytes()
        except OSError as exc:
            QMessageBox.critical(self, tr("Production configurations"), str(exc))
            return
        if len(data) > 3 * 1024 * 1024:
            QMessageBox.critical(
                self,
                tr("Production configurations"),
                tr("Report logo must not exceed 3 MiB."),
            )
            return
        self._station_logo_base64 = base64.b64encode(data).decode("ascii")
        self._report_logo.setText(path.name)

    def _save_station(self) -> None:
        item = StationProfile(
            station_profile_id=self._station_id.text().strip(),
            revision=self._station_revision.value(),
            display_name=self._station_name.text().strip(),
            power_profile_id=str(self._station_power.currentData() or ""),
            motion_profile_id=str(self._station_motion.currentData() or ""),
            reference_profile_id=str(self._station_reference.currentData() or ""),
            branch_count=self._branch_count.value(),
            branch_current_a=self._branch_current.value(),
            report_branding={
                "company_name": self._report_company.text().strip(),
                "logo_base64": self._station_logo_base64,
                "logo_filename": self._report_logo.text().strip(),
                "header": self._report_header.text().strip(),
                "footer": self._report_footer.text().strip(),
                "tester_role": self._report_tester_role.text().strip(),
                "reviewer_role": self._report_reviewer_role.text().strip(),
            },
        )
        self._replace(self._stations, item, "station_profile_id")

    def _delete_station(self) -> None:
        selected = self._station_select.currentData()
        self._stations = [
            item for item in self._stations if item.station_profile_id != selected
        ]
        self._persist()

    def _replace(self, values: list, item, id_attribute: str) -> None:
        try:
            item.validate()
            item_id = getattr(item, id_attribute)
            current = next(
                (value for value in values if getattr(value, id_attribute) == item_id),
                None,
            )
            if current is not None and item.sha256 != current.sha256:
                if item.revision <= current.revision:
                    raise ProductionConfigurationError(
                        "changed configuration requires a higher revision"
                    )
            updated = [
                value for value in values if getattr(value, id_attribute) != item_id
            ]
            updated.append(item)
            values[:] = sorted(updated, key=lambda value: getattr(value, id_attribute))
            self._persist()
        except ProductionConfigurationError as exc:
            QMessageBox.critical(self, tr("Production configurations"), str(exc))

    def _import_bundle(self) -> None:
        filename, _selected = QFileDialog.getOpenFileName(
            self,
            tr("Import production configuration package"),
            "",
            tr("JSON files (*.json);;All files (*)"),
        )
        if not filename:
            return
        try:
            self._store.import_bundle(filename)
        except ProductionConfigurationError as exc:
            QMessageBox.critical(self, tr("Production configurations"), str(exc))
            return
        self.changed = True
        self._reload_catalog()

    def _export_bundle(self) -> None:
        filename, _selected = QFileDialog.getSaveFileName(
            self,
            tr("Export production configuration package"),
            str(Path.home() / "production-configuration.json"),
            tr("JSON files (*.json);;All files (*)"),
        )
        if not filename:
            return
        try:
            self._store.export_bundle(
                filename,
                station_profile_id=str(self._station_select.currentData() or ""),
            )
        except ProductionConfigurationError as exc:
            QMessageBox.critical(self, tr("Production configurations"), str(exc))

    def done(self, result: int) -> None:
        worker = self._power_test_worker
        if worker is not None:
            worker.stop()
            worker.wait(10000)
            self._power_test_worker = None
        super().done(result)


__all__ = ["ProductionConfigurationDialog"]
