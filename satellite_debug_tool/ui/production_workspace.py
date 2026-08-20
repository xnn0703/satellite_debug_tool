"""Batch-production workspace for AFD01 pilot builds."""

from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
import re
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.production import (
    AttemptStatus,
    BatchStatus,
    DeviceSession,
    DeviceSessionState,
    ExternalInsApplicability,
    FleetConfigurationError,
    FleetController,
    ProductionRecipe,
    ProductionResultStore,
    RecipeValidationError,
    ResultStoreError,
    evaluate_external_ins,
)
from satellite_debug_tool.i18n import register_translatable, tr, tr_source
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.production_snr_widget import ProductionSnrPanel


class ProductionWorkspace(QWidget):
    """Dense production console with immutable batch setup and four DUT slots."""

    status_message = Signal(str, int)
    batch_created = Signal(str)

    _TEST_DEFS = (
        (
            "firmware_verification",
            tr_source("Firmware verification"),
            tr_source("Automatic"),
            None,
        ),
        (
            "parameter_verification",
            tr_source("Parameter verification"),
            tr_source("Automatic"),
            None,
        ),
        (
            "static_acquisition",
            tr_source("Static acquisition"),
            tr_source("Power and safety"),
            tr_source("At least 60 min"),
        ),
        (
            "locked_rocking",
            tr_source("Locked rocking tracking"),
            tr_source("Motion platform"),
            tr_source("At least 60 min"),
        ),
        (
            "power_on_rocking",
            tr_source("Power-on while rocking"),
            tr_source("Power and motion"),
            tr_source("At least 60 min"),
        ),
        (
            "locked_drive",
            tr_source("Locked driving tracking"),
            tr_source("Operator gate"),
            tr_source("At least 60 min"),
        ),
        (
            "power_on_drive",
            tr_source("Power-on while driving"),
            tr_source("Power and operator"),
            tr_source("At least 60 min"),
        ),
        (
            "gnss",
            tr_source("GNSS performance summary"),
            tr_source("Static and driving windows"),
            tr_source("Shared windows"),
        ),
        (
            "imu_static",
            tr_source("Raw IMU static performance summary"),
            tr_source("Static window"),
            tr_source("Shared windows"),
        ),
        (
            "external_ins",
            tr_source("External INS performance summary"),
            tr_source("Configured external INS and shared windows"),
            tr_source("Shared windows"),
        ),
        (
            "whole_navigation",
            tr_source("Whole-unit navigation summary"),
            tr_source("All enabled scenario windows"),
            tr_source("Shared windows"),
        ),
    )
    _ALWAYS_ENABLED_TEST_IDS = frozenset(
        {"firmware_verification", "parameter_verification"}
    )
    _SCENARIO_TEST_IDS = (
        "static_acquisition",
        "locked_rocking",
        "power_on_rocking",
        "locked_drive",
        "power_on_drive",
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
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._theme = "dark"
        self._recipe: Optional[ProductionRecipe] = None
        self._store: Optional[ProductionResultStore] = None
        self._batch: Optional[dict] = None
        self._batch_output_dir: Optional[Path] = None
        self._fixture_rows: dict[str, int] = {}
        self._test_rows: dict[str, int] = {}
        self._workflow_default_states: dict[str, bool] = {
            test_id: True
            for test_id, _label, _prerequisite, _duration in self._TEST_DEFS
        }
        self._registered_serials: set[str] = set()
        self._registered_hardware_identity: dict[
            str, tuple[str, str, Optional[int]]
        ] = {}
        self._participant_serials: tuple[str, ...] = ()
        self._participant_identity_keys: tuple[str, ...] = ()
        self._ignored_after_start: set[str] = set()
        self._fleet: Optional[FleetController] = None
        self._snr_origin_monotonic_ns = time.monotonic_ns()
        self._snr_panels: dict[int, ProductionSnrPanel] = {}
        self._build_ui()
        self._configure_fleet()
        self._restore_setup_defaults()
        self._session_refresh_timer = QTimer(self)
        self._session_refresh_timer.setInterval(1000)
        self._session_refresh_timer.timeout.connect(self._refresh_session_rows)
        self._snr_refresh_timer = QTimer(self)
        self._snr_refresh_timer.setInterval(200)
        self._snr_refresh_timer.timeout.connect(self._refresh_snr_charts)
        register_translatable(self)

    @property
    def recipe(self) -> Optional[ProductionRecipe]:
        return self._recipe

    @property
    def result_store(self) -> Optional[ProductionResultStore]:
        return self._store

    @property
    def batch(self) -> Optional[dict]:
        return None if self._batch is None else dict(self._batch)

    @property
    def batch_output_dir(self) -> Optional[Path]:
        return self._batch_output_dir

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        title_row = QHBoxLayout()
        self._title = QLabel(tr("Production batch test"))
        self._title.setObjectName("productionTitle")
        self._batch_state = QLabel(tr("No batch"))
        self._batch_state.setObjectName("productionState")
        title_row.addWidget(self._title)
        title_row.addStretch(1)
        title_row.addWidget(self._batch_state)
        root.addLayout(title_row)

        self._preview_notice = QLabel(
            tr(
                "M19-A engineering preview: evidence capture only; not approved for "
                "formal production release."
            )
        )
        self._preview_notice.setObjectName("productionPreviewNotice")
        self._preview_notice.setWordWrap(True)
        root.addWidget(self._preview_notice)

        self._setup_group = QGroupBox(tr("Batch setup"))
        setup = QGridLayout(self._setup_group)
        setup.setContentsMargins(10, 13, 10, 9)
        setup.setHorizontalSpacing(7)
        setup.setVerticalSpacing(6)

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
        self._output_label = QLabel(tr("Output folder"))
        self._output_edit = QLineEdit()
        self._output_edit.setObjectName("productionOutput")
        self._output_button = QPushButton(tr("Browse..."))
        self._output_button.clicked.connect(self._choose_output_dir)
        self._create_button = QPushButton(tr("Create batch"))
        self._create_button.setProperty("variant", "primary")
        self._create_button.clicked.connect(self._on_create_batch)

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
        setup.addWidget(self._create_button, 0, 6)
        setup.addWidget(self._recipe_label, 1, 0)
        setup.addWidget(self._recipe_edit, 1, 1, 1, 2)
        setup.addWidget(self._recipe_button, 1, 3)
        setup.addWidget(self._output_label, 1, 4)
        setup.addWidget(self._output_edit, 1, 5)
        setup.addWidget(self._output_button, 1, 6)
        setup.setColumnStretch(1, 2)
        setup.setColumnStretch(2, 1)
        setup.setColumnStretch(4, 0)
        setup.setColumnStretch(5, 2)
        root.addWidget(self._setup_group)

        content = QSplitter(Qt.Orientation.Vertical)
        content.setChildrenCollapsible(False)
        content.setObjectName("productionContent")

        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)
        body.setObjectName("productionBody")
        self._main_splitter = body

        self._devices_group = QGroupBox(tr("Device SNR ({count}/4)", count=0))
        devices_layout = QGridLayout(self._devices_group)
        devices_layout.setContentsMargins(7, 12, 7, 7)
        devices_layout.setHorizontalSpacing(6)
        devices_layout.setVerticalSpacing(6)
        for slot in range(1, 5):
            panel = ProductionSnrPanel(
                slot,
                S.SIGNAL_PALETTE[slot - 1],
                parent=self._devices_group,
            )
            self._snr_panels[slot] = panel
            devices_layout.addWidget(panel, (slot - 1) // 2, (slot - 1) % 2)
        devices_layout.setRowStretch(0, 1)
        devices_layout.setRowStretch(1, 1)
        devices_layout.setColumnStretch(0, 1)
        devices_layout.setColumnStretch(1, 1)
        body.addWidget(self._devices_group)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        self._workflow_group = QGroupBox(tr("Test workflow"))
        workflow_layout = QVBoxLayout(self._workflow_group)
        workflow_layout.setContentsMargins(7, 12, 7, 7)
        self._workflow_table = self._new_table(len(self._TEST_DEFS), 6)
        self._workflow_table.setObjectName("productionWorkflowTable")
        self._set_workflow_headers()
        self._populate_workflow_rows()
        workflow_layout.addWidget(self._workflow_table)
        right_layout.addWidget(self._workflow_group, 1)

        self._fixtures_group = QGroupBox(tr("Shared fixtures"))
        fixtures_layout = QVBoxLayout(self._fixtures_group)
        fixtures_layout.setContentsMargins(7, 12, 7, 7)
        self._fixture_table = self._new_table(len(self._FIXTURE_DEFS), 3)
        self._fixture_table.setObjectName("productionFixtureTable")
        self._fixture_table.verticalHeader().setDefaultSectionSize(24)
        self._set_fixture_headers()
        self._populate_fixture_rows()
        fixtures_layout.addWidget(self._fixture_table)
        right_layout.addWidget(self._fixtures_group, 1)
        right_layout.setStretch(0, 2)
        right_layout.setStretch(1, 1)
        body.addWidget(right)
        body.setStretchFactor(0, 13)
        body.setStretchFactor(1, 7)
        body.setSizes([850, 450])
        content.addWidget(body)

        self._events_group = QGroupBox(tr("Batch events"))
        self._events_group.setMinimumHeight(52)
        events_layout = QVBoxLayout(self._events_group)
        events_layout.setContentsMargins(7, 12, 7, 7)
        self._event_log = QPlainTextEdit()
        self._event_log.setObjectName("productionEventLog")
        self._event_log.setReadOnly(True)
        self._event_log.setMaximumBlockCount(2000)
        events_layout.addWidget(self._event_log)
        content.addWidget(self._events_group)
        content.setStretchFactor(0, 1)
        content.setStretchFactor(1, 0)
        content.setSizes([680, 70])
        self._content_splitter = content
        root.addWidget(content, 1)

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
        footer.addWidget(self._footer_status, 1)
        footer.addWidget(self._start_button)
        footer.addWidget(self._abort_button)
        root.addLayout(footer)
        self._update_responsive_columns()

    @staticmethod
    def _new_table(rows: int, columns: int) -> QTableWidget:
        table = QTableWidget(rows, columns)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(28)
        table.horizontalHeader().setHighlightSections(False)
        return table

    def _set_workflow_headers(self) -> None:
        self._workflow_table.setHorizontalHeaderLabels(
            [
                tr("No."),
                tr("Test item"),
                tr("Prerequisite"),
                tr("Status"),
                tr("Effective duration"),
                tr("Result"),
            ]
        )
        header = self._workflow_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

    def _update_responsive_columns(self) -> None:
        compact = self.width() < 1500
        self._workflow_table.setColumnHidden(0, compact)
        self._workflow_table.setColumnHidden(2, compact)
        self._workflow_table.setColumnHidden(4, compact)
        header = self._workflow_table.horizontalHeader()
        if compact:
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        else:
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_responsive_columns()

    def _set_fixture_headers(self) -> None:
        self._fixture_table.setHorizontalHeaderLabels(
            [tr("Fixture"), tr("State"), tr("Evidence / note")]
        )
        header = self._fixture_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

    def _populate_workflow_rows(self) -> None:
        for row, (test_id, label, prerequisite, duration_source) in enumerate(
            self._TEST_DEFS
        ):
            self._test_rows[test_id] = row
            self._set_item(self._workflow_table, row, 0, str(row + 1))
            self._set_item(self._workflow_table, row, 1, tr(label))
            self._set_item(self._workflow_table, row, 2, tr(prerequisite))
            duration = "-" if duration_source is None else tr(duration_source)
            self._set_item(self._workflow_table, row, 4, duration)
        self._render_workflow_default_states()

    def _apply_recipe_test_gates(
        self,
        recipe: Optional[ProductionRecipe],
    ) -> None:
        if recipe is None:
            self._workflow_default_states = {
                test_id: True
                for test_id, _label, _prerequisite, _duration in self._TEST_DEFS
            }
            self._render_workflow_default_states()
            return

        tests = recipe.payload.get("tests", {})
        scenario_enabled = any(
            bool(tests.get(test_id, {}).get("enabled", False))
            for test_id in self._SCENARIO_TEST_IDS
        )
        states: dict[str, bool] = {}
        for test_id, _label, _prerequisite, _duration in self._TEST_DEFS:
            if test_id in self._ALWAYS_ENABLED_TEST_IDS:
                states[test_id] = True
            elif test_id == "whole_navigation":
                states[test_id] = scenario_enabled
            else:
                states[test_id] = bool(
                    tests.get(test_id, {}).get("enabled", False)
                )
        self._workflow_default_states = states
        self._render_workflow_default_states()

    def _render_workflow_default_states(self) -> None:
        for test_id, enabled in self._workflow_default_states.items():
            row = self._test_rows.get(test_id)
            if row is None:
                continue
            if enabled:
                self._set_item(self._workflow_table, row, 3, tr("Pending"))
                self._set_item(self._workflow_table, row, 5, "-")
            else:
                self._set_item(
                    self._workflow_table,
                    row,
                    3,
                    tr("Not applicable"),
                )
                self._set_item(
                    self._workflow_table,
                    row,
                    5,
                    tr("Disabled by recipe"),
                )

    def _populate_fixture_rows(self) -> None:
        for row, (fixture_id, label) in enumerate(self._FIXTURE_DEFS):
            self._fixture_rows[fixture_id] = row
            self._set_item(self._fixture_table, row, 0, tr(label))
            self._set_item(self._fixture_table, row, 1, tr("Not configured"))
            self._set_item(self._fixture_table, row, 2, "-")

    @staticmethod
    def _set_item(table: QTableWidget, row: int, column: int, text: str) -> None:
        item = table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            table.setItem(row, column, item)
        item.setText(str(text))
        item.setTextAlignment(
            Qt.AlignmentFlag.AlignVCenter
            | (Qt.AlignmentFlag.AlignCenter if column == 0 else Qt.AlignmentFlag.AlignLeft)
        )

    def _restore_setup_defaults(self) -> None:
        self._batch_id_edit.setText(datetime.now().strftime("AFD01-%Y%m%d-%H%M"))
        self._operator_edit.setText(str(self._settings.get("production.last_operator", "")))
        output = str(
            self._settings.get("paths.production_dir", "")
            or (Path.home() / "SatelliteProduction")
        )
        self._output_edit.setText(output)
        recipe_path = str(self._settings.get("production.last_recipe", "") or "")
        if recipe_path and Path(recipe_path).is_file():
            self.load_recipe_file(recipe_path)

    def _configure_fleet(self) -> None:
        try:
            fleet = FleetController(
                discovery_cidr=str(
                    self._settings.get("production.discovery_cidr", "192.168.1.0/24")
                ),
                local_port=int(self._settings.get("production.local_port", 45679)),
                device_port=int(self._settings.get("production.device_port", 4004)),
                max_devices=int(self._settings.get("production.max_devices", 4)),
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
            return True
        self._footer_status.setText(tr("Discovering AFD01 devices..."))
        started = fleet.start()
        if started:
            self._session_refresh_timer.start()
            self._snr_refresh_timer.start()
        return started

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
        source = Path(path).expanduser()
        try:
            recipe = ProductionRecipe.from_path(source)
        except RecipeValidationError as exc:
            self._recipe = None
            self._apply_recipe_test_gates(None)
            self._recipe_edit.setText(str(source))
            self._recipe_status.setText(
                tr("Invalid recipe: {details}", details="; ".join(exc.errors))
            )
            self._recipe_edit.setToolTip(self._recipe_status.text())
            self._footer_status.setText(tr("Correct the recipe before creating a batch."))
            self.status_message.emit(self._recipe_status.text(), 8000)
            return False

        self._recipe = recipe
        self._apply_recipe_test_gates(recipe)
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
        self.append_event(tr("Recipe validated: {recipe_id}", recipe_id=recipe.recipe_id))
        return True

    def _choose_output_dir(self) -> None:
        start = self._output_edit.text().strip() or str(Path.home())
        directory = QFileDialog.getExistingDirectory(
            self,
            tr("Select production output folder"),
            start,
        )
        if directory:
            self._output_edit.setText(directory)

    def _on_create_batch(self) -> None:
        try:
            self.create_batch()
        except (RecipeValidationError, ResultStoreError, OSError, ValueError) as exc:
            message = tr("Cannot create batch: {details}", details=str(exc))
            self._footer_status.setText(message)
            self.status_message.emit(message, 8000)

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
        batch_output.mkdir(parents=True, exist_ok=True)
        store = ProductionResultStore(batch_output / "batch.sqlite3")
        try:
            self._recipe.write_snapshot(batch_output / "recipe.json")
            batch = store.create_batch(
                batch_id,
                self._recipe,
                operator=operator,
                output_dir=batch_output,
            )
        except Exception:
            store.close()
            raise

        self._store = store
        self._batch = batch
        self._batch_output_dir = batch_output
        self._settings.set("paths.production_dir", str(Path(output_root).expanduser()))
        self._settings.set("production.last_operator", operator)
        self._settings.set("production.last_recipe", self._recipe_edit.text().strip())
        self._settings.save()
        recording_ready = True
        fleet = self._fleet
        if fleet is not None:
            fleet.clear_snr_histories()
            self._snr_origin_monotonic_ns = time.monotonic_ns()
            recording_ready = fleet.arm_batch_recording(batch_id, batch_output)
            for session in fleet.sessions():
                self._register_session(session)
                self._on_session_updated(session)
        self._lock_batch_setup()
        self._batch_state.setText(tr("Batch ready"))
        self._footer_status.setText(
            tr("Batch ready. Waiting for devices and fixture checks.")
            if recording_ready
            else tr("Batch created, but one or more SDB recorders could not be armed.")
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
        self._snr_panels[slot].set_status(
            serial_number=serial_number,
            endpoint=endpoint,
            connection=connection,
            recording=recording,
            current_test=current_test,
            result=result,
        )

    def update_test_status(
        self,
        test_id: str,
        *,
        status: str,
        result: str = "-",
        effective_duration: Optional[str] = None,
    ) -> None:
        try:
            row = self._test_rows[test_id]
        except KeyError as exc:
            raise ValueError(f"unknown production test: {test_id}") from exc
        self._workflow_default_states.pop(test_id, None)
        self._set_item(self._workflow_table, row, 3, status)
        if effective_duration is not None:
            self._set_item(self._workflow_table, row, 4, effective_duration)
        self._set_item(self._workflow_table, row, 5, result)

    def update_fixture_state(
        self,
        fixture_id: str,
        *,
        state: str,
        evidence: str = "-",
    ) -> None:
        try:
            row = self._fixture_rows[fixture_id]
        except KeyError as exc:
            raise ValueError(f"unknown production fixture: {fixture_id}") from exc
        self._set_item(self._fixture_table, row, 1, state)
        self._set_item(self._fixture_table, row, 2, evidence)

    def append_event(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._event_log.appendPlainText(f"[{timestamp}] {message}")

    def _on_session_updated(self, session: DeviceSession) -> None:
        state_result = {
            DeviceSessionState.DISCOVERED: tr("Identifying"),
            DeviceSessionState.IDENTIFIED: "-",
            DeviceSessionState.CONFLICT: tr("Identity conflict"),
            DeviceSessionState.UNSUPPORTED: tr("Unsupported hardware"),
        }[session.state]
        self.update_device_slot(
            session.slot,
            serial_number=session.serial_number or tr("Identifying"),
            endpoint=f"{session.endpoint[0]}:{session.endpoint[1]}",
            connection=tr("Online") if session.is_online() else tr("Offline"),
            recording=tr("Armed") if session.recording_armed else tr("Not armed"),
            result=state_result,
        )
        if session.state == DeviceSessionState.IDENTIFIED:
            self._register_session(session)
        if self._batch is not None and self._batch.get("status") == BatchStatus.RUNNING.value:
            self._apply_external_ins_gates((session,))
        self._update_start_gate()

    def _refresh_session_rows(self) -> None:
        fleet = self._fleet
        if fleet is None:
            return
        for session in fleet.sessions():
            self._on_session_updated(session)
        self._update_snr_group_title()
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
        for slot, panel in self._snr_panels.items():
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
        for panel in self._snr_panels.values():
            panel.set_ranges(
                x_min=x_min,
                x_max=x_max,
                y_min=y_min,
                y_max=y_max,
            )

    def _update_snr_group_title(self) -> None:
        fleet = self._fleet
        count = 0
        if fleet is not None:
            now_ns = time.monotonic_ns()
            count = sum(
                1
                for session in fleet.sessions()
                if session.is_online(now_monotonic_ns=now_ns)
            )
        self._devices_group.setTitle(tr("Device SNR ({count}/4)", count=count))

    def _register_session(self, session: DeviceSession) -> None:
        store = self._store
        if (
            store is None
            or self._batch is None
            or session.state != DeviceSessionState.IDENTIFIED
            or not session.serial_number
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

    def _evaluate_start_gate(self) -> tuple[tuple[DeviceSession, ...], str]:
        if self._batch is None or self._store is None:
            return (), tr("Create a batch before starting.")
        if self._batch.get("status") != BatchStatus.READY.value:
            return (), tr("The batch is not in the ready state.")
        fleet = self._fleet
        if fleet is None:
            return (), tr("Device discovery is unavailable.")

        now_ns = time.monotonic_ns()
        online = tuple(
            session
            for session in fleet.sessions()
            if session.is_online(now_monotonic_ns=now_ns)
        )
        unresolved = tuple(
            session
            for session in online
            if session.state != DeviceSessionState.IDENTIFIED
        )
        if unresolved:
            return (), tr("Wait for online device identity checks to finish.")

        participants = tuple(
            session
            for session in online
            if session.state == DeviceSessionState.IDENTIFIED
            and session.serial_number
            and session.identity_key
        )
        if not participants:
            return (), tr("At least one identified online device is required.")
        unarmed = tuple(
            session.serial_number
            for session in participants
            if not session.recording_armed
        )
        if unarmed:
            return (), tr(
                "Recording is not armed for: {devices}",
                devices=", ".join(unarmed),
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

    def _update_start_gate(self) -> None:
        if not hasattr(self, "_start_button"):
            return
        if self._batch is not None and self._batch.get("status") == BatchStatus.RUNNING.value:
            self._start_button.setEnabled(False)
            self._abort_button.setEnabled(True)
            self._start_button.setToolTip(tr("Batch participants are frozen."))
            return
        if self._batch is not None and self._batch.get("status") in {
            BatchStatus.ABORTED.value,
            BatchStatus.COMPLETED.value,
            BatchStatus.INCOMPLETE.value,
        }:
            self._start_button.setEnabled(False)
            self._abort_button.setEnabled(False)
            return

        participants, reason = self._evaluate_start_gate()
        ready = bool(participants) and not reason
        self._start_button.setEnabled(ready)
        self._abort_button.setEnabled(False)
        if ready:
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
        participants, reason = self._evaluate_start_gate()
        if reason:
            raise ResultStoreError(reason)
        if self._store is None or self._batch is None:
            raise ResultStoreError("no batch is ready")
        enabled_tests = tuple(
            test_id
            for test_id, _label, _prerequisite, _duration in self._TEST_DEFS
            if self._workflow_default_states.get(test_id, False)
        )
        serials = tuple(session.serial_number for session in participants)
        identity_keys = tuple(session.identity_key for session in participants)
        batch = self._store.start_batch(
            self._batch["batch_id"],
            serials,
            enabled_tests,
        )
        try:
            if self._fleet is not None:
                self._fleet.freeze_batch_participants(identity_keys)
        except (RuntimeError, ValueError) as exc:
            batch = self._store.transition_batch(
                self._batch["batch_id"], BatchStatus.INCOMPLETE
            )
            self._batch = batch
            raise ResultStoreError(
                f"participant recording freeze failed; batch marked incomplete: {exc}"
            ) from exc

        self._batch = batch
        self._participant_serials = serials
        self._participant_identity_keys = identity_keys
        self._apply_external_ins_gates(participants)
        self._batch_state.setText(tr("Batch running"))
        message = tr(
            "Batch started with {count} participant device(s).",
            count=len(serials),
        )
        self._footer_status.setText(message)
        self.append_event(message)
        self._update_start_gate()
        self.status_message.emit(message, 5000)
        return dict(batch)

    def _apply_external_ins_gates(
        self, participants: tuple[DeviceSession, ...]
    ) -> None:
        if (
            self._store is None
            or self._batch is None
            or not self._workflow_default_states.get("external_ins", False)
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
            self._store.transition_attempt(
                int(attempt["attempt_id"]),
                AttemptStatus.SKIPPED,
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

        external_attempts = tuple(
            attempt
            for attempt in self._store.list_attempts(self._batch["batch_id"])
            if attempt["test_id"] == "external_ins"
        )
        if external_attempts and all(
            attempt["status"] == AttemptStatus.SKIPPED.value
            for attempt in external_attempts
        ):
            self.update_test_status(
                "external_ins",
                status=tr("Not applicable"),
                result=tr("No participant has external INS configured"),
            )

    def _on_abort_batch(self) -> None:
        try:
            self.abort_batch()
        except ResultStoreError as exc:
            message = tr("Cannot abort batch: {details}", details=str(exc))
            self._footer_status.setText(message)
            self.status_message.emit(message, 8000)

    def abort_batch(self) -> dict:
        if (
            self._store is None
            or self._batch is None
            or self._batch.get("status") != BatchStatus.RUNNING.value
        ):
            raise ResultStoreError("no running batch can be aborted")
        for attempt in self._store.list_attempts(self._batch["batch_id"]):
            if attempt["status"] in {
                AttemptStatus.PENDING.value,
                AttemptStatus.WAITING_PREREQUISITE.value,
                AttemptStatus.ARMED.value,
                AttemptStatus.RUNNING.value,
                AttemptStatus.ANALYZING.value,
            }:
                self._store.transition_attempt(
                    int(attempt["attempt_id"]), AttemptStatus.ABORTED
                )
        batch = self._store.transition_batch(
            self._batch["batch_id"], BatchStatus.ABORTED
        )
        self._batch = batch
        if self._fleet is not None:
            self._fleet.finalize_recordings()
        self._batch_state.setText(tr("Batch aborted"))
        message = tr("Batch aborted by operator.")
        self._footer_status.setText(message)
        self.append_event(message)
        self._update_start_gate()
        return dict(batch)

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
        if self._store is None or self._batch is None:
            return
        try:
            self._store.record_event(self._batch["batch_id"], event_type, payload)
        except ResultStoreError:
            return

    def shutdown(self) -> None:
        self._session_refresh_timer.stop()
        self._snr_refresh_timer.stop()
        if (
            self._store is not None
            and self._batch is not None
            and self._batch.get("status") == BatchStatus.RUNNING.value
        ):
            for attempt in self._store.list_attempts(self._batch["batch_id"]):
                if attempt["status"] in {
                    AttemptStatus.WAITING_PREREQUISITE.value,
                    AttemptStatus.ARMED.value,
                    AttemptStatus.RUNNING.value,
                    AttemptStatus.ANALYZING.value,
                }:
                    self._store.transition_attempt(
                        int(attempt["attempt_id"]), AttemptStatus.INCOMPLETE
                    )
                elif attempt["status"] == AttemptStatus.PENDING.value:
                    self._store.transition_attempt(
                        int(attempt["attempt_id"]), AttemptStatus.ABORTED
                    )
            self._batch = self._store.transition_batch(
                self._batch["batch_id"], BatchStatus.INCOMPLETE
            )
        if self._fleet is not None:
            self._fleet.stop()
        if self._store is not None:
            self._store.close()
            self._store = None

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = theme
        palette = S.palette(theme)
        self.setStyleSheet(
            f"ProductionWorkspace {{ background: {palette['bg']}; }}"
            f"#productionTitle {{ color: {palette['text']}; font-size: 16px; font-weight: 700; }}"
            f"#productionState {{ color: {palette['accent_2']}; font-weight: 600; "
            f"padding: 3px 9px; border: 1px solid {palette['border_2']}; border-radius: 5px; }}"
            f"#productionPreviewNotice {{ color: {palette['warn']}; background: {palette['warn_soft']}; "
            f"padding: 6px 9px; border: 1px solid {palette['warn']}; border-radius: 5px; }}"
            f"#productionRecipeStatus, #productionFooterStatus {{ color: {palette['text_2']}; }}"
        )
        for panel in self._snr_panels.values():
            panel.set_theme(theme)

    def retranslate_ui(self) -> None:
        self._title.setText(tr("Production batch test"))
        self._preview_notice.setText(
            tr(
                "M19-A engineering preview: evidence capture only; not approved for "
                "formal production release."
            )
        )
        self._setup_group.setTitle(tr("Batch setup"))
        self._batch_id_label.setText(tr("Batch ID"))
        self._operator_label.setText(tr("Operator"))
        self._recipe_label.setText(tr("Recipe"))
        self._output_label.setText(tr("Output folder"))
        self._recipe_button.setText(tr("Import recipe..."))
        self._output_button.setText(tr("Browse..."))
        self._create_button.setText(tr("Create batch"))
        self._update_snr_group_title()
        self._workflow_group.setTitle(tr("Test workflow"))
        self._fixtures_group.setTitle(tr("Shared fixtures"))
        self._events_group.setTitle(tr("Batch events"))
        self._start_button.setText(tr("Start batch"))
        self._start_button.setToolTip(
            tr("At least one identified online device with armed recording is required.")
        )
        self._abort_button.setText(tr("Abort"))
        self._set_workflow_headers()
        self._set_fixture_headers()
        for panel in self._snr_panels.values():
            panel.retranslate_ui()
        self._refresh_session_rows()
        for row, (_test_id, label, prerequisite, duration_source) in enumerate(
            self._TEST_DEFS
        ):
            self._set_item(self._workflow_table, row, 1, tr(label))
            self._set_item(self._workflow_table, row, 2, tr(prerequisite))
            duration = "-" if duration_source is None else tr(duration_source)
            self._set_item(self._workflow_table, row, 4, duration)
        self._render_workflow_default_states()
        for row, (_fixture_id, label) in enumerate(self._FIXTURE_DEFS):
            self._set_item(self._fixture_table, row, 0, tr(label))
        self._update_start_gate()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)
