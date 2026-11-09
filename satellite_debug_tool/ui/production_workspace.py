"""Batch-production workspace for registered product recipes."""

from __future__ import annotations

from datetime import datetime
from dataclasses import asdict
import json
import math
from pathlib import Path
import re
import time
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QMessageBox,
    QSpinBox,
    QScrollArea,
    QFrame,
    QToolButton,
    QStackedWidget,
    QSizePolicy,
    QDialogButtonBox,
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QCheckBox,
    QDoubleSpinBox,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.production.abort_coordinator import AbortState, ProductionAbortCoordinator, ProductionCleanupWorker
from satellite_debug_tool.core.production.start_preparation import PreparationState, ProductionStartPreparation
from satellite_debug_tool.core.production.execution import ProductionExecution
from satellite_debug_tool.core.production.execution_hardware import ProductionExecutionHardware
from satellite_debug_tool.core.production.run_coordinator import ProductionRunState
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.comm import UdpEndpointBroker
from satellite_debug_tool.core.product import production_product_policy
from satellite_debug_tool.core.session import EndpointSessionDirectory, SessionRegistry
from satellite_debug_tool.core.production import (
    AttemptStatus,
    BatchCoordinator,
    BatchStatus,
    DeviceSession,
    DeviceSessionState,
    ExternalInsApplicability,
    FixtureControlLease,
    FixtureRuntime,
    FixtureRuntimeFact,
    FixtureRuntimeState,
    Ms6222ControlLease,
    FleetConfigurationError,
    FleetController,
    GwInstekPswAdapter,
    PowerSupplyConfig,
    ProductionPowerWorker,
    ProductionRecipe,
    ProductionConfigurationError,
    ProductionConfigurationStore,
    ResolvedProductionConfiguration,
    ProductionResultStore,
    ProductionReportService,
    ReportBranding,
    RecipeValidationError,
    ResultStoreError,
    evaluate_external_ins,
    psw80_27_validation_policy,
)
from satellite_debug_tool.core.production.storage import resolve_production_storage
from satellite_debug_tool.i18n import register_translatable, tr, tr_source
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.core.production.presentation import (
    chart_slots, enabled_production_tests, fixture_requirements, tracking_reason_text,
)
from satellite_debug_tool.ui.production_progress_widgets import (
    FixturePresentation, ProductionFixtureStrip, ProductionProgressPanel, StepPresentation,
)
from satellite_debug_tool.ui.production_snr_widget import ProductionSnrPanel
from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace
from satellite_debug_tool.ui.ms6222_debug_workspace import Ms6222DebugWorkspace
from satellite_debug_tool.ui.lazy_view_host import LazyViewHost
from satellite_debug_tool.ui.power_supply_debug_workspace import (
    PowerSupplyDebugWorkspace,
)
from satellite_debug_tool.ui.report_review_dialog import ReportReviewDialog
from satellite_debug_tool.ui.view_lifecycle import activate_view, deactivate_view


class ProductionWorkspace(QWidget):
    """Batch console presenting the frozen participants and domain progress."""

    status_message = Signal(str, int)
    batch_created = Signal(str)

    _TEST_DEFS = (
        ("firmware_verification", tr_source("Firmware verification")),
        ("parameter_verification", tr_source("Parameter verification")),
        ("static_acquisition", tr_source("Static acquisition")),
        ("locked_rocking", tr_source("Locked rocking tracking")),
        ("power_on_rocking", tr_source("Power-on while rocking")),
        ("locked_drive", tr_source("Locked driving tracking")),
        ("power_on_drive", tr_source("Power-on while driving")),
        ("gnss", tr_source("GNSS performance summary")),
        ("imu_static", tr_source("Raw IMU static performance summary")),
        ("external_ins", tr_source("External INS performance summary")),
        ("whole_navigation", tr_source("Whole-unit navigation summary")),
    )
    _FIXTURE_DEFS = (
        ("power", tr_source("LAN power supply")),
        ("motion", tr_source("Motion platform")),
        ("reference", tr_source("MS-6222 reference")),
        ("vehicle", tr_source("Vehicle operator gate")),
    )

    def __init__(
        self,
        settings: Settings,
        parent: Optional[QWidget] = None,
        *,
        session_registry: Optional[SessionRegistry] = None,
        broker: Optional[UdpEndpointBroker] = None,
        session_directory: Optional[EndpointSessionDirectory] = None,
        configuration_store: Optional[ProductionConfigurationStore] = None,
        fixture_runtime: Optional[FixtureRuntime] = None,
        production_power_adapter_factory: Callable[
            [PowerSupplyConfig], GwInstekPswAdapter
        ] = GwInstekPswAdapter,
        open_tcp_shell: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._session_registry = session_registry
        self._broker = broker
        self._session_directory = session_directory or session_registry
        self._fixture_runtime = fixture_runtime or FixtureRuntime(self)
        self._configuration_store = configuration_store or ProductionConfigurationStore(
            fixture_profile_store=self._fixture_runtime.profile_store,
            calibration_store=self._fixture_runtime.calibration_store,
        )
        self._resolved_configuration: Optional[ResolvedProductionConfiguration] = None
        self._production_power_adapter_factory = production_power_adapter_factory
        self._open_tcp_shell = open_tcp_shell
        self._production_power_worker: Optional[ProductionPowerWorker] = None
        self._power_record_count = 0
        self._abort_coordinator = ProductionAbortCoordinator(self)
        self._abort_coordinator.changed.connect(self._render_abort)
        self._abort_coordinator.settled.connect(self._on_abort_settled)
        self._abort_archive = None
        self._archive_worker: Optional[ProductionCleanupWorker] = None
        self._complete_source = ""
        self._execution = None
        self._execution_hardware = None
        self._preparation = ProductionStartPreparation(self._evaluate_start_gate, parent=self)
        self._preparation.admitted.connect(self._advance_powered_start)
        self._preparation.waiting.connect(self._render_preparation_wait)
        self._theme = "dark"
        self._recipe: Optional[ProductionRecipe] = None
        self._registered_serials: set[str] = set()
        self._registered_hardware_identity: dict[
            str, tuple[str, str, Optional[int]]
        ] = {}
        self._participant_serials: tuple[str, ...] = ()
        self._participant_identity_keys: tuple[str, ...] = ()
        self._ignored_after_start: set[str] = set()
        self._fleet: Optional[FleetController] = None
        self._fixture_control_lease = self._fixture_runtime.fixture_lease
        self._ms6222_control_lease = self._fixture_runtime.reference_lease
        self._batch_coordinator = BatchCoordinator(
            self._fixture_control_lease,
            parent=self,
        )
        self._snr_origin_monotonic_ns = time.monotonic_ns()
        self._snr_panels: dict[int, ProductionSnrPanel] = {}
        self._pending_snr_panels: dict[int, ProductionSnrPanel] = {}
        self._visible_chart_slots: tuple[int | None, ...] = ()
        self._chart_layout_key = None
        self._view_active = False
        self._build_ui()
        self._configure_fleet()
        self._fixture_runtime.telemetry.sample_published.connect(
            self._record_fixture_attitude_sample
        )
        self._fixture_runtime.state_changed.connect(self._on_fixture_runtime_state)
        self._restore_setup_defaults()
        self._session_refresh_timer = QTimer(self)
        self._session_refresh_timer.setInterval(1000)
        self._session_refresh_timer.timeout.connect(self._refresh_session_rows)
        self._snr_refresh_timer = QTimer(self)
        self._snr_refresh_timer.setInterval(200)
        self._snr_refresh_timer.timeout.connect(self._refresh_snr_charts)
        register_translatable(self)

    @Slot(object)
    def _record_fixture_attitude_sample(self, sample: object) -> None:
        fleet = self._fleet
        if fleet is not None:
            fleet.record_fixture_attitude_sample(sample)

    @property
    def recipe(self) -> Optional[ProductionRecipe]:
        return self._recipe

    @property
    def result_store(self) -> Optional[ProductionResultStore]:
        return self._batch_coordinator.store

    @property
    def batch(self) -> Optional[dict]:
        return self._batch_coordinator.batch

    @property
    def batch_output_dir(self) -> Optional[Path]:
        return self._batch_coordinator.output_dir

    @property
    def _store(self):
        return self._batch_coordinator.store

    @property
    def _batch(self):
        return self._batch_coordinator.batch

    @property
    def _batch_output_dir(self):
        return self._batch_coordinator.output_dir

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(7)

        navigation = QHBoxLayout()
        navigation.addStretch(1)
        self._subpage_group = QButtonGroup(self)
        self._subpage_group.setExclusive(True)
        self._batch_page_button = QPushButton(tr("Batch test"))
        self._batch_page_button.setCheckable(True)
        self._batch_page_button.setChecked(True)
        self._fixture_page_button = QPushButton(tr("Motion platform test"))
        self._fixture_page_button.setCheckable(True)
        self._ms6222_page_button = QPushButton(tr("MS-6222 test"))
        self._ms6222_page_button.setCheckable(True)
        self._power_page_button = QPushButton(tr("Power supply diagnostics"))
        self._power_page_button.setCheckable(True)
        self._subpage_group.addButton(self._batch_page_button, 0)
        self._subpage_group.addButton(self._fixture_page_button, 1)
        self._subpage_group.addButton(self._ms6222_page_button, 2)
        self._subpage_group.addButton(self._power_page_button, 3)
        self._subpage_group.idClicked.connect(self._switch_subpage)
        navigation.addWidget(self._batch_page_button)
        navigation.addWidget(self._fixture_page_button)
        navigation.addWidget(self._ms6222_page_button)
        navigation.addWidget(self._power_page_button)
        self._tcp_shell_button = QPushButton(tr("Shell..."))
        self._tcp_shell_button.setObjectName("productionTcpShellButton")
        self._tcp_shell_button.setVisible(self._open_tcp_shell is not None)
        self._tcp_shell_button.clicked.connect(self._on_open_tcp_shell)
        navigation.addWidget(self._tcp_shell_button)
        navigation.addStretch(1)
        outer.addLayout(navigation)

        self._subpages = QStackedWidget()
        self._subpages.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Ignored,
        )
        self._batch_page = QWidget()
        batch_root = QVBoxLayout(self._batch_page)
        batch_root.setContentsMargins(0, 0, 0, 0)
        batch_root.setSpacing(8)
        self._batch_content = QWidget()
        root = QVBoxLayout(self._batch_content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        self._batch_scroll = QScrollArea()
        self._batch_scroll.setObjectName("productionBatchScroll")
        self._batch_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._batch_scroll.setWidgetResizable(True)
        self._batch_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._batch_scroll.setWidget(self._batch_content)
        batch_root.addWidget(self._batch_scroll, 1)

        title_row = QHBoxLayout()
        self._title = QLabel(tr("Automated testing"))
        self._title.setObjectName("productionTitle")
        self._batch_state = QLabel(tr("No batch"))
        self._batch_state.setObjectName("productionState")
        title_row.addWidget(self._title)
        title_row.addStretch(1)
        title_row.addWidget(self._batch_state)
        root.addLayout(title_row)

        self._preview_notice = QLabel(
            tr(
                "Formal automation uses the selected station profile. Devices may be powered off when "
                "creating a batch. Starting automatically prepares power and fixtures and verifies live preflight evidence."
            )
        )
        self._preview_notice.setObjectName("productionPreviewNotice")
        self._preview_notice.setWordWrap(True)
        root.addWidget(self._preview_notice)

        self._setup_group = QGroupBox(tr("Batch setup"))
        setup = QGridLayout(self._setup_group)
        setup.setContentsMargins(10, 10, 10, 7)
        setup.setHorizontalSpacing(7)
        setup.setVerticalSpacing(4)

        self._batch_id_label = QLabel(tr("Batch ID"))
        self._batch_id_edit = QLineEdit()
        self._batch_id_edit.setObjectName("productionBatchId")
        self._operator_label = QLabel(tr("Operator"))
        self._operator_edit = QLineEdit()
        self._operator_edit.setObjectName("productionOperator")
        self._recipe_label = QLabel(tr("Recipe"))
        self._recipe_edit = QLineEdit()
        self._recipe_edit.setObjectName("productionRecipe")
        self._recipe_edit.setReadOnly(True)
        self._recipe_button = QPushButton(tr("Import recipe..."))
        self._recipe_button.clicked.connect(self._choose_recipe)
        self._recipe_label.hide()
        self._recipe_edit.hide()
        self._recipe_button.hide()

        self._environment_check = QCheckBox(tr("Record today's environment"))
        self._environment_check.setObjectName("productionEnvironmentCheck")
        self._environment_check.toggled.connect(self._on_environment_toggled)
        self._weather_label = QLabel(tr("Weather"))
        self._weather_combo = QComboBox()
        self._weather_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self._weather_combo.setMinimumWidth(96)
        self._weather_combo.addItem(tr("Sunny"), "Sunny")
        self._weather_combo.addItem(tr("Cloudy"), "Cloudy")
        self._weather_combo.addItem(tr("Overcast"), "Overcast")
        self._weather_combo.addItem(tr("Rain"), "Rain")
        self._weather_combo.addItem(tr("Snow"), "Snow")
        self._weather_combo.addItem(tr("Fog"), "Fog")
        self._ambient_temperature_label = QLabel(tr("Ambient temperature (°C)"))
        self._ambient_temperature_spin = QDoubleSpinBox()
        self._ambient_temperature_spin.setRange(-40.0, 60.0)
        self._ambient_temperature_spin.setDecimals(1)
        self._ambient_humidity_label = QLabel(tr("Ambient humidity (%RH)"))
        self._ambient_humidity_spin = QDoubleSpinBox()
        self._ambient_humidity_spin.setRange(0.0, 100.0)
        self._ambient_humidity_spin.setDecimals(0)
        self._set_environment_inputs_enabled(False)

        self._product_template_label = QLabel(tr("Device model and power"))
        self._product_template_combo = QComboBox()
        self._product_template_combo.currentIndexChanged.connect(
            self._on_configuration_selection_changed
        )
        self._device_count_label = QLabel(tr("Device quantity"))
        self._device_count_spin = QSpinBox()
        self._device_count_spin.setRange(1, 4)
        self._device_count_spin.valueChanged.connect(
            self._on_configuration_selection_changed
        )
        self._station_profile_label = QLabel(tr("Station profile"))
        self._station_profile_combo = QComboBox()
        self._station_profile_combo.currentIndexChanged.connect(
            self._on_configuration_selection_changed
        )
        self._configuration_summary = QLabel()
        self._configuration_summary.setWordWrap(True)
        self._configuration_summary.hide()
        self._configuration_summary.setObjectName("productionConfigurationSummary")
        self._output_label = QLabel(tr("Storage location"))
        self._output_edit = QLineEdit()
        self._output_edit.setObjectName("productionOutput")
        self._output_button = QPushButton(tr("Browse..."))
        self._output_button.clicked.connect(self._choose_output_dir)
        self._create_button = QPushButton(tr("Start test"))
        self._create_button.setProperty("variant", "primary")
        self._create_button.clicked.connect(self._on_begin_automatic_test)

        self._recipe_status = QLabel(tr("No recipe selected"))
        self._recipe_status.setObjectName("productionRecipeStatus")
        self._recipe_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._recipe_status.hide()

        setup.addWidget(self._batch_id_label, 0, 0)
        setup.addWidget(self._batch_id_edit, 0, 1, 1, 2)
        setup.addWidget(self._operator_label, 0, 3)
        setup.addWidget(self._operator_edit, 0, 4, 1, 2)

        environment_row = QHBoxLayout()
        environment_row.addWidget(self._environment_check)
        environment_row.addWidget(self._weather_label)
        environment_row.addWidget(self._weather_combo)
        environment_row.addWidget(self._ambient_temperature_label)
        environment_row.addWidget(self._ambient_temperature_spin)
        environment_row.addWidget(self._ambient_humidity_label)
        environment_row.addWidget(self._ambient_humidity_spin)
        environment_row.addStretch(1)
        setup.addWidget(self._create_button, 0, 6)
        setup.addWidget(self._product_template_label, 1, 0)
        setup.addWidget(self._product_template_combo, 1, 1, 1, 2)
        setup.addWidget(self._device_count_label, 1, 3)
        setup.addWidget(self._device_count_spin, 1, 4)
        setup.addWidget(self._station_profile_label, 1, 5)
        setup.addWidget(self._station_profile_combo, 1, 6)
        setup.addLayout(environment_row, 2, 0, 1, 8)
        self._details_button = QToolButton()
        self._details_button.setText(tr("Configuration details"))
        self._details_button.setCheckable(True)
        self._details_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._details_button.setArrowType(Qt.ArrowType.RightArrow)
        self._details_button.toggled.connect(self._toggle_configuration_details)
        setup.addWidget(self._details_button, 3, 0, 1, 7)
        setup.addWidget(self._configuration_summary, 4, 0, 1, 7)
        setup.addWidget(self._output_label, 3, 0)
        setup.addWidget(self._output_edit, 3, 1, 1, 5)
        setup.addWidget(self._output_button, 3, 6)
        setup.setColumnStretch(1, 2)
        setup.setColumnStretch(2, 1)
        setup.setColumnStretch(4, 0)
        setup.setColumnStretch(5, 2)
        root.addWidget(self._setup_group)
        self._toggle_configuration_details(False)

        self._progress_panel = ProductionProgressPanel()
        self._execution_summary = self._progress_panel.message_label
        self._progress_panel.show_message(tr("Automatic execution has not started."))
        root.addWidget(self._progress_panel)
        self._fixture_strip = ProductionFixtureStrip()
        root.addWidget(self._fixture_strip)

        self._devices_group = QGroupBox()
        self._devices_layout = QGridLayout(self._devices_group)
        self._devices_layout.setContentsMargins(7, 12, 7, 7)
        self._devices_layout.setSpacing(6)
        self._charts_scroll = QScrollArea()
        self._charts_scroll.setObjectName("productionChartsScroll")
        self._charts_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._charts_scroll.setWidgetResizable(True)
        self._charts_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._charts_scroll.setMinimumHeight(120)
        # 忽略图表的偏好高度，外层按当前窗口分配空间；最小可读高度由卡片拥有。
        self._charts_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self._charts_scroll.setWidget(self._devices_group)
        root.addWidget(self._charts_scroll, 1)
        self._reflow_device_charts()
        self._render_fixtures()

        self._events_container = QWidget()
        events_layout = QVBoxLayout(self._events_container)
        events_layout.setContentsMargins(0, 0, 0, 0)
        events_layout.setSpacing(4)
        events_header = QHBoxLayout()
        self._events_button = QToolButton()
        self._events_button.setText(tr("Batch events"))
        self._events_button.setCheckable(True)
        self._events_button.setArrowType(Qt.ArrowType.RightArrow)
        self._events_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._events_button.toggled.connect(self._toggle_events)
        self._latest_event = QLabel(tr("No batch events"))
        self._latest_event.setMinimumWidth(0)
        self._latest_event.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        events_header.addWidget(self._events_button)
        events_header.addWidget(self._latest_event, 1)
        events_layout.addLayout(events_header)
        self._event_log = QPlainTextEdit()
        self._event_log.setObjectName("productionEventLog")
        self._event_log.setReadOnly(True)
        self._event_log.setMaximumBlockCount(2000)
        self._event_log.setFixedHeight(110)
        self._event_log.hide()
        events_layout.addWidget(self._event_log)
        root.addWidget(self._events_container)

        footer = QHBoxLayout()
        self._footer_status = QLabel(tr("Configure a recipe to create a batch."))
        self._footer_status.setObjectName("productionFooterStatus")
        self._start_button = QPushButton(tr("Start batch"))
        self._start_button.setProperty("variant", "primary")
        self._start_button.setEnabled(False)
        self._start_button.setToolTip(tr("All selected device and fixture gates must be ready."))
        self._start_button.clicked.connect(self._on_start_batch)
        self._abort_button = QPushButton(tr("Abort"))
        self._abort_button.setProperty("variant", "danger")
        self._abort_button.setEnabled(False)
        self._abort_button.clicked.connect(self._on_abort_batch)
        self._complete_button = QPushButton(tr("Complete and generate reports"))
        self._complete_button.setEnabled(False)
        self._complete_button.clicked.connect(self._on_complete_batch)
        self._review_button = QPushButton(tr("Review report..."))
        self._review_button.setEnabled(False)
        self._review_button.clicked.connect(self._on_review_report)
        self._resume_button = QPushButton(tr("Resume batch..."))
        self._resume_button.setObjectName("productionResumeBatch")
        self._resume_button.clicked.connect(self._on_resume_batch)
        self._resume_button.hide()
        self._vehicle_gate_button = QPushButton(tr("Start driving record"))
        self._vehicle_gate_button.setObjectName("productionVehicleGate")
        self._vehicle_gate_button.clicked.connect(self._on_vehicle_gate)
        self._vehicle_gate_button.hide()
        self._next_batch_button = QPushButton(tr("Next batch"))
        self._next_batch_button.hide()
        self._next_batch_button.clicked.connect(self._prepare_next_batch)
        footer.addWidget(self._footer_status, 1)
        footer.addWidget(self._review_button)
        footer.addWidget(self._complete_button)
        footer.addWidget(self._vehicle_gate_button)
        footer.addWidget(self._resume_button)
        footer.addWidget(self._next_batch_button)
        footer.addWidget(self._start_button)
        footer.addWidget(self._abort_button)
        batch_root.addLayout(footer)
        self._subpages.addWidget(self._batch_page)
        self._fixture_debug: Optional[FixtureDebugWorkspace] = None
        self._fixture_host = LazyViewHost(self._create_fixture_debug)
        self._fixture_host.status_message.connect(self.status_message)
        self._subpages.addWidget(self._fixture_host)
        self._ms6222_debug: Optional[Ms6222DebugWorkspace] = None
        self._ms6222_host = LazyViewHost(self._create_ms6222_debug)
        self._ms6222_host.status_message.connect(self.status_message)
        self._subpages.addWidget(self._ms6222_host)
        self._power_debug: Optional[PowerSupplyDebugWorkspace] = None
        self._power_host = LazyViewHost(self._create_power_debug)
        self._power_host.status_message.connect(self.status_message)
        self._subpages.addWidget(self._power_host)
        outer.addWidget(self._subpages, 1)
        self._reflow_device_charts()

    def _create_fixture_debug(self) -> FixtureDebugWorkspace:
        fixture = FixtureDebugWorkspace(
            self._settings,
            self._fixture_control_lease,
            profile_store=self._fixture_runtime.profile_store,
            calibration_store=self._fixture_runtime.calibration_store,
            reference_lease=self._ms6222_control_lease,
            telemetry=self._fixture_runtime.telemetry,
            motion_only=False,
        )
        fixture.active_changed.connect(self._on_fixture_debug_active_changed)
        fixture.runtime_state_changed.connect(self._on_fixture_page_runtime_state)
        if self._resolved_configuration is not None:
            fixture.select_reference_port(
                self._resolved_configuration.reference_profile.serial_port
            )
        self._fixture_debug = fixture
        return fixture

    def _ensure_fixture_debug(self) -> FixtureDebugWorkspace:
        return self._fixture_host.ensure_view()

    def _create_ms6222_debug(self) -> Ms6222DebugWorkspace:
        reference = Ms6222DebugWorkspace(
            self._settings,
            self._ms6222_control_lease,
            telemetry=self._fixture_runtime.telemetry,
            profile_port=(
                ""
                if self._resolved_configuration is None
                else self._resolved_configuration.reference_profile.serial_port
            ),
        )
        reference.active_changed.connect(self._on_ms6222_debug_active_changed)
        reference.runtime_state_changed.connect(self._on_fixture_page_runtime_state)
        if self._resolved_configuration is not None:
            reference.select_profile_port(
                self._resolved_configuration.reference_profile.serial_port
            )
        self._ms6222_debug = reference
        return reference

    def _ensure_ms6222_debug(self) -> Ms6222DebugWorkspace:
        return self._ms6222_host.ensure_view()

    def _create_power_debug(self) -> PowerSupplyDebugWorkspace:
        power = PowerSupplyDebugWorkspace(
            self._settings,
            self._fixture_control_lease,
        )
        power.active_changed.connect(self._on_power_debug_active_changed)
        self._power_debug = power
        return power

    def _ensure_power_debug(self) -> PowerSupplyDebugWorkspace:
        return self._power_host.ensure_view()

    def _on_open_tcp_shell(self) -> None:
        opener = self._open_tcp_shell
        if opener is not None:
            opener()

    def _switch_subpage(self, index: int) -> None:
        requested = int(index)
        if requested not in {0, 1, 2, 3}:
            requested = 0
        if requested in {1, 2, 3} and self._batch_coordinator.lease_active:
            self._batch_page_button.setChecked(True)
            self.status_message.emit(
                tr("Diagnostics are unavailable while a batch owns fixture control."),
                6000,
            )
            return
        if (
            requested != 1
            and self._fixture_debug is not None
            and self._fixture_debug.session_active
        ):
            self._fixture_page_button.setChecked(True)
            self.status_message.emit(
                tr("Finish the fixture engineering session before leaving this page."),
                6000,
            )
            return
        if (
            requested != 2
            and self._ms6222_debug is not None
            and self._ms6222_debug.connection_active
        ):
            self._ms6222_page_button.setChecked(True)
            self.status_message.emit(
                tr("Disconnect MS-6222 before leaving this page."),
                6000,
            )
            return
        if (
            requested != 3
            and self._power_debug is not None
            and self._power_debug.session_active
        ):
            self._power_page_button.setChecked(True)
            self.status_message.emit(
                tr("Disconnect the power supply before leaving this page."),
                6000,
            )
            return
        if requested != 1 and self._fixture_debug is not None:
            self._fixture_debug.clear_safety_confirmation()
        previous = self._subpages.currentIndex()
        if self._view_active and previous != requested:
            deactivate_view(self._subpages.widget(previous))
        self._subpages.setCurrentIndex(requested)
        if self._view_active and previous != requested:
            activate_view(self._subpages.widget(requested))
        self._sync_visible_refresh()

    def _on_fixture_debug_active_changed(self, active: bool) -> None:
        self._batch_page_button.setEnabled(not active)
        self._ms6222_page_button.setEnabled(not active)
        self._power_page_button.setEnabled(not active)

    def _on_ms6222_debug_active_changed(self, active: bool) -> None:
        self._batch_page_button.setEnabled(not active)
        self._fixture_page_button.setEnabled(not active)
        self._power_page_button.setEnabled(not active)

    def _on_power_debug_active_changed(self, active: bool) -> None:
        self._batch_page_button.setEnabled(not active)
        self._fixture_page_button.setEnabled(not active)
        self._ms6222_page_button.setEnabled(not active)

    def _set_configuration_details(self, message: str) -> None:
        self._configuration_summary.setText(message)
        self._details_button.setToolTip(message)
        if self._resolved_configuration is not None:
            resolved = self._resolved_configuration
            self._details_button.setText(tr(
                "Configuration details · {voltage:g} V / {current:g} A · {power:g} W",
                voltage=resolved.product_template.supply_voltage_v,
                current=resolved.total_current_a, power=resolved.total_power_w,
            ))
        else:
            self._details_button.setText(tr("Configuration details"))

    @Slot(bool)
    def _toggle_configuration_details(self, expanded: bool) -> None:
        self._configuration_summary.setVisible(expanded)
        for widget in (self._output_label, self._output_edit, self._output_button):
            widget.setVisible(expanded)
        self._configuration_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._details_button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    @Slot(bool)
    def _toggle_events(self, expanded: bool) -> None:
        self._event_log.setVisible(expanded)
        self._events_button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reflow_device_charts()

    def _refresh_recipe_presentation(self) -> None:
        self._render_steps()
        self._render_fixtures()
        self._reflow_device_charts()

    def _render_steps(self) -> None:
        titles = {test_id: label for test_id, label in self._TEST_DEFS}
        ids = enabled_production_tests(self._recipe)
        grouped: dict[str, list] = {}
        if self._store is not None and self._batch is not None:
            for attempt in self._store.list_attempts(self._batch["batch_id"]):
                grouped.setdefault(attempt["test_id"], []).append(attempt)
            if grouped:
                ids = tuple(test for test in ids if test in grouped) + tuple(
                    test for test in grouped if test not in ids
                )
        steps = []
        priority = ("incomplete", "fail", "running", "analyzing", "armed",
                    "waiting_prerequisite", "pending", "aborted", "pass", "skipped")
        for test in ids:
            status, detail = AttemptStatus.PENDING, ""
            attempts = grouped.get(test, ())
            if attempts:
                states = {item["status"] for item in attempts}
                status = AttemptStatus(next(key for key in priority if key in states))
                reasons = sorted({item.get("result_json", {}).get("reason", "") for item in attempts} - {""})
                detail = "; ".join(self._execution_reason(reason) for reason in reasons)
            steps.append(StepPresentation(test, titles.get(test, test), status, detail))
        self._progress_panel.set_steps(steps)

    def _ensure_snr_panel(self, slot: int) -> ProductionSnrPanel:
        if slot not in self._snr_panels:
            panel = ProductionSnrPanel(slot, S.SIGNAL_PALETTE[slot - 1], self._devices_group)
            panel.set_theme(self._theme)
            panel.setMinimumHeight(180)
            panel.hide()
            self._snr_panels[slot] = panel
        return self._snr_panels[slot]

    def _reflow_device_charts(self) -> None:
        if not hasattr(self, "_devices_layout"):
            return
        sessions = tuple(() if self._fleet is None else self._fleet.sessions())
        candidates = tuple(session for session in sessions if self._recipe is None or
                           self._session_recipe_product(session) == self._recipe.product)
        candidates = tuple(sorted(candidates, key=lambda item: (not item.is_online(), item.slot)))
        frozen = None
        if self._participant_serials:
            by_serial = {session.serial_number: session.slot for session in sessions}
            if self._store is not None and self._batch is not None:
                by_serial.update({device["serial_number"]: int(device["slot"])
                                  for device in self._store.list_devices(self._batch["batch_id"])})
            if self._execution is not None:
                by_serial.update({session.serial_number: session.slot for session in self._execution.sessions.values()})
            frozen = tuple(by_serial[serial] for serial in self._participant_serials if serial in by_serial)
        count = self._recipe.target_device_count if self._recipe is not None else self._device_count_spin.value()
        visible = chart_slots(count, tuple(session.slot for session in candidates), frozen)
        columns = 2 if len(visible) > 1 and self.width() >= 1000 else 1
        key = (visible, columns)
        if self._chart_layout_key == key:
            return
        self._chart_layout_key = key
        self._visible_chart_slots = visible
        while self._devices_layout.count():
            item = self._devices_layout.takeAt(0)
            item.widget().hide()
        for row in range(4):
            self._devices_layout.setRowStretch(row, 0)
        for column in range(2):
            self._devices_layout.setColumnStretch(column, int(column < columns))
        for position, slot in enumerate(visible):
            if slot is None:
                if position not in self._pending_snr_panels:
                    panel = ProductionSnrPanel(position + 1, S.SIGNAL_PALETTE[position], self._devices_group)
                    panel.set_theme(self._theme)
                    panel.setMinimumHeight(180)
                    self._pending_snr_panels[position] = panel
                panel = self._pending_snr_panels[position]
            else:
                panel = self._ensure_snr_panel(slot)
            row, column = divmod(position, columns)
            span = 2 if len(visible) == 3 and position == 2 and columns == 2 else 1
            self._devices_layout.addWidget(panel, row, column, 1, span)
            self._devices_layout.setRowStretch(row, 1)
            panel.show()
        self._update_snr_group_title()

    def _render_fixtures(self) -> None:
        if not hasattr(self, "_fixture_strip"):
            return
        required = fixture_requirements(self._recipe)
        labels = {
            FixtureRuntimeState.NEEDS_BINDING: tr_source("Needs binding"),
            FixtureRuntimeState.CONFIGURED: tr_source("Configured"),
            FixtureRuntimeState.CONNECTING: tr_source("Connecting"),
            FixtureRuntimeState.CONNECTED: tr_source("Connected"),
            FixtureRuntimeState.READY: tr_source("Ready"),
            FixtureRuntimeState.RUNNING: tr_source("Running"),
            FixtureRuntimeState.FAULT: tr_source("Failed"),
        }
        fixtures = []
        for fixture_id, title in self._FIXTURE_DEFS:
            fact = self._fixture_runtime.fact(fixture_id)
            relevant = fixture_id in required
            state = labels[fact.state] if relevant else tr_source("Not applicable")
            if fixture_id == "vehicle" and relevant and fact.state == FixtureRuntimeState.CONFIGURED:
                state = tr_source("Manual gate")
            level = "irrelevant" if not relevant else (
                "good" if fact.state in (FixtureRuntimeState.READY, FixtureRuntimeState.RUNNING) else "pending"
            )
            evidence = self._execution_reason(fact.evidence)
            if fixture_id == "reference" and relevant:
                evidence = tr("MS precision is advisory for SNR tests. {details}", details=evidence)
            fixtures.append(FixturePresentation(fixture_id, title, state, level, evidence))
        self._fixture_strip.set_fixtures(fixtures)

    def refresh_production_configurations(self) -> None:
        """Reload the catalog before creation; an existing batch owns its frozen configuration."""
        if self._batch is not None:
            self._render_configuration_details()
            self._render_steps()
            self._render_fixtures()
            return
        selected_template = str(
            self._product_template_combo.currentData()
            or self._settings.get("production.product_template_id", "")
            or ""
        )
        selected_station = str(
            self._station_profile_combo.currentData()
            or self._settings.get("production.station_profile_id", "")
            or ""
        )
        try:
            products = self._configuration_store.load_effective_product_templates()
            _saved_products, _powers, stations = self._configuration_store.load_catalog()
        except ProductionConfigurationError as exc:
            products, stations = (), ()
            self._resolved_configuration = None
            self._recipe = None
            self._set_configuration_details(
                tr("Production configuration error: {details}", details=str(exc))
            )
        for combo in (self._product_template_combo, self._station_profile_combo):
            combo.blockSignals(True)
            combo.clear()
        for template in products:
            self._product_template_combo.addItem(
                f"{template.display_name} (R{template.revision})",
                template.template_id,
            )
        for station in stations:
            self._station_profile_combo.addItem(
                f"{station.display_name} (R{station.revision})",
                station.station_profile_id,
            )
        for combo, selected in (
            (self._product_template_combo, selected_template),
            (self._station_profile_combo, selected_station),
        ):
            index = combo.findData(selected)
            combo.setCurrentIndex(index if index >= 0 else (0 if combo.count() else -1))
            combo.blockSignals(False)
        self._on_configuration_selection_changed()

    def _on_configuration_selection_changed(self, *_args) -> None:
        if self._batch is not None:
            return
        template_id = str(self._product_template_combo.currentData() or "")
        station_id = str(self._station_profile_combo.currentData() or "")
        if not template_id or not station_id:
            self._fixture_runtime.clear_bindings()
            self._resolved_configuration = None
            if self._recipe_edit.text().strip() == "":
                self._recipe = None
                self._refresh_recipe_presentation()
            self._set_configuration_details(
                tr("Configure product and station profiles in Settings.")
            )
            self._update_start_gate()
            return
        try:
            resolved = self._configuration_store.resolve(
                template_id,
                station_id,
                int(self._device_count_spin.value()),
            )
            recipe = ProductionRecipe.from_mapping(resolved.to_recipe_payload())
        except (ProductionConfigurationError, RecipeValidationError) as exc:
            self._fixture_runtime.clear_bindings()
            self._resolved_configuration = None
            self._recipe = None
            self._refresh_recipe_presentation()
            self._set_configuration_details(
                tr("Configuration is not ready: {details}", details=str(exc))
            )
            self._update_start_gate()
            return
        self._resolved_configuration = resolved
        self._fixture_runtime.bind_configuration(resolved)
        if self._fixture_debug is not None:
            self._fixture_debug.select_reference_port(
                resolved.reference_profile.serial_port
            )
        if self._ms6222_debug is not None:
            self._ms6222_debug.select_profile_port(
                resolved.reference_profile.serial_port
            )
        self._recipe = recipe
        self._recipe_edit.clear()
        self._refresh_recipe_presentation()
        self._render_configuration_details()
        self._render_fixtures()
        self._footer_status.setText(tr("Production configuration is ready."))
        self._refresh_session_rows()

    def _render_configuration_details(self) -> None:
        resolved = self._resolved_configuration
        if resolved is None:
            if self._recipe is not None:
                self._set_configuration_details(tr(
                    "Legacy recipe loaded for migration: {recipe_id} | target {count} device(s)",
                    recipe_id=self._recipe.recipe_id, count=self._recipe.target_device_count,
                ))
            return
        product = resolved.product_template
        power = resolved.power_profile
        station = resolved.station_profile
        motion = resolved.motion_profile
        reference = resolved.reference_profile
        self._set_configuration_details(
            tr(
                "{product} × {count} | per device {voltage:g} V / {single_current:g} A | "
                "combined {voltage:g} V / {total_current:g} A / {power_w:g} W | "
                "power {power_model} at {host} | motion {motion_host}:{motion_port} | "
                "MS-6222 {serial_port} | branches {branches} × {branch_current:g} A | "
                "configuration SHA-256 {sha}",
                product=product.product.upper(),
                count=resolved.device_count,
                voltage=product.supply_voltage_v,
                single_current=product.per_device_current_a,
                total_current=resolved.total_current_a,
                power_w=resolved.total_power_w,
                power_model=power.model,
                host=power.host,
                motion_host=motion.controller_ipv4,
                motion_port=motion.command_port,
                serial_port=reference.serial_port,
                branches=station.branch_count,
                branch_current=station.branch_current_a,
                sha=resolved.sha256,
            )
        )

    def _restore_setup_defaults(self) -> None:
        self._batch_id_edit.setText(datetime.now().strftime("BATCH-%Y%m%d-%H%M%S"))
        self._operator_edit.setText(str(self._settings.get("production.last_operator", "")))
        try:
            storage = resolve_production_storage(self._settings)
            self._production_storage_mode = storage.mode
            self._output_edit.setText(str(storage.root))
        except ValueError:
            self._production_storage_mode = "follow_recording"
            self._output_edit.clear()
        self._device_count_spin.setValue(
            max(1, min(4, int(self._settings.get("production.device_count", 1))))
        )
        self.refresh_production_configurations()
        recipe_path = str(self._settings.get("production.last_recipe", "") or "")
        if self._recipe is None and recipe_path and Path(recipe_path).is_file():
            self.load_recipe_file(recipe_path)

    def _configure_fleet(self) -> None:
        if bool(
            getattr(self._settings, "device_configuration_blocked", False)
            or getattr(self._settings, "read_only_recovery", False)
        ):
            self._footer_status.setText(
                tr("Device UDP settings require recovery before Production can start")
            )
            return
        try:
            fleet = FleetController(
                discovery_cidr=str(
                    self._settings.get("production.discovery_cidr", "192.168.1.0/24")
                ),
                local_port=int(self._settings.get("device_udp.local_port", 45678)),
                device_port=int(self._settings.get("production.device_port", 4004)),
                max_devices=int(self._settings.get("production.max_devices", 4)),
                session_registry=self._session_registry,
                broker=self._broker,
                session_directory=self._session_directory,
                parent=self,
            )
        except (FleetConfigurationError, TypeError, ValueError) as exc:
            self._footer_status.setText(
                tr("Discovery configuration is invalid: {details}", details=str(exc))
            )
            return
        self._fleet = fleet
        fleet.session_added.connect(self._on_session_updated)
        fleet.session_updated.connect(self._on_session_updated)
        fleet.identity_conflict.connect(self._on_identity_conflict)
        fleet.endpoint_migrated.connect(self._on_endpoint_migrated)
        fleet.endpoint_rejected.connect(self._on_endpoint_rejected)
        fleet.status_changed.connect(self._on_fleet_status_changed)
        fleet.error.connect(self._on_fleet_error)

    def activate(self) -> bool:
        """Start discovery once; leaving the page does not interrupt a batch."""
        fleet = self._fleet
        if fleet is None:
            return False
        if fleet.is_running:
            self._sync_visible_refresh()
            return True
        self._footer_status.setText(tr("Discovering supported devices..."))
        started = fleet.start()
        if started:
            self._sync_visible_refresh()
        return started

    def activate_view(self) -> None:
        """Render the active production subpage while keeping fleet state independent."""
        if self._view_active:
            return
        self._view_active = True
        activate_view(self._subpages.currentWidget())
        self._sync_visible_refresh()

    def deactivate_view(self) -> None:
        """Suspend hidden production rendering without stopping discovery or sessions."""
        if not self._view_active:
            return
        self._view_active = False
        self._session_refresh_timer.stop()
        self._snr_refresh_timer.stop()
        deactivate_view(self._subpages.currentWidget())

    def _sync_visible_refresh(self) -> None:
        fleet = self._fleet
        if (
            not self._view_active
            or self._subpages.currentIndex() != 0
            or fleet is None
            or not fleet.is_running
        ):
            self._session_refresh_timer.stop()
            self._snr_refresh_timer.stop()
            return
        self._refresh_session_rows()
        self._refresh_snr_charts()
        if self._execution is not None:
            self._render_execution(self._execution.run.progress)
        self._session_refresh_timer.start()
        self._snr_refresh_timer.start()

    def _choose_recipe(self) -> None:
        start = self._recipe_edit.text().strip()
        if start:
            start = str(Path(start).parent)
        else:
            start = str(Path.home())
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            tr("Import production recipe"),
            start,
            tr("Production recipes (*.json);;All files (*)"),
        )
        if path:
            self.load_recipe_file(path)

    def load_recipe_file(self, path: str | Path) -> bool:
        """Import a legacy schema-v1 recipe for migration and historical tests."""
        if self._batch is not None:
            raise ResultStoreError("the created batch owns its frozen recipe")
        source = Path(path).expanduser()
        try:
            recipe = ProductionRecipe.from_path(source)
        except RecipeValidationError as exc:
            self._recipe = None
            self._refresh_recipe_presentation()
            self._recipe_edit.setText(str(source))
            self._recipe_status.setText(
                tr("Invalid recipe: {details}", details="; ".join(exc.errors))
            )
            self._recipe_edit.setToolTip(self._recipe_status.text())
            self._footer_status.setText(tr("Correct the recipe before creating a batch."))
            self.status_message.emit(self._recipe_status.text(), 8000)
            return False

        self._recipe = recipe
        self._resolved_configuration = None
        self._refresh_recipe_presentation()
        self._recipe_edit.setText(str(source.resolve()))
        scope = tr("Engineering only") if recipe.engineering_only else tr("Formal production")
        self._recipe_status.setText(
            tr(
                "Recipe {recipe_id} | SHA-256 {sha} | {scope}",
                recipe_id=recipe.recipe_id,
                sha=recipe.sha256[:12],
                scope=scope,
            )
        )
        self._recipe_edit.setToolTip(self._recipe_status.text())
        self._footer_status.setText(self._recipe_status.text())
        self._set_configuration_details(
            tr(
                "Legacy recipe loaded for migration: {recipe_id} | target {count} device(s)",
                recipe_id=recipe.recipe_id,
                count=recipe.target_device_count,
            )
        )
        self.append_event(tr("Recipe validated: {recipe_id}", recipe_id=recipe.recipe_id))
        self._refresh_session_rows()
        return True

    def _choose_output_dir(self) -> None:
        start = self._output_edit.text().strip() or str(Path.home())
        directory = QFileDialog.getExistingDirectory(
            self,
            tr("Select production output folder"),
            start,
        )
        if directory:
            self._production_storage_mode = "custom_root"
            self._output_edit.setText(directory)

    def _on_create_batch(self) -> None:
        try:
            self.create_batch()
        except (
            RecipeValidationError,
            ResultStoreError,
            RuntimeError,
            OSError,
            ValueError,
        ) as exc:
            message = tr("Cannot create batch: {details}", details=str(exc))
            self._footer_status.setText(message)
            self.status_message.emit(message, 8000)

    def _on_begin_automatic_test(self) -> None:
        """Single preparation-page action: freeze the batch then start automation."""

        try:
            if self._batch is None:
                self.create_batch()
            self.start_batch()
        except (RecipeValidationError, ResultStoreError, RuntimeError, OSError, ValueError) as exc:
            message = tr("Cannot start automatic test: {details}", details=str(exc))
            self._footer_status.setText(message)
            self.status_message.emit(message, 8000)

    def _prepare_next_batch(self) -> None:
        try:
            self._batch_coordinator.reset_terminal_batch()
        except ResultStoreError as exc:
            self._footer_status.setText(tr("Cannot prepare next batch: {details}", details=str(exc)))
            return
        self._registered_serials.clear()
        self._registered_hardware_identity.clear()
        self._participant_serials = ()
        self._participant_identity_keys = ()
        self._ignored_after_start.clear()
        self._power_record_count = 0
        self._execution = None
        self._execution_hardware = None
        self._batch_id_edit.setText(datetime.now().strftime("BATCH-%Y%m%d-%H%M%S"))
        for editor in (self._batch_id_edit, self._operator_edit, self._output_edit):
            editor.setReadOnly(False)
        self._product_template_combo.setEnabled(True)
        self._device_count_spin.setEnabled(True)
        self._station_profile_combo.setEnabled(True)
        self._output_button.setEnabled(True)
        self._recipe_button.setEnabled(True)
        self._create_button.setEnabled(True)
        self._next_batch_button.hide()
        self._review_button.setEnabled(False)
        self.refresh_production_configurations()
        self._progress_panel.show_message(tr("Automatic execution has not started."))
        self._batch_state.setText(tr("Ready for next batch"))
        self._footer_status.setText(tr("Review the retained settings, then start the next test."))
        self._update_start_gate()

    def create_batch(self) -> dict:
        if self._batch is not None:
            raise ResultStoreError("a batch is already active in this workspace")
        if self._recipe is None:
            raise RecipeValidationError(("select a valid recipe first",))

        batch_id = self._batch_id_edit.text().strip()
        operator = self._operator_edit.text().strip()
        output_root = self._output_edit.text().strip()
        if not batch_id:
            raise ValueError("batch ID is required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", batch_id):
            raise ValueError(
                "batch ID must be a single ASCII path component using letters, "
                "digits, dot, underscore, or hyphen"
            )
        if not operator:
            raise ValueError("operator is required")
        if not output_root:
            raise ValueError("output folder is required")

        batch_output = Path(output_root).expanduser().resolve() / batch_id
        if batch_output.exists() and any(batch_output.iterdir()):
            raise ResultStoreError(
                f"batch output directory already exists and is not empty: {batch_output}"
            )
        storage_mode = getattr(self, "_production_storage_mode", "custom_root")
        self._settings.set("production.storage_mode", storage_mode)
        if storage_mode == "custom_root":
            self._settings.set("paths.production_dir", str(Path(output_root).expanduser()))
        self._settings.set("production.last_operator", operator)
        self._settings.set(
            "production.product_template_id",
            str(self._product_template_combo.currentData() or ""),
        )
        self._settings.set(
            "production.station_profile_id",
            str(self._station_profile_combo.currentData() or ""),
        )
        self._settings.set("production.device_count", self._recipe.target_device_count)
        self._settings.set("production.last_recipe", "")
        self._settings.persist_preferences()
        batch_output.mkdir(parents=True, exist_ok=True)
        fleet = self._fleet
        if fleet is not None:
            fleet.clear_snr_histories()
            self._snr_origin_monotonic_ns = time.monotonic_ns()
            if not fleet.arm_batch_recording(batch_id, batch_output):
                fleet.finalize_recordings(wait_for_capture_restore=False)
                raise RuntimeError(
                    "evidence recording could not be created for one or more devices"
                )
        try:
            self._recipe.write_snapshot(batch_output / "recipe.json")
            if self._resolved_configuration is not None:
                snapshot = self._resolved_configuration.to_snapshot()
                snapshot["sha256"] = self._resolved_configuration.sha256
                (batch_output / "production_configuration.json").write_text(
                    json.dumps(
                        snapshot,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )
            environment = self._batch_environment()
            batch = self._batch_coordinator.create(
                batch_id=batch_id,
                recipe=self._recipe,
                operator=operator,
                **environment,
                output_dir=batch_output,
            )
        except Exception:
            if fleet is not None:
                fleet.finalize_recordings(wait_for_capture_restore=False)
            raise
        if fleet is not None:
            for session in fleet.sessions():
                self._register_session(session)
                self._on_session_updated(session)
        self._lock_batch_setup()
        self._batch_state.setText(tr("Batch ready"))
        self._footer_status.setText(
            tr("Batch ready. Waiting for devices and fixture checks.")
        )
        self.append_event(tr("Batch created: {batch_id}", batch_id=batch_id))
        self.batch_created.emit(batch_id)
        self.status_message.emit(tr("Batch {batch_id} created", batch_id=batch_id), 5000)
        self._update_start_gate()
        return dict(batch)

    def _lock_batch_setup(self) -> None:
        for editor in (
            self._batch_id_edit,
            self._operator_edit,
            self._recipe_edit,
            self._output_edit,
        ):
            editor.setReadOnly(True)
        self._recipe_button.setEnabled(False)
        self._product_template_combo.setEnabled(False)
        self._device_count_spin.setEnabled(False)
        self._station_profile_combo.setEnabled(False)
        self._output_button.setEnabled(False)
        self._create_button.setEnabled(False)

    def update_device_slot(
        self,
        slot: int,
        *,
        serial_number: Optional[str] = None,
        endpoint: Optional[str] = None,
        connection: Optional[str] = None,
        recording: Optional[str] = None,
        current_test: Optional[str] = None,
        result: Optional[str] = None,
    ) -> None:
        if slot not in range(1, 5):
            raise ValueError("slot must be between 1 and 4")
        self._ensure_snr_panel(slot).set_status(
            serial_number=serial_number,
            endpoint=endpoint,
            connection=connection,
            recording=recording,
            current_test=current_test,
            result=result,
        )

    def update_fixture_state(
        self, fixture_id: str, *, state: FixtureRuntimeState, evidence: str = "",
    ) -> None:
        self._fixture_runtime.set_state(fixture_id, FixtureRuntimeState(state), evidence)

    @Slot(object)
    def _on_fixture_runtime_state(self, fact: FixtureRuntimeFact) -> None:
        self._render_fixtures()
        self._update_start_gate()

    def _on_fixture_page_runtime_state(
        self, fixture: str, state: str, evidence: str
    ) -> None:
        self._fixture_runtime.set_state(
            fixture,
            FixtureRuntimeState(state),
            evidence,
        )

    def append_event(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        self._event_log.appendPlainText(line)
        self._latest_event.setText(line)
        self._latest_event.setToolTip(line)

    def _on_session_updated(self, session: DeviceSession) -> None:
        self._maybe_confirm_vehicle_power_cycle()
        if self._view_active:
            self._reflow_device_charts()
        state_result = {
            DeviceSessionState.DISCOVERED: tr("Identifying"),
            DeviceSessionState.IDENTITY_PENDING: tr("Serial number pending"),
            DeviceSessionState.IDENTIFIED: "-",
            DeviceSessionState.CONFLICT: tr("Identity conflict"),
            DeviceSessionState.UNSUPPORTED: tr("Unsupported device"),
        }[session.state]
        if session.slot in self._visible_chart_slots and self._view_active:
            self.update_device_slot(
                session.slot,
                serial_number=(
                    session.serial_number
                    or session.hardware_type
                    or tr("Identifying")
                ),
                endpoint=f"{session.endpoint[0]}:{session.endpoint[1]}",
                connection=tr_source("Online") if session.is_online() else tr_source("Offline"),
                recording=self._recording_status_text(session),
                result=None if session.state == DeviceSessionState.IDENTIFIED and self._execution is not None else state_result,
            )
        if session.state == DeviceSessionState.IDENTIFIED:
            self._register_session(session)
        if self._batch is not None and self._batch.get("status") == BatchStatus.RUNNING.value:
            self._apply_external_ins_gates((session,))
        self._update_start_gate()

    def _recording_status_text(self, session: DeviceSession) -> str:
        """Describe the evidence state with its actual workflow cause."""

        if session.capture_ready:
            return tr_source("Full analysis capture active")
        if session.recording_armed:
            if session.capture_error and session.capture_state.value in {
                "failed",
                "unsupported",
            }:
                return tr(
                    "Full analysis capture failed: {reason}",
                    reason=session.capture_error,
                )
            return tr_source("Preparing full analysis capture")
        if self._batch is None:
            return tr_source("Batch not created")
        return tr_source("Evidence recording not ready")

    def _refresh_session_rows(self) -> None:
        fleet = self._fleet
        if fleet is None:
            return
        for session in fleet.sessions():
            self._on_session_updated(session)
        self._update_snr_group_title()
        if self._execution is not None:
            self._render_execution(self._execution.run.progress)
        self._update_start_gate()

    def _refresh_snr_charts(
        self,
        *,
        now_monotonic_ns: Optional[int] = None,
    ) -> None:
        fleet = self._fleet
        now_ns = (
            time.monotonic_ns()
            if now_monotonic_ns is None
            else int(now_monotonic_ns)
        )
        sessions = {
            session.slot: session
            for session in (() if fleet is None else fleet.sessions())
        }
        finite_values: list[float] = []
        self._reflow_device_charts()
        visible_panels = [self._snr_panels[slot] if slot is not None else self._pending_snr_panels[position]
                          for position, slot in enumerate(self._visible_chart_slots)]
        for slot, panel in zip(self._visible_chart_slots, visible_panels):
            session = sessions.get(slot)
            if session is None:
                panel.set_samples(
                    (),
                    origin_monotonic_ns=self._snr_origin_monotonic_ns,
                    online=False,
                )
                continue
            samples = session.snr_history(
                window_s=300.0,
                now_monotonic_ns=now_ns,
            )
            online = session.is_online(now_monotonic_ns=now_ns)
            snr_fresh = bool(
                online
                and samples
                and now_ns - samples[-1].monotonic_ns <= 1_500_000_000
            )
            panel.set_samples(
                samples,
                origin_monotonic_ns=self._snr_origin_monotonic_ns,
                online=snr_fresh,
            )
            finite_values.extend(
                sample.value_db
                for sample in samples
                if math.isfinite(sample.value_db)
            )

        y_min = 0.0
        y_max = 20.0
        if finite_values:
            y_min = min(y_min, math.floor(min(finite_values) - 1.0))
            y_max = max(y_max, math.ceil(max(finite_values) + 1.0))
        elapsed_s = max(
            0.0,
            (now_ns - self._snr_origin_monotonic_ns) / 1_000_000_000.0,
        )
        x_max = max(30.0, elapsed_s)
        x_min = max(0.0, x_max - 300.0)
        for panel in visible_panels:
            panel.set_ranges(
                x_min=x_min,
                x_max=x_max,
                y_min=y_min,
                y_max=y_max,
            )

    def _update_snr_group_title(self) -> None:
        sessions = {session.slot: session for session in (() if self._fleet is None else self._fleet.sessions())}
        count = sum(1 for slot in self._visible_chart_slots if slot in sessions and sessions[slot].is_online())
        self._devices_group.setTitle(tr("Device SNR · {online}/{total} online", online=count, total=len(self._visible_chart_slots)))

    def _register_session(self, session: DeviceSession) -> None:
        store = self._store
        if (
            store is None
            or self._batch is None
            or self._recipe is None
            or session.state != DeviceSessionState.IDENTIFIED
            or not session.serial_number
            or self._session_recipe_product(session) != self._recipe.product
        ):
            return
        if (
            self._batch.get("status") == BatchStatus.RUNNING.value
            and session.serial_number not in self._participant_serials
        ):
            if session.serial_number not in self._ignored_after_start:
                self._ignored_after_start.add(session.serial_number)
                self.append_event(
                    tr(
                        "Device detected after batch start and not added: {serial}",
                        serial=session.serial_number,
                    )
                )
                self._record_batch_event(
                    "device_ignored_after_participant_freeze",
                    {
                        "serial_number": session.serial_number,
                        "device_uid": session.device_uid,
                        "slot": session.slot,
                    },
                )
            return
        hardware_identity = (
            session.device_uid,
            session.mac_address,
            session.mac_source,
        )
        if getattr(self, "_identity_checks", None) and session.serial_number in self._identity_checks:
            expected_uid, expected_mac = self._frozen_identities.get(
                session.serial_number, ("", "")
            )
            matches = (
                (not expected_uid or session.device_uid == expected_uid)
                and (not expected_mac or session.mac_address == expected_mac)
            )
            self._identity_checks[session.serial_number] = (
                "consistent" if matches else "mismatched"
            )
            state = tr("identity consistent") if matches else tr("identity mismatch")
            self.append_event(tr(
                "Participant identity check: {details}",
                details=f"{session.serial_number}: {state}",
            ))
            self._update_start_gate()
            return
        if session.serial_number in self._registered_serials:
            if self._registered_hardware_identity.get(session.serial_number) == hardware_identity:
                return
            try:
                store.update_device_identity(
                    self._batch["batch_id"],
                    session.serial_number,
                    device_uid=session.device_uid,
                    mac_address=session.mac_address,
                    mac_source=session.mac_source,
                )
            except ResultStoreError as exc:
                self._on_fleet_error(str(exc))
                return
            self._registered_hardware_identity[session.serial_number] = hardware_identity
            return
        try:
            store.register_device(
                self._batch["batch_id"],
                serial_number=session.serial_number,
                slot=session.slot,
                endpoint_ip=session.endpoint[0],
                endpoint_port=session.endpoint[1],
                hardware_type=session.hardware_type,
                device_uid=session.device_uid,
                mac_address=session.mac_address,
                mac_source=session.mac_source,
                metadata={
                    "main_firmware": (
                        session.identity.main_firmware if session.identity else ""
                    )
                },
            )
        except ResultStoreError as exc:
            self._on_fleet_error(str(exc))
            return
        self._registered_serials.add(session.serial_number)
        self._registered_hardware_identity[session.serial_number] = hardware_identity
        self.append_event(
            tr(
                "Device registered: slot {slot}, {serial}",
                slot=session.slot,
                serial=session.serial_number,
            )
        )

    def _evaluate_start_gate(self, *, require_devices: bool = True) -> tuple[tuple[DeviceSession, ...], str]:
        if bool(
            getattr(self._settings, "device_configuration_blocked", False)
            or getattr(self._settings, "read_only_recovery", False)
        ):
            return (), tr(
                "Device UDP settings require recovery before Production can start"
            )
        if self._batch is None or self._store is None:
            return (), tr("Create a batch before starting.")
        if self._batch.get("status") != BatchStatus.READY.value:
            return (), tr("The batch is not in the ready state.")
        fleet = self._fleet
        if fleet is None:
            return (), tr("Device discovery is unavailable.")

        if not require_devices:
            if self._recipe is None:
                return (), tr("Select a valid recipe first.")
            return (), ""
        now_ns = time.monotonic_ns()
        online = tuple(
            session
            for session in fleet.sessions()
            if session.is_online(now_monotonic_ns=now_ns)
        )
        conflicted = tuple(
            session
            for session in online
            if session.state == DeviceSessionState.CONFLICT
        )
        if conflicted:
            details = ", ".join(
                f"{session.endpoint[0]}:{session.endpoint[1]}"
                for session in conflicted
            )
            return (), tr("Identity conflict: {details}", details=details)
        pending_identity = tuple(
            session
            for session in online
            if session.state in {
                DeviceSessionState.DISCOVERED,
                DeviceSessionState.IDENTITY_PENDING,
            }
        )
        if pending_identity:
            return (), tr("Wait for online devices to report complete identities.")
        unsupported = tuple(
            session
            for session in online
            if session.state == DeviceSessionState.UNSUPPORTED
        )
        if unsupported:
            models = ", ".join(
                sorted({session.hardware_type or "?" for session in unsupported})
            )
            return (), tr(
                "Unsupported online device(s): {devices}",
                devices=models,
            )
        if self._recipe is None:
            return (), tr("Select a valid recipe first.")
        mismatched = tuple(
            session
            for session in online
            if session.state == DeviceSessionState.IDENTIFIED
            and self._session_recipe_product(session) != self._recipe.product
        )
        if mismatched:
            models = ", ".join(
                sorted({session.hardware_type or "?" for session in mismatched})
            )
            return (), tr(
                "Online device product does not match recipe {product}: {devices}",
                product=self._recipe.product.upper(),
                devices=models,
            )

        participants = tuple(
            session
            for session in online
            if session.state == DeviceSessionState.IDENTIFIED
            and session.serial_number
            and session.identity_key
            and self._session_recipe_product(session) == self._recipe.product
        )
        target_count = self._recipe.target_device_count
        if len(participants) != target_count:
            return (), tr(
                "Exactly {expected} identified {product} device(s) are required; "
                "currently ready: {actual}.",
                expected=target_count,
                product=self._recipe.product.upper(),
                actual=len(participants),
            )
        recording_pending = tuple(
            session.serial_number
            for session in participants
            if not session.recording_armed
        )
        if recording_pending:
            return (), tr(
                "Evidence recording is not ready for: {devices}",
                devices=", ".join(recording_pending),
            )
        capture_pending = tuple(
            session.serial_number
            for session in participants
            if not session.capture_ready
        )
        if capture_pending:
            details = ", ".join(
                f"{session.serial_number}: {session.capture_error or session.capture_state.value}"
                for session in participants
                if not session.capture_ready
            )
            return (), tr(
                "Full analysis capture is not ready for: {devices}",
                devices=details,
            )
        unregistered = tuple(
            session.serial_number
            for session in participants
            if session.serial_number not in self._registered_serials
        )
        if unregistered:
            return (), tr(
                "Device identity is not persisted yet: {devices}",
                devices=", ".join(unregistered),
            )
        return tuple(sorted(participants, key=lambda value: value.slot)), ""

    @staticmethod
    def _session_recipe_product(session: DeviceSession) -> str:
        policy = production_product_policy(session.hardware_type)
        return "" if policy is None else str(policy.production_recipe_product or "")

    def _update_start_gate(self) -> None:
        if not hasattr(self, "_start_button"):
            return
        self._start_button.setVisible(
            self._batch is not None
            and self._batch.get("status") == BatchStatus.READY.value
        )
        if self._abort_coordinator.state in {AbortState.STOPPING, AbortState.FAILED}:
            self._start_button.setEnabled(False)
            self._abort_button.setEnabled(False)
            self._complete_button.setEnabled(False)
            return
        if self._production_power_worker is not None and self._execution is None:
            self._start_button.setEnabled(False)
            self._abort_button.setEnabled(True)
            self._start_button.setToolTip(tr("Preparing and verifying production power."))
            return
        if self._batch is not None and self._batch.get("status") == BatchStatus.RUNNING.value:
            self._start_button.setEnabled(False)
            self._abort_button.setEnabled(True)
            attempts = [] if self._store is None else self._store.list_attempts(
                self._batch["batch_id"]
            )
            self._complete_button.setEnabled(
                bool(attempts)
                and all(
                    item["status"]
                    in {
                        AttemptStatus.PASS.value,
                        AttemptStatus.FAIL.value,
                        AttemptStatus.SKIPPED.value,
                        AttemptStatus.INCOMPLETE.value,
                        AttemptStatus.ABORTED.value,
                    }
                    for item in attempts
                )
            )
            self._start_button.setToolTip(tr("Batch participants are frozen."))
            return
        suspended = (
            self._batch is not None
            and self._batch.get("status") == BatchStatus.SUSPENDED.value
        )
        self._resume_button.setVisible(self._batch is None)
        self._vehicle_gate_button.setVisible(suspended)
        self._vehicle_gate_button.setEnabled(
            suspended
            and bool(self._identity_checks)
            and all(state == "consistent" for state in self._identity_checks.values())
            and len(self._identity_checks) >= len(self._registered_serials)
        )
        if self._batch is not None and self._batch.get("status") in {
            BatchStatus.ABORTED.value,
            BatchStatus.COMPLETED.value,
            BatchStatus.INCOMPLETE.value,
            BatchStatus.SUSPENDED.value,
        }:
            self._start_button.setEnabled(False)
            self._abort_button.setEnabled(False)
            self._complete_button.setEnabled(False)
            if self._store is not None:
                self._review_button.setEnabled(
                    any(
                        item["status"] == "generated_v1"
                        for item in self._store.list_report_artifacts(
                            self._batch["batch_id"]
                        )
                    )
                )
            self._next_batch_button.show()
            return

        participants, reason = self._evaluate_start_gate(
            require_devices=self._resolved_configuration is None,
        )
        ready = not reason and (bool(participants) or self._resolved_configuration is not None)
        self._start_button.setEnabled(ready)
        self._abort_button.setEnabled(self._batch is not None and self._batch.get("status") == BatchStatus.READY.value)
        self._complete_button.setEnabled(False)
        if ready:
            if self._resolved_configuration is not None:
                message = tr("Start automatic power and fixture preparation; devices may be powered off.")
            else:
                message = tr(
                    "{count} device(s) ready. Starting will freeze the participant list.",
                    count=len(participants),
                )
            self._start_button.setToolTip(message)
            self._footer_status.setText(message)
        else:
            self._start_button.setToolTip(reason)
            if self._batch is not None:
                self._footer_status.setText(reason)

    def _on_start_batch(self) -> None:
        try:
            self.start_batch()
        except (ResultStoreError, RuntimeError, ValueError) as exc:
            message = tr("Cannot start batch: {details}", details=str(exc))
            self._footer_status.setText(message)
            self.status_message.emit(message, 8000)

    def start_batch(self) -> dict:
        if self._resolved_configuration is not None:
            self._begin_powered_batch_start()
            return dict(self._batch)
        participants, reason = self._evaluate_start_gate()
        if reason:
            raise ResultStoreError(reason)
        return self._commit_batch_start(participants)

    def _commit_batch_start(
        self,
        participants: tuple[DeviceSession, ...],
    ) -> dict:
        if self._store is None or self._batch is None:
            raise ResultStoreError("no batch is ready")
        if self._resolved_configuration is not None and self._preparation.state is not PreparationState.ADMITTED:
            raise ResultStoreError("formal batch start requires confirmed power and device admission")
        enabled_tests = enabled_production_tests(self._recipe)
        serials = tuple(session.serial_number for session in participants)
        identity_keys = tuple(session.identity_key for session in participants)
        try:
            batch = self._batch_coordinator.start(
                serials,
                enabled_tests,
                freeze_participants=(
                    lambda: self._fleet.freeze_batch_participants(identity_keys)
                    if self._fleet is not None
                    else None
                ),
            )
        except (RuntimeError, ValueError) as exc:
            raise ResultStoreError(
                f"participant recording freeze failed; batch marked incomplete: {exc}"
            ) from exc

        self._participant_serials = serials
        self._participant_identity_keys = identity_keys
        self._reflow_device_charts()
        self._render_steps()
        self._apply_external_ins_gates(participants)
        self._batch_state.setText(tr("Batch running"))
        message = tr(
            "Batch started with {count} participant device(s).",
            count=len(serials),
        )
        self._footer_status.setText(message)
        self.append_event(message)
        self._update_start_gate()
        self._fixture_page_button.setEnabled(False)
        self._power_page_button.setEnabled(False)
        self.status_message.emit(message, 5000)
        if self._resolved_configuration is not None:
            self._start_execution(participants)
        return dict(batch)

    def _begin_powered_batch_start(self) -> None:
        if self._production_power_worker is not None:
            raise ResultStoreError("production power preparation is already active")
        participants, reason = self._evaluate_start_gate(require_devices=False)
        if reason:
            raise ResultStoreError(reason)
        resolved = self._resolved_configuration
        if resolved is None:
            raise ResultStoreError("production configuration is not resolved")
        profile = resolved.power_profile
        if profile.driver_id != "gwinstek_psw80_27":
            raise ResultStoreError(f"unsupported production power driver: {profile.driver_id}")
        policy = psw80_27_validation_policy(
            resolved.product_template.supply_voltage_v,
            resolved.total_current_a,
        )
        config = PowerSupplyConfig(
            host=profile.host,
            port=profile.port,
            expected_manufacturer=profile.manufacturer,
            expected_model=profile.model,
            expected_serial=profile.serial_number,
            voltage_set_v=resolved.product_template.supply_voltage_v,
            current_set_a=resolved.total_current_a,
            voltage_setpoint_tolerance_v=policy.voltage_setpoint_tolerance_v,
            current_setpoint_tolerance_a=policy.current_setpoint_tolerance_a,
            output_voltage_min_v=policy.output_voltage_min_v,
            output_voltage_max_v=policy.output_voltage_max_v,
            off_voltage_max_v=policy.off_voltage_max_v,
            output_settle_timeout_s=profile.output_settle_timeout_s,
        )
        self._batch_coordinator.reserve_start()
        worker = ProductionPowerWorker(
            config,
            adapter_factory=self._production_power_adapter_factory,
            parent=self,
        )
        worker.power_ready.connect(self._on_production_power_ready)
        worker.power_failed.connect(self._on_production_power_failed)
        worker.power_stopped.connect(self._on_production_power_stopped)
        worker.output_changed.connect(self._on_power_cycle_output)
        self._production_power_worker = worker
        self._power_record_count = 0
        self._start_button.setEnabled(False)
        self._start_button.setToolTip(tr("Preparing and verifying production power."))
        message = tr("Production power preparation started.")
        self._show_execution_message(message)
        self._footer_status.setText(message)
        self.append_event(message)
        self.update_fixture_state("power", state=FixtureRuntimeState.CONNECTING, evidence=message)
        self._preparation.begin()
        worker.start()

    def _on_production_power_ready(
        self,
        identity,
        result,
        records,
        action_id: str,
    ) -> None:
        self._append_production_power_records(records)
        self._write_production_power_summary(
            "on_confirmed",
            action_id=action_id,
            identity=asdict(identity),
            result=asdict(result),
        )
        if self._preparation.state is not PreparationState.PREPARING_POWER:
            return
        self._preparation.confirm_power()
        self.update_fixture_state(
            "power",
            state=FixtureRuntimeState.READY,
            evidence=tr("Identity and voltage confirmed"),
        )

    @Slot(bool, object, object, str)
    def _on_power_cycle_output(self, enabled, result, records, action_id) -> None:
        self._append_production_power_records(records)
        self._write_production_power_summary("on_confirmed" if enabled else "off_confirmed",
                                             action_id=action_id, result=asdict(result))
        self.update_fixture_state(
            "power", state=FixtureRuntimeState.READY if enabled else FixtureRuntimeState.CONFIGURED,
            evidence=tr("Output on confirmed") if enabled else tr("Output off confirmed"),
        )

    @Slot(str)
    def _render_preparation_wait(self, reason: str) -> None:
        self._show_execution_message(
            tr("Power ready. Waiting for device admission: {reason}", reason=reason),
            title_source=tr_source("Waiting for online devices"),
        )

    @Slot(object)
    def _advance_powered_start(self, participants) -> None:
        try:
            self._commit_batch_start(participants)
        except (RuntimeError, ValueError) as exc:
            worker = self._production_power_worker
            worker.request_verified_off()
            if not worker.wait(10000):
                self._footer_status.setText(tr("Automatic execution stopped: {reason}", reason=str(exc)))
                return
            self._on_production_power_failed(str(exc), (), worker.action_id)

    def _start_execution(self, participants, *, vehicle_phase: bool = False) -> None:
        execution = ProductionExecution(self._store, self._batch["batch_id"], self._recipe,
                                        self._fleet, participants,
                                        fixture_telemetry=self._fixture_runtime.telemetry,
                                        parent=self)
        self._execution = execution
        execution.changed.connect(self._render_execution)
        execution.failed.connect(self._execution_failed)
        execution.finished.connect(self._execution_finished)
        if vehicle_phase:
            # 车载段无摇摆台动作：工位就绪由操作员门禁给出，启动确认走车辆路径。
            execution.fixture_ready = lambda: (True, "")
            execution.hardware_requested.connect(self._on_vehicle_hardware_requested)
        elif self._production_power_worker is not None:
            self._execution_hardware = ProductionExecutionHardware(
                execution, self._fixture_runtime, self._resolved_configuration,
                self._production_power_worker, self._batch_output_dir, parent=self,
            )
        execution.start()

    @staticmethod
    def _execution_reason(reason: str) -> str:
        key, separator, details = reason.partition(": ")
        labels = {
            "MS-6222 shares the controlled power cycle": tr("MS-6222 shares the controlled power cycle"),
            "Waiting for power output-on confirmation": tr("Waiting for power output-on confirmation"),
            "Waiting for MS-6222 recovery after power-on": tr("Waiting for MS-6222 recovery after power-on"),
            "Waiting for motion connection": tr("Waiting for motion connection"),
            "Waiting for platform center confirmation": tr("Waiting for platform center confirmation"),
            "Waiting for power output-off confirmation": tr("Waiting for power output-off confirmation"),
            "Waiting for trajectory command send confirmation": tr("Waiting for trajectory command send confirmation"),
            "Waiting for trajectory steady phase": tr("Waiting for trajectory steady phase"),
            "Waiting for motion feedback confirmation": tr("Waiting for motion feedback confirmation"),
            "Waiting for fresh MS-6222 reference": tr("Waiting for fresh MS-6222 reference"),
            "Waiting for valid MS-6222 reference": tr("Waiting for valid MS-6222 reference"),
            "MS-6222 attitude uncertainty exceeds profile limit": tr("MS-6222 attitude uncertainty exceeds profile limit"),
            "MS-6222 attitude uncertainty contains invalid values": tr("MS-6222 attitude uncertainty contains invalid values"),
            "MS-6222 navigation state is unsuitable for reference": tr("MS-6222 navigation state is unsuitable for reference"),
            "MS-6222 attitude contains invalid values": tr("MS-6222 attitude contains invalid values"),
            "Waiting for prerequisite evidence": tr("Waiting for prerequisite evidence"),
            "Waiting for firmware readback": tr("Waiting for firmware readback"),
            "Waiting for parameter table readback": tr("Waiting for parameter table readback"),
            "Recipe requires per-device parameter expectations": tr("Recipe requires per-device parameter expectations"),
            "Waiting for fixture preparation and start confirmation": tr("Waiting for fixture preparation and start confirmation"),
            "Waiting for online devices": tr("Waiting for online devices"),
            "Waiting for device restart and analysis capture": tr("Waiting for device restart and analysis capture"),
            "Device restart and analysis capture preparation timed out": tr("Device restart and analysis capture preparation timed out"),
            "Waiting for initial acquisition confirmation": tr("Waiting for initial acquisition confirmation"),
            "Finishing fixture motion and verifying center": tr("Finishing fixture motion and verifying center"),
            "Vehicle scenario requires operator start and driving evidence": tr("Vehicle scenario requires operator start and driving evidence"),
            "Navigation summary requires navigation analysis criteria": tr("Navigation summary requires navigation analysis criteria"),
        }
        label = labels.get(key, key)
        return tr("{reason}: {details}", reason=label, details=details) if separator else label

    def _show_execution_message(self, message: str, *, title_source: str = "Test progress") -> None:
        self._progress_panel.show_message(message, clear_progress=True, title_source=title_source)

    @Slot(object)
    def _render_execution(self, progress) -> None:
        if not self._view_active or self._subpages.currentIndex() != 0:
            return
        if self._abort_coordinator.state is not AbortState.IDLE:
            return
        titles = {test_id: label for test_id, label in self._TEST_DEFS}
        title_source = titles.get(progress.test_id, progress.test_id)
        title = tr(title_source)
        if progress.state in {ProductionRunState.INCOMPLETE, ProductionRunState.ABORTED}:
            source = tr_source("Incomplete") if progress.state is ProductionRunState.INCOMPLETE else tr_source("Aborted")
            self._render_steps()
            self._show_execution_message(self._execution_reason(progress.reason), title_source=source)
            if self._execution is not None:
                for session in self._execution.sessions.values():
                    self.update_device_slot(session.slot, current_test=title, result=tr(source))
            self._refresh_batch_state()
            self._update_start_gate()
            return
        phase_sources = {
            "preparing": tr_source("Preparing"), "converging": tr_source("Converging"),
            "effective_observation": tr_source("Effective observation"), "finishing": tr_source("Finishing"),
        }
        phases = {key: tr(value) for key, value in phase_sources.items()}
        def duration(seconds):
            value = max(0, math.ceil(seconds))
            return f"{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}"
        self._render_steps()
        node_ids = self._progress_panel.node_ids
        ordinal = node_ids.index(progress.test_id) + 1 if progress.test_id in node_ids else progress.ordinal
        self._progress_panel.set_progress(
            title_source=title_source, phase_source=phase_sources[progress.phase.value],
            ordinal=ordinal, total=len(node_ids), elapsed_s=progress.elapsed_s,
            remaining_s=progress.remaining_s, reason=self._execution_reason(progress.reason),
        )
        if self._store is not None:
            for session in self._execution.sessions.values():
                own = progress.effective_s.get(session.serial_number, 0)
                metrics = self._execution.tracking_metrics(session.serial_number)
                if session.serial_number in self._execution.snr_failures:
                    reasons = self._execution.snr_failures[session.serial_number]["reasons"]
                    device_result = tr("Tracking criteria failed") + ": " + "; ".join(tracking_reason_text(r) for r in reasons)
                elif session.serial_number in self._execution.evidence_gaps:
                    device_result = tr("Incomplete evidence")
                elif metrics:
                    device_result = tr("Interruptions: {count} | Longest: {longest}s | Total: {total}s",
                        count=metrics.get("interruption_count", 0),
                        longest=f"{metrics.get('interruption_max_s', 0):.2f}",
                        total=f"{metrics.get('interruption_total_s', 0):.2f}")
                    if metrics.get("recovery_pending"):
                        device_result += " · " + tr("Confirming tracking recovery")
                else:
                    device_result = tr("Positive SNR duration: {duration}", duration=duration(own))
                device_phase = phases[self._execution.device_phase(session.serial_number).value]
                self.update_device_slot(session.slot, current_test=f"{title} · {device_phase}", result=device_result)
        if progress.state is ProductionRunState.COMPLETE:
            self._show_execution_message(
                tr("Automatic steps finished. Remaining report items require analysis evidence."),
                title_source=tr_source("Tests finished; awaiting reports"),
            )
        self._update_start_gate()

    @Slot(str)
    def _execution_failed(self, reason: str) -> None:
        if self._production_power_worker is not None:
            self._production_power_worker.request_verified_off()
        self._stop_execution()
        self._footer_status.setText(tr("Automatic execution stopped: {reason}", reason=reason))
        self.append_event(self._footer_status.text())
        if self._production_power_worker is not None:
            self._production_power_worker.request_verified_off()
        self._batch_coordinator.mark_incomplete()
        if self._fleet is not None:
            self._fleet.finalize_recordings(wait_for_capture_restore=False)
        self._batch_state.setText(tr("Batch incomplete"))
        if self._batch is not None:
            self._start_archive_worker(
                {
                    "batch_id": self._batch["batch_id"],
                    "service": self._report_service(),
                    "batch_status": self._batch["status"],
                }
            )
        self._show_execution_message(self._footer_status.text(), title_source=tr_source("Batch incomplete"))
        self._render_steps()
        self._update_start_gate()

    @Slot()
    def _execution_finished(self) -> None:
        self._stop_execution()
        self._render_execution(self._execution.run.progress)
        self._show_execution_message(
            tr("Automatic steps finished. Restoring capture and archiving evidence."),
            title_source=tr_source("Finalizing batch"),
        )
        self._batch_state.setText(tr("Finalizing batch"))
        self._footer_status.setText(self._execution_summary.text())
        self.append_event(self._execution_summary.text())
        # complete_batch owns the affirmative normal shutdown order:
        # capture restore + recorder close, verified power off, terminal batch,
        # then the background report freeze. Power must not be removed before
        # capture restore.
        self._begin_complete_batch(source="auto")

    def _stop_execution(self) -> None:
        self._preparation.stop()
        if self._execution is not None:
            self._execution.stop()
        if self._execution_hardware is not None:
            self._execution_hardware.close()
            self._execution_hardware = None

    def _on_production_power_failed(
        self,
        details: str,
        records,
        action_id: str,
    ) -> None:
        self._append_production_power_records(records)
        self._write_production_power_summary(
            "failed",
            action_id=action_id,
            details=details,
        )
        if self._abort_coordinator.active:
            self._abort_coordinator.fail_power(details)
            return
        if self._execution is not None and self._execution.run.state in {
            ProductionRunState.WAITING_PREREQUISITE, ProductionRunState.ARMED, ProductionRunState.RUNNING,
        }:
            self._execution.run.fail_shared_step(details, evidence={"power_action_id": action_id})
        self._stop_execution()
        running = bool(
            self._batch is not None
            and self._batch.get("status") == BatchStatus.RUNNING.value
        )
        if running:
            self._batch_coordinator.mark_incomplete()
            if self._fleet is not None:
                self._fleet.finalize_recordings(wait_for_capture_restore=False)
            try:
                self._archive_terminal_batch()
            except (ResultStoreError, OSError, ValueError) as exc:
                self.append_event(
                    tr("Evidence archive failed: {details}", details=str(exc))
                )
        else:
            self._batch_coordinator.cancel_start_reservation()
        self._footer_status.setText(
            tr("Production power failed: {details}", details=details)
        )
        self.append_event(self._footer_status.text())
        self.update_fixture_state("power", state=FixtureRuntimeState.FAULT, evidence=details)
        self._show_execution_message(self._footer_status.text(), title_source=tr_source("Failed"))
        self._render_steps()
        self._release_production_power_worker()
        self._update_start_gate()

    def _on_production_power_stopped(
        self,
        result,
        records,
        action_id: str,
    ) -> None:
        self._append_production_power_records(records)
        self._write_production_power_summary(
            "off_confirmed",
            action_id=action_id,
            result=None if result is None else asdict(result),
        )
        self.update_fixture_state("power", state=FixtureRuntimeState.CONFIGURED,
                                  evidence=tr("Output off confirmed") if result is not None else "")
        if self._abort_coordinator.active:
            self._abort_coordinator.confirm_power_off()
            return
        self._release_production_power_worker()

    def _append_production_power_records(self, records) -> None:
        if self._batch_output_dir is None:
            return
        typed = tuple(records)
        new_records = typed[self._power_record_count :]
        if not new_records:
            return
        target = self._batch_output_dir / "power_supply_records.jsonl"
        with target.open("a", encoding="utf-8") as stream:
            for record in new_records:
                stream.write(
                    json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n"
                )
        self._power_record_count = len(typed)

    def _write_production_power_summary(self, status: str, **details) -> None:
        if self._batch_output_dir is None:
            return
        target = self._batch_output_dir / "power_supply_summary.json"
        payload = {
            "status": status,
            "batch_id": "" if self._batch is None else self._batch.get("batch_id", ""),
            **details,
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _release_production_power_worker(self) -> None:
        worker = self._production_power_worker
        if worker is not None and worker.isRunning() and not worker.wait(10000):
            raise ResultStoreError("production power worker is still stopping")
        self._production_power_worker = None
        if worker is not None:
            worker.deleteLater()

    def _apply_external_ins_gates(
        self, participants: tuple[DeviceSession, ...]
    ) -> None:
        if (
            self._store is None
            or self._batch is None
            or "external_ins" not in enabled_production_tests(self._recipe)
        ):
            return
        attempts = {
            attempt["serial_number"]: attempt
            for attempt in self._store.list_attempts(self._batch["batch_id"])
            if attempt["test_id"] == "external_ins"
            and attempt["status"] == AttemptStatus.PENDING.value
        }
        for session in participants:
            if session.serial_number not in self._participant_serials:
                continue
            attempt = attempts.get(session.serial_number)
            if attempt is None:
                continue
            gate = evaluate_external_ins(session.product_store.snapshot())
            if gate.applicability != ExternalInsApplicability.NOT_APPLICABLE:
                continue
            self._batch_coordinator.skip_attempt(
                int(attempt["attempt_id"]),
                result={
                    "verdict": "not_applicable",
                    "reason": gate.reason,
                },
            )
            self.append_event(
                tr(
                    "External INS test is not applicable to {serial}: {reason}",
                    serial=session.serial_number,
                    reason=gate.reason,
                )
            )

        self._render_steps()

    def _on_abort_batch(self) -> None:
        try:
            self.abort_batch()
        except (ResultStoreError, RuntimeError, ValueError) as exc:
            message = tr("Cannot abort batch: {details}", details=str(exc))
            self._footer_status.setText(message)
            self.status_message.emit(message, 8000)

    @Slot(object)
    def _render_abort(self, _state) -> None:
        if self._abort_coordinator.active:
            message = tr("Aborting: stopping tests and verifying fixture shutdown...")
            self._show_execution_message(message, title_source=tr_source("Aborting"))
            self._footer_status.setText(message)
            self._batch_state.setText(tr("Aborting"))
        elif self._abort_coordinator.state is AbortState.ABORTED:
            message = tr("Batch aborted by operator.")
            self._show_execution_message(message, title_source=tr_source("Batch aborted"))
            self._footer_status.setText(message)
            self._batch_state.setText(tr("Batch aborted"))
        elif self._abort_coordinator.state is AbortState.FAILED:
            message = tr("Abort cleanup failed: {details}", details=self._abort_coordinator.error)
            self._show_execution_message(message, title_source=tr_source("Abort cleanup failed"))
            self._footer_status.setText(message)
            self._batch_state.setText(tr("Abort cleanup failed"))
        self._update_start_gate()

    @Slot(bool, str)
    def _on_abort_settled(self, successful: bool, reason: str) -> None:
        if successful:
            self._execution_hardware = None
            self._release_production_power_worker()
            self._finish_abort_batch()
        else:
            self._record_batch_event("abort_cleanup_failed", {"details": reason})
            message = tr("Abort cleanup failed: {details}", details=reason)
            self._footer_status.setText(message)
            self._show_execution_message(message)
            self._batch_state.setText(tr("Abort cleanup failed"))
            self.append_event(message)
            self._update_start_gate()

    def _on_environment_toggled(self, checked: bool) -> None:
        self._set_environment_inputs_enabled(bool(checked))

    def _set_environment_inputs_enabled(self, enabled: bool) -> None:
        for widget in (
            self._weather_label, self._weather_combo,
            self._ambient_temperature_label, self._ambient_temperature_spin,
            self._ambient_humidity_label, self._ambient_humidity_spin,
        ):
            widget.setEnabled(enabled)

    def _batch_environment(self) -> dict:
        if not self._environment_check.isChecked():
            return {"weather": "", "ambient_temperature_c": None,
                    "ambient_humidity_percent": None}
        return {
            "weather": str(self._weather_combo.currentData() or ""),
            "ambient_temperature_c": float(self._ambient_temperature_spin.value()),
            "ambient_humidity_percent": float(self._ambient_humidity_spin.value()),
        }

    def _on_resume_batch(self) -> None:
        from satellite_debug_tool.core.production.result_store import scan_suspended_batches

        storage_root = str(self._settings.get("paths.production_dir") or "")
        if not storage_root:
            self._footer_status.setText(tr("Select a production output folder first"))
            return
        candidates = scan_suspended_batches(storage_root)
        if not candidates:
            QMessageBox.information(
                self, tr("Resume batch"), tr("No suspended batch is waiting.")
            )
            return
        chosen = _SuspendedBatchDialog(candidates, parent=self).choose()
        if chosen is None:
            return
        try:
            self.resume_batch(Path(chosen["output_dir"]), chosen["batch_id"])
        except (ResultStoreError, RecipeValidationError, OSError, ValueError) as exc:
            message = tr("Cannot resume batch: {details}", details=str(exc))
            self._footer_status.setText(message)
            self.status_message.emit(message, 8000)

    def resume_batch(self, output_dir: Path, batch_id: str) -> None:
        """接续挂起批次：读回批次事实、配方与工位配置，进入车载段。"""

        store = ProductionResultStore(output_dir / "batch.sqlite3")
        batch = self._batch_coordinator.adopt_suspended(
            store=store, batch_id=batch_id, output_dir=output_dir
        )
        try:
            self._recipe = ProductionRecipe.from_mapping(batch["recipe_json"])
            configuration_file = output_dir / "production_configuration.json"
            if configuration_file.is_file():
                snapshot = json.loads(configuration_file.read_text(encoding="utf-8"))
                self._resolved_configuration = ResolvedProductionConfiguration.from_snapshot(snapshot)
        except Exception:
            self._batch_coordinator.close()
            self._recipe = None
            self._resolved_configuration = None
            raise
        self._registered_serials = {
            item["serial_number"] for item in store.list_devices(batch_id)
        }
        self._registered_hardware_identity = {}
        frozen = {
            item["serial_number"]: (item.get("device_uid") or "", item.get("mac_address") or "")
            for item in store.list_devices(batch_id)
        }
        self._frozen_identities = frozen
        self._identity_checks = {serial: "pending" for serial in frozen}
        self._batch_state.setText(tr("Bench phase finished; batch suspended for vehicle tests"))
        self._render_batch_identity_checks()
        self._update_start_gate()
        message = tr(
            "Resumed batch {batch_id}; connect the frozen participants to verify identity",
            batch_id=batch_id,
        )
        self._footer_status.setText(message)
        self.append_event(message)

    def _on_vehicle_hardware_requested(self, request) -> None:
        """车辆工况启动确认：行驶项立即确认；动态上电项等待离线→回在线。"""

        execution = self._execution
        if execution is None:
            return
        test_id = str(request.test_id)
        if test_id == "locked_drive":
            execution.confirm_hardware(test_id, {"vehicle_start": "operator_gate"})
            return
        if test_id != "power_on_drive":
            return
        execution.prepare_power_cycle()
        self._vehicle_power_await_off = True
        self._vehicle_power_await_on = False
        self.append_event(tr(
            "Power the device off, start driving, then power it on; waiting for power cycle"
        ))

    def _maybe_confirm_vehicle_power_cycle(self) -> None:
        execution = self._execution
        if execution is None or not getattr(execution, "_power_cycle_pending", False):
            return
        sessions = list(execution.sessions.values())
        if not sessions:
            return
        all_online = all(session.is_online() for session in sessions)
        all_offline = all(not session.is_online() for session in sessions)
        if getattr(self, "_vehicle_power_await_off", False) and all_offline:
            self._vehicle_power_await_off = False
            self._vehicle_power_await_on = True
            return
        if getattr(self, "_vehicle_power_await_on", False) and all_online:
            self._vehicle_power_await_on = False
            execution.confirm_hardware(execution.run.current_test_id, {
                "power_off": "operator",
                "output": "operator",
                "power_restored_monotonic_ns": time.monotonic_ns(),
            })

    def _on_vehicle_gate(self) -> None:
        """操作员门禁：身份续验通过后启动车载段（固定时长姿态窗口）。"""

        try:
            self._batch_coordinator.resume_vehicle_phase()
        except ResultStoreError as exc:
            message = tr("Cannot start vehicle tests: {details}", details=str(exc))
            self._footer_status.setText(message)
            self.status_message.emit(message, 8000)
            return
        participants = [
            session for session in (self._fleet.sessions() if self._fleet else ())
            if session.serial_number in self._registered_serials
        ]
        self._vehicle_gate_button.hide()
        self._vehicle_power_await_off = False
        self._vehicle_power_await_on = False
        self._start_execution(participants, vehicle_phase=True)
        message = tr("Vehicle phase started; window runs the frozen recipe duration")
        self._footer_status.setText(message)
        self.append_event(message)

    def _render_batch_identity_checks(self) -> None:
        checks = getattr(self, "_identity_checks", {})
        if not checks:
            return
        detail = "；".join(f"{serial}：{state}" for serial, state in sorted(checks.items()))
        self.append_event(tr("Participant identity check: {details}", details=detail))

    def _on_complete_batch(self) -> None:
        self._begin_complete_batch(source="manual")

    def _begin_complete_batch(self, *, source: str) -> None:
        """Run the synchronous shutdown segment, then archive reports in the background."""

        try:
            context = self.complete_batch()
        except (ResultStoreError, RuntimeError, OSError, ValueError) as exc:
            if source == "manual":
                message = tr("Cannot complete batch: {details}", details=str(exc))
                self._footer_status.setText(message)
                self.status_message.emit(message, 8000)
            else:
                message = tr("Batch finalization requires attention: {details}", details=str(exc))
                self._footer_status.setText(message)
                self.append_event(message)
                self._batch_state.setText(tr("Archive retry required"))
                self._show_execution_message(message, title_source=tr_source("Archive retry required"))
            return
        self._complete_source = source
        if context["batch_status"] == BatchStatus.SUSPENDED.value:
            message = tr(
                "Bench phase finished; batch suspended for vehicle tests"
            )
            self._footer_status.setText(message)
            self.append_event(message)
            if source == "manual":
                self.status_message.emit(message, 8000)
            self._update_start_gate()
            return
        self._start_archive_worker(context)

    def complete_batch(self):
        """Finalize recordings, stop power, and commit the terminal batch state.

        The heavy report archive runs in a background worker started by the
        caller; this method only owns the affirmative shutdown order.
        """

        if self._execution is not None and self._execution.active:
            raise ResultStoreError("automatic execution is active")
        if self._archive_worker is not None and self._archive_worker.isRunning():
            raise ResultStoreError("report generation is already running")
        if self._fleet is not None:
            self._fleet.finalize_recordings()
            pending = [
                session.serial_number or str(session.endpoint)
                for session in self._fleet.sessions()
                if session.recording_armed
            ]
            if pending:
                raise ResultStoreError(
                    "device evidence recording is still finalizing: " + ", ".join(pending)
                )
        worker = self._production_power_worker
        if worker is not None:
            worker.request_verified_off()
            if not worker.wait(10000):
                raise ResultStoreError("production power did not stop within 10 seconds")
            self._release_production_power_worker()
        batch = self._batch_coordinator.complete()
        if batch["status"] == BatchStatus.SUSPENDED.value:
            self._batch_state.setText(tr("Bench phase finished; batch suspended for vehicle tests"))
        elif batch["status"] == BatchStatus.COMPLETED.value:
            self._batch_state.setText(tr("Batch completed"))
        else:
            self._batch_state.setText(tr("Batch incomplete"))
        self._fixture_page_button.setEnabled(True)
        self._power_page_button.setEnabled(True)
        self._update_start_gate()
        return {
            "batch_id": batch["batch_id"],
            "service": self._report_service(),
            "batch_status": batch["status"],
        }

    def _report_service(self) -> ProductionReportService:
        if self._store is None or self._batch_output_dir is None:
            raise ResultStoreError("no batch result store is active")
        # The batch directory is the primary archive owner. An optional global
        # report directory is an export target, never the only copy.
        output_root = str(self._batch_output_dir / "reports")
        branding_value = self._settings.get("production.report_branding", {})
        branding = ReportBranding.from_mapping(
            branding_value if isinstance(branding_value, dict) else {}
        )
        return ProductionReportService(
            self._store,
            batch_output_dir=self._batch_output_dir,
            report_root=output_root,
            branding=branding,
        )

    def _start_archive_worker(self, context: dict) -> None:
        """Archive device reports in a background thread with typed progress."""

        service = context["service"]
        batch_id = context["batch_id"]
        worker = ProductionCleanupWorker(
            lambda progress: service.archive_batch(batch_id, progress=progress),
            parent=self,
            report_progress=True,
        )
        worker.progress.connect(self._on_archive_progress)
        worker.finished.connect(lambda: self._on_archive_finished(worker))
        worker.batch_status = context.get("batch_status", "")
        worker.manual_source = self._complete_source == "manual"
        self._archive_worker = worker
        self._complete_button.setEnabled(False)
        self._footer_status.setText(tr("Generating device reports..."))
        worker.start()

    @Slot(object)
    def _on_archive_progress(self, item: object) -> None:
        done, total, serial, stage = item
        self._footer_status.setText(
            tr(
                "Generating device report {serial} ({done}/{total}): {stage}",
                serial=str(serial),
                done=int(done),
                total=int(total),
                stage=self._archive_stage_text(str(stage)),
            )
        )

    @staticmethod
    def _archive_stage_text(stage: str) -> str:
        return {
            "analyzing": tr("Analyzing recordings"),
            "packaging": tr("Packaging evidence"),
            "document": tr("Writing report document"),
        }.get(stage, stage)

    def _on_archive_finished(self, worker: ProductionCleanupWorker) -> None:
        if self._archive_worker is worker:
            self._archive_worker = None
        worker.deleteLater()
        error = worker.error
        if error:
            message = tr("Evidence archive failed: {details}", details=error)
            self._footer_status.setText(message)
            self.append_event(message)
            self.status_message.emit(message, 8000)
            self._update_start_gate()
            return
        artifacts = list(worker.result or [])
        self._write_batch_summary(artifacts)
        if worker.batch_status == BatchStatus.COMPLETED.value:
            message = tr(
                "Batch completed; generated {count} device archive(s).", count=len(artifacts)
            )
        else:
            message = tr("Device report archives are ready.")
        self._footer_status.setText(message)
        self.append_event(message)
        if worker.manual_source:
            self.status_message.emit(message, 6000)
        self._complete_source = ""
        self._update_start_gate()

    def _write_batch_summary(self, artifacts) -> None:
        if self._batch is None:
            raise ResultStoreError("no batch is active")
        if self._batch_output_dir is None:
            return
        summary = {
            "schema": "satellite-debug-tool/production-batch-summary",
            "schema_version": 1,
            "batch_id": self._batch["batch_id"],
            "status": self._batch["status"],
            "reports": [
                {
                    "serial_number": item.serial_number,
                    "automatic_verdict": item.automatic_verdict,
                    "final_verdict": item.final_verdict,
                    "document_path": "" if item.document_path is None else str(item.document_path),
                    "evidence_path": str(item.evidence_path),
                    "manifest_path": str(item.manifest_path),
                }
                for item in artifacts
            ],
        }
        target = self._batch_output_dir / "batch_summary.json"
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)

    def _on_review_report(self) -> None:
        if self._store is None or self._batch is None:
            return
        candidates = []
        for device in self._store.list_devices(self._batch["batch_id"]):
            records = self._store.list_report_artifacts(
                self._batch["batch_id"], device["serial_number"]
            )
            if any(item["status"] == "generated_v1" for item in records) and not any(
                item["version"] == 2 for item in records
            ):
                candidates.append(device["serial_number"])
        if not candidates:
            QMessageBox.information(
                self, tr("Review production report"), tr("No V1 report is waiting for review.")
            )
            return
        dialog = ReportReviewDialog(
            candidates,
            default_reviewer=str(
                self._settings.get("production.last_reviewer", "") or ""
            ),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            artifact = self._report_service().review(
                self._batch["batch_id"],
                dialog.serial_number,
                reviewer=dialog.reviewer,
                final_verdict=dialog.final_verdict,
                reason=dialog.reason,
            )
            self._settings.set("production.last_reviewer", dialog.reviewer)
            self._settings.persist_preferences()
        except (ResultStoreError, OSError, ValueError) as exc:
            QMessageBox.critical(self, tr("Review production report"), str(exc))
            return
        QMessageBox.information(
            self,
            tr("Review production report"),
            tr("V2 report generated: {path}", path=str(artifact.document_path)),
        )
        self._update_start_gate()

    def abort_batch(self) -> dict:
        if self._abort_coordinator.active:
            return dict(self._batch)
        if self._batch is None or self._batch.get("status") not in {BatchStatus.READY.value, BatchStatus.RUNNING.value}:
            raise ResultStoreError("abort requires a ready or running batch")
        hardware = self._execution_hardware
        if hardware is not None:
            hardware.quiesce()
        def stop_progress() -> None:
            self._preparation.stop()
            if self._execution is not None:
                self._execution.stop()
        finish_recordings = self._fleet.prepare_recording_stop() if self._fleet is not None else None
        def cleanup() -> None:
            if hardware is not None:
                hardware.close()
            if finish_recordings is not None:
                finish_recordings()
        self._abort_coordinator.request(
            stop_progress=stop_progress, power=self._production_power_worker,
            cleanup=cleanup,
        )
        return dict(self._batch)

    def _finish_abort_batch(self) -> dict:
        batch = self._batch_coordinator.abort()
        if self._fleet is not None:
            self._fleet.finalize_recordings(wait_for_capture_restore=False)
        service = self._report_service()
        batch_id = batch["batch_id"]
        self._abort_archive = ProductionCleanupWorker(lambda: service.archive_batch(batch_id), parent=self)
        self._abort_archive.finished.connect(self._on_abort_archive_finished)
        self._abort_archive.start()
        self._fixture_page_button.setEnabled(True)
        self._power_page_button.setEnabled(True)
        self._batch_state.setText(tr("Batch aborted"))
        message = tr("Batch aborted by operator.")
        self._render_steps()
        if self._execution is not None:
            for session in self._execution.sessions.values():
                self.update_device_slot(session.slot, current_test=tr("Aborted"), result=tr("Aborted"))
        self._footer_status.setText(message)
        self._show_execution_message(message)
        self.append_event(message)
        self._update_start_gate()
        return dict(batch)

    @Slot()
    def _on_abort_archive_finished(self) -> None:
        if self._abort_archive is not None and self._abort_archive.error:
            message = tr("Evidence archive failed: {details}", details=self._abort_archive.error)
            self.append_event(message)
            self._footer_status.setText(message)
            self.status_message.emit(message, 8000)
        self._update_start_gate()

    def _on_identity_conflict(self, details: str) -> None:
        message = tr("Identity conflict: {details}", details=details)
        self._footer_status.setText(message)
        self.append_event(message)
        self._record_batch_event("identity_conflict", {"details": details})

    def _on_endpoint_migrated(
        self,
        serial_number: str,
        old_endpoint: str,
        new_endpoint: str,
    ) -> None:
        message = tr(
            "Device endpoint changed: {serial}, {old_endpoint} -> {new_endpoint}",
            serial=serial_number,
            old_endpoint=old_endpoint,
            new_endpoint=new_endpoint,
        )
        self.append_event(message)
        if self._store is None or self._batch is None:
            return
        host, separator, port_text = new_endpoint.rpartition(":")
        if not separator:
            self._on_fleet_error(f"invalid migrated endpoint: {new_endpoint}")
            return
        try:
            self._store.update_device_endpoint(
                self._batch["batch_id"],
                serial_number,
                endpoint_ip=host,
                endpoint_port=int(port_text),
            )
        except (ResultStoreError, ValueError) as exc:
            self._on_fleet_error(str(exc))

    def _on_endpoint_rejected(self, endpoint: str) -> None:
        message = tr("Device capacity exceeded: {endpoint}", endpoint=endpoint)
        self._footer_status.setText(message)
        self.append_event(message)
        self._record_batch_event("endpoint_rejected", {"endpoint": endpoint})

    def _on_fleet_status_changed(self, status: str) -> None:
        if status.startswith("listening:"):
            port = status.partition(":")[2]
            message = tr("Discovering on UDP port {port}", port=port)
        elif status == "stopped":
            message = tr("Device discovery stopped")
        elif status == "finalize_pending":
            message = tr("Production evidence recording is still finalizing")
        elif status == "release_pending":
            message = tr("Production session ownership is still being released")
        else:
            message = tr("Device discovery failed")
        self._footer_status.setText(message)
        self.append_event(message)
        self._update_start_gate()

    def _on_fleet_error(self, details: str) -> None:
        message = tr("Device discovery error: {details}", details=details)
        self._footer_status.setText(message)
        self.append_event(message)
        self.status_message.emit(message, 8000)
        self._record_batch_event("fleet_error", {"details": details})

    def _record_batch_event(self, event_type: str, payload: dict) -> None:
        try:
            self._batch_coordinator.record_event(event_type, payload)
        except ResultStoreError:
            return

    def shutdown(self) -> bool:
        if self._abort_coordinator.active or (self._abort_archive is not None and self._abort_archive.isRunning()):
            return False
        if self._archive_worker is not None and self._archive_worker.isRunning():
            return False
        if self._production_power_worker is not None:
            self._production_power_worker.request_verified_off()
        self._stop_execution()
        self.deactivate_view()
        self._fixture_host.shutdown()
        self._ms6222_host.shutdown()
        self._power_host.shutdown()
        power_worker = self._production_power_worker
        if power_worker is not None:
            power_worker.request_verified_off()
            if not power_worker.wait(10000):
                return False
            self._production_power_worker = None
        if self._fleet is not None:
            self._fleet.stop()
            if not self._fleet.shutdown_ready:
                return False
        self._batch_coordinator.close()
        return True

    def confirm_shutdown(self) -> bool:
        if self._abort_coordinator.active or (self._abort_archive is not None and self._abort_archive.isRunning()):
            self._footer_status.setText(tr("Batch cleanup is active; waiting for evidence finalization."))
            return False
        if self._archive_worker is not None and self._archive_worker.isRunning():
            self._footer_status.setText(tr("Device report generation is running; retry exit after it completes"))
            return False
        if (
            self._fixture_debug is not None
            and not self._fixture_debug.confirm_shutdown()
        ):
            return False
        if (
            self._power_debug is not None
            and not self._power_debug.confirm_shutdown()
        ):
            return False
        if (
            self._ms6222_debug is not None
            and not self._ms6222_debug.confirm_shutdown()
        ):
            return False
        fleet = self._fleet
        if fleet is None:
            return True
        fleet.stop()
        if fleet.shutdown_ready:
            return True
        if fleet.recording_finalize_pending:
            message = tr(
                "Production evidence recording is still finalizing; retry exit after it completes"
            )
        else:
            message = tr(
                "Production session ownership is still active; finish the operation and retry exit"
            )
        self._footer_status.setText(message)
        self.status_message.emit(message, 8000)
        return False

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = theme
        palette = S.palette(theme)
        self.setStyleSheet(
            f"ProductionWorkspace {{ background: {palette['bg']}; }}"
            f"#productionTitle {{ color: {palette['text']}; font-size: 16px; font-weight: 700; }}"
            f"#productionState {{ color: {palette['accent_2']}; font-weight: 600; "
            f"padding: 3px 9px; border: 1px solid {palette['border_2']}; border-radius: 5px; }}"
            f"#productionPreviewNotice {{ color: {palette['text_muted']}; font-size: 11px; padding: 1px 0; }}"
            f"#productionChartsScroll, #productionBatchScroll {{ background: {palette['bg']}; border: 0; }}"
            f"#productionRecipeStatus, #productionFooterStatus {{ color: {palette['text_2']}; }}"
        )
        self._progress_panel.set_theme(theme, _scale)
        self._fixture_strip.set_theme(theme, _scale)
        for panel in (*self._snr_panels.values(), *self._pending_snr_panels.values()):
            panel.set_theme(theme)
        self._fixture_host.set_theme(theme, _scale)
        self._ms6222_host.set_theme(theme, _scale)
        self._power_host.set_theme(theme, _scale)

    def _refresh_batch_state(self) -> None:
        abort = self._abort_coordinator
        if abort.active:
            source = "Aborting"
        elif abort.state is AbortState.FAILED:
            source = "Abort cleanup failed"
        elif self._batch is None:
            source = "No batch"
        elif (self._batch["status"] == BatchStatus.RUNNING.value and self._execution is not None
              and self._execution.run.state is ProductionRunState.COMPLETE):
            source = "Tests finished; awaiting reports"
        else:
            source = {
                BatchStatus.DRAFT.value: "No batch", BatchStatus.READY.value: "Batch ready",
                BatchStatus.RUNNING.value: "Batch running", BatchStatus.COMPLETED.value: "Batch completed",
                BatchStatus.INCOMPLETE.value: "Batch incomplete", BatchStatus.ABORTED.value: "Batch aborted",
            }[self._batch["status"]]
        self._batch_state.setText(tr(source))

    def retranslate_ui(self) -> None:
        self._batch_page_button.setText(tr("Batch test"))
        self._fixture_page_button.setText(tr("Motion platform test"))
        self._ms6222_page_button.setText(tr("MS-6222 test"))
        self._power_page_button.setText(tr("Power supply diagnostics"))
        self._tcp_shell_button.setText(tr("Shell..."))
        self._title.setText(tr("Automated testing"))
        self._preview_notice.setText(
            tr(
                "Formal automation uses the selected station profile. Devices may be powered off when "
                "creating a batch. Starting automatically prepares power and fixtures and verifies live preflight evidence."
            )
        )
        self._setup_group.setTitle(tr("Batch setup"))
        self._batch_id_label.setText(tr("Batch ID"))
        self._operator_label.setText(tr("Operator"))
        self._recipe_label.setText(tr("Recipe"))
        self._product_template_label.setText(tr("Device model and power"))
        self._device_count_label.setText(tr("Device quantity"))
        self._station_profile_label.setText(tr("Station profile"))
        self._output_label.setText(tr("Storage location"))
        self._recipe_button.setText(tr("Import recipe..."))
        self._output_button.setText(tr("Browse..."))
        self._create_button.setText(tr("Start test"))
        self._refresh_batch_state()
        self._update_snr_group_title()
        self._render_configuration_details()
        self._events_button.setText(tr("Batch events"))
        self._progress_panel.retranslate_ui()
        self._render_steps()
        self._render_fixtures()
        self._start_button.setText(tr("Start batch"))
        self._start_button.setToolTip(
            tr(
                "At least one identified online device with evidence recording ready is required."
            )
        )
        self._abort_button.setText(tr("Abort"))
        self._complete_button.setText(tr("Complete and generate reports"))
        self._next_batch_button.setText(tr("Next batch"))
        self._review_button.setText(tr("Review report..."))
        for panel in (*self._snr_panels.values(), *self._pending_snr_panels.values()):
            panel.retranslate_ui()
        self._refresh_session_rows()
        self._fixture_host.retranslate_ui()
        self._ms6222_host.retranslate_ui()
        self._power_host.retranslate_ui()
        if self._resolved_configuration is not None and self._batch is None:
            self._on_configuration_selection_changed()
        if self._execution is not None:
            self._render_execution(self._execution.run.progress)
        elif self._preparation.state is PreparationState.PREPARING_POWER:
            self._show_execution_message(tr("Production power preparation started."), title_source=tr_source("Preparing"))
        elif self._preparation.state is PreparationState.WAITING_DEVICES:
            _participants, reason = self._evaluate_start_gate()
            self._render_preparation_wait(reason)
        else:
            self._show_execution_message(tr("Automatic execution has not started."))
        if self._abort_coordinator.state is not AbortState.IDLE:
            self._render_abort(self._abort_coordinator.state)
        self._update_start_gate()

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self.shutdown():
            event.ignore()
            return
        super().closeEvent(event)


class _SuspendedBatchDialog(QDialog):
    """列出所有挂起批次；选中后由工作区执行恢复。"""

    def __init__(self, candidates: list[dict], *, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Resume batch"))
        self.setMinimumSize(680, 280)
        layout = QVBoxLayout(self)
        self._table = QTableWidget(len(candidates), 5, self)
        self._table.setHorizontalHeaderLabels([
            tr("Batch ID"), tr("Suspended at"), tr("Operator"),
            tr("Devices"), tr("Pending tests"),
        ])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, item in enumerate(candidates):
            values = (
                item["batch_id"],
                str(item.get("finished_utc") or ""),
                str(item.get("operator") or ""),
                "、".join(item.get("serials", ())),
                "、".join(item.get("pending_tests", ())),
            )
            for column, value in enumerate(values):
                self._table.setItem(row, column, QTableWidgetItem(str(value)))
        layout.addWidget(self._table)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Resume"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._candidates = candidates

    def choose(self) -> Optional[dict]:
        if not self.exec():
            return None
        row = self._table.currentRow()
        if row < 0 or row >= len(self._candidates):
            return None
        return self._candidates[row]
