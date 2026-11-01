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
from .run_coordinator import (
    DeviceStepResult, ProductionRunCoordinator, ProductionRunState, ProductionStepRequest,
)


def acquisition_confirmed(phase: TrackingPhase, locked: bool, snr_db: float) -> bool:
    """初扫/精扫仍属于初始搜星；正式锁定与正 SNR 同帧回报才确认完成。"""
    return phase is TrackingPhase.LOCKED and locked is True and math.isfinite(snr_db) and snr_db > 0


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
            observation_s=recipe.payload["duration_policy"]["minimum_effective_observation_s"],
        )
        self.clock = clock
        self._scope = {serial: (s.core.generation, s.core.product_store.telemetry_epoch)
                       for serial, s in self.sessions.items()}
        self._request = None
        self._tables: dict[str, ParaTableReport] = {}
        self._queries: set[str] = set()
        self._acquired: dict[str, float] = {}
        self._snr_failures: dict[str, dict] = {}
        self._last_sample: dict[str, float] = {}
        self._sample_counts: dict[str, int] = {}
        self._last_device_timestamp: dict[str, int] = {}
        self._tracking_state_seen: set[str] = set()
        self._evidence_gaps: set[str] = set()
        self._hardware_ready = False
        self._stopped = False
        self._finishing_observation = False
        self._expected_boot = False
        self._boot_seen: set[str] = set()
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
        return dict(self._snr_failures)

    @property
    def evidence_gaps(self) -> frozenset[str]:
        return frozenset(self._evidence_gaps)

    def start(self) -> None:
        self.run.start()
        self._timer.start()
        self.tick()

    @Slot(object)
    def _step(self, request: ProductionStepRequest) -> None:
        self._request = request
        self._tables.clear()
        self._queries.clear()
        self._acquired.clear()
        self._snr_failures.clear()
        self._last_sample.clear()
        self._sample_counts.clear()
        self._last_device_timestamp.clear()
        self._tracking_state_seen.clear()
        self._evidence_gaps.clear()
        self._hardware_ready = False
        self._finishing_observation = False
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
        if isinstance(record, ServiceFastState) and self._hardware_ready and not self._finishing_observation:
            previous = self._last_device_timestamp.get(serial)
            current = int(record.timestamp)
            if previous is not None and not 0 < ((current - previous) & 0xFFFFFFFF) < 0x80000000:
                return
            self._last_device_timestamp[serial] = current
            self.record_tracking_sample(serial, record, self.clock())

    def record_tracking_sample(self, serial: str, record: ServiceFastState, received_at: float) -> None:
        """同帧设备阶段、锁定和 SNR 决定首次搜星完成；之后逐帧执行正值判据。"""
        if self._stopped or not self._hardware_ready or self._finishing_observation:
            return
        if serial not in self.sessions:
            raise ValueError("tracking sample requires a frozen participant")
        if self._expected_boot and serial not in self._boot_seen:
            return
        if self.run.progress.remaining_s == 0:
            return
        state_mask = (1 << 1) | (1 << 2)
        phase = tracking_phase_from_wire(record.tracking_phase)
        state_valid = record.valid_mask & state_mask == state_mask and phase is not TrackingPhase.UNKNOWN
        if state_valid:
            self._tracking_state_seen.add(serial)
        if not record.valid_mask & (1 << 11):
            return
        value = float(record.snr_db)
        previous = self._last_sample.get(serial)
        if previous is not None and received_at - previous > 3.0:
            self._evidence_gaps.add(serial)
        self._last_sample[serial] = received_at
        self._sample_counts[serial] = self._sample_counts.get(serial, 0) + 1
        if serial not in self._acquired:
            if state_valid and acquisition_confirmed(phase, record.locked, value):
                self._acquired[serial] = received_at
                self.store.record_event(self.batch_id, "scenario_acquired", {
                    "test_id": self.run.current_test_id, "serial_number": serial,
                    "host_monotonic_s": received_at, "snr_db": value,
                    "tracking_phase": phase.value, "locked": record.locked,
                })
        elif (not math.isfinite(value) or value <= 0) and serial not in self._snr_failures:
            self._snr_failures[serial] = {"host_monotonic_s": received_at, "snr_db": value if math.isfinite(value) else None}
            self.store.record_event(self.batch_id, "scenario_snr_failed", {
                "test_id": self.run.current_test_id, "serial_number": serial,
                **self._snr_failures[serial],
            })

    def device_phase(self, serial: str) -> AttemptPhase:
        if not self._hardware_ready:
            return AttemptPhase.PREPARING
        if self._finishing_observation:
            return AttemptPhase.FINISHING
        return AttemptPhase.EFFECTIVE_OBSERVATION if serial in self._acquired else AttemptPhase.CONVERGING

    def confirm_hardware(self, test_id: str, evidence: Mapping[str, object]) -> None:
        if self._stopped or self.run.current_test_id != test_id:
            return
        if self.run.state is not ProductionRunState.ARMED:
            return
        self.run.confirm_started(evidence=evidence)
        self._hardware_ready = True

    def expect_boot(self) -> None:
        if self.run.current_test_id != "power_on_rocking" or self.run.state is not ProductionRunState.ARMED:
            raise RuntimeError("boot expectation requires armed power-on test")
        self._expected_boot = True
        self._boot_seen.clear()

    def finish_observation(self, evidence: Mapping[str, object]) -> None:
        if self._stopped or not self._finishing_observation:
            return
        self.run.complete_step({serial: DeviceStepResult(
            serial in self._acquired and serial not in self._snr_failures and serial not in self._evidence_gaps,
            {"criteria": "snr_strictly_positive_after_initial_acquisition",
             "acquired_monotonic_s": self._acquired.get(serial),
             "snr_failure": self._snr_failures.get(serial),
             "evidence_gap": serial in self._evidence_gaps,
             "sample_count": self._sample_counts.get(serial, 0),
             "tracking_state_available": serial in self._tracking_state_seen,
             "fixture_finish": dict(evidence)},
            incomplete=serial not in self._snr_failures and (
                serial in self._evidence_gaps or self._sample_counts.get(serial, 0) == 0
                or serial not in self._tracking_state_seen),
        ) for serial in self.sessions})

    def fail(self, reason: str) -> None:
        if self._stopped:
            return
        if self.run.state in {ProductionRunState.WAITING_PREREQUISITE,
                              ProductionRunState.ARMED, ProductionRunState.RUNNING}:
            self.run.fail_shared_step(reason, evidence={"host_monotonic_s": self.clock()})
        self.run.explain_wait(reason)
        self.stop()
        self.failed.emit(reason)

    def stop(self) -> None:
        if self._stopped:
            return
        self.run.abort("execution stopped")
        self._stopped = True
        self._timer.stop()
        for signal, callback in self._connections:
            signal.disconnect(callback)
        self._connections.clear()

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
        for serial, session in self.sessions.items():
            if session.serial_number and session.serial_number != serial:
                self.fail("Participant identity changed: " + serial)
                return
            scope = (session.core.generation, session.core.product_store.telemetry_epoch)
            if scope != self._scope[serial] and self._expected_boot and serial not in self._boot_seen:
                self._scope[serial] = scope
                self._boot_seen.add(serial)
                self._last_device_timestamp.pop(serial, None)
            elif scope != self._scope[serial]:
                self.fail(f"Device session changed: {serial}")
                return
        test = self.run.current_test_id
        offline = [serial for serial, session in self.sessions.items() if not session.is_online()]
        if offline:
            if self._hardware_ready:
                self._evidence_gaps.update(offline)
            self.run.explain_wait("Waiting for online devices: " + ", ".join(offline))
            if self.run.state is ProductionRunState.RUNNING and self._hardware_ready:
                if self.run.observe((), acquired_serials=tuple(self._acquired), maximum_gap_s=3.0):
                    self._finishing_observation = True
                    self.observation_complete.emit(test)
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
            results[serial] = DeviceStepResult(passed, {"actual": version.value, "expected": rule})
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
            })
        self._instant_start()
        self.run.complete_step(results)

    def _observation(self) -> None:
        if self.run.state is ProductionRunState.WAITING_PREREQUISITE:
            if self.run.current_test_id == "locked_rocking":
                for session in self.sessions.values():
                    operation = session.product_store.snapshot().operation
                    values = (operation.tracking_phase, operation.locked, operation.snr_db)
                    if any(value.availability is not Availability.VALID for value in values) or not acquisition_confirmed(
                        operation.tracking_phase.value, operation.locked.value, operation.snr_db.value,
                    ):
                        self.run.explain_wait("Waiting for initial acquisition confirmation")
                        return
            self.run.confirm_prerequisites(evidence={
                "criteria": "snr_strictly_positive_after_initial_acquisition",
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
        if self._expected_boot:
            if self._boot_seen == set(self.sessions):
                self._expected_boot = False
            else:
                self.run.explain_wait("Waiting for new device boot evidence")
        now = self.clock()
        for serial in self.sessions:
            last = self._last_sample.get(serial)
            if last is not None and now - last > 3.0:
                self._evidence_gaps.add(serial)
        valid = [serial for serial in self._acquired if serial not in self._snr_failures and serial not in self._evidence_gaps]
        if not self._expected_boot:
            searching = set(self.sessions) - set(self._acquired)
            self.run.explain_wait("Waiting for initial acquisition confirmation" if searching else "")
        if self.run.observe(valid, acquired_serials=tuple(self._acquired), maximum_gap_s=3.0):
            if self._expected_boot:
                self._evidence_gaps.update(set(self.sessions) - self._boot_seen)
            self._finishing_observation = True
            self.observation_complete.emit(self.run.current_test_id)
