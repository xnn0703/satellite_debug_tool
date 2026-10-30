"""M19-A.2 fixture diagnostics workspace integration tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

import pytest


def test_attitude_plot_series_breaks_measurement_time_gaps() -> None:
    from satellite_debug_tool.core.production import TimedAttitude
    from satellite_debug_tool.ui.fixture_debug_workspace import _attitude_plot_series

    samples = (
        TimedAttitude(1_000_000_000, 1.0, 2.0, 3.0),
        TimedAttitude(1_010_000_000, 1.1, 2.1, 3.1),
        TimedAttitude(2_000_000_000, 1.2, 2.2, 3.2),
    )
    x, y = _attitude_plot_series(
        samples,
        axis="roll",
        origin_ns=1_000_000_000,
        break_time_gaps=True,
    )

    assert len(x) == 4
    assert y[0] == pytest.approx(1.0)
    assert y[1] == pytest.approx(1.1)
    assert y[2] != y[2]
    assert y[3] == pytest.approx(1.2)


def test_fixture_attitude_plot_uses_one_shared_degree_axis_and_hides_unmapped_curves(
    qapp,
) -> None:
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureAttitudePlot

    plot = FixtureAttitudePlot()
    plot.set_coordinate_valid(False)

    assert tuple(plot._plots) == ("combined",)
    assert all(
        not axis.getAxis("left").autoSIPrefix
        for axis in plot._plots.values()
    )
    assert all(
        curve.getViewBox() is plot._plot.vb
        for curves in plot._curves.values()
        for curve in curves.values()
    )
    assert not plot._curves["roll"]["target"].isVisible()
    assert plot._curves["roll"]["measured"].isVisible()
    assert not plot._curves["roll"]["error"].isVisible()
    plot.close()


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _wait_until(qapp, predicate, *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return bool(predicate())


def _profile():
    from satellite_debug_tool.core.production import (
        FixtureAxisLimits,
        PlatformPose,
        WorkstationFixtureProfile,
    )

    limits = FixtureAxisLimits(15.0, 0.5, 30.0, 100.0)
    return WorkstationFixtureProfile(
        profile_id="fixture-ui",
        revision=1,
        model_id="lingjing-a6-200mm",
        local_ipv4="192.168.15.101",
        feedback_port=8410,
        controller_ipv4="192.168.15.201",
        command_port=7408,
        center_pose=PlatformPose(0, 0, 0, 0, 0, 100),
        roll_limits=limits,
        pitch_limits=limits,
        yaw_limits=limits,
        calibration_id="fixture-axis-map-1",
    )


def _settings(tmp_path: Path, monkeypatch):
    from satellite_debug_tool.core.config import Settings

    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    settings.set("production.fixture_profile_id", "fixture-ui")
    settings.set("production.last_operator", "operator-a")
    return settings


def _controller_factory(commands=None):
    from satellite_debug_tool.tests.motion_fakes import ConfirmedMotionController

    captured = commands if commands is not None else []
    return lambda profile: ConfirmedMotionController(profile.to_motion_config(), captured)


def test_trajectory_result_uses_nested_arrival_settlement() -> None:
    from satellite_debug_tool.core.motion_platform import PlatformPose, TrajectoryResult
    from satellite_debug_tool.tests.motion_fakes import ConfirmedMotionController
    from satellite_debug_tool.ui.fixture_debug_workspace import _settlement_result

    controller = ConfirmedMotionController(_profile().to_motion_config())
    arrival = controller.wait_for_target(PlatformPose(0, 0, 0))
    trajectory = TrajectoryResult(
        intended_points=1680,
        sent_points=1680,
        skipped_points=0,
        maximum_lateness_ms=1.0,
        stopped=False,
        arrival=arrival,
    )

    assert _settlement_result(trajectory) is arrival
    assert _settlement_result(trajectory).target_guard_passed


def test_automatic_calibration_preview_can_be_confirmed_and_saved(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from PySide6.QtWidgets import QMessageBox

    from satellite_debug_tool.core.production import (
        FixtureCalibration,
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    profile_store = FixtureProfileStore(tmp_path / "profiles-confirmation")
    profile_store.save(_profile())
    calibration_store = FixtureCalibrationStore(tmp_path / "calibrations-confirmation")
    workspace = FixtureDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=calibration_store,
        session_root=tmp_path / "sessions-confirmation",
        motion_only=True,
    )
    assert workspace.profile is not None
    original_revision = workspace.profile.revision
    preview = FixtureCalibration(
        calibration_id="MS6222-CAL-AUTO-TEST",
        profile_id=workspace.profile.profile_id,
        profile_sha256=workspace.profile.sha256,
        created_utc="2026-10-09T10:00:00+00:00",
        sensor_axis_for_logical=("roll", "pitch", "yaw"),
        logical_signs=(1, 1, 1),
        zero_offsets_deg=(0.2, -0.5, 153.5),
        response_matrix=((0.99, 0.01, 0.0), (0.0, 0.99, 0.01), (0.0, 0.0, 1.01)),
        cross_coupling_ratio=(0.01, 0.01, 0.0),
        static_noise_std_deg=(0.003, 0.003, 0.01),
        sample_coverage_ratio=1.0,
        confirmed=False,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    confirmed = workspace._confirm_calibration_preview(preview)

    assert confirmed is not None and confirmed.coordinate_valid
    assert workspace.profile.revision == original_revision + 1
    assert workspace.profile.calibration_id == confirmed.calibration_id
    assert calibration_store.load(confirmed.calibration_id) == confirmed
    workspace.shutdown()
    workspace.close()

    restarted = FixtureDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=calibration_store,
        session_root=tmp_path / "sessions-after-restart",
        motion_only=True,
    )
    assert restarted.profile is not None
    assert restarted.profile.calibration_id == confirmed.calibration_id
    assert restarted._calibration == confirmed
    assert restarted._attitude_plot._coordinate_valid
    restarted.shutdown()
    restarted.close()


def test_unstable_center_reference_cannot_be_confirmed(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from PySide6.QtWidgets import QMessageBox

    from satellite_debug_tool.core.production import (
        FixtureCalibration,
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    profile_store = FixtureProfileStore(tmp_path / "profiles-unstable-reference")
    profile_store.save(_profile())
    calibration_store = FixtureCalibrationStore(tmp_path / "calibrations-unstable-reference")
    workspace = FixtureDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=calibration_store,
        session_root=tmp_path / "sessions-unstable-reference",
        motion_only=True,
    )
    assert workspace.profile is not None
    original_revision = workspace.profile.revision
    preview = FixtureCalibration(
        calibration_id="MS6222-CAL-UNSTABLE-TEST",
        profile_id=workspace.profile.profile_id,
        profile_sha256=workspace.profile.sha256,
        created_utc="2026-10-09T10:00:00+00:00",
        sensor_axis_for_logical=("roll", "pitch", "yaw"),
        logical_signs=(1, 1, 1),
        zero_offsets_deg=(0.2, -0.5, 153.5),
        response_matrix=((0.99, 0.01, 0.0), (0.0, 0.99, 0.01), (0.0, 0.0, 1.01)),
        cross_coupling_ratio=(0.01, 0.01, 0.0),
        static_noise_std_deg=(0.003, 0.003, 0.647),
        sample_coverage_ratio=1.0,
        confirmed=False,
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message, *args, **kwargs: warnings.append(message),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: pytest.fail("unstable calibration must not reach confirmation"),
    )

    confirmed = workspace._confirm_calibration_preview(preview)

    assert confirmed is None
    assert warnings and "0.647" in warnings[0]
    assert workspace.profile.revision == original_revision
    assert workspace.profile.calibration_id == "fixture-axis-map-1"
    assert not (tmp_path / "calibrations-unstable-reference" / f"{preview.calibration_id}.json").exists()
    workspace.shutdown()
    workspace.close()


def test_trajectory_completion_records_execution_statistics(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.motion_platform import (
        MotionActionOutcome,
        MotionActionResult,
        PlatformPose,
        TrajectoryResult,
    )
    from satellite_debug_tool.core.production import (
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    profile_store = FixtureProfileStore(tmp_path / "profiles-trajectory-statistics")
    profile_store.save(_profile())
    workspace = FixtureDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(
            tmp_path / "calibrations-trajectory-statistics"
        ),
        session_root=tmp_path / "sessions-trajectory-statistics",
        motion_only=True,
    )
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        workspace,
        "_append_event",
        lambda message, *, event_type="workspace_event", details=None: events.append(
            (message, event_type, details or {})
        ),
    )
    arrival = MotionActionResult(
        action="trajectory",
        sent=True,
        outcome=MotionActionOutcome.SETTLED,
        motion_stopped=True,
        target_guard_passed=True,
        generation=0,
        started_monotonic_ns=1,
        settled_monotonic_ns=2,
        target_pose=PlatformPose(0.0, 0.0, 0.0, z_mm=100.0),
        target_pulses=(1, 1, 1, 1, 1, 1),
        final_pulses=(1, 1, 1, 1, 1, 1),
        errors_pulses=(0, 0, 0, 0, 0, 0),
        stationary_spans_pulses=(0, 0, 0, 0, 0, 0),
        maximum_error_pulses=0,
        maximum_error_mm=0.0,
        details="settled",
    )
    result = TrajectoryResult(
        intended_points=4000,
        sent_points=3999,
        skipped_points=1,
        maximum_lateness_ms=20.4,
        stopped=False,
        arrival=arrival,
    )

    workspace._on_control_finished("trajectory-test", result)

    execution = next(item for item in events if item[1] == "trajectory_execution_completed")
    assert "3999/4000" in execution[0]
    assert execution[2] == {
        "intended_points": 4000,
        "sent_points": 3999,
        "skipped_points": 1,
        "maximum_lateness_ms": 20.4,
    }
    workspace.shutdown()
    workspace.close()


def test_registered_model_installation_configuration_keeps_internal_id_read_only(
    qapp,
) -> None:
    from satellite_debug_tool.core.production import fixture_model_presets
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureProfileDialog

    profile = fixture_model_presets()[0].create_profile()
    dialog = FixtureProfileDialog(profile)

    assert dialog._model_combo.currentData() == "lingjing-a6-200mm"
    assert not dialog._model_combo.isEnabled()
    assert dialog._profile_id.isReadOnly()
    assert dialog._local_ipv4.isReadOnly()
    assert dialog._controller_ipv4.isReadOnly()
    assert all(not box.isEnabled() for box in dialog._sign_boxes.values())
    assert tuple(
        int(dialog._sign_boxes[axis].currentData())
        for axis in ("roll", "pitch", "yaw")
    ) == (-1, 1, -1)
    dialog.close()


def test_customer_mode_hides_maintenance_and_keeps_daily_pose_controls(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.production import (
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
        Ms6222ControlLease,
        Ms6222FrameEnvelope,
        Ms6222FrameType,
        Ms6222InsRecord,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    profile_store = FixtureProfileStore(tmp_path / "profiles-customer-mode")
    profile_store.save(_profile())
    workspace = FixtureDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(tmp_path / "calibrations-customer-mode"),
        reference_lease=Ms6222ControlLease(),
        customer_mode=True,
    )

    assert workspace._model_combo.isEnabled() is False
    assert workspace._installation_config.isHidden()
    assert not workspace._trajectory_group.isHidden()
    assert workspace._qualification_group.isHidden()
    assert workspace._calibration_group.isHidden()
    assert workspace._reset.isHidden()
    assert not workspace._manual_group.isHidden()
    assert not workspace._live_group.isHidden()
    assert workspace._absolute_heading.objectName() == "fixtureAbsoluteHeading"
    assert workspace._absolute_heading.text()
    assert workspace._profile_group.title() == "摇摆台与姿态参考"
    assert workspace._safety_group.title() == "运动控制安全确认"
    assert workspace._session_start.text() == "开始控制会话"
    assert workspace._trajectory_group.title() == "组合连续摇摆"
    assert workspace._trajectory_start.text() == "开始组合摇摆"
    assert workspace._stop.text() == "受控停止"
    for axis, (enabled, amplitude, frequency, phase) in workspace._trajectory_axes.items():
        enabled.setChecked(True)
        amplitude.setValue({"roll": 2.0, "pitch": 1.5, "yaw": 1.0}[axis])
        frequency.setValue(0.1)
        phase.setValue({"roll": 0.0, "pitch": 90.0, "yaw": 180.0}[axis])
    trajectory = workspace._trajectory_profile()
    assert trajectory.roll.enabled and trajectory.pitch.enabled and trajectory.yaw.enabled
    assert trajectory.total_duration_s == pytest.approx(80.0)
    workspace._on_control_busy(True)
    assert not workspace._preview_button.isEnabled()
    assert all(
        not widget.isEnabled()
        for widgets in workspace._trajectory_axes.values()
        for widget in widgets
    )
    workspace._on_control_busy(False)
    assert workspace._preview_button.isEnabled()
    invalid_record = Ms6222InsRecord(
        gps_week=0,
        gps_ms=0,
        message_sequence=0,
        time_state=0,
        gnss_state=0,
        fix_type=0,
        ekf_state=0,
        latitude_deg=0.0,
        longitude_deg=0.0,
        altitude_m=0.0,
        height_m=0.0,
        velocity_n_m_s=0.0,
        velocity_e_m_s=0.0,
        velocity_u_m_s=0.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        yaw_deg=153.5,
        latitude_std=0.0,
        longitude_std=0.0,
        altitude_std=0.0,
        velocity_n_std=0.0,
        velocity_e_std=0.0,
        velocity_u_std=0.0,
        roll_std_deg=0.0,
        pitch_std_deg=0.0,
        yaw_std_deg=0.02,
    )
    workspace._on_ms_frame(
        Ms6222FrameEnvelope(
            frame_type=Ms6222FrameType.INSPVAXB,
            raw=b"valid-crc-invalid-quality",
            host_monotonic_ns=time.monotonic_ns(),
            host_wall_time_ns=time.time_ns(),
            crc_valid=True,
            protocol_version=1,
            record=invalid_record,
        )
    )
    assert "不可用" in workspace._absolute_heading.text()
    assert "153.500" not in workspace._absolute_heading.text()
    assert workspace._session_end.text() == "结束控制会话"
    workspace.retranslate_ui()
    assert workspace._profile_group.title() == "摇摆台与姿态参考"
    assert workspace._safety_group.title() == "运动控制安全确认"
    assert workspace._session_start.text() == "开始控制会话"
    assert workspace._trajectory_group.title() == "组合连续摇摆"
    assert workspace._trajectory_start.text() == "开始组合摇摆"
    workspace.shutdown()
    workspace.close()


def test_fixture_workspace_publishes_platform_target_and_invalid_reference_facts(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.core.motion_platform import MotionCommandEvent, PlatformPose
    from satellite_debug_tool.core.production import (
        FixtureAttitudeSource,
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
        FixtureTelemetryBus,
        Ms6222FrameEnvelope,
        Ms6222FrameType,
        Ms6222InsRecord,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    monkeypatch.setenv("HOME", str(tmp_path))
    profile_store = FixtureProfileStore(tmp_path / "profiles-telemetry")
    profile_store.save(_profile())
    telemetry = FixtureTelemetryBus()
    published = []
    telemetry.sample_published.connect(published.append)
    workspace = FixtureDebugWorkspace(
        Settings(),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(tmp_path / "calibrations-telemetry"),
        telemetry=telemetry,
        motion_only=True,
    )
    pose = PlatformPose(1.0, 2.0, 3.0, z_mm=100.0)
    workspace._on_platform_send(
        MotionCommandEvent(
            action="pose",
            raw_command=b"command",
            target_pose=pose,
            target_pulses=(1, 2, 3, 4, 5, 6),
            duration_ms=1000,
            monotonic_ns=time.monotonic_ns(),
            generation=1,
        ),
        "pose",
        7,
    )
    invalid_record = Ms6222InsRecord(
        gps_week=0,
        gps_ms=0,
        message_sequence=0,
        time_state=0,
        gnss_state=0,
        fix_type=0,
        ekf_state=0,
        latitude_deg=0.0,
        longitude_deg=0.0,
        altitude_m=0.0,
        height_m=0.0,
        velocity_n_m_s=0.0,
        velocity_e_m_s=0.0,
        velocity_u_m_s=0.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        yaw_deg=153.5,
        latitude_std=0.0,
        longitude_std=0.0,
        altitude_std=0.0,
        velocity_n_std=0.0,
        velocity_e_std=0.0,
        velocity_u_std=0.0,
        roll_std_deg=0.01,
        pitch_std_deg=0.01,
        yaw_std_deg=0.02,
    )
    workspace._on_ms_frame(
        Ms6222FrameEnvelope(
            frame_type=Ms6222FrameType.INSPVAXB,
            raw=b"invalid-quality",
            host_monotonic_ns=time.monotonic_ns() + 30_000_000,
            host_wall_time_ns=time.time_ns(),
            crc_valid=True,
            protocol_version=1,
            record=invalid_record,
        )
    )

    assert [sample.source for sample in published] == [
        FixtureAttitudeSource.PLATFORM_TARGET,
        FixtureAttitudeSource.MS6222_TRUE_HEADING,
        FixtureAttitudeSource.MS6222_RELATIVE,
    ]
    assert published[0].metadata_event()["quality"]["fact"] == "command_sent"
    assert not published[1].valid and not published[2].valid
    assert published[2].invalid_reason == "reference_quality_invalid"
    workspace.shutdown()
    workspace.close()


def test_fixture_workspace_selects_model_and_materializes_internal_profile(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.core.production import (
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    profile_store = FixtureProfileStore(tmp_path / "profiles-model")
    workspace = FixtureDebugWorkspace(
        settings,
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(tmp_path / "calibrations-model"),
        session_root=tmp_path / "sessions-model",
        motion_only=True,
    )

    assert workspace._model_combo.count() == 1
    assert workspace._model_combo.currentText() == "南京灵境六自由度平台（200 mm）"
    assert workspace.profile is not None
    assert workspace.profile.model_id == "lingjing-a6-200mm"
    assert workspace.profile.profile_id == "lingjing-a6-200mm-01"
    assert profile_store.load("lingjing-a6-200mm-01") == workspace.profile
    assert settings.get("production.fixture_profile_id") == "lingjing-a6-200mm-01"
    assert not hasattr(workspace, "_profile_new")
    assert not hasattr(workspace, "_profile_reload")
    workspace.shutdown()
    workspace.close()


def test_automatic_qualification_one_click_opens_reference_before_session(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.production import (
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    settings = _settings(tmp_path, monkeypatch)
    workspace = FixtureDebugWorkspace(
        settings,
        FixtureControlLease(),
        profile_store=FixtureProfileStore(tmp_path / "profiles-one-click"),
        calibration_store=FixtureCalibrationStore(tmp_path / "calibrations-one-click"),
        session_root=tmp_path / "sessions-one-click",
        motion_only=False,
    )
    for check in workspace._safety_checks:
        check.setChecked(True)
    workspace._serial_combo.setEditText("COM7")
    opened: list[str] = []

    def open_reference() -> None:
        opened.append(workspace._serial_combo.currentText())
        workspace._ms_worker = object()  # type: ignore[assignment]

    monkeypatch.setattr(workspace, "_toggle_ms_connection", open_reference)
    workspace._update_start_gate()

    assert not workspace.session_active
    assert workspace._qualification_start.isEnabled()
    workspace._toggle_qualification()

    assert opened == ["COM7"]
    assert workspace._qualification_start_pending
    assert not workspace.session_active
    assert workspace._qualification_start.isEnabled()

    workspace._qualification_start_pending = False
    workspace._ms_worker = None
    workspace.shutdown()
    workspace.close()


def test_fixture_workspace_preserves_installation_endpoints_and_reconciles_model_facts(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from dataclasses import replace

    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.core.production import (
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
        fixture_model_preset,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    settings.set("production.fixture_profile_id", "lingjing-a6-200mm-01")
    profile_store = FixtureProfileStore(tmp_path / "profiles-endpoint-upgrade")
    old_profile = replace(
        fixture_model_preset("lingjing-a6-200mm").create_profile(),
        local_ipv4="192.168.15.99",
        command_port=9800,
        pitch_sign=-1,
    )
    profile_store.save(old_profile)

    workspace = FixtureDebugWorkspace(
        settings,
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(tmp_path / "calibrations-endpoint-upgrade"),
        session_root=tmp_path / "sessions-endpoint-upgrade",
        motion_only=True,
    )

    assert workspace.profile is not None
    assert workspace.profile.revision == 2
    assert workspace.profile.local_ipv4 == "192.168.15.99"
    assert workspace.profile.feedback_port == 8410
    assert workspace.profile.controller_ipv4 == "192.168.15.201"
    assert workspace.profile.command_port == 9800
    assert (workspace.profile.roll_sign, workspace.profile.pitch_sign, workspace.profile.yaw_sign) == (-1, 1, -1)
    assert workspace.profile.center_pose == old_profile.center_pose
    assert profile_store.load(old_profile.profile_id) == workspace.profile
    workspace.shutdown()
    workspace.close()


def test_motion_session_start_gate_uses_profile_and_safety_confirmation(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.core.production import (
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    workspace = FixtureDebugWorkspace(
        settings,
        FixtureControlLease(),
        profile_store=FixtureProfileStore(tmp_path / "profiles-service-gate"),
        calibration_store=FixtureCalibrationStore(tmp_path / "calibrations-service-gate"),
        session_root=tmp_path / "sessions-service-gate",
        controller_factory=_controller_factory(),
        motion_only=True,
    )
    for check in workspace._safety_checks:
        check.setChecked(True)

    assert workspace._session_start.isEnabled()

    workspace.shutdown()
    workspace.close()


def test_engineering_session_requires_no_batch_dut_or_reference(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.production import (
        FixtureCalibration,
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
        FixtureSessionConclusion,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    profile_store = FixtureProfileStore(tmp_path / "profiles")
    profile_store.save(_profile())
    commands = []
    workspace = FixtureDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(tmp_path / "calibrations"),
        session_root=tmp_path / "sessions",
        controller_factory=_controller_factory(commands),
    )

    assert workspace.profile is not None
    assert not workspace._session_start.isEnabled()
    for check in workspace._safety_checks:
        check.setChecked(True)
    assert workspace._session_start.isEnabled()

    session_dir = workspace.start_engineering_session()
    assert workspace.session_active
    assert workspace._ms_worker is None
    assert _wait_until(
        qapp,
        lambda: workspace._control.controller.snapshot.state.value == "ready",
    )
    workspace._control.submit_axis_move("roll", 4.0, total_duration_ms=300)
    assert _wait_until(qapp, lambda: len(workspace._run_summaries) == 1)
    assert len(commands) == 1
    assert commands[0][0].startswith(b"\xAA\x55\x00\x1b\xC0")
    assert commands[0][1] == ("192.168.15.201", 7408)

    workspace._begin_session_finish(
        conclusion=FixtureSessionConclusion.INCONCLUSIVE,
        notes="reference not installed",
    )
    assert _wait_until(
        qapp,
        lambda: not workspace.session_active and workspace._finalizer is None,
    )

    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["result_class"] == "ENGINEERING_ONLY"
    assert summary["counts"]["ms_raw"] == 0
    assert summary["counts"]["commands"] == 1
    assert summary["metrics"]["coordinate_valid"] is False
    assert all(not check.isChecked() for check in workspace._safety_checks)
    workspace.shutdown()


def test_finishing_keeps_ms_evidence_until_current_a6t_completion(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.production import (
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
        FixtureSessionConclusion,
        Ms6222FrameEnvelope,
        Ms6222FrameType,
        TimedAttitude,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    profile_store = FixtureProfileStore(tmp_path / "profiles-finish-window")
    profile_store.save(_profile())
    workspace = FixtureDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(
            tmp_path / "calibrations-finish-window"
        ),
        session_root=tmp_path / "sessions-finish-window",
        controller_factory=_controller_factory(),
    )
    for check in workspace._safety_checks:
        check.setChecked(True)
    session_dir = workspace.start_engineering_session()
    completion_ns = time.monotonic_ns() + 200_000_000
    workspace._targets.append(TimedAttitude(completion_ns, 0.0, 0.0, 0.0))

    workspace._begin_session_finish(
        conclusion=FixtureSessionConclusion.INCONCLUSIVE,
        notes="finish-window evidence",
    )

    assert workspace._session_accepting_frames
    workspace._on_ms_frame(
        Ms6222FrameEnvelope(
            frame_type=Ms6222FrameType.UNKNOWN,
            raw=b"noise",
            host_monotonic_ns=time.monotonic_ns(),
            host_wall_time_ns=time.time_ns(),
            crc_valid=False,
            protocol_version=0,
            record=None,
            error="noise",
        )
    )
    assert _wait_until(
        qapp,
        lambda: not workspace.session_active and workspace._finalizer is None,
    )

    summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["counts"]["ms_raw"] == 1
    assert summary["counts"]["ms_invalid"] == 1
    workspace.shutdown()


def test_long_session_analysis_runs_outside_the_ui_thread(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.production import (
        FixtureCalibration,
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
        FixtureSessionConclusion,
        TimedAttitude,
    )
    from satellite_debug_tool.ui import fixture_debug_workspace as fixture_ui

    profile_store = FixtureProfileStore(tmp_path / "profiles-background-analysis")
    profile_store.save(_profile())
    finalization_threads = []
    original_finalizer = fixture_ui._SessionFinalizeThread

    def observed_finalizer(*args, **kwargs):
        finalization_threads.append(threading.get_ident())
        return original_finalizer(*args, **kwargs)

    monkeypatch.setattr(fixture_ui, "_SessionFinalizeThread", observed_finalizer)
    workspace = fixture_ui.FixtureDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(
            tmp_path / "calibrations-background-analysis"
        ),
        session_root=tmp_path / "sessions-background-analysis",
        controller_factory=_controller_factory(),
    )
    for check in workspace._safety_checks:
        check.setChecked(True)
    session_dir = workspace.start_engineering_session()
    workspace._calibration = FixtureCalibration(
        calibration_id="MS6222-CAL-BACKGROUND",
        profile_id=workspace.profile.profile_id,
        profile_sha256=workspace.profile.sha256,
        created_utc="2026-08-23T00:00:00+00:00",
        sensor_axis_for_logical=("roll", "pitch", "yaw"),
        logical_signs=(1, 1, 1),
        zero_offsets_deg=(0.0, 0.0, 0.0),
        response_matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        cross_coupling_ratio=(0.0, 0.0, 0.0),
        static_noise_std_deg=(0.0, 0.0, 0.0),
        sample_coverage_ratio=1.0,
        confirmed=True,
    )
    now_ns = time.monotonic_ns()
    workspace._targets.clear()
    workspace._targets.extend(
        (
            TimedAttitude(now_ns, 0.0, 0.0, 0.0),
            TimedAttitude(now_ns + 100_000_000, 1.0, 0.0, 0.0),
        )
    )
    workspace._measurements.append(
        TimedAttitude(now_ns + 50_000_000, 0.5, 0.0, 0.0)
    )
    recorder = workspace._recorder
    assert recorder is not None
    recorder.record_target(TimedAttitude(now_ns, 0.0, 0.0, 0.0))
    recorder.record_target(
        TimedAttitude(now_ns + 100_000_000, 1.0, 0.0, 0.0)
    )
    recorder.record_measurement(
        TimedAttitude(now_ns + 50_000_000, 0.5, 0.0, 0.0)
    )
    analysis_threads = []
    original_generate = recorder.generate_comparison_sample

    def traced_generate(*args, **kwargs):
        analysis_threads.append(threading.get_ident())
        time.sleep(0.1)
        return original_generate(*args, **kwargs)

    monkeypatch.setattr(recorder, "generate_comparison_sample", traced_generate)
    ui_thread = threading.get_ident()
    started = time.monotonic()
    workspace._begin_session_finish(
        conclusion=FixtureSessionConclusion.INCONCLUSIVE,
        notes="background analysis",
    )
    assert time.monotonic() - started < 0.05
    assert _wait_until(
        qapp,
        lambda: not workspace.session_active and workspace._finalizer is None,
    )

    assert finalization_threads == [ui_thread]
    assert analysis_threads and analysis_threads[0] != ui_thread
    summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["counts"]["comparisons"] == 1
    workspace.shutdown()


def test_event_recording_failure_finalizes_session_as_incomplete(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.production import (
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
    )
    from satellite_debug_tool.i18n import tr
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    profile_store = FixtureProfileStore(tmp_path / "profiles-event-failure")
    profile_store.save(_profile())
    workspace = FixtureDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(
            tmp_path / "calibrations-event-failure"
        ),
        session_root=tmp_path / "sessions-event-failure",
        controller_factory=_controller_factory(),
    )
    for check in workspace._safety_checks:
        check.setChecked(True)
    session_dir = workspace.start_engineering_session()
    recorder = workspace._recorder
    assert recorder is not None

    def fail_record_event(*_args, **_kwargs) -> None:
        raise RuntimeError("event evidence disk failed")

    monkeypatch.setattr(recorder, "record_event", fail_record_event)
    workspace._append_event("trigger event evidence failure")

    assert _wait_until(
        qapp,
        lambda: not workspace.session_active and workspace._finalizer is None,
    )
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "incomplete"
    assert summary["status"] == "incomplete"
    assert "event evidence disk failed" in summary["metrics"]["abort_reason"]
    event_text = workspace._event_log.toPlainText()
    assert tr("Session incomplete: {path}", path=str(session_dir)) in event_text
    assert tr("Session saved: {path}", path=str(session_dir)) not in event_text
    workspace.shutdown()


def test_ms_frame_recording_failure_finalizes_session_as_incomplete(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.production import (
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
        Ms6222FrameEnvelope,
        Ms6222FrameType,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    profile_store = FixtureProfileStore(tmp_path / "profiles-ms-failure")
    profile_store.save(_profile())
    workspace = FixtureDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(tmp_path / "calibrations-ms-failure"),
        session_root=tmp_path / "sessions-ms-failure",
        controller_factory=_controller_factory(),
    )
    for check in workspace._safety_checks:
        check.setChecked(True)
    session_dir = workspace.start_engineering_session()
    recorder = workspace._recorder
    assert recorder is not None

    def fail_record_ms_frame(*_args, **_kwargs) -> None:
        raise RuntimeError("MS evidence disk failed")

    monkeypatch.setattr(recorder, "record_ms_frame", fail_record_ms_frame)
    workspace._on_ms_frame(
        Ms6222FrameEnvelope(
            frame_type=Ms6222FrameType.UNKNOWN,
            raw=b"evidence",
            host_monotonic_ns=time.monotonic_ns(),
            host_wall_time_ns=time.time_ns(),
            crc_valid=False,
            protocol_version=0,
            record=None,
            error="test frame",
        )
    )

    assert _wait_until(
        qapp,
        lambda: not workspace.session_active and workspace._finalizer is None,
    )
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "incomplete"
    assert summary["status"] == "incomplete"
    assert "MS evidence disk failed" in summary["metrics"]["abort_reason"]
    workspace.shutdown()


def test_metric_finalization_failure_produces_incomplete_result(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.production import (
        FixtureCalibration,
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
        FixtureSessionConclusion,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    profile_store = FixtureProfileStore(tmp_path / "profiles-metric-failure")
    profile_store.save(_profile())
    workspace = FixtureDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(
            tmp_path / "calibrations-metric-failure"
        ),
        session_root=tmp_path / "sessions-metric-failure",
        controller_factory=_controller_factory(),
    )
    for check in workspace._safety_checks:
        check.setChecked(True)
    session_dir = workspace.start_engineering_session()
    recorder = workspace._recorder
    assert recorder is not None

    workspace._calibration = FixtureCalibration(
        calibration_id="MS6222-CAL-METRIC-FAILURE",
        profile_id=workspace.profile.profile_id,
        profile_sha256=workspace.profile.sha256,
        created_utc="2026-08-23T00:00:00+00:00",
        sensor_axis_for_logical=("roll", "pitch", "yaw"),
        logical_signs=(1, 1, 1),
        zero_offsets_deg=(0.0, 0.0, 0.0),
        response_matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        cross_coupling_ratio=(0.0, 0.0, 0.0),
        static_noise_std_deg=(0.0, 0.0, 0.0),
        sample_coverage_ratio=1.0,
        confirmed=True,
    )

    def fail_generate_comparisons(*_args, **_kwargs) -> None:
        raise RuntimeError("metric evidence disk failed")

    monkeypatch.setattr(
        recorder,
        "generate_comparison_sample",
        fail_generate_comparisons,
    )
    workspace._begin_session_finish(
        conclusion=FixtureSessionConclusion.INCONCLUSIVE,
        notes="metric finalization",
    )

    assert _wait_until(
        qapp,
        lambda: not workspace.session_active and workspace._finalizer is None,
    )
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "incomplete"
    assert summary["status"] == "incomplete"
    assert "metric evidence disk failed" in summary["metrics"]["abort_reason"]
    workspace.shutdown()


def test_leaving_inactive_fixture_page_clears_safety_confirmations(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.production import FixtureProfileStore
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    settings = _settings(tmp_path, monkeypatch)
    FixtureProfileStore(tmp_path / ".satellite_debug_tool" / "fixture_profiles").save(
        _profile()
    )
    workspace = ProductionWorkspace(settings)
    workspace.activate_view()
    workspace._switch_subpage(1)
    assert workspace._fixture_debug is not None
    for check in workspace._fixture_debug._safety_checks:
        check.setChecked(True)
    assert all(check.isChecked() for check in workspace._fixture_debug._safety_checks)

    workspace._switch_subpage(0)

    assert workspace._subpages.currentIndex() == 0
    assert all(
        not check.isChecked() for check in workspace._fixture_debug._safety_checks
    )
    workspace.close()


def test_fixture_workspace_runtime_retranslates_dynamic_status(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.production import (
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
    )
    from satellite_debug_tool.i18n import initialize_translation_manager
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    monkeypatch.delenv("SATELLITE_DEBUG_LOCALE", raising=False)
    settings = _settings(tmp_path, monkeypatch)
    settings.set("ui.language", "zh_CN")
    manager = initialize_translation_manager(qapp, settings)
    manager.set_preference("zh_CN")
    profile_store = FixtureProfileStore(tmp_path / "profiles-i18n")
    profile_store.save(_profile())
    workspace = FixtureDebugWorkspace(
        settings,
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(tmp_path / "calibrations-i18n"),
        session_root=tmp_path / "sessions-i18n",
    )
    qapp.processEvents()
    assert workspace._profile_status.text().startswith("档案有效")

    manager.set_preference("en_US")
    qapp.processEvents()

    assert workspace._profile_status.text().startswith("Profile valid")
    assert workspace._ms_status.text() == "Reference disconnected"
    assert workspace._safety_checks[0].text() == "Platform is centered"

    manager.set_preference("zh_CN")
    qapp.processEvents()
    assert workspace._safety_checks[0].text() == "摇摆台已回中"
    workspace.shutdown()
    workspace.close()


def test_active_session_requires_close_confirmation(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from PySide6.QtWidgets import QMessageBox

    from satellite_debug_tool.core.production import (
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    profile_store = FixtureProfileStore(tmp_path / "profiles-close")
    profile_store.save(_profile())
    workspace = FixtureDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(tmp_path / "calibrations-close"),
        session_root=tmp_path / "sessions-close",
        controller_factory=_controller_factory(),
    )
    for check in workspace._safety_checks:
        check.setChecked(True)
    workspace.start_engineering_session()

    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Cancel,
    )
    assert not workspace.confirm_shutdown()
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    assert workspace.confirm_shutdown()
    workspace.shutdown()
    workspace.close()
