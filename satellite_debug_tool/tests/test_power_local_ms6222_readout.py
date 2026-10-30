"""M46：电源交还面板与共享 MS 姿态读数的会话回归。"""
from __future__ import annotations

from dataclasses import replace
import time

import pytest
from PySide6.QtCore import QElapsedTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from satellite_debug_tool.core.external_power_monitor import (
    ExternalPowerAction, ExternalPowerConfig, ExternalPowerMonitor, ExternalPowerPhase,
    ExternalPowerStore, ExternalPowerWorker, PswControlledSession,
)
from satellite_debug_tool.core.production.fixture_telemetry import (
    FixtureAttitudeSample, FixtureAttitudeSource, FixtureTelemetryBus,
)
from satellite_debug_tool.core.production.power_supply import GwInstekPswAdapter, PowerSupplyError, PowerSupplyState
from satellite_debug_tool.core.production.ms6222_protocol import Ms6222StreamParser
from satellite_debug_tool.tests.test_external_power_monitor import _identity, _sample
from satellite_debug_tool.tests.test_ms6222_protocol import _ins_frame
from satellite_debug_tool.tests.test_customer_overview_view import _LiveDouble, _SettingsDouble
from satellite_debug_tool.ui.customer_overview_view import CustomerOverviewView
from satellite_debug_tool.ui.external_power_window import ExternalPowerHistoryWindow
from satellite_debug_tool.i18n import tr


def _wait(predicate):
    timer = QElapsedTimer()
    timer.start()
    while not predicate() and timer.elapsed() < 2000:
        QTest.qWait(5)
    assert predicate()


@pytest.mark.parametrize("fail", [False, True])
def test_release_finishes_current_read_discards_queued_control_and_closes(fail, qapplication_session):
    calls = []

    class Session:
        def connect(self):
            calls.append("connect")
            return _identity()

        def read_sample(self, generation):
            calls.append("read")
            worker.request_action(ExternalPowerAction.ENABLE, "queued-enable")
            worker.request_release_local("local")
            return _sample(1, generation=generation)

        def release_local(self):
            calls.append("local")
            if fail:
                raise PowerSupplyError("connection lost")

        def execute(self, *args):
            pytest.fail("queued output command executed after release")

        def close(self):
            calls.append("close")

    worker = ExternalPowerWorker(ExternalPowerConfig("192.168.1.16"), session_factory=lambda _: Session())
    outcomes = []
    worker.action_finished.connect(outcomes.append)
    worker.run()
    assert calls == ["connect", "read", "local", "close"]
    assert len(outcomes) == 1
    assert outcomes[0].action is ExternalPowerAction.RELEASE_LOCAL
    assert outcomes[0].succeeded is (not fail)
    assert outcomes[0].sample is None


@pytest.mark.parametrize("output", [False, True])
def test_session_release_writes_only_local_command_and_closes(output):
    commands = []

    class Transport:
        def connect(self):
            pass

        def write(self, data):
            commands.append(data.decode("ascii").strip())

        def read_line(self):
            return "GW-INSTEK,PSW 80-27,PSW1234,1.70"

        def close(self):
            commands.append("close")

    session = PswControlledSession(ExternalPowerConfig("192.168.1.16"))
    session._adapter = GwInstekPswAdapter(
        ExternalPowerConfig("192.168.1.16").supply_config(), transport_factory=lambda _: Transport(),
    )
    session.connect()
    # 输出状态不影响交还路径；脚本拒绝任何额外查询/输出/设参命令。
    session._adapter._state = PowerSupplyState.ON_CONFIRMED if output else PowerSupplyState.OFF_CONFIRMED
    session.release_local()
    session.close()
    assert commands == ["*IDN?", "SYST:COMM:RLST LOC", "close"]


def test_monitor_holds_local_across_activity_changes_and_explicitly_resumes(qapplication_session):
    calls = []

    class Session:
        def connect(self):
            calls.append("connect")
            return _identity()

        def read_sample(self, generation):
            return _sample(time.monotonic_ns(), generation=generation)

        def release_local(self):
            calls.append("local")

        def close(self):
            calls.append("close")

    store = ExternalPowerStore()
    monitor = ExternalPowerMonitor(store, worker_factory=lambda config: ExternalPowerWorker(
        config, session_factory=lambda _: Session(),
    ))
    monitor.configure("192.168.1.16")
    monitor.set_active(True)
    try:
        _wait(lambda: store.snapshot.phase is ExternalPowerPhase.ONLINE)
        history_size = len(store.history()[0])
        monitor.release_local()
        monitor.release_local()
        _wait(lambda: monitor._worker is None)
        assert store.snapshot.phase is ExternalPowerPhase.LOCAL_REQUESTED
        assert len(store.history()[0]) == history_size
        monitor.set_active(False)
        monitor.set_active(True)
        QTest.qWait(50)
        assert calls == ["connect", "local", "close"]
        assert store.snapshot.phase is ExternalPowerPhase.LOCAL_REQUESTED
        with pytest.raises(PowerSupplyError, match="panel control"):
            monitor.request_action(ExternalPowerAction.ENABLE)
        monitor.resume_monitoring()
        _wait(lambda: store.snapshot.phase is ExternalPowerPhase.ONLINE)
        assert calls.count("connect") == 2
        assert store.snapshot.generation == 2
    finally:
        assert monitor.shutdown()


def _publish(bus, envelope):
    record = envelope.record
    bus.publish_reference_attitude(
        host_monotonic_ns=envelope.host_monotonic_ns,
        roll_deg=record.roll_deg, pitch_deg=record.pitch_deg,
        yaw_deg=record.yaw_deg, gnss_state=record.gnss_state,
    )


def _ins(now_ns):
    return Ms6222StreamParser().feed(_ins_frame(), host_monotonic_ns=now_ns, host_wall_time_ns=200)[0]


def test_reference_single_snapshot_tracks_quality_age_calibration_and_session():
    bus = FixtureTelemetryBus()
    assert bus.reference_readout().status == "hidden"
    bus.set_reference_connected(True)
    assert bus.reference_readout().status == "waiting"
    raw = _ins(1_000_000_000)
    _publish(bus, raw)
    readout = bus.reference_readout(now_ns=raw.host_monotonic_ns)
    assert readout.angles == (-2.5, 1.25, 359.5)
    bus.publish(FixtureAttitudeSample(
        FixtureAttitudeSource.MS6222_RELATIVE, raw.host_monotonic_ns, 200,
        roll_deg=1, pitch_deg=2, yaw_deg=3, calibration_id="cal-1",
    ))
    readout = bus.reference_readout(now_ns=raw.host_monotonic_ns)
    assert readout.angles == (-2.5, 1.25, 359.5)
    assert bus.reference_readout(now_ns=4_000_000_001).status == "stale"
    _publish(bus, replace(raw, record=replace(raw.record, gnss_state=0)))
    assert bus.reference_readout(now_ns=raw.host_monotonic_ns).status == "invalid"
    bus.set_reference_connected(False)
    assert bus.reference_readout().status == "disconnected"
    bus.set_reference_connected(True)
    assert bus.reference_readout().status == "waiting"


def test_overview_reference_row_and_power_button(qapplication_session, monkeypatch):
    import satellite_debug_tool.ui.customer_overview_view as overview

    class AttitudeDouble(QWidget):
        def __getattr__(self, name):
            return lambda *a, **kw: None

    monkeypatch.setattr(overview, "AttitudeWidget", AttitudeDouble)
    bus = FixtureTelemetryBus()
    view = CustomerOverviewView(_LiveDouble(), _SettingsDouble(), fixture_telemetry=bus)
    window = ExternalPowerHistoryWindow(ExternalPowerStore())
    try:
        assert view._reference_readout.isHidden()
        bus.set_reference_connected(True)
        raw = _ins(time.monotonic_ns())
        _publish(bus, replace(raw, record=replace(raw.record, yaw_deg=158.468)))
        bus.publish(FixtureAttitudeSample(
            FixtureAttitudeSource.MS6222_RELATIVE, raw.host_monotonic_ns, 200,
            roll_deg=-0.04, pitch_deg=-0.1, yaw_deg=5.0174212761964,
            calibration_id="cal-20261009",
        ))
        view.refresh()
        assert "158.468" in view._reference_readout.text()
        assert "5.017" not in view._reference_readout.text()
        assert tr("Raw INS, true heading") in view._reference_readout.text()
        assert not view._reference_readout.isHidden()
        bus.set_reference_connected(False)
        view.refresh()
        assert "158.468" not in view._reference_readout.text()
        assert tr("Reference disconnected") in view._reference_readout.text()
        assert window._local.text() == tr("Exit remote (panel control)")
        view.deactivate_view()
        assert not view._timer.isActive()
    finally:
        view.close()
        window.close()


@pytest.mark.parametrize("stage", ["connect", "read"])
def test_worker_release_during_io_failure_finishes_without_reconnect(stage, qapplication_session):
    calls = []

    class Session:
        def connect(self):
            calls.append("connect")
            if stage == "connect":
                worker.request_release_local("local")
                raise PowerSupplyError("connect failed")
            return _identity()

        def read_sample(self, generation):
            worker.request_release_local("local")
            raise PowerSupplyError("read failed")

        def release_local(self):
            calls.append("local")
            if stage == "connect":
                raise PowerSupplyError("connection unavailable")

        def close(self):
            calls.append("close")

    worker = ExternalPowerWorker(ExternalPowerConfig("192.168.1.16"), session_factory=lambda _: Session())
    outcomes = []
    worker.action_finished.connect(outcomes.append)
    worker.run()
    assert calls == ["connect", "local", "close"]
    assert len(outcomes) == 1
    assert outcomes[0].succeeded is (stage == "read")


def test_both_ms_ingress_pages_publish_to_shared_reference(qapplication_session, tmp_path):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.core.production import Ms6222ControlLease, FixtureControlLease
    from satellite_debug_tool.core.production.fixture_profile import FixtureProfileStore
    from satellite_debug_tool.core.production.fixture_analysis import FixtureCalibrationStore
    from satellite_debug_tool.ui.ms6222_debug_workspace import Ms6222DebugWorkspace
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    bus = FixtureTelemetryBus()
    settings = Settings(config_directory=tmp_path)
    independent = Ms6222DebugWorkspace(settings, Ms6222ControlLease(), telemetry=bus)
    fixture = None
    try:
        independent._on_connection_changed(True, "COM1")
        independent._on_frame(_ins(time.monotonic_ns()))
        assert bus.reference_readout().angles == (-2.5, 1.25, 359.5)
        # 懒创建另一页不会清空独立 MS 会话；无串口 owner 的关闭也保持参考。
        fixture = FixtureDebugWorkspace(
            settings, FixtureControlLease(), telemetry=bus,
            profile_store=FixtureProfileStore(tmp_path / "profiles"),
            calibration_store=FixtureCalibrationStore(tmp_path / "calibrations"),
        )
        assert bus.reference_readout().status == "valid"
        fixture._disconnect_ms()
        assert bus.reference_readout().status == "valid"
        independent._on_connection_changed(False, "COM1")
        assert bus.reference_readout().status == "disconnected"
        fixture._on_ms_connection_changed(True, "COM1")
        fixture._on_ms_frame(_ins(time.monotonic_ns()))
        assert bus.reference_readout().angles == (-2.5, 1.25, 359.5)
        fixture._on_ms_connection_changed(False, "COM1")
        assert bus.reference_readout().status == "disconnected"
    finally:
        independent.shutdown()
        independent.close()
        if fixture is not None:
            fixture.shutdown()
            fixture.close()


def test_overview_absolute_heading_survives_relative_calibration():
    """158.468° 原始航向与 5.017° 相对航向分别服务总览与摇摆台。"""
    bus = FixtureTelemetryBus()
    bus.set_reference_connected(True)
    stamp = 1_000_000_000
    bus.publish_reference_attitude(
        host_monotonic_ns=stamp, roll_deg=0.2, pitch_deg=-0.5,
        yaw_deg=158.468, gnss_state=3,
    )
    relative = FixtureAttitudeSample(
        FixtureAttitudeSource.MS6222_RELATIVE, stamp, 200,
        roll_deg=-0.04, pitch_deg=-0.1, yaw_deg=158.468 - 153.4505787238036,
        calibration_id="cal-20261009",
    )
    published = []
    bus.sample_published.connect(published.append)
    assert bus.publish(relative)
    assert bus.reference_readout(now_ns=stamp).angles == (0.2, -0.5, 158.468)
    assert published == [relative]
    assert published[0].yaw_deg == pytest.approx(5.0174212761964)
