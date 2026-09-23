"""Standalone engineering UI for GW Instek PSW diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
import uuid

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.production import (
    FixtureControlLease,
    FixtureLeaseError,
    FixtureLeaseHandle,
    GwInstekPswAdapter,
    PowerActionResult,
    PowerCommandRecord,
    PowerDebugOperation,
    PowerDebugSessionRecorder,
    PowerEvidenceLevel,
    PowerIdentity,
    PowerSupplyConfig,
    PowerSupplyDebugWorker,
    PowerSupplyError,
    PowerSupplyState,
    psw80_27_validation_policy,
)
from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.ui import styles as S


class PowerSupplyDebugWorkspace(QWidget):
    status_message = Signal(str, int)
    active_changed = Signal(bool)

    def __init__(
        self,
        settings: Settings,
        lease: FixtureControlLease,
        *,
        adapter_factory: Callable[[PowerSupplyConfig], GwInstekPswAdapter] = GwInstekPswAdapter,
        session_root: Optional[Path] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._lease = lease
        self._adapter_factory = adapter_factory
        self._session_root = session_root
        self._lease_handle: Optional[FixtureLeaseHandle] = None
        self._worker: Optional[PowerSupplyDebugWorker] = None
        self._recorder: Optional[PowerDebugSessionRecorder] = None
        self._session_dir: Optional[Path] = None
        self._state = PowerSupplyState.DISCONNECTED
        self._evidence = PowerEvidenceLevel.NONE
        self._last_known_state = PowerSupplyState.DISCONNECTED
        self._last_result: Optional[PowerActionResult] = None
        self._connection_generation = 0
        self._busy_operation = ""
        self._shutdown_incomplete_reason = ""
        self._view_active = False
        self._theme = "dark"
        self._build_ui()
        self._restore_non_action_defaults()
        self._render_state()
        register_translatable(self)

    @property
    def session_active(self) -> bool:
        return self._lease.held_by(self._lease_handle)

    @property
    def state(self) -> PowerSupplyState:
        return self._state

    @property
    def session_dir(self) -> Optional[Path]:
        return self._session_dir

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(7)

        self._notice = QLabel(
            tr(
                "Engineering-only PSW diagnostics. TCP disconnect does not turn off "
                "the physical output."
            )
        )
        self._notice.setObjectName("powerDebugNotice")
        self._notice.setWordWrap(True)
        outer.addWidget(self._notice)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(7)

        self._config_group = QGroupBox(tr("Power supply configuration"))
        config_grid = QGridLayout(self._config_group)
        config_grid.setContentsMargins(9, 13, 9, 8)
        config_grid.setHorizontalSpacing(8)
        config_grid.setVerticalSpacing(6)
        self._host = QLineEdit()
        self._host.setPlaceholderText("192.168.1.x")
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(2268)
        self._manufacturer = QLineEdit("GW-INSTEK")
        self._model = QLineEdit("PSW 80-27")
        self._serial = QLineEdit()
        self._serial.setPlaceholderText(tr("Optional identity lock"))

        self._voltage_set = self._numeric_edit(tr("Required"))
        self._current_set = self._numeric_edit(tr("Required"))
        self._connect_timeout = self._numeric_edit()
        self._connect_timeout.setText("3")
        self._command_timeout = self._numeric_edit()
        self._command_timeout.setText("2")
        self._output_settle_timeout = self._numeric_edit()
        self._output_settle_timeout.setText("3")

        self._config_labels = {
            "host": QLabel(tr("IPv4 address")),
            "port": QLabel(tr("TCP port")),
            "manufacturer": QLabel(tr("Expected manufacturer")),
            "model": QLabel(tr("Expected model")),
            "serial": QLabel(tr("Expected serial number")),
            "voltage": QLabel(tr("Set voltage (V)")),
            "current": QLabel(tr("Set current (A)")),
            "connect_timeout": QLabel(tr("Connect timeout (s)")),
            "command_timeout": QLabel(tr("Command timeout (s)")),
            "output_settle_timeout": QLabel(tr("Output settle timeout (s)")),
        }
        fields = (
            ("host", self._host),
            ("port", self._port),
            ("manufacturer", self._manufacturer),
            ("model", self._model),
            ("serial", self._serial),
            ("voltage", self._voltage_set),
            ("current", self._current_set),
            ("connect_timeout", self._connect_timeout),
            ("command_timeout", self._command_timeout),
            ("output_settle_timeout", self._output_settle_timeout),
        )
        for index, (name, editor) in enumerate(fields):
            pair = index % 2
            row = index // 2
            column = pair * 2
            config_grid.addWidget(self._config_labels[name], row, column)
            config_grid.addWidget(editor, row, column + 1)
        self._validation_policy_label = QLabel()
        self._validation_policy_label.setObjectName("powerValidationPolicy")
        self._validation_policy_label.setWordWrap(True)
        policy_row = (len(fields) + 1) // 2
        config_grid.addWidget(self._validation_policy_label, policy_row, 0, 1, 4)
        self._voltage_set.textChanged.connect(self._render_validation_policy)
        self._current_set.textChanged.connect(self._render_validation_policy)
        config_grid.setColumnStretch(1, 1)
        config_grid.setColumnStretch(3, 1)
        root.addWidget(self._config_group)
        self._render_validation_policy()

        self._actions_group = QGroupBox(tr("Closed-loop actions"))
        actions = QHBoxLayout(self._actions_group)
        actions.setContentsMargins(9, 13, 9, 8)
        self._connect = QPushButton(tr("Connect and identify"))
        self._connect.setProperty("variant", "primary")
        self._connect.clicked.connect(self._connect_clicked)
        self._inspect = QPushButton(tr("Read status"))
        self._inspect.clicked.connect(
            lambda: self._submit(PowerDebugOperation.INSPECT)
        )
        self._prepare_off = QPushButton(tr("Apply settings and confirm OFF"))
        self._prepare_off.clicked.connect(
            lambda: self._submit(PowerDebugOperation.PREPARE_OFF)
        )
        self._enable_output = QPushButton(tr("Enable output..."))
        self._enable_output.setProperty("variant", "primary")
        self._enable_output.clicked.connect(self._enable_clicked)
        self._disable_output = QPushButton(tr("Disable output"))
        self._disable_output.setProperty("variant", "danger")
        self._disable_output.clicked.connect(
            lambda: self._submit(PowerDebugOperation.DISABLE, priority=True)
        )
        self._release_local = QPushButton(tr("Return local control"))
        self._release_local.clicked.connect(
            lambda: self._submit(PowerDebugOperation.RELEASE_LOCAL)
        )
        self._disconnect = QPushButton(tr("Disconnect"))
        self._disconnect.clicked.connect(self._disconnect_clicked)
        for button in (
            self._connect,
            self._inspect,
            self._prepare_off,
            self._enable_output,
            self._disable_output,
            self._release_local,
            self._disconnect,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        root.addWidget(self._actions_group)

        self._status_group = QGroupBox(tr("Identity and live status"))
        status_grid = QGridLayout(self._status_group)
        status_grid.setContentsMargins(9, 13, 9, 8)
        self._state_label = QLabel("-")
        self._identity_label = QLabel("-")
        self._identity_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._measurement_label = QLabel("-")
        self._condition_label = QLabel("-")
        self._session_label = QLabel("-")
        self._status_labels = {
            "state": QLabel(tr("State / evidence")),
            "identity": QLabel(tr("Identity")),
            "measurement": QLabel(tr("Output / measurement")),
            "condition": QLabel(tr("Condition / protection")),
            "session": QLabel(tr("Evidence session")),
        }
        for row, (name, value) in enumerate(
            (
                ("state", self._state_label),
                ("identity", self._identity_label),
                ("measurement", self._measurement_label),
                ("condition", self._condition_label),
                ("session", self._session_label),
            )
        ):
            status_grid.addWidget(self._status_labels[name], row, 0)
            status_grid.addWidget(value, row, 1)
        status_grid.setColumnStretch(1, 1)
        root.addWidget(self._status_group)

        self._records_group = QGroupBox(tr("SCPI command records"))
        records_layout = QVBoxLayout(self._records_group)
        records_layout.setContentsMargins(7, 12, 7, 7)
        self._records = QTableWidget(0, 5)
        self._records.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._records.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._records.setAlternatingRowColors(True)
        self._records.verticalHeader().hide()
        self._set_record_headers()
        records_layout.addWidget(self._records)
        root.addWidget(self._records_group, 1)

        self._events_group = QGroupBox(tr("Power supply events"))
        events_layout = QVBoxLayout(self._events_group)
        events_layout.setContentsMargins(7, 12, 7, 7)
        self._events = QPlainTextEdit()
        self._events.setReadOnly(True)
        self._events.setMaximumBlockCount(1000)
        events_layout.addWidget(self._events)
        root.addWidget(self._events_group)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

    @staticmethod
    def _numeric_edit(placeholder: str = "") -> QLineEdit:
        editor = QLineEdit()
        editor.setValidator(QDoubleValidator(0.0, 1_000_000.0, 6, editor))
        if placeholder:
            editor.setPlaceholderText(placeholder)
        return editor

    def _restore_non_action_defaults(self) -> None:
        self._host.setText(
            str(self._settings.get("production.power_supply_host", "") or "")
        )

    def _build_config(self) -> PowerSupplyConfig:
        def number(editor: QLineEdit, name: str, *, positive: bool = False) -> float:
            text = editor.text().strip()
            if not text:
                raise PowerSupplyError(tr("{name} is required", name=name))
            value = float(text)
            if positive and value <= 0:
                raise PowerSupplyError(tr("{name} must be positive", name=name))
            return value

        voltage = number(self._voltage_set, tr("Set voltage"), positive=True)
        current = number(self._current_set, tr("Set current"), positive=True)
        policy = psw80_27_validation_policy(voltage, current)
        config = PowerSupplyConfig(
            host=self._host.text().strip(),
            port=self._port.value(),
            expected_manufacturer=self._manufacturer.text().strip(),
            expected_model=self._model.text().strip(),
            expected_serial=self._serial.text().strip(),
            voltage_set_v=voltage,
            current_set_a=current,
            voltage_setpoint_tolerance_v=policy.voltage_setpoint_tolerance_v,
            current_setpoint_tolerance_a=policy.current_setpoint_tolerance_a,
            output_voltage_min_v=policy.output_voltage_min_v,
            output_voltage_max_v=policy.output_voltage_max_v,
            off_voltage_max_v=policy.off_voltage_max_v,
            connect_timeout_s=number(
                self._connect_timeout, tr("Connect timeout"), positive=True
            ),
            command_timeout_s=number(
                self._command_timeout, tr("Command timeout"), positive=True
            ),
            output_settle_timeout_s=number(
                self._output_settle_timeout,
                tr("Output settle timeout"),
                positive=True,
            ),
        )
        config.validate()
        return config

    def _render_validation_policy(self) -> None:
        try:
            voltage = float(self._voltage_set.text().strip())
            current = float(self._current_set.text().strip())
            policy = psw80_27_validation_policy(voltage, current)
        except (PowerSupplyError, ValueError):
            self._validation_policy_label.setText(
                tr("Enter voltage and current to calculate automatic validation limits.")
            )
            return
        self._validation_policy_label.setText(
            tr(
                "Automatic validation: voltage setpoint readback ±{voltage_tolerance:.3f} V; "
                "current setpoint readback ±{current_tolerance:.3f} A; output ON "
                "{minimum:.3f}..{maximum:.3f} V; output OFF ≤{off_maximum:.3f} V.",
                voltage_tolerance=policy.voltage_setpoint_tolerance_v,
                current_tolerance=policy.current_setpoint_tolerance_a,
                minimum=policy.output_voltage_min_v,
                maximum=policy.output_voltage_max_v,
                off_maximum=policy.off_voltage_max_v,
            )
        )

    def _connect_clicked(self) -> None:
        try:
            self.connect_power_supply()
        except (FixtureLeaseError, PowerSupplyError, OSError, ValueError) as exc:
            self._show_error(str(exc))

    def connect_power_supply(self) -> Path:
        if self.session_active or self._worker is not None:
            raise PowerSupplyError("power supply debug session is already active")
        config = self._build_config()
        handle = self._lease.acquire("power-supply-debug")
        recorder = PowerDebugSessionRecorder(
            config,
            operator=str(self._settings.get("production.last_operator", "") or ""),
            root=self._session_root,
        )
        try:
            session_dir = recorder.start()
            worker = PowerSupplyDebugWorker(
                config,
                adapter_factory=self._adapter_factory,
                parent=self,
            )
            worker.operation_started.connect(self._on_operation_started)
            worker.operation_succeeded.connect(self._on_operation_succeeded)
            worker.operation_failed.connect(self._on_operation_failed)
            worker.finished.connect(self._on_worker_finished)
            worker.start()
        except Exception:
            self._lease.release(handle)
            raise
        self._settings.set("production.power_supply_host", config.host)
        self._settings.persist_preferences()
        self._lease_handle = handle
        self._recorder = recorder
        self._session_dir = session_dir
        self._worker = worker
        self._shutdown_incomplete_reason = ""
        self._connection_generation = 0
        self._identity_label.setText("-")
        self._last_result = None
        self._append_event(
            tr("Power supply session started: {path}", path=str(session_dir)),
            "session_started_ui",
            {"path": str(session_dir)},
        )
        self.active_changed.emit(True)
        self._set_config_enabled(False)
        self._busy_operation = PowerDebugOperation.CONNECT.value
        self._render_state()
        worker.submit(PowerDebugOperation.CONNECT)
        return session_dir

    def _enable_clicked(self) -> None:
        try:
            config = self._build_config()
        except (PowerSupplyError, ValueError) as exc:
            self._show_error(str(exc))
            return
        answer = QMessageBox.warning(
            self,
            tr("Enable power output"),
            tr(
                "First turn output OFF and apply {voltage:.3f} V / {current:.3f} A, "
                "then enable output and wait up to {timeout:.3f} s for measured "
                "voltage {minimum:.3f}..{maximum:.3f} V?",
                voltage=config.voltage_set_v,
                current=config.current_set_a,
                timeout=config.output_settle_timeout_s,
                minimum=config.output_voltage_min_v,
                maximum=config.output_voltage_max_v,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._submit(PowerDebugOperation.ENABLE)

    def _disconnect_clicked(self) -> None:
        if not self.session_active:
            return
        output_off_confirmed = self._state in {
            PowerSupplyState.READY_OFF,
            PowerSupplyState.OFF_CONFIRMED,
        }
        if not output_off_confirmed:
            answer = QMessageBox.warning(
                self,
                tr("Disconnect power supply"),
                tr(
                    "The last known power state is {state}. Disconnecting TCP does not "
                    "turn off the physical output. Disconnect anyway?",
                    state=self._state_text(self._state),
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not output_off_confirmed and self._state != PowerSupplyState.ON_CONFIRMED:
            self._shutdown_incomplete_reason = (
                "disconnected without confirmed output OFF"
            )
        self._submit(PowerDebugOperation.DISCONNECT)

    def _submit(
        self,
        operation: PowerDebugOperation,
        *,
        priority: bool = False,
    ) -> None:
        worker = self._worker
        if worker is None:
            self._show_error(tr("Power supply is not connected."))
            return
        if self._busy_operation:
            if not priority or self._busy_operation == operation.value:
                return
        action_id = f"PSW-{uuid.uuid4().hex[:12]}"
        self._busy_operation = operation.value
        self._render_state()
        worker.submit(operation, action_id=action_id)

    def _on_operation_started(self, operation: str) -> None:
        self._busy_operation = str(operation)
        self._append_event(
            tr("Power operation started: {operation}", operation=self._operation_text(operation)),
            "operation_started",
            {"operation": operation},
        )
        self._render_state()

    def _on_operation_succeeded(
        self,
        operation: str,
        payload,
        state_evidence,
        records,
    ) -> None:
        state, evidence = state_evidence
        self._consume_records(records)
        if operation != PowerDebugOperation.DISCONNECT.value:
            self._state = PowerSupplyState(state)
            self._evidence = PowerEvidenceLevel(evidence)
            self._last_known_state = self._state
        if isinstance(payload, PowerIdentity):
            self._identity_label.setText(
                f"GEN {self._connection_generation} | {payload.manufacturer} | "
                f"{payload.model} | {payload.serial_number} | "
                f"FW {payload.firmware} | {payload.raw}"
            )
            if self._recorder is not None:
                try:
                    self._recorder.record_identity(payload)
                except OSError as exc:
                    self._shutdown_incomplete_reason = f"evidence write failed: {exc}"
        if isinstance(payload, PowerActionResult):
            self._last_result = payload
        self._busy_operation = ""
        self._append_event(
            tr("Power operation completed: {operation}", operation=self._operation_text(operation)),
            "operation_completed",
            self._operation_result_details(operation, payload),
        )
        if operation == PowerDebugOperation.RELEASE_LOCAL.value:
            self.status_message.emit(
                tr("Local control requested; output state is unchanged."), 6000
            )
        self._render_state()

    def _on_operation_failed(
        self,
        operation: str,
        details: str,
        state_evidence,
        records,
    ) -> None:
        state, evidence = state_evidence
        self._consume_records(records)
        self._state = PowerSupplyState(state)
        self._evidence = PowerEvidenceLevel(evidence)
        self._last_known_state = self._state
        self._busy_operation = ""
        self._append_event(
            tr(
                "Power operation failed: {operation}: {details}",
                operation=self._operation_text(operation),
                details=details,
            ),
            "operation_failed",
            {
                "operation": operation,
                "details": details,
                "state": self._state.value,
                "evidence_level": self._evidence.value,
            },
        )
        self._render_state()
        self.status_message.emit(details, 8000)
        if operation == PowerDebugOperation.CONNECT.value:
            self._shutdown_incomplete_reason = details
            worker = self._worker
            if worker is not None:
                worker.stop()

    def _on_worker_finished(self) -> None:
        self._finalize_session()

    def _consume_records(self, records) -> None:
        typed = tuple(record for record in records if isinstance(record, PowerCommandRecord))
        if typed:
            self._connection_generation = max(
                self._connection_generation,
                *(record.connection_generation for record in typed),
            )
        if self._recorder is not None and typed:
            try:
                self._recorder.record_commands(typed)
            except OSError as exc:
                self._shutdown_incomplete_reason = f"evidence write failed: {exc}"
        for record in typed:
            row = self._records.rowCount()
            self._records.insertRow(row)
            timestamp = datetime.fromtimestamp(
                record.wall_time_ns / 1_000_000_000.0,
                tz=timezone.utc,
            ).astimezone().strftime("%H:%M:%S.%f")[:-3]
            values = (
                timestamp,
                str(record.connection_generation),
                record.command,
                record.response,
                record.fixture_action_id,
            )
            for column, value in enumerate(values):
                self._records.setItem(row, column, QTableWidgetItem(value))
        if typed:
            self._records.scrollToBottom()

    def _operation_result_details(self, operation: str, payload) -> dict:
        details = {
            "operation": operation,
            "state": self._state.value,
            "evidence_level": self._evidence.value,
            "connection_generation": self._connection_generation,
        }
        if isinstance(payload, PowerActionResult):
            details["result"] = {
                "output_enabled": payload.output_enabled,
                "voltage_v": payload.measurement.voltage_v,
                "current_a": payload.measurement.current_a,
                "operation_condition": payload.operation_condition,
                "questionable_condition": payload.questionable_condition,
                "protection_tripped": payload.protection_tripped,
            }
        return details

    def _render_state(self) -> None:
        self._state_label.setText(
            tr(
                "{state} | evidence: {evidence}",
                state=self._state_text(self._state),
                evidence=self._evidence_text(self._evidence),
            )
        )
        result = self._last_result
        if result is None:
            self._measurement_label.setText("-")
            self._condition_label.setText("-")
        else:
            self._measurement_label.setText(
                tr(
                    "output={output} | {voltage:.4f} V | total {current:.4f} A",
                    output=tr("ON") if result.output_enabled else tr("OFF"),
                    voltage=result.measurement.voltage_v,
                    current=result.measurement.current_a,
                )
            )
            self._condition_label.setText(
                tr(
                    "operation=0x{operation:X} | questionable=0x{questionable:X} | "
                    "protection={protection}",
                    operation=result.operation_condition,
                    questionable=result.questionable_condition,
                    protection=tr("TRIPPED") if result.protection_tripped else tr("clear"),
                )
            )
        self._session_label.setText(
            "-" if self._session_dir is None else str(self._session_dir)
        )
        connected = self.session_active and self._worker is not None
        busy = bool(self._busy_operation)
        self._connect.setEnabled(not connected and not busy)
        self._inspect.setEnabled(connected and not busy)
        self._prepare_off.setEnabled(connected and not busy)
        self._enable_output.setEnabled(
            connected
            and not busy
            and self._state
            in {
                PowerSupplyState.IDENTIFIED,
                PowerSupplyState.OFF_CONFIRMED,
                PowerSupplyState.READY_OFF,
            }
        )
        self._disable_output.setEnabled(connected)
        self._release_local.setEnabled(connected and not busy)
        self._disconnect.setEnabled(connected and not busy)

    def _finalize_session(self) -> None:
        recorder = self._recorder
        final_state = self._last_known_state
        if recorder is not None:
            try:
                recorder.finalize(
                    final_state=final_state,
                    evidence_level=self._evidence,
                    output_preserved=final_state == PowerSupplyState.ON_CONFIRMED,
                    incomplete_reason=self._shutdown_incomplete_reason,
                )
            except OSError as exc:
                self.status_message.emit(str(exc), 8000)
        handle = self._lease_handle
        self._lease_handle = None
        if handle is not None:
            try:
                self._lease.release(handle)
            except FixtureLeaseError as exc:
                self.status_message.emit(str(exc), 8000)
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        self._recorder = None
        self._busy_operation = ""
        self._state = PowerSupplyState.DISCONNECTED
        self._evidence = PowerEvidenceLevel.NONE
        self._set_config_enabled(True)
        self.active_changed.emit(False)
        self._render_state()

    def _set_config_enabled(self, enabled: bool) -> None:
        for editor in (
            self._host,
            self._port,
            self._manufacturer,
            self._model,
            self._serial,
            self._voltage_set,
            self._current_set,
            self._connect_timeout,
            self._command_timeout,
            self._output_settle_timeout,
        ):
            editor.setEnabled(enabled)

    def _append_event(
        self,
        text: str,
        event_type: str,
        details: dict,
    ) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._events.appendPlainText(f"[{stamp}] {text}")
        recorder = self._recorder
        if recorder is not None:
            try:
                recorder.record_event(event_type, details)
            except OSError as exc:
                self._shutdown_incomplete_reason = f"evidence write failed: {exc}"

    def _show_error(self, details: str) -> None:
        QMessageBox.critical(self, tr("Power supply diagnostics"), str(details))
        self.status_message.emit(str(details), 8000)

    def confirm_shutdown(self) -> bool:
        if not self.session_active and self._worker is None:
            return True
        if self._state in {
            PowerSupplyState.READY_OFF,
            PowerSupplyState.OFF_CONFIRMED,
        }:
            return True
        answer = QMessageBox.warning(
            self,
            tr("Close power supply diagnostics"),
            tr(
                "The last known power state is {state}. Closing the TCP connection does "
                "not turn off the physical output. Close diagnostics anyway?",
                state=self._state_text(self._state),
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes and self._state == PowerSupplyState.UNKNOWN:
            self._shutdown_incomplete_reason = "workspace closed with unknown output state"
        return answer == QMessageBox.StandardButton.Yes

    def activate_view(self) -> None:
        self._view_active = True
        self._render_state()

    def deactivate_view(self) -> None:
        self._view_active = False

    def shutdown(self, *, wait: bool = True) -> None:
        self.deactivate_view()
        worker = self._worker
        if worker is not None:
            if self._busy_operation and not self._shutdown_incomplete_reason:
                self._shutdown_incomplete_reason = "workspace closed during a power operation"
            worker.stop()
            if wait:
                worker.wait(10000)
        if self._worker is not None:
            self._finalize_session()

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = theme
        palette = S.palette(theme)
        self.setStyleSheet(
            f"PowerSupplyDebugWorkspace {{ background: {palette['bg']}; }}"
            f"#powerDebugNotice {{ color: {palette['warn']}; background: {palette['warn_soft']}; "
            f"padding: 6px 9px; border: 1px solid {palette['warn']}; border-radius: 5px; }}"
            f"#powerValidationPolicy {{ color: {palette['text_2']}; padding: 4px 2px; }}"
        )

    def retranslate_ui(self) -> None:
        self._notice.setText(
            tr(
                "Engineering-only PSW diagnostics. TCP disconnect does not turn off "
                "the physical output."
            )
        )
        self._config_group.setTitle(tr("Power supply configuration"))
        label_texts = {
            "host": "IPv4 address",
            "port": "TCP port",
            "manufacturer": "Expected manufacturer",
            "model": "Expected model",
            "serial": "Expected serial number",
            "voltage": "Set voltage (V)",
            "current": "Set current (A)",
            "connect_timeout": "Connect timeout (s)",
            "command_timeout": "Command timeout (s)",
            "output_settle_timeout": "Output settle timeout (s)",
        }
        for key, source in label_texts.items():
            self._config_labels[key].setText(tr(source))
        self._serial.setPlaceholderText(tr("Optional identity lock"))
        for editor in (
            self._voltage_set,
            self._current_set,
        ):
            editor.setPlaceholderText(tr("Required"))
        self._actions_group.setTitle(tr("Closed-loop actions"))
        self._connect.setText(tr("Connect and identify"))
        self._inspect.setText(tr("Read status"))
        self._prepare_off.setText(tr("Apply settings and confirm OFF"))
        self._enable_output.setText(tr("Enable output..."))
        self._disable_output.setText(tr("Disable output"))
        self._release_local.setText(tr("Return local control"))
        self._disconnect.setText(tr("Disconnect"))
        self._status_group.setTitle(tr("Identity and live status"))
        for key, source in {
            "state": "State / evidence",
            "identity": "Identity",
            "measurement": "Output / measurement",
            "condition": "Condition / protection",
            "session": "Evidence session",
        }.items():
            self._status_labels[key].setText(tr(source))
        self._records_group.setTitle(tr("SCPI command records"))
        self._events_group.setTitle(tr("Power supply events"))
        self._set_record_headers()
        self._render_validation_policy()
        self._render_state()

    def _set_record_headers(self) -> None:
        self._records.setHorizontalHeaderLabels(
            [tr("Time"), tr("Generation"), tr("Command"), tr("Response"), tr("Action ID")]
        )
        header = self._records.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    @staticmethod
    def _state_text(state: PowerSupplyState) -> str:
        return {
            PowerSupplyState.DISCONNECTED: tr("Disconnected"),
            PowerSupplyState.IDENTIFIED: tr("Identity verified"),
            PowerSupplyState.READY_OFF: tr("Ready with output OFF"),
            PowerSupplyState.TURNING_ON: tr("Turning output ON"),
            PowerSupplyState.ON_CONFIRMED: tr("Output confirmed ON"),
            PowerSupplyState.TURNING_OFF: tr("Turning output OFF"),
            PowerSupplyState.OFF_CONFIRMED: tr("Output confirmed OFF"),
            PowerSupplyState.PROTECTION_TRIPPED: tr("Protection tripped"),
            PowerSupplyState.UNKNOWN: tr("Unknown"),
        }[state]

    @staticmethod
    def _evidence_text(evidence: PowerEvidenceLevel) -> str:
        return {
            PowerEvidenceLevel.NONE: tr("No evidence"),
            PowerEvidenceLevel.COMMAND_SENT: tr("Command sent"),
            PowerEvidenceLevel.OUTPUT_STATE_CONFIRMED: tr("Output state confirmed"),
            PowerEvidenceLevel.VOLTAGE_CONFIRMED: tr("Voltage confirmed"),
            PowerEvidenceLevel.DUT_BOOT_OBSERVED: tr("DUT boot observed"),
        }[evidence]

    @staticmethod
    def _operation_text(operation: str) -> str:
        return {
            PowerDebugOperation.CONNECT.value: tr("Connect and identify"),
            PowerDebugOperation.INSPECT.value: tr("Read status"),
            PowerDebugOperation.PREPARE_OFF.value: tr(
                "Apply settings and confirm OFF"
            ),
            PowerDebugOperation.ENABLE.value: tr("Enable output"),
            PowerDebugOperation.DISABLE.value: tr("Disable output"),
            PowerDebugOperation.RELEASE_LOCAL.value: tr("Return local control"),
            PowerDebugOperation.DISCONNECT.value: tr("Disconnect"),
        }.get(str(operation), str(operation))


__all__ = ["PowerSupplyDebugWorkspace"]
