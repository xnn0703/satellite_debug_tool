"""M47 执行链路与有效观察计时回归。"""
from dataclasses import replace
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal
import pytest

from satellite_debug_tool.core.product import Availability, ProductSnapshot, ProductValue, TrackingPhase
from satellite_debug_tool.core.production import ProductionRecipe, ProductionRunCoordinator, ProductionRunState
from satellite_debug_tool.core.production.execution import ProductionExecution, compare_parameter
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
    if config is not None:
        payload['tests'][test_id] = dict(enabled=True, **config)
    recipe = ProductionRecipe.from_mapping(payload)
    sessions = []
    for serial in ('AFD01C-A', 'AFD01C-B'):
        core = Core()
        session = SimpleNamespace(serial_number=serial, core=core, endpoint=('127.0.0.1', 4004),
                                  is_online=lambda: True)
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


def test_initial_search_is_excluded_from_failure_and_transient_zero_is_permanent(qapplication_session, tmp_path):
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
    # 两帧位于同一个 UI 计时片段；采样入口必须保留这次归零。
    service.record_tracking_sample('AFD01C-A', tracking_sample(0, phase=4, locked=False), 30.2)
    service.record_tracking_sample('AFD01C-A', tracking_sample(12), 30.3)
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
    assert attempts['AFD01C-A']['status'] == 'fail'
    assert attempts['AFD01C-B']['status'] == 'pass'
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
