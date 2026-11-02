"""正式批次执行服务：步骤、证据准入和有效观察由领域层拥有。"""
from __future__ import annotations

import ipaddress
import math
import time
from typing import Callable, Mapping, Sequence

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from satellite_debug_tool.core.product import Availability, TrackingPhase
from satellite_debug_tool.core.product.models import tracking_phase_from_wire
from satellite_debug_tool.core.protocol import ParaTableReport, ServiceFastState, build_request_para_table
from satellite_debug_tool.core.session.command_sender import SessionOperationClass
from .fleet import DeviceSession, FleetController
from .models import AttemptPhase, AttemptStatus
from .recipe import ProductionRecipe
from .result_store import ProductionResultStore
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
                 clock: Callable[[], float] = time.monotonic, parent=None) -> None:
        super().__init__(parent)
        self.store, self.batch_id, self.recipe = store, batch_id, recipe
        self.fleet = fleet
        self.sessions = {session.serial_number: session for session in participants}
        enabled = {item["test_id"] for item in store.list_attempts(batch_id)
                   if item["status"] == AttemptStatus.PENDING.value}
        steps = [test for test in ProductionRunCoordinator.FIRST_FLOW_TEST_IDS if test in enabled]
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
        self._evaluators: dict[str, TrackingEvaluator] = {}
        self._prelock_evaluators: dict[str, TrackingEvaluator] = {}
        self._preparation_started_at = self.clock()
        self._last_device_timestamp: dict[str, int] = {}
        self._hardware_ready = False
        self._stopped = False
        self._finishing_observation = False
        self._power_cycle_pending = False
        self._power_cycle_started_at = 0.0
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
        if request.test_id in {"static_acquisition", "locked_rocking", "power_on_rocking"}:
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
        if received_at > self.run.observation_deadline:
            return
        state_mask = (1 << 1) | (1 << 2) | (1 << 11)
        phase = tracking_phase_from_wire(record.tracking_phase)
        state_valid = record.valid_mask & state_mask == state_mask and phase is not TrackingPhase.UNKNOWN
        evaluator = self._evaluators[serial]
        previous_acquired = evaluator.acquired
        previous_failures = evaluator.failure_reasons
        evaluator.observe(received_at, phase is TrackingPhase.LOCKED, record.locked,
                          float(record.snr_db), state_valid)
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

    def _start_window(self, test_id: str, evidence: Mapping[str, object]) -> None:
        self._last_device_timestamp.clear()
        duration = float(self.run.progress.remaining_s)
        self.run.confirm_started(evidence={**evidence, "tracking_criteria": self.policy.to_payload()})
        started = self.run.observation_started_at
        self._evaluators = {serial: TrackingEvaluator(
            self.policy, duration, acquisition_required=test_id != "locked_rocking")
            for serial in self.sessions}
        for evaluator in self._evaluators.values():
            evaluator.start(started)
        self._hardware_ready = True

    def prepare_power_cycle(self) -> None:
        """在已ARMED的摇摆上电项准备一次供电循环；不宣称已测得物理重启。"""
        if self.run.current_test_id != "power_on_rocking" or self.run.state is not ProductionRunState.ARMED:
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
            for attempt in self.store.list_attempts(self.batch_id):
                if attempt["status"] != AttemptStatus.PENDING.value:
                    continue
                test_id = attempt["test_id"]
                reason = ("Vehicle scenario requires operator start and driving evidence"
                          if test_id in {"locked_drive", "power_on_drive"}
                          else "Navigation summary requires navigation analysis criteria")
                self.store.transition_attempt(attempt["attempt_id"], AttemptStatus.WAITING_PREREQUISITE)
                self.store.transition_attempt(attempt["attempt_id"], AttemptStatus.INCOMPLETE,
                                              result={"reason": reason, "verdict": "incomplete"})
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
        for serial, session in self.sessions.items():
            version = session.product_store.snapshot().identity.main_firmware
            if version.availability is not Availability.VALID or not version.value:
                self.run.explain_wait("Waiting for firmware readback: " + serial)
                return
            passed = rule["match"] == "optional" or version.value == rule.get("value")
            results[serial] = DeviceStepResult(passed, {"actual": version.value, "expected": rule,
                "reason": "Version recorded; exact expectation is not configured" if rule["match"] == "optional" else ""},
                skipped=rule["match"] == "optional")
        self._instant_start()
        self.run.complete_step(results)

    def _parameters(self) -> None:
        rules = {key: value for key, value in self.recipe.payload["expected"]["parameters"].items()
                 if not value.get("ignore", False)}
        if any(rule.get("per_device", False) for rule in rules.values()):
            self.run.explain_wait("Recipe requires per-device parameter expectations")
            return
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
        results = {}
        for serial in self.sessions:
            table = self._tables.get(serial)
            actual = {} if table is None else {entry.name: entry.value for entry in table.params}
            matches = {name: name in actual and compare_parameter(actual[name], rule)
                       for name, rule in rules.items()}
            results[serial] = DeviceStepResult(all(matches.values()), {
                "parameters": actual, "matches": matches, "required_count": len(rules), "expected": rules,
                "reason": "Parameter expectations are not configured" if not rules else "",
            }, skipped=not rules)
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
