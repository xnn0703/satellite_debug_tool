"""正式台架动作适配：复用运动组件和电源 worker 的唯一 I/O owner。"""
from __future__ import annotations

from dataclasses import asdict, replace
import math
from enum import Enum
from pathlib import Path
import time

from PySide6.QtCore import QObject, Slot

from satellite_debug_tool.core.motion_platform import MotionPlatformState, SineAxis, SineTrajectory
from .configuration import ResolvedProductionConfiguration
from .execution import ProductionExecution
from .fixture_control import FixtureControlWorker
from .fixture_runtime import FixtureRuntime, FixtureRuntimeState
from .fixture_analysis import TimedAttitude
from .ms6222_worker import Ms6222SerialWorker
from .ms6222_protocol import Ms6222FrameType, Ms6222InsRecord
from .fixture_session import FixtureSessionRecorder, FixtureSessionConclusion
from .fixture_telemetry import FixtureAttitudeSource, FixtureAttitudeSample
from .production_power import ProductionPowerWorker
from .power_supply import PowerActionResult, PowerEvidenceLevel, PowerSupplyState


class PowerOnRockingPhase(str, Enum):
    IDLE = "idle"
    POWERING_OFF = "powering_off"
    POWER_OFF = "power_off"
    POWERING_ON = "powering_on"
    RECOVERING_REFERENCE = "recovering_reference"
    OBSERVING = "observing"


class ProductionExecutionHardware(QObject):
    def __init__(self, execution: ProductionExecution, runtime: FixtureRuntime,
                 configuration: ResolvedProductionConfiguration,
                 power: ProductionPowerWorker, output: Path, *, parent=None) -> None:
        super().__init__(parent)
        self.execution, self.runtime, self.configuration = execution, runtime, configuration
        self.power = power
        self.output = output
        self.control = None
        self.recorder = None
        self._test_id = ""
        self._request_id = ""
        self._operation = ""
        self._finishing = False
        self._motion_started_at = None
        self._ramp_in_s = 0.0
        self._trajectory_result = None
        self._closed = False
        self._motion_confirmed = False
        self._baseline_pulses = ()
        self._reference_worker = None
        self._reference_stopping_worker = None
        self._reference_handle = None
        self._reference_data_valid = False
        self._reference_data_reason = "Waiting for valid MS-6222 reference"
        self._reference_at_ns = 0
        self._start_evidence = None
        self._power_phase = PowerOnRockingPhase.IDLE
        self._power_restored_at_ns = 0
        self._power_requested_at_ns = 0
        self._power_off_evidence: dict | None = None
        execution.hardware_requested.connect(self.prepare)
        execution.observation_complete.connect(self.finish)
        execution.motion_precondition_changed.connect(self._motion_precondition_changed)
        power.output_changed.connect(self._power_changed)
        execution.fixture_ready = self.observation_ready
        execution.preparation_reason = self.preparation_reason

    @Slot(object)
    def prepare(self, request) -> None:
        if self._closed:
            return
        try:
            self._test_id = request.test_id
            self._finishing = False
            self._motion_started_at = None
            self._trajectory_result = None
            self._start_evidence = None
            if self.control is None:
                if self.runtime.motion_owner != f"batch:{self.execution.batch_id}":
                    raise RuntimeError("formal motion requires the batch fixture lease")
                self.recorder = FixtureSessionRecorder(
                    self.configuration.motion_profile, root=self.output / "fixtures",
                )
                self.recorder.start()
                self._start_reference()
                self.control = FixtureControlWorker(
                    self.configuration.motion_profile, recorder=self.recorder, parent=self,
                )
                self.control.confirm_preflight()
                self.control.connected.connect(self._connected)
                self.control.request_finished.connect(self._finished)
                self.control.request_failed.connect(self._failed)
                self.control.evidence_failed.connect(self.execution.fail)
                self.control.send_result.connect(self._sent)
                self.control.snapshot_changed.connect(self._snapshot)
                self.control.start()
            else:
                self._connected()
        except (RuntimeError, ValueError, OSError) as exc:
            self.execution.fail(str(exc))

    def _start_reference(self) -> None:
        profile = self.configuration.reference_profile
        self._reference_handle = self.runtime.reference_lease.acquire(f"batch:{self.execution.batch_id}")
        self._open_reference_worker()

    def _open_reference_worker(self) -> None:
        profile = self.configuration.reference_profile
        worker = Ms6222SerialWorker(profile.serial_port, baudrate=profile.baud_rate, parent=self)
        self._reference_worker = worker
        worker.frame_received.connect(self._reference_frame)
        worker.invalid_frame_received.connect(self._reference_frame)
        worker.connection_changed.connect(self._reference_connection)
        worker.error_occurred.connect(self._reference_error)
        worker.finished.connect(self._reference_worker_finished)
        self.runtime.telemetry.set_reference_connected(False)
        self.runtime.set_state("reference", FixtureRuntimeState.CONNECTING, profile.serial_port)
        worker.start()

    @Slot()
    def _reference_worker_finished(self) -> None:
        worker = self._reference_worker
        restart = worker is not None and worker is self._reference_stopping_worker
        self._reference_worker = None
        self._reference_stopping_worker = None
        if worker is not None:
            worker.deleteLater()
        if restart and not self._closed and self._power_phase is PowerOnRockingPhase.RECOVERING_REFERENCE:
            self._open_reference_worker()

    def _reference_power_cycle_active(self) -> bool:
        return self._power_phase in {
            PowerOnRockingPhase.POWERING_OFF, PowerOnRockingPhase.POWER_OFF,
            PowerOnRockingPhase.POWERING_ON, PowerOnRockingPhase.RECOVERING_REFERENCE,
        }

    def _clear_reference_data(self, reason: str) -> None:
        self._reference_data_valid = False
        self._reference_at_ns = 0
        self._reference_data_reason = reason
        self.runtime.telemetry.set_reference_connected(False)
        self.runtime.set_state("reference", FixtureRuntimeState.CONFIGURED, reason)

    @Slot(bool, str)
    def _reference_connection(self, connected: bool, port: str) -> None:
        self.runtime.telemetry.set_reference_connected(connected)
        if not connected:
            self._reference_data_valid = False
            self._reference_data_reason = "MS-6222 reference disconnected"
        self.runtime.set_state("reference", FixtureRuntimeState.CONNECTED if connected else FixtureRuntimeState.CONFIGURED, port)
        if not connected and not self._closed and not self._reference_power_cycle_active():
            self.execution.fail("MS-6222 reference disconnected")

    @Slot(str)
    def _reference_error(self, reason: str) -> None:
        if not self._closed:
            if self._power_phase in {
                PowerOnRockingPhase.POWERING_OFF, PowerOnRockingPhase.POWER_OFF,
                PowerOnRockingPhase.POWERING_ON,
            }:
                self.runtime.set_state("reference", FixtureRuntimeState.CONFIGURED,
                                       "MS-6222 shares the controlled power cycle: " + reason)
                self.execution.store.record_event(self.execution.batch_id, "reference_power_cycle_event",
                                                 {"phase": self._power_phase.value, "serial_error": reason})
                return
            self.runtime.set_state("reference", FixtureRuntimeState.FAULT, reason)
            self.execution.fail(reason)

    @Slot(object)
    def _reference_frame(self, envelope) -> None:
        try:
            self._apply_reference_frame(envelope)
        except (RuntimeError, ValueError, OSError) as exc:
            self.execution.fail(str(exc))

    def _apply_reference_frame(self, envelope) -> None:
        if self._closed:
            return
        self.recorder.record_ms_frame(envelope)
        if self._power_phase in {
            PowerOnRockingPhase.POWERING_OFF, PowerOnRockingPhase.POWER_OFF,
            PowerOnRockingPhase.POWERING_ON,
        }:
            return
        if (self._power_phase is PowerOnRockingPhase.RECOVERING_REFERENCE
                and envelope.host_monotonic_ns < self._power_restored_at_ns):
            return
        if not envelope.valid or envelope.frame_type is not Ms6222FrameType.INSPVAXB:
            return
        record = envelope.record
        if not isinstance(record, Ms6222InsRecord):
            return
        profile = self.configuration.reference_profile
        angles = (record.roll_deg, record.pitch_deg, record.yaw_deg)
        stds = (record.roll_std_deg, record.pitch_std_deg, record.yaw_std_deg)
        if record.gnss_state not in {1, 3}:
            reason = f"MS-6222 navigation state is unsuitable for reference: gnss_state={record.gnss_state}"
        elif any(not math.isfinite(value) for value in angles):
            reason = "MS-6222 attitude contains invalid values"
        else:
            reason = ""
        sample_reason = reason
        if sample_reason == "":
            named_stds = tuple(zip(("roll_std", "pitch_std", "yaw_std"), stds))
            invalid_stds = [f"{axis}={value:g} deg" for axis, value in named_stds
                            if not math.isfinite(value) or value < 0]
            failed_stds = [f"{axis}={value:g} deg (limit {profile.max_attitude_std_deg:g} deg)"
                           for axis, value in named_stds if math.isfinite(value) and value > profile.max_attitude_std_deg]
            if invalid_stds:
                reason = "MS-6222 attitude uncertainty contains invalid values: " + "; ".join(invalid_stds)
            else:
                reason = ("MS-6222 attitude uncertainty exceeds profile limit: " + "; ".join(failed_stds)) if failed_stds else ""
        # 标准差描述参考精度；SNR 工况的采集准入只要求有效且新鲜的姿态数据。
        precision_reason = reason
        self._reference_data_reason = sample_reason
        self._reference_data_valid = sample_reason == ""
        self._reference_at_ns = envelope.host_monotonic_ns
        self.runtime.telemetry.publish_reference_attitude(
            host_monotonic_ns=envelope.host_monotonic_ns, roll_deg=record.roll_deg,
            pitch_deg=record.pitch_deg, yaw_deg=record.yaw_deg, gnss_state=record.gnss_state,
        )
        raw = TimedAttitude(envelope.host_monotonic_ns, *angles, valid=self._reference_data_valid)
        calibrated = self.configuration.fixture_calibration.apply(raw)
        self._reference_data_valid = self._reference_data_valid and calibrated.valid
        valid = self._reference_data_valid and precision_reason == ""
        self.recorder.record_measurement(replace(raw, valid=valid))
        if not self._reference_data_valid and not self._reference_data_reason:
            self._reference_data_reason = "Waiting for valid MS-6222 reference"
        self.runtime.telemetry.publish(FixtureAttitudeSample(
            source=FixtureAttitudeSource.MS6222_RELATIVE,
            host_monotonic_ns=envelope.host_monotonic_ns, host_wall_time_ns=envelope.host_wall_time_ns,
            roll_deg=calibrated.roll_deg if valid else None,
            pitch_deg=calibrated.pitch_deg if valid else None,
            yaw_deg=calibrated.yaw_deg if valid else None,
            valid=valid, invalid_reason="" if valid else "reference_quality_invalid",
            fixture_profile_id=self.configuration.motion_profile.profile_id,
            fixture_profile_sha256=self.configuration.motion_profile.sha256,
            calibration_id=self.configuration.fixture_calibration.calibration_id,
            control_session_id=self.recorder.session_id,
        ))
        self.runtime.set_state(
            "reference", FixtureRuntimeState.READY if valid else FixtureRuntimeState.CONNECTED,
            "valid calibrated MS-6222 frames" if valid else (precision_reason or self._reference_data_reason),
        )
        if self._reference_data_valid and self._operation == "waiting_reference_trajectory":
            self._start_trajectory()
        self._confirm_start()

    def _reference_data_gate(self) -> tuple[bool, str]:
        if not self._reference_data_valid:
            return False, self._reference_data_reason
        timeout = self.configuration.reference_profile.freshness_timeout_s
        if time.monotonic_ns() - self._reference_at_ns > timeout * 1e9:
            reason = "Waiting for fresh MS-6222 reference"
            self.runtime.set_state("reference", FixtureRuntimeState.CONNECTED, reason)
            return False, reason
        return True, ""

    def preparation_reason(self) -> str:
        if self.control is None:
            return "Waiting for motion connection"
        if self._power_phase is PowerOnRockingPhase.POWERING_OFF:
            return "Waiting for power output-off confirmation"
        if self._power_phase is PowerOnRockingPhase.POWERING_ON:
            return "Waiting for power output-on confirmation"
        if self._power_phase is PowerOnRockingPhase.RECOVERING_REFERENCE:
            return "Waiting for MS-6222 recovery after power-on"
        if self._power_phase is not PowerOnRockingPhase.POWER_OFF:
            reference_valid, reason = self._reference_data_gate()
            if not reference_valid:
                return reason
        if self._operation.startswith("center"):
            return "Waiting for platform center confirmation"
        if self._operation == "power_off":
            return "Waiting for power output-off confirmation"
        if self._operation == "waiting_prelock_trajectory":
            return "Waiting for initial acquisition confirmation"
        if self._operation == "trajectory":
            if self._motion_started_at is None:
                return "Waiting for trajectory command send confirmation"
            if (self._test_id == "power_on_rocking"
                    and time.monotonic() - self._motion_started_at < self._ramp_in_s):
                return "Waiting for trajectory steady phase"
            return "Waiting for motion feedback confirmation"
        return "Waiting for fixture preparation and start confirmation"

    def _confirm_start(self) -> None:
        if self._closed or self._start_evidence is None:
            return
        valid, reason = self._reference_data_gate()
        if not valid:
            self.execution.run.explain_wait(reason)
            if (self._operation == "trajectory" and self._motion_confirmed
                    and self._power_phase is not PowerOnRockingPhase.RECOVERING_REFERENCE):
                # 运动已启动，参考数据失效或过期时进入证据收尾；标准差单独呈现。
                self.execution.fail(reason)
            return
        evidence = self._start_evidence
        self._start_evidence = None
        if self._power_phase is PowerOnRockingPhase.RECOVERING_REFERENCE:
            self._power_phase = PowerOnRockingPhase.OBSERVING
        self.execution.confirm_hardware(self._test_id, evidence)

    @Slot()
    def _connected(self) -> None:
        if self._closed:
            return
        try:
            if self._test_id == "power_on_rocking":
                self.execution.prepare_power_cycle()
                self._power_off_evidence = None
                self._power_requested_at_ns = time.monotonic_ns()
                self._power_phase = PowerOnRockingPhase.POWERING_OFF
                self._operation = "power_off"
                self._clear_reference_data("MS-6222 shares the controlled power cycle")
                if self._reference_worker is not None:
                    self._reference_stopping_worker = self._reference_worker
                    self._reference_worker.stop()
                self.power.request_output(False)
            else:
                self._operation = "center_prepare"
                self._request_id = self.control.submit_center(duration_ms=1000)
        except (RuntimeError, ValueError) as exc:
            self.execution.fail(str(exc))

    def _trajectory(self) -> SineTrajectory:
        config = self.execution.recipe.payload["fixtures"]["motion_platform"]["profile"]
        self._ramp_in_s = float(config.get("ramp_in_s", 0))
        axes = {}
        for axis in ("roll", "pitch", "yaw"):
            raw = config[axis]
            axes[axis] = SineAxis(
                amplitude_deg=float(raw["amplitude_deg"]), frequency_hz=float(raw["frequency_hz"]),
                phase_deg=float(raw.get("phase_deg", 0)), enabled=raw.get("enabled", True),
            )
        return SineTrajectory(
            sample_period_ms=int(config["sample_period_ms"]), ramp_in_s=self._ramp_in_s,
            # 工况观察拥有唯一结束时刻；上电和参考恢复耗时不消耗运动时长。
            steady_duration_s=None,
            ramp_out_s=0,
            work_z_mm=self.configuration.motion_profile.center_pose.z_mm,
            profile_id=config["profile_id"], **axes,
        )

    def _start_trajectory(self) -> None:
        if self._power_phase is not PowerOnRockingPhase.POWER_OFF:
            reference_valid, _reason = self._reference_data_gate()
            if not reference_valid:
                self._operation = "waiting_reference_trajectory"
                return
        if not self.execution.motion_start_ready():
            self._operation = "waiting_prelock_trajectory"
            return
        self._baseline_pulses = self.control.controller.snapshot.actual_pulses
        self._motion_confirmed = False
        self._operation = "trajectory"
        self._request_id = self.control.submit_trajectory(self._trajectory())

    @Slot()
    def _motion_precondition_changed(self) -> None:
        if self._closed or self._operation != "waiting_prelock_trajectory":
            return
        try:
            self._start_trajectory()
        except (RuntimeError, ValueError, KeyError) as exc:
            self.execution.fail(str(exc))

    @Slot(bool, object, object, str)
    def _power_changed(self, enabled, result, records, action_id) -> None:
        """当前供电动作的闭环回执推进准备；旧动作、错阶段及停止后回执无副作用。"""
        if self._closed or self._test_id != "power_on_rocking" or action_id != self.power.action_id:
            return
        expected = (PowerOnRockingPhase.POWERING_ON if enabled
                    else PowerOnRockingPhase.POWERING_OFF)
        if self._power_phase is not expected:
            return
        # action_id整批复用，必须同时匹配本次请求之后的OUTP命令与回读。
        command = f"OUTP {int(enabled)}"
        current = [item for item in records
                   if item.fixture_action_id == action_id
                   and item.monotonic_ns >= self._power_requested_at_ns]
        writes = [item for item in current if item.command == command and not item.is_query]
        if not writes:
            return
        issued = writes[-1]
        readback = [item for item in current if item.is_query and item.command == "OUTP?"
                    and item.monotonic_ns > issued.monotonic_ns
                    and item.response.strip() == str(int(enabled))]
        if not readback:
            return
        try:
            states = ((PowerSupplyState.ON_CONFIRMED,) if enabled
                      else (PowerSupplyState.OFF_CONFIRMED, PowerSupplyState.READY_OFF))
            evidence = (PowerEvidenceLevel.VOLTAGE_CONFIRMED if enabled
                        else PowerEvidenceLevel.OUTPUT_STATE_CONFIRMED)
            if (not isinstance(result, PowerActionResult) or result.state not in states
                    or result.evidence_level is not evidence or result.output_enabled is not enabled
                    or result.protection_tripped or result.questionable_condition
                    or not math.isfinite(result.measurement.voltage_v)):
                raise ValueError("power output result does not match the current verified action")
            record_end = max(item.monotonic_ns for item in current)
            if enabled:
                if self._power_off_evidence is None:
                    raise ValueError("power restoration requires this cycle's verified OFF")
                self._power_phase = PowerOnRockingPhase.RECOVERING_REFERENCE
                self._power_restored_at_ns = time.monotonic_ns()
                for session in self.execution.sessions.values():
                    session.prepare_capture_after_power_cycle(self._power_restored_at_ns)
                self._clear_reference_data("Waiting for MS-6222 recovery after power-on")
                self._start_evidence = {
                    "power_action": action_id, "power_off": self._power_off_evidence,
                    "output": asdict(result), "power_on_record_monotonic_ns": record_end,
                    "power_restored_monotonic_ns": self._power_restored_at_ns,
                }
                if self._reference_worker is None:
                    self._open_reference_worker()
                self._confirm_start()
            else:
                self._power_off_evidence = {
                    "action_id": action_id, "output": asdict(result),
                    "record_monotonic_ns": record_end,
                    "participants": list(self.execution.sessions),
                }
                self._power_phase = PowerOnRockingPhase.POWER_OFF
                self._clear_reference_data("MS-6222 shares the controlled power cycle")
                self._start_trajectory()
        except (RuntimeError, ValueError, KeyError) as exc:
            self.execution.fail(str(exc))

    @Slot(object, str, int)
    def _sent(self, event, action, sequence) -> None:
        if self._closed:
            return
        if event.target_pose is not None:
            pose = event.target_pose
            self.runtime.telemetry.publish(FixtureAttitudeSample(
                source=FixtureAttitudeSource.PLATFORM_TARGET,
                host_monotonic_ns=event.monotonic_ns, host_wall_time_ns=time.time_ns(),
                roll_deg=pose.roll_deg, pitch_deg=pose.pitch_deg, yaw_deg=pose.yaw_deg,
                fixture_profile_id=self.configuration.motion_profile.profile_id,
                control_session_id=self.recorder.session_id,
            ))
        if self._operation == "trajectory" and self._motion_started_at is None:
            self._motion_started_at = event.monotonic_ns / 1e9

    @Slot(object)
    def _snapshot(self, snapshot) -> None:
        if self._closed and snapshot.state is MotionPlatformState.OFFLINE:
            self.runtime.set_state("motion", FixtureRuntimeState.CONFIGURED, snapshot.details)
        if not self._closed:
            states = {
                MotionPlatformState.READY: FixtureRuntimeState.READY,
                MotionPlatformState.MOVING: FixtureRuntimeState.RUNNING,
                MotionPlatformState.TRAJECTORY: FixtureRuntimeState.RUNNING,
                MotionPlatformState.HOMING: FixtureRuntimeState.RUNNING,
                MotionPlatformState.FAULT: FixtureRuntimeState.FAULT,
                MotionPlatformState.RECOVERY_REQUIRED: FixtureRuntimeState.FAULT,
                MotionPlatformState.OFFLINE: FixtureRuntimeState.CONFIGURED,
                MotionPlatformState.STARTING: FixtureRuntimeState.CONNECTING,
            }
            self.runtime.set_state("motion", states[snapshot.state], snapshot.details)
        if self._closed or self._operation != "trajectory" or self._motion_confirmed or self._motion_started_at is None:
            return
        moved = bool(snapshot.actual_pulses and snapshot.actual_pulses != self._baseline_pulses)
        fresh = snapshot.feedback_monotonic_ns / 1e9 >= self._motion_started_at
        phase_ready = (self._test_id == "locked_rocking"
                       or time.monotonic() - self._motion_started_at >= self._ramp_in_s)
        if moved and fresh and phase_ready and snapshot.state is MotionPlatformState.TRAJECTORY:
            if self._power_phase is not PowerOnRockingPhase.POWER_OFF:
                reference_valid, reason = self._reference_data_gate()
                if not reference_valid:
                    self.execution.fail(reason)
                    return
            self._motion_confirmed = True
            if self._test_id == "power_on_rocking":
                self._power_phase = PowerOnRockingPhase.POWERING_ON
                self._power_requested_at_ns = time.monotonic_ns()
                self.power.request_output(True)
            else:
                self._start_evidence = {"motion_request": self._request_id, "motion_feedback": asdict(snapshot)}
                self._confirm_start()

    @Slot(str, object)
    def _finished(self, request_id, result) -> None:
        if self._closed or request_id != self._request_id:
            return
        try:
            if self._operation.startswith("center"):
                if not result.successful:
                    raise RuntimeError("platform center requires feedback-confirmed arrival")
                if self._finishing:
                    self.execution.finish_observation({"center": asdict(result), "trajectory": self._trajectory_result})
                elif self._test_id == "static_acquisition":
                    self._start_evidence = {"center": asdict(result)}
                    self._confirm_start()
                else:
                    self._start_trajectory()
            elif self._operation == "trajectory":
                self._trajectory_result = asdict(result)
                if self.execution.run.progress.remaining_s == 0:
                    self.execution.tick()
                if self._finishing:
                    self._operation = "center_finish"
                    self._request_id = self.control.submit_center(duration_ms=1000)
                else:
                    raise RuntimeError("trajectory ended before the effective observation window completed")
        except (RuntimeError, ValueError, KeyError) as exc:
            self.execution.fail(str(exc))

    @Slot(str, str)
    def _failed(self, request_id, reason) -> None:
        if not self._closed:
            self.execution.fail(reason)

    def observation_ready(self) -> tuple[bool, str]:
        if self.control is None:
            return False, "Waiting for motion connection"
        snapshot = self.control.controller.snapshot
        if snapshot.feedback_age_ms is None or snapshot.feedback_age_ms > self.control.controller.config.feedback_fresh_ms:
            self.execution.fail("Motion feedback became stale")
            return False, "Motion feedback became stale"
        if snapshot.state in {MotionPlatformState.FAULT, MotionPlatformState.RECOVERY_REQUIRED, MotionPlatformState.OFFLINE}:
            self.execution.fail("Motion feedback requires recovery: " + snapshot.details)
            return False, "Motion feedback requires recovery"
        reference_valid, reason = self._reference_data_gate()
        if not reference_valid:
            return False, reason
        if self._test_id == "power_on_rocking":
            if self._motion_started_at is None or time.monotonic() - self._motion_started_at < self._ramp_in_s:
                return False, "Waiting for trajectory steady phase"
        return True, ""

    @Slot(str)
    def finish(self, test_id: str) -> None:
        if self._closed:
            return
        self._finishing = True
        try:
            if self._operation == "trajectory":
                self.control.stop_sequence()
            else:
                self._operation = "center_finish"
                self._request_id = self.control.submit_center(duration_ms=1000)
        except (RuntimeError, ValueError) as exc:
            self.execution.fail(str(exc))

    def quiesce(self) -> None:
        """立即停止接纳新动作；实际连接和证据由收尾线程关闭。"""
        self._closed = True

    def close(self) -> None:
        self._closed = True
        if self._reference_worker is not None:
            self._reference_worker.stop()
        if self.control is not None:
            self.control.shutdown()
            if not self.control.wait(10000):
                raise RuntimeError("formal motion worker is still stopping")
        if self._reference_worker is not None:
            if not self._reference_worker.wait(2000):
                raise RuntimeError("formal MS-6222 worker is still stopping")
        if self._reference_handle is not None:
            self.runtime.reference_lease.release(self._reference_handle)
            self._reference_handle = None
        if self.recorder is not None:
            self.recorder.complete(conclusion=FixtureSessionConclusion.INCONCLUSIVE, notes="Formal fixture evidence; device verdicts are stored in batch attempts")
        self.power.output_changed.disconnect(self._power_changed)
