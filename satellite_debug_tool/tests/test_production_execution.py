"""M47 执行链路与有效观察计时回归。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal
import pytest

from satellite_debug_tool.core.product import Availability, ProductSnapshot, ProductValue, TrackingPhase
from satellite_debug_tool.core.production import (
    AttemptStatus,
    ProductionRecipe,
    ProductionRunCoordinator,
    ProductionRunState,
    RecipeValidationError,
)
from satellite_debug_tool.core.production.execution import ProductionExecution, compare_parameter
from satellite_debug_tool.core.production.tracking_criteria import TrackingCriteriaPolicy
from satellite_debug_tool.core.protocol import ParaEntry, ParaTableReport, ServiceFastState
from satellite_debug_tool.tests.test_production_run_coordinator import _running_store
from satellite_debug_tool.tests.test_production_recipe import valid_recipe
from satellite_debug_tool.tests.test_production_workspace import _dispose_created_production_widgets


def tracking_sample(snr: float, *, phase: int = 3, locked: bool = True,
                    valid_mask: int = 0xfff, timestamp: int = 1) -> ServiceFastState:
    return ServiceFastState(1, timestamp, valid_mask, 0, phase, locked, 3, 3, True,
                            0., 0., 0., 0., 0., snr)


class Core(QObject):
    record_received = Signal(object)
    def __init__(self):
        super().__init__()
        self.generation = 1
        self.product_store = SimpleNamespace(telemetry_epoch=1)


def execution(tmp_path, test_id, clock, config=None):
    store = _running_store(tmp_path, (test_id,))
    payload = valid_recipe(duration_s=2)
    # 两秒软件用例显式使用短窗口策略；正式默认仍为5/300/2/5/.005/3/2。
    payload['duration_policy']['tracking_criteria'] = replace(
        TrackingCriteriaPolicy(), acquisition_stable_s=.05, acquisition_timeout_s=1,
        recovery_stable_s=.1, interruption_max_s=.25, interruption_ratio_max=.25,
    ).to_payload()
    if config is not None:
        payload['tests'][test_id] = dict(enabled=True, **config)
    recipe = ProductionRecipe.from_mapping(payload)
    sessions = []
    for serial in ('AFD01C-A', 'AFD01C-B'):
        core = Core()
        session = SimpleNamespace(serial_number=serial, core=core, endpoint=('127.0.0.1', 4004),
                                  is_online=lambda: True, capture_ready=True)
        session.capture_restorations = []
        def prepare_capture(restored_ns, session=session):
            session.capture_restorations.append(restored_ns)
            session.capture_ready = False
        session.prepare_capture_after_power_cycle = prepare_capture
        def snapshot():
            base = ProductSnapshot()
            valid = lambda value: ProductValue(value, Availability.VALID, received_monotonic_s=clock[0])
            return replace(base, identity=replace(base.identity, main_firmware=valid('0.0.130 beta')),
                           operation=replace(base.operation, tracking_phase=valid(TrackingPhase.LOCKED), locked=valid(True), snr_db=valid(12.0)))
        session.product_store = SimpleNamespace(snapshot=snapshot)
        sessions.append(session)
    sent = []
    fleet = SimpleNamespace(send=lambda *args, **kw: sent.append(args) or True)
    service = ProductionExecution(store, 'BATCH-AUTO', recipe, fleet, sessions, clock=lambda: clock[0])
    return service, store, sent


def confirm_prelock(service, clock):
    """向运动前准备提供连续的新帧，样本和时钟都前进。"""
    began = clock[0]
    for index, offset in enumerate((0, .025, .05, .075), 1):
        clock[0] = began + offset
        for session in service.sessions.values():
            session.core.record_received.emit(tracking_sample(12, timestamp=index))
        service.tick()


def test_fixed_window_counts_search_and_preserves_per_device_evidence(tmp_path):
    clock = [0.0]
    store = _running_store(tmp_path, ('static_acquisition',))
    run = ProductionRunCoordinator(store, 'BATCH-AUTO', test_ids=('static_acquisition',),
                                   clock=lambda: clock[0], observation_s=30)
    run.start()
    clock[0] = 100
    assert run.progress.remaining_s == 30  # 准备时间独立。
    run.confirm_prerequisites(evidence={'fixture': 'ready'})
    run.confirm_started(evidence={'center': 'feedback_confirmed'})
    run.observe((), acquired_serials=(), maximum_gap_s=3)
    clock[0] = 101
    run.observe(('AFD01C-A',), acquired_serials=('AFD01C-A',), maximum_gap_s=3)
    assert run.progress.remaining_s == 29  # 搜星时间同属配方工况时间。
    clock[0] = 102
    run.observe(('AFD01C-A',), acquired_serials=('AFD01C-A',), maximum_gap_s=3)
    assert run.progress.effective_s == {'AFD01C-A': 1, 'AFD01C-B': 0}
    clock[0] = 131
    assert run.observe((), acquired_serials=('AFD01C-A',), maximum_gap_s=3)  # 缺口不延长窗口。
    assert run.progress.remaining_s == 0
    assert run.state is ProductionRunState.RUNNING  # 到零仍待分析/硬件收尾。
    store.close()


def test_start_drives_firmware_attempt_instead_of_leaving_everything_pending(qapplication_session, tmp_path):
    service, store, _ = execution(tmp_path, 'firmware_verification', [0.0])
    service.start()
    assert service.run.state is ProductionRunState.COMPLETE
    assert {a['status'] for a in store.list_attempts('BATCH-AUTO')} == {'pass'}
    service.stop()
    store.close()


def test_start_requests_real_fixture_preparation(qapplication_session, tmp_path):
    service, store, _ = execution(tmp_path, 'static_acquisition', [0.0])
    requests = []
    service.hardware_requested.connect(requests.append)
    service.start()
    assert service.run.state is ProductionRunState.ARMED
    assert len(requests) == 1
    assert service.run.progress.effective_s == {'AFD01C-A': 0, 'AFD01C-B': 0}
    service.stop()
    store.close()


def test_initial_search_and_tolerated_transient_keep_independent_event_evidence(qapplication_session, tmp_path):
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'static_acquisition', clock)
    service.fixture_ready = lambda: (True, '')
    completed = []
    service.observation_complete.connect(completed.append)
    service.start()
    clock[0] = 30
    service.tick()
    assert service.run.progress.remaining_s == 2
    service.confirm_hardware('static_acquisition', {'center': 'feedback_confirmed'})
    for serial in service.sessions:
        service.record_tracking_sample(serial, tracking_sample(0, phase=1, locked=False), 30)
        service.record_tracking_sample(serial, tracking_sample(12), 30.1)
        service.record_tracking_sample(serial, tracking_sample(12), 30.16)
    # 两帧位于同一个 UI 计时片段；采样入口必须保留这次归零。
    service.record_tracking_sample('AFD01C-A', tracking_sample(0, phase=4, locked=False), 30.2)
    service.record_tracking_sample('AFD01C-A', tracking_sample(12), 30.3)
    for serial in service.sessions:
        service.record_tracking_sample(serial, tracking_sample(12), 30.5)
        service.record_tracking_sample(serial, tracking_sample(12), 31.5)
    clock[0] = 31
    service.tick()
    clock[0] = 32
    service.tick()
    assert completed == ['static_acquisition']
    assert service.run.progress.phase.value == 'effective_observation'
    assert service.device_phase('AFD01C-A').value == 'finishing'
    assert service.run.state is ProductionRunState.RUNNING
    service.finish_observation({'center': 'feedback_confirmed'})
    attempts = {a['serial_number']: a for a in store.list_attempts('BATCH-AUTO')}
    assert attempts['AFD01C-A']['status'] == 'pass'
    assert attempts['AFD01C-B']['status'] == 'pass'
    assert attempts['AFD01C-A']['result_json']['tracking_criteria']['interruption_count'] == 1
    assert attempts['AFD01C-B']['result_json']['tracking_criteria']['interruption_count'] == 0
    assert service.run.state is ProductionRunState.COMPLETE
    service.stop()
    store.close()


def test_device_epoch_change_invalidates_active_test(qapplication_session, tmp_path):
    service, store, _ = execution(tmp_path, 'static_acquisition', [0.0])
    service.start()
    service.sessions['AFD01C-A'].core.product_store.telemetry_epoch += 1
    service.tick()
    assert service.run.state is ProductionRunState.INCOMPLETE
    service.stop()
    store.close()


@pytest.mark.parametrize('actual,rule,passed', [
    ('nan', {'type': 'float', 'range': [0, 20]}, False),
    ('1', {'type': 'bool', 'value': True}, True),
    ('256', {'type': 'uint8', 'value': 256}, False),
    ('1.01', {'type': 'float', 'value': 1, 'abs_tol': .02}, True),
    ('4', {'type': 'int', 'one_of': [1, 2]}, False),
])
def test_parameter_comparison_contract(actual, rule, passed):
    assert compare_parameter(actual, rule) is passed


def test_dynamic_start_requires_changed_controller_feedback(qapplication_session, tmp_path):
    import time
    from PySide6.QtCore import QObject, Signal
    from satellite_debug_tool.core.motion_platform.models import MotionPlatformSnapshot, MotionPlatformState
    from satellite_debug_tool.core.production.execution_hardware import ProductionExecutionHardware

    class Power(QObject):
        output_changed = Signal(bool, object, object, str)

    clock = [0.0]
    service, store, _ = execution(tmp_path, 'locked_rocking', clock)
    service.start()
    confirm_prelock(service, clock)
    assert service.run.state is ProductionRunState.ARMED
    hardware = ProductionExecutionHardware(service, SimpleNamespace(set_state=lambda *args: None),
        SimpleNamespace(reference_profile=SimpleNamespace(freshness_timeout_s=1)), Power(), tmp_path)
    hardware._reference_data_valid = True
    hardware._reference_at_ns = time.monotonic_ns()
    hardware._test_id = 'locked_rocking'
    hardware._operation = 'trajectory'
    hardware._request_id = 'TRAJ-1'
    hardware._baseline_pulses = (1, 2, 3)
    hardware._motion_started_at = time.monotonic() - 1
    timestamp = time.monotonic_ns()
    stationary = MotionPlatformSnapshot(MotionPlatformState.TRAJECTORY, 1,
                                         feedback_monotonic_ns=timestamp, actual_pulses=(1, 2, 3))
    hardware._snapshot(stationary)
    assert service.run.state is ProductionRunState.ARMED
    hardware._snapshot(replace(stationary, actual_pulses=(2, 2, 3)))
    assert service.run.state is ProductionRunState.RUNNING
    service.stop()
    store.close()
    hardware.deleteLater()
    service.deleteLater()
    qapplication_session.processEvents()


def test_missing_snr_evidence_is_incomplete_and_window_still_ends(qapplication_session, tmp_path):
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'static_acquisition', clock)
    service.fixture_ready = lambda: (True, '')
    service.start()
    service.confirm_hardware('static_acquisition', {'center': 'verified'})
    clock[0] = 2
    service.tick()
    service.finish_observation({'center': 'verified'})
    assert {a['status'] for a in store.list_attempts('BATCH-AUTO')} == {'incomplete'}
    service.stop()
    store.close()


def test_workspace_projects_progress_and_restores_hidden_view(qapplication_session, tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
    from satellite_debug_tool.tests.test_production_configuration_workspace import _settings

    clock = [0.0]
    service, store, _ = execution(tmp_path, 'static_acquisition', clock)
    for slot, session in enumerate(service.sessions.values(), 1):
        session.slot = slot
    service.fixture_ready = lambda: (True, '')
    service.start()
    service.confirm_hardware('static_acquisition', {'center': 'verified'})
    for serial in service.sessions:
        service.record_tracking_sample(serial, tracking_sample(12), 0)
        service.record_tracking_sample(serial, tracking_sample(12), .1)
    clock[0] = 1
    service.tick()
    workspace = ProductionWorkspace(_settings(tmp_path, monkeypatch))
    workspace._batch_coordinator._store = store
    workspace._batch_coordinator._batch = store.get_batch('BATCH-AUTO')
    workspace._execution = service
    initial = workspace._execution_summary.text()
    workspace._render_execution(service.run.progress)
    assert workspace._execution_summary.text() == initial  # 隐藏页面停止呈现。
    assert service.active
    workspace._view_active = True
    workspace._render_execution(service.run.progress)
    assert '00:00:01' in workspace._progress_panel.summary_label.text()
    assert '1/1' in workspace._progress_panel.summary_label.text()
    assert workspace._progress_panel.remaining_label.text() == '00:00:01'
    assert workspace._progress_panel.node_ids == ('static_acquisition',)
    assert workspace._progress_panel._nodes['static_acquisition'].presentation.status.value == 'running'
    assert workspace._snr_panels[1]._current_test != '-'
    # 汇总占据自己的节点；大号步骤分母与可见节点完全一致。
    for serial in service.sessions:
        store.create_attempt('BATCH-AUTO', serial, 'whole_navigation')
    workspace._render_execution(service.run.progress)
    assert service.run.progress.total == 1
    assert workspace._progress_panel.node_ids == ('static_acquisition', 'whole_navigation')
    assert '1/2' in workspace._progress_panel.summary_label.text()
    workspace.close()
    workspace.deleteLater()
    service.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapplication_session.processEvents()


def test_workspace_incomplete_refresh_keeps_previous_countdown_cleared(qapplication_session, tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from satellite_debug_tool.i18n import tr
    from satellite_debug_tool.tests.test_production_configuration_workspace import _settings
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    clock = [0.0]
    service, store, _ = execution(tmp_path, 'static_acquisition', clock)
    for slot, session in enumerate(service.sessions.values(), 1):
        session.slot = slot
    service.fixture_ready = lambda: (True, '')
    service.start()
    service.confirm_hardware('static_acquisition', {'center': 'verified'})
    clock[0] = 1
    service.tick()
    workspace = ProductionWorkspace(_settings(tmp_path, monkeypatch))
    workspace._batch_coordinator._store = store
    workspace._batch_coordinator._batch = store.get_batch('BATCH-AUTO')
    workspace._execution = service
    workspace._view_active = True
    workspace._render_execution(service.run.progress)
    assert workspace._progress_panel.remaining_label.text() == '00:00:01'

    service.run.fail_shared_step('Fixture feedback lost', evidence={'feedback': 'missing'})
    terminal = service.run.progress
    assert terminal.state is ProductionRunState.INCOMPLETE
    assert terminal.remaining_s == 1  # 领域保留当时剩余量，终态呈现需要明确清除旧钟。
    for _ in range(2):
        workspace._render_execution(terminal)
        workspace.retranslate_ui()
        assert workspace._progress_panel.remaining_label.text() == '—'
        assert workspace._progress_panel.summary_label.text() == ''
        assert workspace._progress_panel.title_label.text() == tr('Incomplete')
        assert workspace._progress_panel._nodes['static_acquisition'].presentation.status.value == 'incomplete'
    workspace.close()
    workspace.deleteLater()
    service.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapplication_session.processEvents()


def test_formal_hardware_connects_station_without_manual_diagnostic_pages(qapplication_session, tmp_path, monkeypatch):
    import time
    from dataclasses import dataclass
    from PySide6.QtCore import QCoreApplication, QEvent
    from satellite_debug_tool.core.production import FixtureRuntime, FixtureRuntimeState
    from satellite_debug_tool.core.motion_platform.models import MotionPlatformSnapshot, MotionPlatformState
    from satellite_debug_tool.core.production import execution_hardware as module
    from satellite_debug_tool.core.production.ms6222_protocol import Ms6222StreamParser
    from satellite_debug_tool.tests.test_ms6222_protocol import _ins_frame
    from satellite_debug_tool.tests.test_production_configuration_workspace import _catalog

    class Reference(QObject):
        finished = Signal()
        frame_received = Signal(object)
        invalid_frame_received = Signal(object)
        connection_changed = Signal(bool, str)
        error_occurred = Signal(str)
        def __init__(self, port, *, baudrate, parent=None):
            super().__init__(parent)
            self.port, self.baudrate = port, baudrate
            self.started = False
        def start(self):
            self.started = True
            self.connection_changed.emit(True, self.port)
        def stop(self):
            self.connection_changed.emit(False, self.port)
        def wait(self, _timeout):
            return True

    class Control(QObject):
        connected = Signal()
        request_finished = Signal(str, object)
        request_failed = Signal(str, str)
        evidence_failed = Signal(str)
        send_result = Signal(object, str, int)
        snapshot_changed = Signal(object)
        def __init__(self, profile, *, recorder, parent=None):
            super().__init__(parent)
            self.profile = profile
            self.controller = SimpleNamespace(snapshot=MotionPlatformSnapshot(MotionPlatformState.READY, 1))
            self.centers = 0
        def confirm_preflight(self):
            pass
        def start(self):
            self.connected.emit()
        def submit_center(self, **kwargs):
            self.centers += 1
            return 'CENTER-1'
        def shutdown(self):
            pass
        def wait(self, _timeout):
            return True

    class Power(QObject):
        output_changed = Signal(bool, object, object, str)

    @dataclass
    class Arrival:
        successful: bool = True

    monkeypatch.setattr(module, 'Ms6222SerialWorker', Reference)
    monkeypatch.setattr(module, 'FixtureControlWorker', Control)
    catalog = _catalog(tmp_path / 'catalog.json')
    configuration = catalog.resolve('afd01c-standard', 'line-1', 2)
    runtime = FixtureRuntime(profile_store=catalog.fixture_profile_store, calibration_store=catalog.calibration_store)
    runtime.bind_configuration(configuration)
    runtime.fixture_lease.acquire('batch:BATCH-AUTO')
    service, store, _ = execution(tmp_path, 'static_acquisition', [0.0])
    hardware = module.ProductionExecutionHardware(service, runtime, configuration, Power(), tmp_path)
    service.start()
    assert hardware.control.centers == 1
    assert hardware._reference_worker.started
    assert hardware._reference_worker.port == configuration.reference_profile.serial_port
    assert hardware._reference_worker.baudrate == configuration.reference_profile.baud_rate
    hardware.control.request_finished.emit('CENTER-1', Arrival())
    assert service.run.state is ProductionRunState.ARMED
    frame = Ms6222StreamParser().feed(_ins_frame(), host_monotonic_ns=time.monotonic_ns(), host_wall_time_ns=time.time_ns())[0]
    hardware._reference_worker.frame_received.emit(frame)
    assert service.run.state is ProductionRunState.RUNNING
    assert runtime.fact('reference').state is FixtureRuntimeState.READY
    hardware.close()
    assert runtime.reference_lease.owner == ''
    service.stop()
    store.close()
    hardware.deleteLater()
    service.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapplication_session.processEvents()


def test_initial_acquiring_and_fine_tracking_peaks_are_search_not_failure(qapplication_session, tmp_path):
    """1958 现场轨迹：初扫/精扫正值与零值交替，进入 LOCKED 后稳定。"""
    from satellite_debug_tool.core.protocol import ServiceFastState
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'static_acquisition', clock)
    service.fixture_ready = lambda: (True, '')
    service.start()
    service.confirm_hardware('static_acquisition', {'center': 'verified'})
    states = [(1, False, 0.0), (1, False, 12.0), (1, False, 0.0),
              (2, False, 6.2), (2, False, 0.0), (2, False, 3.6)]
    try:
        for timestamp, (phase, locked, snr) in enumerate(states, 1):
            clock[0] = timestamp / 10
            record = ServiceFastState(1, timestamp, 0xfff, 0, phase, locked, 3, 3, True,
                                      0., 0., 0., 0., 0., snr)
            service.sessions['AFD01C-A'].core.record_received.emit(record)
        assert not service.snr_failures
        service.tick()
        assert service.run.progress.phase.value == 'converging'
    finally:
        service.stop()
        store.close()


@pytest.mark.parametrize('phase,locked,mask', [(1, True, 0xfff), (2, True, 0xfff),
    (3, False, 0xfff), (255, True, 0xfff), (3, True, 1 << 11)])
def test_positive_snr_requires_matching_valid_acquisition_state(qapplication_session, tmp_path, phase, locked, mask):
    service, store, _ = execution(tmp_path, 'static_acquisition', [0.0])
    service.start()
    service.confirm_hardware('static_acquisition', {'center': 'verified'})
    try:
        service.record_tracking_sample('AFD01C-A', tracking_sample(12, phase=phase, locked=locked, valid_mask=mask), 0)
        service.record_tracking_sample('AFD01C-A', tracking_sample(0, phase=1, locked=False), .1)
        assert not service.snr_failures
        assert service.device_phase('AFD01C-A').value == 'converging'
    finally:
        service.stop()
        store.close()


def test_locked_rocking_waits_for_device_acquisition_instead_of_snr_peak(qapplication_session, tmp_path):
    service, store, _ = execution(tmp_path, 'locked_rocking', [0.0])
    for session in service.sessions.values():
        snapshot = session.product_store.snapshot()
        snapshot = replace(snapshot, operation=replace(snapshot.operation,
            tracking_phase=ProductValue.valid(TrackingPhase.FINE_TRACKING)))
        session.product_store.snapshot = lambda snapshot=snapshot: snapshot
    requests = []
    service.hardware_requested.connect(requests.append)
    try:
        service.start()
        assert service.run.state is ProductionRunState.WAITING_PREREQUISITE
        assert requests == []
    finally:
        service.stop()
        store.close()


def test_initial_search_peaks_then_device_lock_can_pass(qapplication_session, tmp_path):
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'static_acquisition', clock)
    service.fixture_ready = lambda: (True, '')
    service.start()
    service.confirm_hardware('static_acquisition', {'center': 'verified'})
    try:
        for serial in service.sessions:
            for index, (phase, locked, snr) in enumerate([(1, False, 12), (1, False, 0),
                    (2, False, 6.2), (2, False, 0), (3, True, 12)]):
                service.record_tracking_sample(serial, tracking_sample(snr, phase=phase, locked=locked), .1 * index)
            service.record_tracking_sample(serial, tracking_sample(12), .5)
        clock[0] = 1
        service.tick()
        assert service.run.progress.phase.value == 'effective_observation'
        assert not service.snr_failures
        clock[0] = 2
        service.tick()
        service.finish_observation({'center': 'verified'})
        assert {item['status'] for item in store.list_attempts('BATCH-AUTO')} == {'pass'}
    finally:
        service.stop()
        store.close()


def test_positive_snr_loss_of_lock_fails_only_affected_device(qapplication_session, tmp_path):
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'static_acquisition', clock)
    service.fixture_ready = lambda: (True, '')
    service.start()
    service.confirm_hardware('static_acquisition', {'center': 'verified'})
    try:
        for index in range(20):
            clock[0] = index / 10
            for serial in service.sessions:
                locked = serial != 'AFD01C-A' or not .5 <= clock[0] <= 1.0
                service.record_tracking_sample(serial, tracking_sample(12, locked=locked), clock[0])
            service.tick()
        clock[0] = 2
        service.tick()
        service.finish_observation({'center': 'verified'})
        attempts = {item['serial_number']: item for item in store.list_attempts('BATCH-AUTO')}
        assert attempts['AFD01C-A']['status'] == 'fail'
        assert attempts['AFD01C-B']['status'] == 'pass'
        assert 'interruption_duration_exceeded' in attempts['AFD01C-A']['result_json']['tracking_criteria']['failure_reasons']
    finally:
        service.stop()
        store.close()


def test_execution_uses_individual_test_duration(qapplication_session, tmp_path):
    from satellite_debug_tool.core.production.recipe import resolve_test_plan

    clock = [0.0]
    service, store, _ = execution(tmp_path, 'static_acquisition', clock)
    payload = service.recipe.payload
    payload['tests']['static_acquisition']['duration_s'] = 4
    payload['tests'], payload['duration_policy'] = resolve_test_plan(payload['tests'], payload['duration_policy'])
    payload['schema_version'] = 2
    profile = payload['fixtures']['motion_platform']['profile']
    profile.pop('steady_duration_s')
    profile.update(execution_mode='until_observation_complete', ramp_out_s=0)
    service.recipe = ProductionRecipe.from_mapping(payload)
    service.fixture_ready = lambda: (True, '')
    service.start()
    service.confirm_hardware('static_acquisition', {'center': 'verified'})
    try:
        assert service.run.progress.remaining_s == 4
        for index in range(40):
            clock[0] = index / 10
            for serial in service.sessions:
                service.record_tracking_sample(serial, tracking_sample(12), clock[0])
            service.tick()
            if index == 20:
                assert service.run.progress.remaining_s == 2
                assert not service._finishing_observation
        clock[0] = 4
        service.tick()
        assert service._finishing_observation
        service.finish_observation({'center': 'verified'})
        assert {item['status'] for item in store.list_attempts('BATCH-AUTO')} == {'pass'}
    finally:
        service.stop()
        store.close()


def test_default_300_second_search_timeout_records_failure_without_stopping_batch(qapplication_session, tmp_path):
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'static_acquisition', clock)
    service.recipe = ProductionRecipe.from_mapping(valid_recipe(duration_s=3600))
    service.policy = TrackingCriteriaPolicy()
    service.fixture_ready = lambda: (True, '')
    service.start()
    service.confirm_hardware('static_acquisition', {'center': 'verified'})
    try:
        for second in range(301):
            clock[0] = second
            for serial in service.sessions:
                searching = serial == 'AFD01C-A'
                service.record_tracking_sample(serial, tracking_sample(
                    0 if searching else 12, locked=not searching, phase=1 if searching else 3), second)
            service.tick()
            if second == 299:
                assert not service.snr_failures
        assert service.snr_failures == {'AFD01C-A': {'reasons': ['acquisition_timeout']}}
        assert service.run.state is ProductionRunState.RUNNING
        assert service.run.progress.remaining_s == 3300
    finally:
        service.stop()
        store.close()


@pytest.mark.parametrize('test_id', ['firmware_verification', 'parameter_verification'])
def test_unconfigured_expectations_are_skipped_not_measured_pass(qapplication_session, tmp_path, test_id):
    service, store, sent = execution(tmp_path, test_id, [0.0])
    payload = service.recipe.payload
    payload['expected'] = {'main_firmware': {'match': 'optional'}, 'parameters': {}}
    service.recipe = ProductionRecipe.from_mapping(payload)
    try:
        service.start()
        assert service.run.state is ProductionRunState.COMPLETE
        attempts = store.list_attempts('BATCH-AUTO')
        assert {item['status'] for item in attempts} == {'skipped'}
        assert all(item['result_json']['reason'] for item in attempts)
        assert sent == []
    finally:
        service.stop()
        store.close()


def test_prelock_loss_between_ticks_resets_stability(qapplication_session, tmp_path):
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'locked_rocking', clock)
    service.start()
    try:
        for timestamp, (at, locked) in enumerate(((0, True), (.03, True), (.04, False), (.06, True)), 1):
            clock[0] = at
            for session in service.sessions.values():
                session.core.record_received.emit(tracking_sample(12, locked=locked, timestamp=timestamp))
        service.tick()
        assert service.run.state is ProductionRunState.WAITING_PREREQUISITE
        clock[0] = .12
        for session in service.sessions.values():
            session.core.record_received.emit(tracking_sample(12, timestamp=5))
        service.tick()
        assert service.run.state is ProductionRunState.ARMED
    finally:
        service.stop()
        store.close()


def test_power_cycle_waits_for_current_capture_before_window(qapplication_session, tmp_path):
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'power_on_rocking', clock)
    service.policy = replace(service.policy, maximum_sample_gap_s=.2)
    service.fixture_ready = lambda: (True, '')
    service.start()
    service.prepare_power_cycle()
    for session in service.sessions.values():
        session.capture_ready = False
    service.confirm_hardware('power_on_rocking', {'power_off': {'output': 'off_confirmed'}, 'output': 'on_confirmed', 'power_restored_monotonic_ns': 1})
    try:
        clock[0] = .4  # 正常重启尚无遥测，准备阶段不生成观察缺口。
        service.tick()
        assert service.run.state is ProductionRunState.ARMED
        assert service._evaluators == {}
        for session in service.sessions.values():
            session.capture_ready = False
            session.core.product_store.telemetry_epoch += 1
        clock[0] = .5
        service.tick()
        for session in service.sessions.values():
            session.core.generation += 1  # 采集准备期间接纳同一次重启的代际重建。
        clock[0] = .6
        service.tick()
        assert service.active
        assert service.run.state is ProductionRunState.ARMED
        for session in service.sessions.values():
            session.capture_ready = True
        clock[0] = .8
        service.tick()
        assert service.run.state is ProductionRunState.RUNNING
        assert service.run.progress.remaining_s == pytest.approx(2)
        for index in range(21):
            clock[0] = .8 + index / 10
            for serial in service.sessions:
                service.record_tracking_sample(serial, tracking_sample(12), clock[0])
            service.tick()
        service.finish_observation({'center': 'feedback_confirmed'})
        assert {item['status'] for item in store.list_attempts('BATCH-AUTO')} == {'pass'}
        assert service.evidence_gaps == frozenset()
    finally:
        service.stop()
        store.close()


def test_power_cycle_missing_capture_has_bounded_incomplete_result(qapplication_session, tmp_path):
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'power_on_rocking', clock)
    service.start()
    service.prepare_power_cycle()
    for session in service.sessions.values():
        session.capture_ready = False
    service.confirm_hardware('power_on_rocking', {'power_off': {'output': 'off_confirmed'}, 'output': 'on_confirmed', 'power_restored_monotonic_ns': 1})
    clock[0] = 1
    service.tick()
    assert not service.active
    assert service.run.state is ProductionRunState.INCOMPLETE
    for attempt in store.list_attempts('BATCH-AUTO'):
        assert 'start_evidence' not in attempt['result_json']
        assert 'preparation timed out' in attempt['result_json']['reason']
    store.close()


def test_late_capture_readiness_keeps_power_preparation_timeout(qapplication_session, tmp_path):
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'power_on_rocking', clock)
    service.fixture_ready = lambda: (True, '')
    service.start()
    service.prepare_power_cycle()
    for session in service.sessions.values():
        session.core.product_store.telemetry_epoch += 1
        session.capture_ready = False
    clock[0] = .5
    service.tick()
    clock[0] = 1.1
    for session in service.sessions.values():
        session.capture_ready = True
    service.confirm_hardware('power_on_rocking', {'power_off': {'output': 'off_confirmed'}, 'output': 'on_confirmed', 'power_restored_monotonic_ns': 1})
    assert service.run.state is ProductionRunState.INCOMPLETE
    assert all('start_evidence' not in item['result_json'] for item in store.list_attempts('BATCH-AUTO'))
    service.stop()
    store.close()


def test_prelock_is_rechecked_after_fixture_preparation(qapplication_session, tmp_path):
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'locked_rocking', clock)
    service.start()
    confirm_prelock(service, clock)
    try:
        assert service.run.state is ProductionRunState.ARMED
        for index, (at, locked) in enumerate(((.1, False), (.11, True), (.14, True), (.17, True)), 5):
            clock[0] = at
            for session in service.sessions.values():
                session.core.record_received.emit(tracking_sample(12, locked=locked, timestamp=index))
            assert service.motion_start_ready() is (at == .17)
    finally:
        service.stop()
        store.close()


def test_prelock_timeout_records_result_without_fabricating_hardware_start(qapplication_session, tmp_path):
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'locked_rocking', clock)
    service.start()
    confirm_prelock(service, clock)
    try:
        clock[0] = .1
        for session in service.sessions.values():
            session.core.record_received.emit(tracking_sample(0, locked=False, timestamp=5))
        clock[0] = 1
        service.tick()
        assert service.run.state is ProductionRunState.COMPLETE
        assert all('start_evidence' not in item['result_json'] for item in store.list_attempts('BATCH-AUTO'))
    finally:
        service.stop()
        store.close()


@pytest.mark.parametrize('old_ms,new_ms', [(14377, 14559), (100000, 1000)])
def test_same_scope_power_cycle_opens_fresh_observation_timestamps(
    qapplication_session, tmp_path, old_ms, new_ms,
):
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'power_on_rocking', clock)
    service.fixture_ready = lambda: (True, '')
    try:
        service.start()
        service.prepare_power_cycle()
        scopes = dict(service._scope)
        for serial, session in service.sessions.items():
            service._last_device_timestamp[serial] = old_ms
            session.capture_ready = False
            session.core.record_received.emit(tracking_sample(12, timestamp=new_ms))
        service.confirm_hardware('power_on_rocking', {
            'power_off': {'output': 'off_confirmed'}, 'output': 'on_confirmed',
            'power_restored_monotonic_ns': 1,
        })
        assert service.run.state is ProductionRunState.ARMED
        clock[0] = .5
        for session in service.sessions.values():
            session.capture_ready = True
        service.tick()
        assert service.run.state is ProductionRunState.RUNNING
        assert service._scope == scopes
        assert service._last_device_timestamp == {}
        for serial, session in service.sessions.items():
            session.core.record_received.emit(tracking_sample(12, timestamp=new_ms))
            assert service._last_device_timestamp[serial] == new_ms
        assert service.run.progress.remaining_s == pytest.approx(2)
    finally:
        service.stop()
        store.close()


def test_per_device_parameters_match_registered_facts(qapplication_session, tmp_path):
    """per_device 参数以该设备注册事实为期望：两台设备注册 IP 不同、回读各自
    等于注册 IP 时 PASS；偏离注册 IP 的设备 FAIL，不受另一台影响。"""
    clock = [0.0]
    service, store, sent = execution(tmp_path, 'parameter_verification', clock)
    payload = valid_recipe(duration_s=2)
    payload['expected'] = {
        'main_firmware': {'match': 'optional'},
        'parameters': {'ip': {'type': 'ip', 'per_device': True}},
    }
    service.recipe = ProductionRecipe.from_mapping(payload)
    store.update_device_endpoint('BATCH-AUTO', 'AFD01C-A', endpoint_ip='192.168.1.12', endpoint_port=4004)
    store.update_device_endpoint('BATCH-AUTO', 'AFD01C-B', endpoint_ip='192.168.1.13', endpoint_port=4004)

    def para_table(value):
        return ParaTableReport(
            table_ver=1,
            params=[ParaEntry(name='ip', para_type=0, flags=0, value=value)],
        )
    try:
        service.start()
        assert sent, 'per-device verification must request the parameter table'
        for serial, value in (('AFD01C-A', '192.168.1.12'), ('AFD01C-B', '192.168.1.13')):
            service._record(serial, para_table(value))
        service.tick()
        assert service.run.state is ProductionRunState.COMPLETE
        attempts = {
            row['serial_number']: row for row in store.list_attempts('BATCH-AUTO')
        }
        assert attempts['AFD01C-A']['status'] == AttemptStatus.PASS.value
        assert attempts['AFD01C-B']['status'] == AttemptStatus.PASS.value
        assert attempts['AFD01C-A']['result_json']['expected_per_device'] == {
            'ip': '192.168.1.12'
        }

    finally:
        service.stop()
        store.close()


def test_per_device_recipe_rules_are_constrained_to_device_facts() -> None:
    """per_device 规则只能依赖注册事实：非法类型或自带比较目标在配方校验拒绝。"""
    base = valid_recipe()
    base['expected']['parameters'] = {
        'ip': {'type': 'ip', 'per_device': True},
    }
    assert ProductionRecipe.from_mapping(base).payload['expected']['parameters']['ip']['per_device']

    for bad in (
        {'type': 'ip', 'per_device': True, 'value': '192.168.1.1'},
        {'type': 'float', 'per_device': True},
    ):
        payload = valid_recipe()
        payload['expected']['parameters'] = {'bad': bad}
        with pytest.raises(RecipeValidationError):
            ProductionRecipe.from_mapping(payload)


def _vehicle_recorder(service, *, reference_angles=(0.0, 0.0, 0.0)):
    """把执行器置为姿态模式窗口，返回供喂样的跟踪判据。"""
    from satellite_debug_tool.core.production.tracking_criteria import (
        TrackingCriteriaPolicy,
        TrackingEvaluator,
    )

    policy = replace(
        TrackingCriteriaPolicy(), acquisition_stable_s=.05, acquisition_timeout_s=1,
        recovery_stable_s=.1, interruption_max_s=.25, interruption_ratio_max=.25,
    )
    evaluator = TrackingEvaluator(policy, 30.0, attitude_mode=True)
    evaluator.start(0.0)
    service._request = SimpleNamespace(test_id='locked_drive')
    service._hardware_ready = True
    service._evaluators['AFD01C-A'] = evaluator
    service._last_device_timestamp.clear()
    service.fixture_telemetry = SimpleNamespace(
        reference_readout=lambda now_ns=None: SimpleNamespace(
            status='valid', angles=reference_angles),
    )
    return evaluator


def test_vehicle_mode_judges_attitude_and_exempts_snr(qapplication_session, tmp_path):
    """姿态模式：SNR 掉 0 与失锁不构成中断或失败；姿态误差是唯一健康事实。"""
    from satellite_debug_tool.core.production import AttemptStatus

    clock = [0.0]
    service, store, _ = execution(tmp_path, 'static_acquisition', clock)
    evaluator = _vehicle_recorder(service)
    try:
        # 遮挡期：SNR 0 + 失锁，但姿态误差 1° ≤ 3° —— 健康，稳定确认后 acquired。
        for offset, timestamp in ((0.0, 1), (0.03, 2), (0.06, 3), (0.09, 4)):
            clock[0] = offset
            session = service.sessions['AFD01C-A']
            session.core.record_received.emit(
                ServiceFastState(1, timestamp, 0xfff, 0, 3, False, 3, 3, True,
                                 1.0, 0., 0., 0., 0., 0.0))
        assert evaluator.acquired
        assert evaluator.attitude_mode
        assert not evaluator.failure_reasons
        clock[0] = 0.12
        service.sessions['AFD01C-A'].core.record_received.emit(
            ServiceFastState(1, 5, 0xfff, 0, 3, False, 3, 3, True,
                             1.0, 0., 0., 0., 0., 0.0))
        assert evaluator.failure_reasons == ()
        assert evaluator.metrics()['attitude_error_max_deg'] == pytest.approx(1.0)
        assert evaluator.metrics()['attitude_error_limit_deg'] == 3.0
    finally:
        service.stop()
        store.close()


def test_vehicle_attitude_excess_and_missing_reference_are_typed(qapplication_session, tmp_path):
    """姿态超限形成中断与失败；参考缺失是质量原因，不推断失败。"""
    clock = [0.0]
    service, store, _ = execution(tmp_path, 'static_acquisition', clock)
    evaluator = _vehicle_recorder(service)
    try:
        # 参考缺失：质量原因，不是性能失败。
        service.fixture_telemetry = SimpleNamespace(
            reference_readout=lambda now_ns=None: SimpleNamespace(
                status='stale', angles=None),
        )
        clock[0] = 0.0
        service.sessions['AFD01C-A'].core.record_received.emit(
            tracking_sample(12, timestamp=1))
        assert 'attitude_reference_missing' in evaluator.quality_reasons
        assert evaluator.failure_reasons == ()

        # 参考恢复：姿态 1° 健康帧完成稳定确认，进入正式跟踪阶段。
        service.fixture_telemetry = SimpleNamespace(
            reference_readout=lambda now_ns=None: SimpleNamespace(
                status='valid', angles=(0.0, 0.0, 0.0)),
        )
        for offset, timestamp in ((0.05, 2), (0.08, 3), (0.11, 4), (0.14, 5)):
            clock[0] = offset
            service.sessions['AFD01C-A'].core.record_received.emit(
                ServiceFastState(1, timestamp, 0xfff, 0, 3, False, 3, 3, True,
                                 1.0, 0., 0., 0., 0., 0.0))
        assert evaluator.acquired

        # 已进入跟踪阶段后姿态超限 5° > 3°：形成中断事实。
        clock[0] = 0.3
        service.sessions['AFD01C-A'].core.record_received.emit(
            ServiceFastState(1, 6, 0xfff, 0, 3, True, 3, 3, True,
                             5.0, 0., 0., 0., 0., 12.0))
        metrics = evaluator.metrics()
        assert metrics['attitude_error_max_deg'] == pytest.approx(5.0)
        assert metrics['interruption_count'] >= 1
    finally:
        service.stop()
        store.close()


def test_parameter_auto_correction_sends_para_set_then_repasses(qapplication_session, tmp_path):
    """M66 自动修正：首次不一致时下发 PARA_SET（IP 除外）并等待新回读；
    回读一致转 PASS 且记录 auto_corrected；IP 项不自动修正。"""
    clock = [0.0]
    service, store, sent = execution(tmp_path, 'parameter_verification', clock)
    payload = valid_recipe(duration_s=2)
    payload['expected'] = {
        'main_firmware': {'match': 'optional'},
        'parameters': {
            'modem_baud': {'type': 'int', 'value': 921600},
            'ip': {'type': 'ip', 'per_device': True},
        },
    }
    service.recipe = ProductionRecipe.from_mapping(payload)
    store.update_device_endpoint('BATCH-AUTO', 'AFD01C-A', endpoint_ip='192.168.1.12', endpoint_port=4004)
    store.update_device_endpoint('BATCH-AUTO', 'AFD01C-B', endpoint_ip='192.168.1.13', endpoint_port=4004)

    def table(ip, baud):
        return ParaTableReport(table_ver=1, params=[
            ParaEntry(name='ip', para_type=0, flags=0, value=ip),
            ParaEntry(name='modem_baud', para_type=0, flags=0, value=baud),
        ])
    try:
        service.start()
        service._record('AFD01C-A', table('192.168.1.12', '921600'))
        service._record('AFD01C-B', table('192.168.1.13', '115200'))
        service.tick()
        # modem_baud 不一致：已下发 PARA_SET 并请求新回读，结果尚未形成。
        para_sets = [frame for _, frame in sent if b'modem_baud' in frame]
        assert para_sets, 'auto correction must send PARA_SET for the mismatched parameter'
        assert service._param_corrected['AFD01C-B'] == {'modem_baud': '921600'}
        assert 'AFD01C-A' not in service._param_corrected
        assert service.run.state is ProductionRunState.WAITING_PREREQUISITE

        # 修正后回读一致 → PASS，IP 项按注册事实各自通过。
        service._record('AFD01C-B', table('192.168.1.13', '921600'))
        service.tick()
        assert service.run.state is ProductionRunState.COMPLETE
        attempts = {row['serial_number']: row for row in store.list_attempts('BATCH-AUTO')}
        assert attempts['AFD01C-A']['status'] == AttemptStatus.PASS.value
        assert attempts['AFD01C-B']['status'] == AttemptStatus.PASS.value
        assert attempts['AFD01C-B']['result_json']['auto_corrected'] == {'modem_baud': '921600'}
    finally:
        service.stop()
        store.close()


def test_parameter_auto_correction_runs_only_once_then_fails(qapplication_session, tmp_path):
    """一次修正机会：修正后回读仍不一致 → FAIL，不再循环下发。"""
    clock = [0.0]
    service, store, sent = execution(tmp_path, 'parameter_verification', clock)
    payload = valid_recipe(duration_s=2)
    payload['expected'] = {
        'main_firmware': {'match': 'optional'},
        'parameters': {'modem_baud': {'type': 'int', 'value': 921600}},
    }
    service.recipe = ProductionRecipe.from_mapping(payload)

    def table(baud):
        return ParaTableReport(table_ver=1, params=[
            ParaEntry(name='modem_baud', para_type=0, flags=0, value=baud),
        ])
    try:
        service.start()
        for session in service.sessions.values():
            session.core.record_received.emit(table('115200'))
        service.tick()
        first_sets = [frame for _, frame in sent if b'modem_baud' in frame]
        assert first_sets
        for session in service.sessions.values():
            session.core.record_received.emit(table('115200'))
        service.tick()
        assert service.run.state is ProductionRunState.COMPLETE
        assert all(
            row['status'] == AttemptStatus.FAIL.value
            for row in store.list_attempts('BATCH-AUTO')
        )
        sets_after_fail = [frame for _, frame in sent if b'modem_baud' in frame]
        assert len(sets_after_fail) == len(first_sets), 'must not loop PARA_SET after FAIL'
    finally:
        service.stop()
        store.close()


class _DeferredSignal:
    """可手动 emit 的假信号，便于测试驱动 OTA 终态。"""

    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in self._slots:
            slot(*args)


def test_firmware_auto_upgrade_flow(qapplication_session, tmp_path):
    """M66 固件自动升级：版本不一致且配置 firmware_bin 时走 OTA 工厂；
    TARGET 确认后回读新版本判 PASS；失败则 FAIL 且记录事件。"""
    from satellite_debug_tool.core.production.execution import _FirmwareAutoUpgrade

    clock = [0.0]
    service, store, _sent = execution(tmp_path, 'firmware_verification', clock)
    payload = valid_recipe(duration_s=2)
    payload["expected"] = {
        "main_firmware": {"match": "exact", "value": "0.0.132 beta"},
        "parameters": {},
    }
    payload["expected"]["firmware_bin"] = str(tmp_path / "app_0.0.132_beta_release.bin")
    Path(payload["expected"]["firmware_bin"]).write_bytes(b"BIN")
    service.recipe = ProductionRecipe.from_mapping(payload)

    upgrades = []
    def fake_factory(session, serial, bin_path, parent=None):
        holder = SimpleNamespace(
            finished=_DeferredSignal(),
            configure_engineering_file=lambda data, name: object(),
            start=lambda *, pause_debug: True,
        )
        upgrades.append((serial, holder))
        holder.finished.connect(service._on_firmware_upgrade_finished)
        return holder
    service._ota_factory = fake_factory
    try:
        service.start()
        service.tick()
        assert upgrades, 'mismatch with firmware_bin must trigger the upgrade factory'
        assert service._firmware_upgrade_attempted == {'AFD01C-A', 'AFD01C-B'}

        # 升级成功：设备回读变为期望版本 → PASS。
        base_snapshot = service.sessions['AFD01C-A'].product_store.snapshot()
        valid = lambda value: ProductValue(value, Availability.VALID, received_monotonic_s=clock[0])
        upgraded_snapshot = replace(base_snapshot, identity=replace(base_snapshot.identity, main_firmware=valid("0.0.132 beta")))
        for session in service.sessions.values():
            session.product_store.snapshot = lambda snap=upgraded_snapshot: snap
        for _serial, holder in upgrades:
            holder.finished.emit(_serial, True, "target_version_confirmed")
        service.tick()
        attempts = {row["serial_number"]: row for row in store.list_attempts('BATCH-AUTO')}
        assert all(row['status'] == AttemptStatus.PASS.value for row in attempts.values())
        assert attempts['AFD01C-A']['result_json']['auto_upgrade'] is True
    finally:
        service.stop()
        store.close()


def test_firmware_auto_upgrade_failure_fails_step(qapplication_session, tmp_path):
    """升级失败（OTA 终态非 TARGET 确认）→ 该项 FAIL，事件留痕。"""
    clock = [0.0]
    service, store, _sent = execution(tmp_path, 'firmware_verification', clock)
    payload = valid_recipe(duration_s=2)
    payload["expected"] = {
        "main_firmware": {"match": "exact", "value": "0.0.132 beta"},
        "parameters": {},
    }
    bin_path = tmp_path / "app_0.0.132_beta_release.bin"
    payload["expected"]["firmware_bin"] = str(bin_path)
    bin_path.write_bytes(b"BIN")
    service.recipe = ProductionRecipe.from_mapping(payload)

    def fake_factory(session, serial, bin_path, parent=None):
        print("DBG FACTORY CALLED:", serial)
        holder = SimpleNamespace(
            finished=_DeferredSignal(),
            configure_engineering_file=lambda data, name: object(),
            start=lambda *, pause_debug: True,
        )
        holder.finished.connect(service._on_firmware_upgrade_finished)
        factory_holders.append((serial, holder))
        return holder
    service._ota_factory = fake_factory
    factory_holders = []
    try:
        service.start()
        service.tick()
        assert service._firmware_upgrade_attempted == {serial for serial, _ in factory_holders}
        for serial, holder in factory_holders:
            service._on_firmware_upgrade_finished(serial, False, "begin_timeout")
        service.tick()
        attempts = {row["serial_number"]: row for row in store.list_attempts('BATCH-AUTO')}
        assert all(row['status'] == AttemptStatus.FAIL.value for row in attempts.values())
        events = [row for row in store.list_events('BATCH-AUTO')
                  if row["event_type"] == "firmware_auto_upgrade_finished"]
        assert events and "begin_timeout" in str(events[-1]["payload_json"])
    finally:
        service.stop()
        store.close()
