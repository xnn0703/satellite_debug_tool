"""自动观察窗口独占摇摆结束时间；准备恢复不缩短实际观察。"""

from dataclasses import replace
import threading
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal
import pytest

from satellite_debug_tool.core.motion_platform import MotionPlatformError, MotionTrajectoryRunner, SineAxis, SineTrajectory
from satellite_debug_tool.core.production import FixtureControlWorker
from satellite_debug_tool.core.production.execution_hardware import ProductionExecutionHardware
from satellite_debug_tool.tests.motion_fakes import ConfirmedMotionController
from satellite_debug_tool.tests.test_fixture_control import _profile, _wait_until
from satellite_debug_tool.tests.test_production_recipe import valid_recipe


def _continuous() -> SineTrajectory:
    return SineTrajectory(100, .1, None, 0, SineAxis(1, .2),
                          SineAxis(0, 0, enabled=False), SineAxis(0, 0, enabled=False))


class _Clock:
    ns = 0

    def sleep(self, seconds):
        self.ns += round(seconds * 1_000_000_000)


def test_continuous_motion_requires_owner_and_preserves_operating_limits():
    controller = ConfirmedMotionController(_profile().to_motion_config())
    runner = MotionTrajectoryRunner(controller)
    with pytest.raises(MotionPlatformError, match="stop owner"):
        runner.run(_continuous())
    with pytest.raises(MotionPlatformError, match="amplitude"):
        runner.run(replace(_continuous(), roll=SineAxis(31, .2)), stop_event=threading.Event())
    assert controller.commands == []


def test_continuous_motion_compiles_one_bounded_repeat_program():
    controller = ConfirmedMotionController(_profile().to_motion_config())
    checked = MotionTrajectoryRunner(controller).preflight(_continuous())

    assert checked.compiled.finite_points == ()
    assert len(checked.compiled.prefix_points) == 1
    assert len(checked.compiled.periodic_points) == 50


def test_finite_engineering_trajectory_keeps_full_path_and_arrival():
    clock = _Clock()
    controller = ConfirmedMotionController(_profile().to_motion_config())
    trajectory = replace(_continuous(), steady_duration_s=.2, ramp_out_s=.1)
    result = MotionTrajectoryRunner(controller, clock_ns=lambda: clock.ns, sleep=clock.sleep).run(trajectory)
    assert result.intended_points == trajectory.sample_count == 4
    assert result.sent_points == 4
    assert result.arrival.successful
    assert result.stopped is False


@pytest.mark.parametrize("test_id", ["locked_rocking", "power_on_rocking"])
def test_automatic_motion_runs_through_delayed_observation_then_controlled_center(
    qapplication_session, tmp_path, test_id,
):
    class Execution(QObject):
        hardware_requested = Signal(object)
        observation_complete = Signal(str)
        motion_precondition_changed = Signal()

        def __init__(self):
            super().__init__()
            self.recipe = SimpleNamespace(payload=valid_recipe(duration_s=1))
            self.run = SimpleNamespace(progress=SimpleNamespace(remaining_s=0))
            self.completed = []
            self.failures = []

        def finish_observation(self, evidence):
            self.completed.append(evidence)

        def fail(self, reason):
            self.failures.append(reason)

        def tick(self):
            return

    class Power(QObject):
        output_changed = Signal(bool, object, object, str)

    execution = Execution()
    hardware = ProductionExecutionHardware(execution, SimpleNamespace(),
        SimpleNamespace(motion_profile=_profile()), Power(), tmp_path)
    hardware._test_id = test_id
    hardware._operation = "trajectory"
    hardware._request_id = "motion"
    stop = threading.Event()
    center_requests = []

    def center(**kwargs):
        center_requests.append(kwargs)
        return "center"

    hardware.control = SimpleNamespace(stop_sequence=stop.set, submit_center=center)
    trajectory = hardware._trajectory()
    assert trajectory.total_duration_s is None
    assert trajectory.ramp_out_s == 0
    clock = _Clock()
    controller = ConfirmedMotionController(_profile().to_motion_config())
    # 设备/参考恢复晚于运动启动；原1s有限轨迹会先结束，观察尚未结束。
    observation_start_s = 2.0
    observation_duration_s = 1.0

    def sleep(seconds):
        clock.sleep(seconds)
        if clock.ns >= (observation_start_s + observation_duration_s) * 1e9:
            hardware.finish(test_id)

    result = MotionTrajectoryRunner(controller, clock_ns=lambda: clock.ns, sleep=sleep).run(
        trajectory, stop_event=stop,
    )
    assert clock.ns / 1e9 == pytest.approx(3.0)
    assert result.intended_points is None
    assert result.sent_points == 30
    assert result.stopped
    assert execution.completed == []
    hardware._finished("motion", result)
    assert center_requests == [{"duration_ms": 1000}]
    assert execution.completed == []
    hardware._finished("center", controller.recover_to_center(duration_ms=1000))
    assert len(execution.completed) == 1
    assert execution.completed[0]["trajectory"]["intended_points"] is None
    assert execution.failures == []
    hardware.deleteLater()
    execution.deleteLater()
    qapplication_session.processEvents()


def test_fixture_worker_dispatches_continuous_motion_until_owner_stop(qapplication_session):
    qapp = qapplication_session
    commands = []
    results = []
    worker = FixtureControlWorker(_profile(), controller_factory=lambda profile:
                                  ConfirmedMotionController(profile.to_motion_config(), commands))
    worker.confirm_preflight()
    worker.request_finished.connect(lambda _request_id, result: results.append(result))
    worker.start()
    try:
        assert _wait_until(qapp, lambda: worker.controller.snapshot.ready)
        worker.submit_trajectory(_continuous())
        assert _wait_until(qapp, lambda: len(commands) >= 10)
        assert results == []
        worker.stop_sequence()
        assert _wait_until(qapp, lambda: len(results) == 1)
        assert results[0].stopped and results[0].intended_points is None
    finally:
        worker.shutdown()
        assert worker.wait(2000)
