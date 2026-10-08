"""M19-A.2 fixture diagnostics workspace integration tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

import pytest


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

    limits = FixtureAxisLimits(15.0, 2.0, 0.5, 30.0, 100.0)
    return WorkstationFixtureProfile(
        profile_id="fixture-ui",
        revision=1,
        host="192.168.1.50",
        port=9800,
        center_pose=PlatformPose(0, 0, 0, 0, 0, 100),
        reset_pose=PlatformPose(0, 0, 0, 0, 0, 0),
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
    assert dialog._host.isReadOnly()
    assert all(box.isEnabled() for box in dialog._sign_boxes.values())
    dialog.close()


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


def test_fixture_workspace_upgrades_driver_endpoint_to_platform_service(
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
        host="192.168.15.201",
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
    assert workspace.profile.host == "192.168.15.101"
    assert workspace.profile.pitch_sign == -1
    assert workspace.profile.center_pose == old_profile.center_pose
    assert profile_store.load(old_profile.profile_id) == workspace.profile
    workspace.shutdown()
    workspace.close()


def test_windows_platform_service_listener_is_required_before_motion_session(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.core.production import (
        FixtureCalibrationStore,
        FixtureControlLease,
        FixtureProfileStore,
        LingjingPlatformServiceController,
        PlatformServiceState,
    )
    from satellite_debug_tool.ui.fixture_debug_workspace import FixtureDebugWorkspace

    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    controller = LingjingPlatformServiceController(
        resource_root=tmp_path / "missing-service-resource",
        runtime_root=tmp_path / "service-runtime",
        platform_name="win32",
        local_ip_provider=lambda: {"192.168.15.49"},
        listener_provider=lambda: (),
    )
    workspace = FixtureDebugWorkspace(
        settings,
        FixtureControlLease(),
        profile_store=FixtureProfileStore(tmp_path / "profiles-service-gate"),
        calibration_store=FixtureCalibrationStore(tmp_path / "calibrations-service-gate"),
        session_root=tmp_path / "sessions-service-gate",
        platform_service=controller,
        motion_only=True,
    )
    for check in workspace._safety_checks:
        check.setChecked(True)

    controller.ensure_running()
    assert controller.snapshot.state == PlatformServiceState.FAILED
    assert not workspace._session_start.isEnabled()
    assert "192.168.15.101" in workspace._service_status.text()

    controller._publish(
        PlatformServiceState.LISTENING,
        "UDP 9800 is listening",
        pid=4102,
        owned=True,
        runtime_dir=tmp_path / "service-runtime",
    )
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
        control_sender=lambda data, endpoint: not commands.append((data, endpoint)),
    )

    assert workspace.profile is not None
    assert not workspace._session_start.isEnabled()
    for check in workspace._safety_checks:
        check.setChecked(True)
    assert workspace._session_start.isEnabled()

    session_dir = workspace.start_engineering_session()
    assert workspace.session_active
    assert workspace._ms_worker is None
    workspace._control.submit_axis_move("roll", 4.0, total_duration_ms=300)
    assert _wait_until(qapp, lambda: len(workspace._run_summaries) == 1)
    assert len(commands) == 2

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
    assert summary["counts"]["commands"] == 2
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
        control_sender=lambda _data, _endpoint: True,
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
    workspace = fixture_ui.FixtureDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        profile_store=profile_store,
        calibration_store=FixtureCalibrationStore(
            tmp_path / "calibrations-background-analysis"
        ),
        session_root=tmp_path / "sessions-background-analysis",
        control_sender=lambda _data, _endpoint: True,
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
        control_sender=lambda _data, _endpoint: True,
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
        control_sender=lambda _data, _endpoint: True,
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
        control_sender=lambda _data, _endpoint: True,
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
        control_sender=lambda _data, _endpoint: True,
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
