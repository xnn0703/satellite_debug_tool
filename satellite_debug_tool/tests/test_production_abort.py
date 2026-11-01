"""手动中止：慢速收尾、回执顺序及真实按钮的事件循环回归。"""
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal, QTimer, QCoreApplication, QEvent
from satellite_debug_tool.core.production.abort_coordinator import ProductionAbortCoordinator, AbortState


class Power(QObject):
    finished = Signal()
    power_stopped = Signal(object, object, str)
    power_failed = Signal(str, object, str)
    action_id = 'ABORT-POWER'
    def __init__(self):
        super().__init__()
        self.running = True
        self.off_requests = 0
    def request_verified_off(self):
        self.off_requests += 1
    def isRunning(self):
        return self.running
    def wait(self, *_args):
        raise AssertionError('GUI abort must not wait for the power worker')


def until(app, predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.002)
    assert predicate()


@pytest.mark.parametrize('owner_first', [True, False])
def test_abort_waits_for_both_power_facts_and_slow_motion(qapplication_session, owner_first):
    coordinator = ProductionAbortCoordinator()
    power = Power()
    gate = threading.Event()
    progress_stopped = []
    completed = []
    coordinator.settled.connect(lambda *args: completed.append(args))
    coordinator.request(stop_progress=lambda: progress_stopped.append(True), power=power,
                        cleanup=lambda: gate.wait(2))
    assert coordinator.active and progress_stopped == [True]
    coordinator.request(stop_progress=lambda: pytest.fail('duplicate abort'))
    assert power.off_requests == 1
    heartbeat = []
    QTimer.singleShot(0, lambda: heartbeat.append(True))
    qapplication_session.processEvents()
    assert heartbeat
    if owner_first:
        power.running = False
        power.finished.emit()
        coordinator.confirm_power_off()
    else:
        coordinator.confirm_power_off()
        power.running = False
        power.finished.emit()
    assert coordinator.active and not completed
    gate.set()
    until(qapplication_session, lambda: bool(completed))
    assert completed == [(True, '')]
    assert coordinator.state is AbortState.ABORTED


def test_abort_surfaces_cleanup_failure(qapplication_session):
    coordinator = ProductionAbortCoordinator()
    def failed_close():
        raise RuntimeError('motion owner is still stopping')
    completed = []
    coordinator.settled.connect(lambda *args: completed.append(args))
    coordinator.request(stop_progress=lambda: None, cleanup=failed_close)
    until(qapplication_session, lambda: bool(completed))
    assert completed[0][0] is False
    assert 'motion owner' in completed[0][1]
    assert coordinator.state is AbortState.FAILED


def test_actual_button_remains_responsive_and_refresh_preserves_abort(qapplication_session, tmp_path, monkeypatch):
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
    from satellite_debug_tool.tests.test_production_configuration_workspace import _catalog, _settings
    workspace = ProductionWorkspace(_settings(tmp_path, monkeypatch),
                                    configuration_store=_catalog(tmp_path / 'catalog.json'))
    workspace._batch_id_edit.setText('CLICK-ABORT')
    workspace._operator_edit.setText('operator')
    workspace._output_edit.setText(str(tmp_path / 'output'))
    workspace.create_batch()
    assert workspace._abort_button.isEnabled()  # READY 也可以取消。
    power = Power()
    power.power_stopped.connect(workspace._on_production_power_stopped)
    workspace._production_power_worker = power
    gate = threading.Event()
    quiesced = []
    hardware = SimpleNamespace(quiesce=lambda: quiesced.append(True), close=lambda: gate.wait(2))
    workspace._execution_hardware = hardware
    clicked_at = time.monotonic()
    workspace._abort_button.click()
    assert time.monotonic() - clicked_at < .5
    assert quiesced and power.off_requests == 1
    assert workspace._abort_coordinator.active
    for _ in range(3):
        workspace._update_start_gate()
    assert not workspace._abort_button.isEnabled()
    assert '中止' in workspace._execution_summary.text() or 'Aborting' in workspace._execution_summary.text()
    heartbeat = []
    QTimer.singleShot(0, lambda: heartbeat.append(True))
    qapplication_session.processEvents()
    assert heartbeat
    power.power_stopped.emit(None, (), power.action_id)
    power.running = False
    power.finished.emit()
    assert workspace.batch['status'] == 'ready'
    gate.set()
    until(qapplication_session, lambda: workspace.batch['status'] == 'aborted')
    until(qapplication_session, lambda: not workspace._abort_archive.isRunning())
    assert workspace._execution_hardware is None
    assert workspace._production_power_worker is None
    workspace.close()
    workspace.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapplication_session.processEvents()


def test_cancel_during_power_preparation_keeps_output_off(qapplication_session):
    from satellite_debug_tool.core.production.production_power import ProductionPowerWorker
    from satellite_debug_tool.core.production.power_supply import PowerSupplyConfig, PowerSupplyState
    entered = threading.Event()
    release = threading.Event()
    operations = []
    class Adapter:
        state = PowerSupplyState.READY_OFF
        records = ()
        def connect(self):
            return 'identity'
        def prepare_output_off(self, **_kwargs):
            entered.set()
            release.wait(2)
            return 'off-confirmed'
        def enable_output(self, **_kwargs):
            operations.append('on')
        def close(self):
            operations.append('closed')
    worker = ProductionPowerWorker(
        PowerSupplyConfig(host='127.0.0.1', voltage_set_v=14, current_set_a=2),
        adapter_factory=lambda _config: Adapter(),
    )
    stopped = []
    worker.power_stopped.connect(lambda *args: stopped.append(args))
    worker.start()
    assert entered.wait(1)
    worker.request_verified_off()
    release.set()
    until(qapplication_session, lambda: not worker.isRunning())
    qapplication_session.processEvents()
    assert stopped and operations == ['closed']
    worker.deleteLater()


def test_progress_error_still_requests_power_off_and_closes_motion(qapplication_session):
    coordinator = ProductionAbortCoordinator()
    power = Power()
    closed = []
    completed = []
    def fail_progress():
        raise RuntimeError('attempt store failed')
    coordinator.settled.connect(lambda *args: completed.append(args))
    coordinator.request(stop_progress=fail_progress, power=power, cleanup=lambda: closed.append(True))
    assert power.off_requests == 1
    coordinator.confirm_power_off()
    power.running = False
    power.finished.emit()
    until(qapplication_session, lambda: bool(completed))
    assert closed == [True]
    assert completed[0][0] is False and 'attempt store' in completed[0][1]
