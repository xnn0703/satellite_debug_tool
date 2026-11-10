"""正式批次执行服务：步骤、证据准入和有效观察由领域层拥有。"""
from __future__ import annotations

import ipaddress
import math
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from satellite_debug_tool.core.product import Availability, TrackingPhase
from satellite_debug_tool.core.product.models import tracking_phase_from_wire
from satellite_debug_tool.core.protocol import (
    ParaTableReport, ServiceFastState, build_para_set, build_request_para_table,
)
from satellite_debug_tool.core.session import OtaController
from satellite_debug_tool.core.session.command_sender import SessionOperationClass
from .fleet import DeviceSession, FleetController
from .models import AttemptPhase, AttemptStatus
from .recipe import ProductionRecipe
from .result_store import ProductionResultStore, ResultStoreError
from .tracking_criteria import TrackingCriteriaPolicy, TrackingEvaluator
from .run_coordinator import (
    DeviceStepResult, ProductionRunCoordinator, ProductionRunState, ProductionStepRequest,
)


def compare_parameter(actual: str, rule: Mapping[str, object]) -> bool:
    """按配方类型比较，浮点 NaN 和 Infinity 保持为无效证据。"""
    kind = rule["type"]
    def parse(value):
        if kind == "bool":
            text = str(value).strip().lower()
            if text in {"true", "1"}:
                return True
            if text in {"false", "0"}:
                return False
            raise ValueError("boolean requires true/false or 1/0")
        if kind in {"int", "uint8"}:
            number = int(str(value), 0) if str(value).lower().startswith("0x") else int(str(value))
            if kind == "uint8" and not 0 <= number <= 255:
                raise ValueError("uint8 out of range")
            return number
        if kind == "float":
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("finite parameter required")
            return number
        if kind == "ip":
            return ipaddress.ip_address(str(value))
        return str(value)
    try:
        value = parse(actual)
        if "range" in rule:
            low, high = rule["range"]
            return parse(low) <= value <= parse(high)
        if "one_of" in rule:
            return value in [parse(item) for item in rule["one_of"]]
        if "abs_tol" in rule:
            return abs(value - parse(rule["value"])) <= float(rule["abs_tol"])
        return value == parse(rule["value"])
    except (ValueError, TypeError, KeyError):
        return False


class _FirmwareAutoUpgrade(QObject):
    """M66：核验失败时用配方 bin 自动升级；TARGET_VERSION_CONFIRMED 为唯一成功事实。"""

    finished = Signal(str, bool, str)

    _FAILURE_TERMINALS = frozenset({
        "begin_send_failed", "begin_rejected", "begin_timeout", "chunk_send_failed",
        "chunk_rejected", "chunk_timeout", "end_send_failed", "end_rejected",
        "end_timeout", "reboot_timeout", "target_version_not_confirmed",
        "return_identity_changed", "return_identity_unconfirmed",
        "return_firmware_unconfirmed", "application_unconfirmed",
        "connection_lost", "session_changed", "aborted", "abort_pending",
        "no_file", "artifact_too_large", "artifact_mismatch",
        "identity_unavailable", "identity_facts_conflict", "firmware_facts_conflict",
        "unsupported", "stop_live_data_failed",
    })

    def __init__(self, session, serial: str, bin_path: Path, parent=None) -> None:
        super().__init__(parent)
        self._serial = str(serial)
        attachment = getattr(session, "attachment", None)
        gateway = attachment.new_operation_gateway() if attachment is not None else None
        self._ota = OtaController(session.core, parent=self, operation_gateway=gateway)
        self._ota.status_changed.connect(self._on_status)
        data = Path(bin_path).read_bytes()
        token = self._ota.configure_engineering_file(data, Path(bin_path).name)
        if token is None:
            self.finished.emit(self._serial, False, "artifact configuration failed")
            return
        if not self._ota.start(pause_debug=True):
            self.finished.emit(self._serial, False, "OTA start refused")

    def _on_status(self, status: object, payload: object) -> None:
        token = str(getattr(status, "value", status))
        if token == "target_version_confirmed":
            self.finished.emit(self._serial, True, token)
        elif token in self._FAILURE_TERMINALS:
            self.finished.emit(self._serial, False, token)


class ProductionExecution(QObject):
    """页面生命周期之外持续推进；硬件启动只接受执行 owner 的显式证据。"""

    changed = Signal(object)
    hardware_requested = Signal(object)
    failed = Signal(str)
    finished = Signal()
    observation_complete = Signal(str)
    motion_precondition_changed = Signal()

    def __init__(self, store: ProductionResultStore, batch_id: str,
                 recipe: ProductionRecipe, fleet: FleetController,
                 participants: Sequence[DeviceSession], *,
                 clock: Callable[[], float] = time.monotonic,
                 fixture_telemetry=None, parent=None) -> None:
        super().__init__(parent)
        self.store, self.batch_id, self.recipe = store, batch_id, recipe
        self.fleet = fleet
        self.fixture_telemetry = fixture_telemetry
        self.sessions = {session.serial_number: session for session in participants}
        enabled = {item["test_id"] for item in store.list_attempts(batch_id)
                   if item["status"] == AttemptStatus.PENDING.value}
        flow_pool = (
            ProductionRunCoordinator.VEHICLE_TEST_IDS
            if all(test in ProductionRunCoordinator.VEHICLE_TEST_IDS for test in enabled)
            and enabled
            else ProductionRunCoordinator.FIRST_FLOW_TEST_IDS
        )
        steps = [test for test in flow_pool if test in enabled]
        self.run = ProductionRunCoordinator(
            store, batch_id, test_ids=steps, parent=self, clock=clock,
        )
        self.clock = clock
        self.policy = TrackingCriteriaPolicy.from_mapping(
            recipe.payload["duration_policy"].get("tracking_criteria"))
        self._scope = {serial: (s.core.generation, s.core.product_store.telemetry_epoch)
                       for serial, s in self.sessions.items()}
        self._request = None
        self._tables: dict[str, ParaTableReport] = {}
        self._queries: set[str] = set()
        self._param_corrected: dict[str, dict[str, str]] = {}
        self._firmware_upgrade_attempted: set[str] = set()
        self._firmware_upgrades: list = []
        self._firmware_upgrade_ok: dict[str, bool] = {}
        self._ota_factory = None
        self._evaluators: dict[str, TrackingEvaluator] = {}
        self._prelock_evaluators: dict[str, TrackingEvaluator] = {}
        self._preparation_started_at = self.clock()
        self._last_device_timestamp: dict[str, int] = {}
        self._hardware_ready = False
        self._stopped = False
        self._finishing_observation = False
        self._power_cycle_pending = False
        self._power_cycle_started_at = 0.0
        self._component_temperatures_start: dict[str, dict[str, float]] = {}
        self._pending_start_evidence: dict | None = None
        self._motion_requested = False
        self.fixture_ready: Callable[[], tuple[bool, str]] = lambda: (False, "Waiting for fixture preparation and start confirmation")
        self.preparation_reason: Callable[[], str] = lambda: "Waiting for fixture preparation and start confirmation"
        self._connections = []
        for serial, session in self.sessions.items():
            callback = lambda record, serial=serial: self._record(serial, record)
            session.core.record_received.connect(callback)
            self._connections.append((session.core.record_received, callback))
        self.run.step_requested.connect(self._step)
        self.run.progress_changed.connect(self.changed)
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.tick)

    @property
    def active(self) -> bool:
        return not self._stopped

    @property
    def snr_failures(self) -> Mapping[str, dict]:
        return {serial: {"reasons": list(evaluator.failure_reasons)}
                for serial, evaluator in self._evaluators.items() if evaluator.failure_reasons}

    @property
    def evidence_gaps(self) -> frozenset[str]:
        return frozenset(serial for serial, evaluator in self._evaluators.items()
                         if evaluator.quality_reasons)

    def tracking_metrics(self, serial: str) -> dict:
        evaluator = self._evaluators.get(serial)
        return {} if evaluator is None else evaluator.metrics()

    def start(self) -> None:
        self.run.start()
        self._timer.start()
        self.tick()

    @Slot(object)
    def _step(self, request: ProductionStepRequest) -> None:
        self._request = request
        self._tables.clear()
        self._queries.clear()
        self._evaluators.clear()
        self._prelock_evaluators.clear()
        self._preparation_started_at = self.clock()
        self._last_device_timestamp.clear()
        self._hardware_ready = False
        self._finishing_observation = False
        self._power_cycle_pending = False
        self._pending_start_evidence = None
        self._motion_requested = False
        if request.test_id in {"static_acquisition", "locked_rocking", "power_on_rocking",
                               "locked_drive", "power_on_drive"}:
            self.run.set_observation_duration(self.recipe.duration_for(request.test_id))
        if request.test_id == "locked_rocking":
            for serial in self.sessions:
                evaluator = TrackingEvaluator(self.policy, self.policy.acquisition_timeout_s +
                                               self.policy.recovery_stable_s + 1)
                evaluator.start(self._preparation_started_at)
                self._prelock_evaluators[serial] = evaluator
        self.store.record_event(self.batch_id, "automatic_step_requested", {
            "test_id": request.test_id, "ordinal": request.ordinal,
            "host_monotonic_s": self.clock(),
        })
        self.run.explain_wait("Waiting for prerequisite evidence")

    def _record(self, serial: str, record: object) -> None:
        if self._stopped or self._request is None:
            return
        if isinstance(record, ParaTableReport) and serial in self._queries:
            self._tables[serial] = record
        if isinstance(record, ServiceFastState) and not self._finishing_observation:
            if not self._hardware_ready and not self._prelock_evaluators:
                return
            previous = self._last_device_timestamp.get(serial)
            current = int(record.timestamp)
            if previous is not None and not 0 < ((current - previous) & 0xFFFFFFFF) < 0x80000000:
                return
            self._last_device_timestamp[serial] = current
            if self._prelock_evaluators and self.run.state in {
                    ProductionRunState.WAITING_PREREQUISITE, ProductionRunState.ARMED}:
                evaluator = self._prelock_evaluators[serial]
                mask = (1 << 1) | (1 << 2) | (1 << 11)
                phase = tracking_phase_from_wire(record.tracking_phase)
                evaluator.observe(self.clock(), phase is TrackingPhase.LOCKED, record.locked,
                                  float(record.snr_db), record.valid_mask & mask == mask
                                  and phase is not TrackingPhase.UNKNOWN)
                if self.run.state is ProductionRunState.ARMED:
                    self.motion_precondition_changed.emit()
            elif self._hardware_ready:
                self.record_tracking_sample(serial, record, self.clock())

    def record_tracking_sample(self, serial: str, record: ServiceFastState, received_at: float) -> None:
        """逐帧数据交给唯一判定器，界面刷新节奏不参与判定。"""
        if self._stopped or not self._hardware_ready or self._finishing_observation:
            return
        if serial not in self.sessions:
            raise ValueError("tracking sample requires a frozen participant")
        deadline = self.run.observation_deadline
        if deadline is not None and received_at > deadline:
            return
        state_mask = (1 << 1) | (1 << 2) | (1 << 11)
        phase = tracking_phase_from_wire(record.tracking_phase)
        state_valid = record.valid_mask & state_mask == state_mask and phase is not TrackingPhase.UNKNOWN
        evaluator = self._evaluators[serial]
        previous_acquired = evaluator.acquired
        previous_failures = evaluator.failure_reasons
        gnss_fixed = bool(
            record.valid_mask & (1 << 4) and record.gnss_fix in self._GNSS_FIXED_CODES
        )
        modem_online = self._modem_online(serial)
        attitude_error = self._attitude_error_deg(record, received_at, evaluator)
        evaluator.observe(received_at, phase is TrackingPhase.LOCKED, record.locked,
                          float(record.snr_db), state_valid,
                          gnss_fixed=gnss_fixed, modem_online=modem_online,
                          attitude_error_deg=attitude_error)
        if evaluator.acquired and not previous_acquired:
            self.store.record_event(self.batch_id, "scenario_acquired", {
                "test_id": self.run.current_test_id, "serial_number": serial,
                "host_monotonic_s": evaluator.acquired_at, "snr_db": float(record.snr_db),
                "tracking_phase": phase.value, "locked": record.locked,
            })
        if evaluator.failure_reasons != previous_failures:
            self.store.record_event(self.batch_id, "scenario_tracking_failed", {
                "test_id": self.run.current_test_id, "serial_number": serial,
                "host_monotonic_s": received_at, "reasons": list(evaluator.failure_reasons),
            })

    _COMPONENT_KEYS = ("converter", "tx_array", "rx_array")

    def _component_temperatures(self, serial: str) -> dict[str, float]:
        """三组件当前温度；只记录遥测已确认的有效值。"""

        session = self.sessions.get(serial)
        if session is None:
            return {}
        readings: dict[str, float] = {}
        try:
            snapshot = session.core.product_store.snapshot()
        except (AttributeError, RuntimeError):
            return readings
        for key in self._COMPONENT_KEYS:
            health = getattr(snapshot, key, None)
            value = getattr(health, "temperature_c", None) if health is not None else None
            if value is not None and value.availability is Availability.VALID \
                    and math.isfinite(float(value.value)):
                readings[key] = float(value.value)
        return readings

    def _modem_online(self, serial: str) -> Optional[bool]:
        """modem 在线来自 Product 遥测快照；不可用时保持未观察事实。"""

        session = self.sessions.get(serial)
        if session is None:
            return None
        try:
            online = session.core.product_store.snapshot().operation.modem_online
        except (AttributeError, RuntimeError):
            return None
        if online.availability is not Availability.VALID:
            return None
        return bool(online.value)

    def _attitude_error_deg(
        self, record: ServiceFastState, received_at: float, evaluator: TrackingEvaluator
    ) -> Optional[float]:
        """姿态模式误差：设备 INS 姿态相对 MS-6222 参考的三轴最大绝对差。"""

        if not evaluator.attitude_mode or self.fixture_telemetry is None:
            return None
        attitude_mask = (1 << 6) | (1 << 7) | (1 << 8)
        if record.valid_mask & attitude_mask != attitude_mask:
            return None
        readout = self.fixture_telemetry.reference_readout(
            now_ns=int(received_at * 1_000_000_000)
        )
        angles = getattr(readout, "angles", None)
        if angles is None:
            return None
        device = (record.roll_deg, record.pitch_deg, record.yaw_deg)
        worst = 0.0
        for actual, reference in zip(device, angles):
            delta = abs(float(actual) - float(reference))
            if delta > 180.0:
                delta = 360.0 - delta
            worst = max(worst, delta)
        return worst

    def device_phase(self, serial: str) -> AttemptPhase:
        if not self._hardware_ready:
            return AttemptPhase.PREPARING
        if self._finishing_observation:
            return AttemptPhase.FINISHING
        evaluator = self._evaluators.get(serial)
        return AttemptPhase.EFFECTIVE_OBSERVATION if evaluator and evaluator.acquired else AttemptPhase.CONVERGING

    def confirm_hardware(self, test_id: str, evidence: Mapping[str, object]) -> None:
        if self._stopped or self.run.current_test_id != test_id:
            return
        if self.run.state is not ProductionRunState.ARMED:
            return
        if self._power_cycle_pending:
            # 共享电源OFF/ON和本次新采集是准备证据，不要求观测到uptime倒退。
            if (not evidence.get("power_off") or not evidence.get("output")
                    or int(evidence.get("power_restored_monotonic_ns", 0)) <= 0):
                raise ValueError("power cycle start requires matched OFF/ON evidence")
            self._pending_start_evidence = dict(evidence)
            self._resume_after_power_cycle()
            return
        self._start_window(test_id, evidence)

    _VEHICLE_TEST_IDS = frozenset({"locked_drive", "power_on_drive"})
    _GNSS_FIXED_CODES = frozenset({1, 2, 3, 4, 5})

    def _power_on_anchor(self, test_id: str, evidence: Mapping[str, object]) -> float:
        """上电计时锚点：上电工况用恢复供电回执，其余工况为窗口起点。"""

        if test_id in ("power_on_rocking", "power_on_drive"):
            restored_ns = int(evidence.get("power_restored_monotonic_ns", 0) or 0)
            if restored_ns > 0:
                return restored_ns / 1_000_000_000.0
        return self.run.observation_started_at

    def _start_window(self, test_id: str, evidence: Mapping[str, object]) -> None:
        self._last_device_timestamp.clear()
        duration = float(self.run.progress.remaining_s)
        self.run.confirm_started(evidence={**evidence, "tracking_criteria": self.policy.to_payload()})
        started = self.run.observation_started_at
        attitude_mode = test_id in self._VEHICLE_TEST_IDS
        power_on_s = self._power_on_anchor(test_id, evidence)
        self._evaluators = {serial: TrackingEvaluator(
            self.policy, duration, acquisition_required=test_id != "locked_rocking",
            attitude_mode=attitude_mode)
            for serial in self.sessions}
        for evaluator in self._evaluators.values():
            evaluator.start(started, power_on_monotonic_s=power_on_s)
        self._component_temperatures_start = {
            serial: self._component_temperatures(serial) for serial in self.sessions
        }
        self._hardware_ready = True

    def prepare_power_cycle(self) -> None:
        """在已ARMED的上电工况准备一次供电循环；不宣称已测得物理重启。"""
        if (self.run.current_test_id not in {"power_on_rocking", "power_on_drive"}
                or self.run.state is not ProductionRunState.ARMED):
            raise RuntimeError("power cycle preparation requires armed power-on test")
        self._power_cycle_pending = True
        self._power_cycle_started_at = self.clock()

    def _resume_after_power_cycle(self) -> bool:
        elapsed = self.clock() - self._power_cycle_started_at
        if elapsed > self.policy.acquisition_timeout_s:
            self.fail("Power cycle and analysis capture preparation timed out")
            return False
        pending = [serial for serial, session in self.sessions.items()
                   if not session.is_online() or not session.capture_ready]
        if pending or self._pending_start_evidence is None:
            self.run.explain_wait("Waiting for power cycle and analysis capture: " + ", ".join(pending))
            if elapsed >= self.policy.acquisition_timeout_s:
                self.fail("Power cycle and analysis capture preparation timed out")
            return False
        ready, reason = self.fixture_ready()
        if not ready:
            self.fail(reason)
            return False
        evidence = {**self._pending_start_evidence, "recovery_scopes": dict(self._scope),
                    "analysis_capture_ready": list(self.sessions)}
        self._pending_start_evidence = None
        self._power_cycle_pending = False
        self._start_window(self.run.current_test_id, evidence)
        return True

    def finish_observation(self, evidence: Mapping[str, object]) -> None:
        if self._stopped or not self._finishing_observation:
            return
        results = {}
        for serial, evaluator in self._evaluators.items():
            outcome = evaluator.finish(self.run.observation_deadline)
            results[serial] = DeviceStepResult(outcome.passed, {
                "criteria": "tracking_criteria_v1", "tracking_criteria": outcome.metrics,
                "performance_verdict": outcome.performance_verdict,
                "data_quality": outcome.data_quality, "fixture_finish": dict(evidence),
                "component_temperatures": {
                    "start": self._component_temperatures_start.get(serial, {}),
                    "end": self._component_temperatures(serial),
                },
            }, incomplete=outcome.incomplete)
        self.run.complete_step(results)

    def fail(self, reason: str) -> None:
        if self._stopped:
            return
        if self.run.state in {ProductionRunState.WAITING_PREREQUISITE,
                              ProductionRunState.ARMED, ProductionRunState.RUNNING}:
            self.run.fail_shared_step(reason, evidence={"host_monotonic_s": self.clock()},
                                      device_results=self._partial_tracking_results())
        self.run.explain_wait(reason)
        self.stop()
        self.failed.emit(reason)

    def stop(self) -> None:
        if self._stopped:
            return
        self.run.abort("execution stopped", device_results=self._partial_tracking_results())
        self._stopped = True
        self._timer.stop()
        for signal, callback in self._connections:
            signal.disconnect(callback)
        self._connections.clear()

    def _partial_tracking_results(self) -> dict[str, dict]:
        """停止原因与已经观察到的性能事实同时留存。"""
        return {serial: {"tracking_criteria": evaluator.metrics(),
                         "performance_verdict": "FAIL" if evaluator.failure_reasons else "INCOMPLETE",
                         "data_quality": "INCOMPLETE"}
                for serial, evaluator in self._evaluators.items()}

    @Slot()
    def tick(self) -> None:
        if self._stopped:
            return
        try:
            self._tick()
        except (RuntimeError, ValueError, OSError, KeyError, TypeError) as exc:
            self.fail(f"{type(exc).__name__}: {exc}")

    def _tick(self) -> None:
        if self.run.state is ProductionRunState.COMPLETE:
            # 车辆待测项保留 PENDING，由批次挂起流程接管；其余未执行项明确 INCOMPLETE。
            for attempt in self.store.list_attempts(self.batch_id):
                if attempt["status"] != AttemptStatus.PENDING.value:
                    continue
                if attempt["test_id"] in self._VEHICLE_TEST_IDS:
                    continue
                self.store.transition_attempt(attempt["attempt_id"], AttemptStatus.WAITING_PREREQUISITE)
                self.store.transition_attempt(attempt["attempt_id"], AttemptStatus.INCOMPLETE,
                                              result={"reason": "Navigation summary requires navigation analysis criteria",
                                                      "verdict": "incomplete"})
            self.stop()
            self.finished.emit()
            return
        if self._hardware_ready and not self._finishing_observation:
            for evaluator in self._evaluators.values():
                evaluator.tick(min(self.clock(), self.run.observation_deadline))
        for serial, session in self.sessions.items():
            if session.serial_number and session.serial_number != serial:
                self.fail("Participant identity changed: " + serial)
                return
            scope = (session.core.generation, session.core.product_store.telemetry_epoch)
            if scope != self._scope[serial] and self._power_cycle_pending:
                self.store.record_event(self.batch_id, "power_cycle_scope_changed", {
                    "serial_number": serial, "previous": self._scope[serial], "current": scope,
                })
                self._scope[serial] = scope
            elif scope != self._scope[serial]:
                self.fail(f"Device session changed: {serial}")
                return
        if self._power_cycle_pending and not self._resume_after_power_cycle():
            return
        if (self.run.state is ProductionRunState.ARMED and self._prelock_evaluators
                and not self._motion_requested and not self._prelock_ready()):
            return
        test = self.run.current_test_id
        offline = [serial for serial, session in self.sessions.items() if not session.is_online()]
        if offline:
            self.run.explain_wait("Waiting for online devices: " + ", ".join(offline))
            if self.run.state is ProductionRunState.RUNNING and self._hardware_ready:
                acquired = tuple(s for s, e in self._evaluators.items() if e.acquired)
                if self.run.observe((), acquired_serials=acquired, maximum_gap_s=self.policy.maximum_sample_gap_s):
                    self._finishing_observation = True
                    self.observation_complete.emit(test)
            elif test == "locked_rocking":
                self._prelock_ready()
            return
        if test == "firmware_verification":
            self._firmware()
        elif test == "parameter_verification":
            self._parameters()
        else:
            self._observation()

    def _instant_start(self) -> None:
        if self.run.state is ProductionRunState.WAITING_PREREQUISITE:
            self.run.confirm_prerequisites(evidence={"identity_scopes": self._scope})
            self.run.confirm_started(evidence={"source": "current_session_readback"})

    def _firmware(self) -> None:
        rule = self.recipe.payload["expected"]["main_firmware"]
        results = {}
        upgrades: dict[str, str] = {}
        for serial, session in self.sessions.items():
            version = session.product_store.snapshot().identity.main_firmware
            if version.availability is not Availability.VALID or not version.value:
                self.run.explain_wait("Waiting for firmware readback: " + serial)
                return
            passed = rule["match"] == "optional" or version.value == rule.get("value")
            if not passed and serial not in self._firmware_upgrade_attempted:
                bin_path = str(self.recipe.payload["expected"].get("firmware_bin") or "").strip()
                if bin_path and Path(bin_path).is_file():
                    upgrades[serial] = bin_path
                    continue
            if serial in self._firmware_upgrade_attempted:
                if serial not in self._firmware_upgrade_ok:
                    # 升级进行中：等待 OTA 终态，不提前判定。
                    self.run.explain_wait("Firmware auto-upgrade in progress: " + serial)
                    continue
                passed = self._firmware_upgrade_ok[serial] and (
                    rule["match"] == "optional" or version.value == rule.get("value")
                )
            results[serial] = DeviceStepResult(passed, {"actual": version.value, "expected": rule,
                "reason": "Version recorded; exact expectation is not configured" if rule["match"] == "optional" else "",
                "auto_upgrade": serial in self._firmware_upgrade_attempted},
                skipped=rule["match"] == "optional")
        if upgrades:
            for serial, bin_path in upgrades.items():
                self._firmware_upgrade_attempted.add(serial)
                session = self.sessions[serial]
                upgrade = self._ota_factory(session, serial, bin_path) if self._ota_factory \
                    else _FirmwareAutoUpgrade(session, serial, bin_path, parent=self)
                upgrade.finished.connect(self._on_firmware_upgrade_finished)
                self._firmware_upgrades.append(upgrade)
            self.run.explain_wait(
                "Auto-upgrading firmware: "
                + ", ".join(f"{serial} ← {Path(p).name}" for serial, p in upgrades.items())
            )
            return
        if len(results) < len(self.sessions):
            self.run.explain_wait("Firmware auto-upgrade in progress")
            return
        self._instant_start()
        self.run.complete_step(results)

    def _on_firmware_upgrade_finished(self, serial: str, ok: bool, detail: str) -> None:
        self._firmware_upgrade_ok[serial] = ok
        self.store.record_event(self.batch_id, "firmware_auto_upgrade_finished", {
            "serial_number": serial, "ok": ok, "detail": detail,
        })
        if not ok:
            self.run.explain_wait(
                f"Firmware auto-upgrade failed: {serial}: {detail}; "
                "finish the upgrade manually and retest verification"
            )

    _PER_DEVICE_FACTS = {"ip": "endpoint_ip", "string": "serial_number"}

    def _per_device_expected(self, serial: str, rule: Mapping[str, object]) -> str:
        """逐设备期望值取自该设备的注册事实，而不是配方常量。"""

        fact = self._PER_DEVICE_FACTS.get(str(rule.get("type")))
        if fact is None:
            return ""
        try:
            device = self.store.get_device(self.batch_id, serial)
        except ResultStoreError:
            return ""
        return str(device.get(fact) or "").strip()

    def _parameters(self) -> None:
        rules = {key: value for key, value in self.recipe.payload["expected"]["parameters"].items()
                 if not value.get("ignore", False)}
        for serial, session in self.sessions.items():
            if rules and serial not in self._queries:
                self._queries.add(serial)
                if not self.fleet.send(session.endpoint, build_request_para_table(),
                                       operation=SessionOperationClass.READ_ONLY_QUERY):
                    self.fail("Parameter query could not be sent: " + serial)
                    return
        if rules and set(self._tables) != set(self.sessions):
            self.run.explain_wait("Waiting for parameter table readback")
            return
        corrections: dict[str, dict[str, str]] = {}
        results = {}
        for serial in self.sessions:
            table = self._tables.get(serial)
            actual = {} if table is None else {entry.name: entry.value for entry in table.params}
            matches = {}
            expected_per_device = {}
            for name, rule in rules.items():
                effective = rule
                if rule.get("per_device", False):
                    expected_value = self._per_device_expected(serial, rule)
                    if not expected_value:
                        matches[name] = False
                        expected_per_device[name] = "registered value is missing"
                        continue
                    effective = dict(rule)
                    effective["value"] = expected_value
                    expected_per_device[name] = expected_value
                ok = name in actual and compare_parameter(actual[name], effective)
                matches[name] = ok
                # M66 自动修正：显式期望值、非 IP、每台一次机会；IP 留待批次收尾。
                if not ok and serial not in self._param_corrected:
                    target = effective.get("value") if isinstance(effective, dict) else None
                    if target is not None and str(rule.get("type")) != "ip":
                        corrections.setdefault(serial, {})[name] = str(target)
            if corrections.get(serial):
                continue  # 修正后等新回读，再形成最终结果。
            results[serial] = DeviceStepResult(all(matches.values()), {
                "parameters": actual, "matches": matches, "required_count": len(rules),
                "expected": rules, "expected_per_device": expected_per_device,
                "auto_corrected": dict(self._param_corrected.get(serial, {})),
                "reason": "Parameter expectations are not configured" if not rules else "",
            }, skipped=not rules)
        if corrections:
            for serial, name_values in corrections.items():
                self._param_corrected[serial] = dict(name_values)
                session = self.sessions[serial]
                for name, value in name_values.items():
                    if not self.fleet.send(session.endpoint, build_para_set(name, value),
                                           operation=SessionOperationClass.MUTATING):
                        self.fail("Parameter auto-correction could not be sent: " + serial)
                        return
                self._tables.pop(serial, None)
                self.fleet.send(session.endpoint, build_request_para_table(),
                                operation=SessionOperationClass.READ_ONLY_QUERY)
                self.run.explain_wait(
                    "Auto-correcting parameters: "
                    + ", ".join(
                        f"{serial}:{name}={value}"
                        for serial, name_values in corrections.items()
                        for name, value in name_values.items()
                    )
                )
            return
        self._instant_start()
        self.run.complete_step(results)

    def _observation(self) -> None:
        if self.run.state is ProductionRunState.WAITING_PREREQUISITE:
            if self.run.current_test_id == "locked_rocking":
                if not self._prelock_ready():
                    return
            self.run.confirm_prerequisites(evidence={
                "criteria": "tracking_criteria_v1",
                "tracking_criteria": self.policy.to_payload(),
                "duration_s": self.run.progress.remaining_s,
            })
            self.run.explain_wait("Waiting for fixture preparation and start confirmation")
            self.hardware_requested.emit(self._request)
            return
        if not self._hardware_ready:
            self.run.explain_wait(self.preparation_reason())
            return
        if self._finishing_observation:
            self.run.explain_wait("Finishing fixture motion and verifying center")
            return
        fixture_valid, reason = self.fixture_ready()
        if self._stopped:
            return
        if not fixture_valid:
            self.fail(reason)
            return
        acquired = [serial for serial, evaluator in self._evaluators.items() if evaluator.acquired]
        valid = [serial for serial, evaluator in self._evaluators.items()
                 if evaluator.acquired and evaluator.healthy and not evaluator.quality_reasons]
        searching = set(self.sessions) - set(acquired)
        self.run.explain_wait("Waiting for initial acquisition confirmation" if searching else "")
        if self.run.observe(valid, acquired_serials=acquired, maximum_gap_s=self.policy.maximum_sample_gap_s):
            self._finishing_observation = True
            self.observation_complete.emit(self.run.current_test_id)

    def motion_start_ready(self) -> bool:
        """硬件提交摇摆前消费最新逐帧锁定证据。"""
        if self.run.current_test_id != "locked_rocking":
            return True
        if self.run.state is not ProductionRunState.ARMED:
            return False
        ready = self._prelock_ready()
        if ready:
            self._motion_requested = True
        return ready

    def _prelock_ready(self) -> bool:
        """逐帧判定器确认运动前稳定，250ms界面节奏不遗漏中间失锁。"""
        now = self.clock()
        ready = set()
        for serial, evaluator in self._prelock_evaluators.items():
            evaluator.tick(now)
            if (evaluator.acquired and evaluator.healthy_duration_s >= self.policy.acquisition_stable_s
                    and self.sessions[serial].is_online()
                    and now - self._preparation_started_at <= self.policy.acquisition_timeout_s):
                ready.add(serial)
        if ready == set(self.sessions):
            return True
        if now - self._preparation_started_at >= self.policy.acquisition_timeout_s:
            self.run.complete_preparation({serial: DeviceStepResult(False, {
                "reason": "Initial acquisition timeout" if serial not in ready else "Shared motion start unavailable",
                "performance_verdict": "FAIL" if self._prelock_evaluators[serial].failure_reasons else "INCOMPLETE",
                "tracking_criteria": self._prelock_evaluators[serial].metrics(),
                "data_quality": "INCOMPLETE" if self._prelock_evaluators[serial].quality_reasons else "COMPLETE",
            }, incomplete=not self._prelock_evaluators[serial].failure_reasons) for serial in self.sessions})
        else:
            self.run.explain_wait("Waiting for initial acquisition confirmation")
        return False
