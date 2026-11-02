"""MS 精度仅作提示；有效数据、平台启动证据和 SNR 判定各自保持合同。"""

from __future__ import annotations

from dataclasses import replace, dataclass
import re
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QCoreApplication, QEvent, Signal

from satellite_debug_tool.core.motion_platform import (
    MotionCommandEvent, MotionPlatformSnapshot, MotionPlatformState, PlatformPose,
)
from satellite_debug_tool.core.production import (
    FixtureRuntime, FixtureRuntimeState, ProductionRecipe, ProductionRunState,
)
from satellite_debug_tool.core.production import execution_hardware as module
from satellite_debug_tool.core.production.power_supply import (
    PowerActionResult, PowerCommandRecord, PowerEvidenceLevel, PowerMeasurement, PowerSupplyState,
)
from satellite_debug_tool.core.production.ms6222_protocol import Ms6222InsRecord, Ms6222StreamParser
from satellite_debug_tool.tests.test_ms6222_protocol import _ins_frame
from satellite_debug_tool.tests.test_production_configuration_workspace import _catalog
from satellite_debug_tool.tests.test_production_execution import execution, confirm_prelock


def _output(enabled: bool = True) -> PowerActionResult:
    return PowerActionResult(
        PowerSupplyState.ON_CONFIRMED if enabled else PowerSupplyState.READY_OFF,
        PowerEvidenceLevel.VOLTAGE_CONFIRMED if enabled else PowerEvidenceLevel.OUTPUT_STATE_CONFIRMED,
        enabled, PowerMeasurement(24.0 if enabled else .024, .5 if enabled else 0), 0, 0, False,
    )


def _power_records(hardware, enabled: bool, *, at_ns: int | None = None):
    at = module.time.monotonic_ns() if at_ns is None else at_ns
    action_id = hardware.power.action_id
    return (
        PowerCommandRecord(f"OUTP {int(enabled)}", "", False, 1, at, 1, action_id),
        PowerCommandRecord("OUTP?", str(int(enabled)), True, 2, at + 1, 1, action_id),
    )


class _Power(QObject):
    output_changed = Signal(bool, object, object, str)

    def __init__(self) -> None:
        super().__init__()
        self.action_id = "POWER-CYCLE-1"
        self.outputs: list[bool] = []

    def request_output(self, enabled: bool) -> None:
        self.outputs.append(enabled)


class _Recorder:
    session_id = "REFERENCE-START-REGRESSION"

    def __init__(self) -> None:
        self.frames = []
        self.measurements = []

    def record_ms_frame(self, envelope) -> None:
        self.frames.append(envelope)

    def record_measurement(self, sample) -> None:
        self.measurements.append(sample)


class _Control:
    def __init__(self, snapshot: MotionPlatformSnapshot) -> None:
        self.controller = SimpleNamespace(snapshot=snapshot, config=SimpleNamespace(feedback_fresh_ms=500))

    def stop_sequence(self) -> None:
        pass

    def submit_trajectory(self, _trajectory) -> str:
        return "TRAJECTORY-1"


@pytest.fixture
def reference_hardware(qapplication_session, tmp_path, monkeypatch, request):
    clock = [0.0]
    origin_ns = 1_000_000_000_000
    wall_ns = 1_700_000_000_000_000_000
    test_time = SimpleNamespace(
        monotonic=lambda: origin_ns / 1e9 + clock[0],
        monotonic_ns=lambda: origin_ns + round(clock[0] * 1e9),
        time_ns=lambda: wall_ns + round(clock[0] * 1e9),
    )
    monkeypatch.setattr(module, "time", test_time)
    catalog = _catalog(tmp_path / "catalog.json")
    configuration = catalog.resolve("afd01c-standard", "line-1", 2)
    runtime = FixtureRuntime(profile_store=catalog.fixture_profile_store,
                             calibration_store=catalog.calibration_store)
    runtime.bind_configuration(configuration)
    test_id = getattr(request, "param", "locked_rocking")
    service, store, _sent = execution(tmp_path, test_id, clock)
    payload = service.recipe.payload
    payload["fixtures"]["motion_platform"]["profile"]["ramp_in_s"] = 5
    if test_id == "power_on_rocking":
        service.policy = replace(service.policy, acquisition_timeout_s=20)
        payload["duration_policy"].update(minimum_effective_observation_s=30,
                                          tracking_criteria=service.policy.to_payload())
        payload["fixtures"]["motion_platform"]["profile"]["steady_duration_s"] = 30
    service.recipe = ProductionRecipe.from_mapping(payload)
    if test_id == 'locked_rocking':
        clock[0] = -.075
    service.start()
    if test_id == 'locked_rocking':
        confirm_prelock(service, clock)
    assert service.run.state is ProductionRunState.ARMED
    hardware = module.ProductionExecutionHardware(service, runtime, configuration, _Power(), tmp_path)
    hardware._test_id = test_id
    monkeypatch.setattr(hardware, "_open_reference_worker", lambda: None)
    hardware.recorder = _Recorder()
    baseline = MotionPlatformSnapshot(
        MotionPlatformState.READY, 1, feedback_monotonic_ns=origin_ns, feedback_age_ms=0,
        actual_pulses=(100, 100, 100, 100, 100, 100),
    )
    hardware.control = _Control(baseline)
    hardware._reference_connection(True, configuration.reference_profile.serial_port)
    envelope = Ms6222StreamParser().feed(
        _ins_frame(), host_monotonic_ns=origin_ns, host_wall_time_ns=wall_ns,
    )[0]
    assert isinstance(envelope.record, Ms6222InsRecord)

    def reference(yaw_std_deg: float, **values) -> None:
        sample = replace(envelope, host_monotonic_ns=test_time.monotonic_ns(),
                         host_wall_time_ns=test_time.time_ns(),
                         record=replace(envelope.record, yaw_std_deg=yaw_std_deg, **values))
        hardware._reference_frame(sample)

    def start_trajectory() -> None:
        if test_id == "power_on_rocking":
            hardware._connected()
            hardware._power_changed(False, _output(False), _power_records(hardware, False), hardware.power.action_id)
        else:
            hardware._start_trajectory()
        event = MotionCommandEvent(
            action="trajectory_point", raw_command=b"fixture-command",
            target_pose=PlatformPose(1.0, 0.0, 0.0, z_mm=100),
            target_pulses=(120, 110, 115, 100, 105, 125), duration_ms=100,
            monotonic_ns=test_time.monotonic_ns(), generation=1,
        )
        hardware._sent(event, event.action, 1)

    def moved_feedback() -> None:
        snapshot = replace(
            baseline, state=MotionPlatformState.TRAJECTORY,
            feedback_monotonic_ns=test_time.monotonic_ns(),
            actual_pulses=(120, 110, 115, 100, 105, 125),
        )
        hardware.control.controller.snapshot = snapshot
        hardware._snapshot(snapshot)

    yield SimpleNamespace(service=service, hardware=hardware, runtime=runtime,
                          clock=clock, reference=reference,
                          start_trajectory=start_trajectory, moved_feedback=moved_feedback)
    service.stop()
    store.close()
    hardware.deleteLater()
    service.deleteLater()
    runtime.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapplication_session.processEvents()


def test_reference_quality_loss_revokes_ready_and_explains_yaw_threshold(reference_hardware) -> None:
    case = reference_hardware
    case.reference(0.9722)
    assert case.runtime.fact("reference").state is FixtureRuntimeState.READY

    case.clock[0] = 3.0
    case.reference(1.42)

    fact = case.runtime.fact("reference")
    assert fact.state is not FixtureRuntimeState.READY
    ready, reason = case.hardware._reference_data_gate()
    assert ready is True
    assert reason == ""
    assert "yaw" in fact.evidence.lower()
    numbers = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", fact.evidence)]
    assert 1.42 in numbers
    assert 1.0 in numbers  # 档案门限作为明确事实展示。
    assert case.hardware.recorder.frames[-1].record.yaw_std_deg == 1.42
    assert case.hardware.recorder.measurements[-1].valid is False


def test_reference_precision_loss_during_ramp_allows_countdown(reference_hardware) -> None:
    case = reference_hardware
    failed = []
    case.service.failed.connect(failed.append)
    case.reference(0.85)
    case.start_trajectory()

    # 现场 2046：最后有效 yaw_std 在首轨迹后 2.684546083 秒，随后超过 1°。
    for elapsed, yaw_std in ((2.684546083, 0.9722), (3.0, 1.08), (5.1, 1.42), (10.0, 1.58)):
        case.clock[0] = elapsed
        case.reference(yaw_std)
        case.moved_feedback()

    assert case.service.run.state is ProductionRunState.RUNNING
    assert case.service.active is True
    assert failed == []
    assert case.service.run.progress.remaining_s == 0
    assert case.runtime.fact("reference").state is not FixtureRuntimeState.READY


def test_locked_rocking_counts_ramp_from_first_fresh_motion_feedback(reference_hardware) -> None:
    case = reference_hardware
    case.reference(0.9)
    case.start_trajectory()
    assert case.hardware._motion_started_at is not None

    case.clock[0] = 0.1
    case.reference(0.95)
    case.moved_feedback()
    assert case.service.run.state is ProductionRunState.RUNNING
    assert case.service.run.progress.remaining_s == 2.0
    assert case.hardware.observation_ready() == (True, "")

    case.clock[0] = 0.3
    case.reference(0.97)
    case.moved_feedback()
    assert case.service.run.state is ProductionRunState.RUNNING
    assert case.service.run.progress.remaining_s == pytest.approx(1.8)
    case.clock[0] = 0.6
    assert case.service.run.progress.remaining_s == pytest.approx(1.5)


def test_armed_prelock_loss_waits_for_new_stable_frames_before_motion(reference_hardware):
    from satellite_debug_tool.tests.test_production_execution import tracking_sample

    case = reference_hardware
    case.reference(0.9)
    submitted = []
    case.hardware.control.submit_trajectory = lambda trajectory: submitted.append(trajectory) or "MOTION-1"
    case.clock[0] = .1
    for session in case.service.sessions.values():
        session.core.record_received.emit(tracking_sample(0, locked=False, timestamp=100))
    case.hardware._start_trajectory()
    assert case.hardware._operation == "waiting_prelock_trajectory"
    assert submitted == []
    for index, moment in enumerate((.2, .225, .25, .275), 101):
        case.clock[0] = moment
        for session in case.service.sessions.values():
            session.core.record_received.emit(tracking_sample(12, timestamp=index))
    assert case.hardware._operation == "trajectory"
    assert len(submitted) == 1
    case.service.motion_precondition_changed.emit()
    assert len(submitted) == 1


@pytest.mark.parametrize("reference_hardware", ("power_on_rocking",), indirect=True)
def test_power_on_rocking_accepts_precision_advisory_after_motion_confirmation(reference_hardware) -> None:
    case = reference_hardware
    failed = []
    case.service.failed.connect(failed.append)
    case.reference(0.9)
    case.start_trajectory()
    case.clock[0] = 5.1
    case.reference(1.42)
    case.moved_feedback()

    assert case.hardware.power.outputs == [False, True]
    assert case.service.run.state is ProductionRunState.ARMED  # 仍等待电源输出回读。
    assert failed == []


def test_reference_freshness_expiration_revokes_runtime_ready(reference_hardware) -> None:
    case = reference_hardware
    case.reference(0.9)
    assert case.runtime.fact("reference").state is FixtureRuntimeState.READY
    case.clock[0] = case.hardware.configuration.reference_profile.freshness_timeout_s + 0.1

    ready, reason = case.hardware._reference_data_gate()

    assert ready is False
    assert "fresh" in reason.lower()
    assert case.runtime.fact("reference").state is FixtureRuntimeState.CONNECTED
    assert case.runtime.fact("reference").evidence == reason


@pytest.mark.parametrize("reference_hardware", ("static_acquisition", "locked_rocking"), indirect=True)
@pytest.mark.parametrize("std", (1.42, 8.0, float("nan"), -1.0))
def test_precision_advisory_preserves_runtime_observation_and_snr_verdict(reference_hardware, std):
    from satellite_debug_tool.tests.test_production_execution import tracking_sample

    case = reference_hardware
    case.reference(std)
    if case.hardware._test_id == "static_acquisition":
        case.hardware._start_evidence = {"center": "feedback_confirmed"}
        case.hardware._confirm_start()
    else:
        case.start_trajectory()
        case.clock[0] = 5.1
        case.reference(std)
        case.moved_feedback()
    assert case.service.run.state is ProductionRunState.RUNNING
    assert case.hardware.observation_ready() == (True, "")
    started = case.clock[0]
    for offset in (0, 0.5, 1.0, 1.5, 2.0):
        case.clock[0] = started + offset
        case.reference(std)
        for serial in case.service.sessions:
            case.service.record_tracking_sample(serial, tracking_sample(12), case.clock[0])
        if case.hardware._test_id != "static_acquisition":
            case.moved_feedback()
        case.service.tick()
    case.service.finish_observation({"center": "feedback_confirmed"})
    attempts = case.service.store.list_attempts(case.service.batch_id)
    assert {attempt["status"] for attempt in attempts} == {"pass"}
    assert case.runtime.fact("reference").state is FixtureRuntimeState.CONNECTED


@pytest.mark.parametrize("values", ({"gnss_state": 0}, {"yaw_deg": float("nan")}))
def test_invalid_reference_data_still_blocks_start(reference_hardware, values):
    case = reference_hardware
    case.reference(8.0, **values)
    case.start_trajectory()
    assert case.hardware._operation == "waiting_reference_trajectory"
    assert case.service.run.state is ProductionRunState.ARMED
    assert case.hardware._reference_data_gate()[0] is False


def test_snr_drop_during_ramp_counts_with_reference_precision_advisory(reference_hardware):
    from satellite_debug_tool.tests.test_production_execution import tracking_sample

    case = reference_hardware
    case.reference(8.0)
    case.start_trajectory()
    case.clock[0] = .1
    case.reference(8.0)
    case.moved_feedback()
    for offset in (0, 0.5, 1.0, 1.5, 2.0):
        case.clock[0] = .1 + offset
        case.reference(8.0)
        case.moved_feedback()
        for index, serial in enumerate(case.service.sessions):
            snr = 0 if offset == 1.0 and index == 0 else 12
            case.service.record_tracking_sample(serial, tracking_sample(snr), case.clock[0])
        case.service.tick()
    case.service.finish_observation({"center": "feedback_confirmed"})
    assert {item["status"] for item in case.service.store.list_attempts(case.service.batch_id)} == {"pass", "fail"}


@pytest.mark.parametrize("reference_hardware", ("power_on_rocking",), indirect=True)
def test_shared_power_restores_output_without_reference_and_waits_for_new_frames(reference_hardware):
    case = reference_hardware
    failed = []
    case.service.failed.connect(failed.append)
    case.reference(0.9)
    old_at = case.hardware._reference_at_ns
    case.start_trajectory()  # 真实关电意图和关电回读。
    assert case.hardware._reference_data_valid is False
    case.hardware._reference_connection(False, "MS-PORT")
    case.hardware._reference_error("expected loss during shared power-off")
    case.clock[0] = 6.0  # 已超过 MS 新鲜度，平台仍须能触发恢复供电。
    case.moved_feedback()
    assert case.hardware.power.outputs == [False, True]
    assert failed == []
    assert case.service.run.state is ProductionRunState.ARMED
    assert case.hardware._reference_at_ns == 0

    case.hardware._power_changed(True, _output(), _power_records(case.hardware, True), case.hardware.power.action_id)
    assert "recovery" in case.hardware.preparation_reason()
    assert case.service.run.state is ProductionRunState.ARMED
    assert case.hardware._power_restored_at_ns > old_at
    # 模拟恢复供电前已接收、主线程迟到的旧帧。
    case.clock[0] = 5.9
    case.reference(0.9)
    assert case.hardware._reference_at_ns == 0
    case.clock[0] = 6.2
    case.hardware._reference_connection(True, "MS-PORT")
    case.reference(8.0)
    assert case.service.run.state is ProductionRunState.ARMED  # MS恢复后仍等待本次DUT采集。
    for session in case.service.sessions.values():
        assert session.capture_restorations == [case.hardware._power_restored_at_ns]
        session.capture_ready = True
    case.service.tick()
    assert case.service.run.state is ProductionRunState.RUNNING
    case.clock[0] = 6.7
    assert case.service.run.progress.remaining_s == pytest.approx(29.5)
    assert failed == []
    case.hardware._reference_connection(False, "MS-PORT")
    assert len(failed) == 1  # 正常观察阶段的真实断连继续生效。


@pytest.mark.parametrize("reference_hardware", ("power_on_rocking",), indirect=True)
def test_shared_power_requires_real_motion_before_requesting_output_on(reference_hardware):
    case = reference_hardware
    case.start_trajectory()
    case.clock[0] = 6.0
    baseline = case.hardware.control.controller.snapshot
    case.hardware._snapshot(replace(baseline, state=MotionPlatformState.TRAJECTORY,
                                   feedback_monotonic_ns=1_006_000_000_000))
    assert case.hardware.power.outputs == [False]
    case.service.run.abort("operator stop during power-off")
    case.hardware.quiesce()
    case.moved_feedback()
    assert case.hardware.power.outputs == [False]


@pytest.mark.parametrize("reference_hardware", ("power_on_rocking",), indirect=True)
def test_shared_power_serial_restart_waits_for_old_reader_to_finish(reference_hardware, monkeypatch):
    case = reference_hardware
    class Reader:
        def __init__(self):
            self.stops = 0
            self.deleted = False
        def stop(self):
            self.stops += 1
        def deleteLater(self):
            self.deleted = True
    reader = Reader()
    case.hardware._reference_worker = reader
    opened = []
    monkeypatch.setattr(case.hardware, "_open_reference_worker", lambda: opened.append("open"))
    case.start_trajectory()
    assert reader.stops == 1
    case.clock[0] = 6.0
    case.moved_feedback()
    case.hardware._power_changed(True, _output(), _power_records(case.hardware, True), case.hardware.power.action_id)
    assert opened == []
    case.hardware._reference_worker_finished()
    assert opened == ["open"]
    assert reader.deleted
    # 新采集器发生异常结束，不作无限重开。
    case.hardware._reference_worker = Reader()
    case.hardware._reference_worker_finished()
    assert opened == ["open"]


@pytest.mark.parametrize('reference_hardware', ('power_on_rocking',), indirect=True)
@pytest.mark.parametrize('fault', ['action', 'old_records', 'missing_readback'])
def test_power_cycle_ignores_unmatched_output_receipts(reference_hardware, fault):
    case = reference_hardware
    case.start_trajectory()
    case.clock[0] = 6.0
    case.moved_feedback()
    records = _power_records(case.hardware, True)
    action_id = case.hardware.power.action_id
    if fault == 'action':
        action_id = 'OTHER-OWNER'
    elif fault == 'old_records':
        records = _power_records(case.hardware, True, at_ns=1_000_000_000_000)
    else:
        records = records[:1]
    case.hardware._power_changed(True, _output(), records, action_id)
    assert case.hardware._power_phase is module.PowerOnRockingPhase.POWERING_ON
    assert all(session.capture_restorations == [] for session in case.service.sessions.values())
    assert case.service.run.state is ProductionRunState.ARMED


@pytest.mark.parametrize('reference_hardware', ('power_on_rocking',), indirect=True)
def test_power_cycle_rejects_output_result_contradicting_command(reference_hardware):
    case = reference_hardware
    case.start_trajectory()
    case.clock[0] = 6.0
    case.moved_feedback()
    case.hardware._power_changed(True, _output(False), _power_records(case.hardware, True),
                                 case.hardware.power.action_id)
    assert not case.service.active
    assert all(session.capture_restorations == [] for session in case.service.sessions.values())


@pytest.mark.parametrize('reference_hardware', ('power_on_rocking',), indirect=True)
def test_power_cycle_duplicate_on_and_quiesced_callback_do_not_prepare_capture(reference_hardware):
    case = reference_hardware
    case.start_trajectory()
    case.clock[0] = 6.0
    case.moved_feedback()
    result, records, action_id = _output(), _power_records(case.hardware, True), case.hardware.power.action_id
    case.hardware._power_changed(True, result, records, action_id)
    case.hardware._power_changed(True, result, records, action_id)
    assert all(len(session.capture_restorations) == 1 for session in case.service.sessions.values())
    case.hardware.quiesce()
    case.hardware._power_changed(True, result, records, action_id)
    assert all(len(session.capture_restorations) == 1 for session in case.service.sessions.values())
