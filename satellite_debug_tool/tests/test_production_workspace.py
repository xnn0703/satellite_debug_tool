"""M19 production workspace batch setup and hidden-entry tests."""

from __future__ import annotations

import json
import gc
import math
import os
import time
from pathlib import Path

import pytest

from satellite_debug_tool.tests.test_production_recipe import valid_recipe


@pytest.fixture(autouse=True)
def _dispose_created_production_widgets(qapplication_session):
    """在主线程收尾本项工作区，避免后台 worker 触发循环 GC 销毁 Qt 控件。"""
    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid

    from satellite_debug_tool.ui.main_window import MainWindow
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    created = []

    def track_constructor(widget_type, constructors):
        initialize = widget_type.__init__

        def initialize_owned(widget, *args, **kwargs):
            initialize(widget, *args, **kwargs)
            created.append(widget)

        constructors.setattr(widget_type, "__init__", initialize_owned)

    with pytest.MonkeyPatch.context() as constructors:
        for widget_type in (MainWindow, ProductionWorkspace):
            track_constructor(widget_type, constructors)
        try:
            yield
        finally:
            created.sort(key=lambda widget: isinstance(widget, MainWindow), reverse=True)
            for widget in created:
                if not isValid(widget):
                    continue
                closed = widget.close()
                deadline = time.monotonic() + 5
                while not closed and time.monotonic() < deadline:
                    qapplication_session.processEvents()
                    time.sleep(.001)
                    closed = widget.close()
                assert closed, "production UI ownership must settle before deleting test widgets"
            for widget in created:
                if isValid(widget):
                    widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapplication_session.processEvents()
            created.clear()
            gc.collect()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapplication_session.processEvents()


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def settings(tmp_path: Path, monkeypatch):
    from satellite_debug_tool.core.config import Settings

    monkeypatch.setenv("HOME", str(tmp_path))
    value = Settings()
    value.set("production.discovery_cidr", "127.0.0.1/32")
    value.set("production.local_port", 0)
    return value


def _write_recipe(
    path: Path,
    *,
    duration_s: int = 3600,
    product: str = "afd01",
    target_device_count: int = 1,
) -> Path:
    payload = valid_recipe(duration_s=duration_s)
    payload["product"] = product
    payload["target_device_count"] = target_device_count
    if product == "afd01c":
        payload["recipe_id"] = "AFD01C-PILOT-R1"
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _mark_analysis_capture_ready(workspace) -> None:
    """Unrelated UI tests enter after M50's independently-tested capture gate."""

    from satellite_debug_tool.core.production.fleet import ProductionCaptureState

    for session in workspace._fleet.sessions():
        if not session.recording_armed:
            continue
        session._capture_confirmed = True
        session._capture_data_observed = True
        session._capture_state = ProductionCaptureState.READY
        session._capture_error = ""
    workspace._update_start_gate()


def test_workspace_describes_formal_preflight_contract(qapp, settings) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)

    notice = workspace._preview_notice.text()
    assert "station profile" in notice or "工位档案" in notice
    assert "preflight" in notice or "预检" in notice
    workspace.close()


def test_shutdown_preflight_blocks_active_production_ownership(qapp, settings) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    class PendingFleet:
        recording_finalize_pending = False
        shutdown_ready = False

        def __init__(self) -> None:
            self.stop_calls = 0

        def stop(self) -> dict:
            self.stop_calls += 1
            return {}

    workspace = ProductionWorkspace(settings)
    real_fleet = workspace._fleet
    pending = PendingFleet()
    workspace._fleet = pending

    assert not workspace.confirm_shutdown()
    assert pending.stop_calls == 1
    assert "ownership" in workspace._footer_status.text().lower() or "所有权" in (
        workspace._footer_status.text()
    )

    workspace._fleet = real_fleet
    workspace.close()


def test_workspace_creates_immutable_batch_artifacts(
    qapp,
    settings,
    tmp_path: Path,
) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    recipe_path = _write_recipe(tmp_path / "recipe.json")
    output_root = tmp_path / "output"
    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(recipe_path)
    workspace._batch_id_edit.setText("PILOT-001")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(output_root))

    batch = workspace.create_batch()

    assert batch["batch_id"] == "PILOT-001"
    assert batch["recipe_sha256"] == workspace.recipe.sha256
    assert workspace.batch_output_dir == output_root / "PILOT-001"
    assert (workspace.batch_output_dir / "recipe.json").is_file()
    assert (workspace.batch_output_dir / "batch.sqlite3").is_file()
    snapshot = json.loads(
        (workspace.batch_output_dir / "recipe.json").read_text(
            encoding="utf-8"
        )
    )
    assert snapshot["recipe_id"] == "AFD01-PILOT-R1"
    assert workspace._batch_id_edit.isReadOnly()
    assert not workspace._create_button.isEnabled()
    assert settings.get("production.last_operator") == "operator-a"
    workspace.close()


def test_batch_id_cannot_escape_output_root_or_overwrite_existing_evidence(
    qapp,
    settings,
    tmp_path: Path,
) -> None:
    from satellite_debug_tool.core.production import ResultStoreError
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    recipe_path = _write_recipe(tmp_path / "recipe.json")
    output_root = tmp_path / "output"
    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(recipe_path)
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(output_root))
    workspace._batch_id_edit.setText("../escape")
    with pytest.raises(ValueError, match="single ASCII path component"):
        workspace.create_batch()
    workspace.close()

    occupied = output_root / "PILOT-EXISTING"
    occupied.mkdir(parents=True)
    evidence = occupied / "do-not-overwrite.txt"
    evidence.write_text("original", encoding="utf-8")
    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(recipe_path)
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(output_root))
    workspace._batch_id_edit.setText("PILOT-EXISTING")
    with pytest.raises(ResultStoreError, match="already exists"):
        workspace.create_batch()
    assert evidence.read_text(encoding="utf-8") == "original"
    workspace.close()


def test_engineering_recipe_is_marked_in_result_store(
    qapp,
    settings,
    tmp_path: Path,
) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(
        _write_recipe(tmp_path / "short.json", duration_s=30)
    )
    workspace._batch_id_edit.setText("DEV-001")
    workspace._operator_edit.setText("developer")
    workspace._output_edit.setText(str(tmp_path / "output"))

    batch = workspace.create_batch()

    assert batch["engineering_only"] == 1
    workspace.close()


def test_identified_session_is_registered_and_recording_armed(
    qapp,
    settings,
    tmp_path: Path,
) -> None:
    from satellite_debug_tool.core.production import FleetDatagram
    from satellite_debug_tool.i18n import tr
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
    from satellite_debug_tool.tests.test_production_fleet import _identity

    workspace = ProductionWorkspace(settings)
    workspace.activate_view()
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json"))
    workspace._batch_id_edit.setText("PILOT-FLEET")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))
    workspace.create_batch()
    endpoint = ("127.0.0.1", 4004)
    workspace._fleet._on_datagram(
        FleetDatagram(
            endpoint=endpoint,
            data=_identity("AFD01-UI-001"),
            wall_time_ns=1_000_000_000,
            monotonic_ns=2_000_000_000,
        )
    )

    devices = workspace.result_store.list_devices("PILOT-FLEET")
    assert len(devices) == 1
    assert devices[0]["serial_number"] == "AFD01-UI-001"
    assert workspace._snr_panels[1].serial_number == "AFD01-UI-001"
    assert workspace._fleet.sessions()[0].recording_armed
    assert tr("Preparing full analysis capture") in (
        workspace._snr_panels[1]._connection_label.full_text
    )
    workspace.close()


@pytest.mark.parametrize("product", ("afd01a", "afd01b2", "afd01c", "afd01d"))
def test_afd01_variant_recipe_registers_matching_device(
    qapp,
    settings,
    tmp_path: Path,
    product: str,
) -> None:
    import time

    from satellite_debug_tool.core.production import FleetDatagram
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
    from satellite_debug_tool.tests.test_production_fleet import _identity

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(
        _write_recipe(tmp_path / f"{product}.json", product=product)
    )
    workspace._batch_id_edit.setText(f"PILOT-{product.upper()}")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))
    workspace.create_batch()
    workspace._fleet._on_datagram(
        FleetDatagram(
            endpoint=("127.0.0.1", 4004),
            data=_identity(
                f"{product.upper()}-UI-001",
                model=product.upper(),
                service_protocol=8,
            ),
            wall_time_ns=time.time_ns(),
            monotonic_ns=time.monotonic_ns(),
        )
    )
    qapp.processEvents()
    _mark_analysis_capture_ready(workspace)

    devices = workspace.result_store.list_devices(f"PILOT-{product.upper()}")
    assert [(item["serial_number"], item["hardware_type"]) for item in devices] == [
        (f"{product.upper()}-UI-001", product.upper())
    ]
    assert workspace._start_button.isEnabled()
    workspace.close()


def test_recipe_product_mismatch_blocks_batch_start(
    qapp,
    settings,
    tmp_path: Path,
) -> None:
    import time

    from satellite_debug_tool.core.production import FleetDatagram
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
    from satellite_debug_tool.tests.test_production_fleet import _identity

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(
        _write_recipe(tmp_path / "afd01c.json", product="afd01c")
    )
    workspace._batch_id_edit.setText("PILOT-MISMATCH")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))
    workspace.create_batch()
    workspace._fleet._on_datagram(
        FleetDatagram(
            endpoint=("127.0.0.1", 4004),
            data=_identity("AFD01-WRONG-RECIPE"),
            wall_time_ns=time.time_ns(),
            monotonic_ns=time.monotonic_ns(),
        )
    )
    qapp.processEvents()

    assert not workspace._start_button.isEnabled()
    assert "AFD01C" in workspace._start_button.toolTip()
    assert "AFD01" in workspace._start_button.toolTip()
    assert workspace.result_store.list_devices("PILOT-MISMATCH") == []
    workspace.close()


def test_online_identity_conflict_blocks_batch_start(
    qapp,
    settings,
    tmp_path: Path,
) -> None:
    import time

    from satellite_debug_tool.core.production import FleetDatagram
    from satellite_debug_tool.i18n import tr
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
    from satellite_debug_tool.tests.test_production_fleet import _identity

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json"))
    workspace._batch_id_edit.setText("PILOT-CONFLICT")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))
    workspace.create_batch()
    now_ns = time.monotonic_ns()
    for index, endpoint in enumerate(
        (("127.0.0.1", 4004), ("127.0.0.2", 4004)),
        start=1,
    ):
        workspace._fleet._on_datagram(
            FleetDatagram(
                endpoint=endpoint,
                data=_identity("AFD01-DUPLICATE"),
                wall_time_ns=time.time_ns(),
                monotonic_ns=now_ns + index,
            )
        )
    qapp.processEvents()

    participants, reason = workspace._evaluate_start_gate()
    assert participants == ()
    assert reason.startswith(tr("Identity conflict"))
    assert "127.0.0.1:4004" in reason
    assert "127.0.0.2:4004" in reason
    assert not workspace._start_button.isEnabled()
    workspace.close()


def test_supported_afd01c_without_sn_is_not_reported_as_unsupported(
    qapp,
    settings,
) -> None:
    import time

    from satellite_debug_tool.core.production import DeviceSessionState, FleetDatagram
    from satellite_debug_tool.i18n import tr
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
    from satellite_debug_tool.tests.test_production_fleet import _identity

    workspace = ProductionWorkspace(settings)
    workspace.activate_view()
    workspace._fleet._on_datagram(
        FleetDatagram(
            endpoint=("127.0.0.1", 4004),
            data=_identity("", model="AFD01C", service_protocol=8),
            wall_time_ns=time.time_ns(),
            monotonic_ns=time.monotonic_ns(),
        )
    )
    qapp.processEvents()

    session = workspace._fleet.sessions()[0]
    assert session.state == DeviceSessionState.IDENTITY_PENDING
    assert workspace._snr_panels[1].serial_number == "AFD01C"
    assert workspace._snr_panels[1]._test_label.full_text == tr(
        "Test: {test} | Result: {result}",
        test="-",
        result=tr("Serial number pending"),
    )
    assert tr("Batch not created") in (
        workspace._snr_panels[1]._connection_label.full_text
    )
    workspace.close()


def test_batch_creation_reports_evidence_recorder_start_failure(
    qapp,
    settings,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.i18n import tr
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json"))
    workspace._batch_id_edit.setText("PILOT-RECORDING-FAIL")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))
    monkeypatch.setattr(
        workspace._fleet,
        "arm_batch_recording",
        lambda _batch_id, _output: False,
    )

    workspace._on_create_batch()

    assert workspace._footer_status.text() == tr(
        "Cannot create batch: {details}",
        details="evidence recording could not be created for one or more devices",
    )
    assert workspace.batch is None
    database = tmp_path / "output" / "PILOT-RECORDING-FAIL" / "batch.sqlite3"
    assert not database.exists()
    workspace.close()


def test_late_device_recording_failure_retries_before_batch_start(
    qapp,
    settings,
    tmp_path: Path,
    monkeypatch,
) -> None:
    import time

    from satellite_debug_tool.core.production import FleetDatagram
    from satellite_debug_tool.io.data_recorder import DataRecorder
    from satellite_debug_tool.tests.test_production_fleet import _identity
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json"))
    workspace._batch_id_edit.setText("PILOT-LATE-RECORDING-RETRY")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))
    workspace.create_batch()

    original_start = DataRecorder.start
    attempts = 0

    def flaky_start(recorder: DataRecorder) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        return original_start(recorder)

    monkeypatch.setattr(DataRecorder, "start", flaky_start)
    endpoint = ("127.0.0.1", 4004)
    first = FleetDatagram(
        endpoint=endpoint,
        data=_identity("AFD01-LATE-RETRY"),
        wall_time_ns=time.time_ns(),
        monotonic_ns=time.monotonic_ns(),
    )
    workspace._fleet._on_datagram(first)
    qapp.processEvents()

    session = workspace._fleet.sessions()[0]
    assert session.is_online(now_monotonic_ns=first.monotonic_ns)
    assert not session.recording_armed
    assert not workspace._start_button.isEnabled()

    second = FleetDatagram(
        endpoint=endpoint,
        data=_identity("AFD01-LATE-RETRY"),
        wall_time_ns=time.time_ns(),
        monotonic_ns=time.monotonic_ns(),
    )
    workspace._fleet._on_datagram(second)
    qapp.processEvents()
    _mark_analysis_capture_ready(workspace)

    assert attempts == 2
    assert session.recording_armed
    assert workspace._start_button.isEnabled()
    workspace.close()


def test_batch_creation_reports_existing_evidence_owner_without_partial_batch(
    qapp,
    settings,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.i18n import tr
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json"))
    workspace._batch_id_edit.setText("PILOT-RECORDING-CONFLICT")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))
    monkeypatch.setattr(
        workspace._fleet,
        "arm_batch_recording",
        lambda _batch_id, _output: (_ for _ in ()).throw(
            RuntimeError("fleet evidence recording already belongs to another batch")
        ),
    )

    workspace._on_create_batch()

    assert workspace.batch is None
    assert workspace._footer_status.text() == tr(
        "Cannot create batch: {details}",
        details="fleet evidence recording already belongs to another batch",
    )
    workspace.close()


def test_settings_save_failure_prevents_partial_batch(
    qapp,
    settings,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json"))
    workspace._batch_id_edit.setText("PILOT-SETTINGS-FAIL")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))

    def fail_save() -> None:
        raise OSError("settings unavailable")

    monkeypatch.setattr(workspace._settings, "save", fail_save)

    with pytest.raises(OSError, match="settings unavailable"):
        workspace.create_batch()

    assert workspace.batch is None
    assert not (tmp_path / "output" / "PILOT-SETTINGS-FAIL").exists()
    workspace.close()


def test_one_online_device_can_start_and_late_device_does_not_join(
    qapp,
    settings,
    tmp_path: Path,
) -> None:
    import time

    from satellite_debug_tool.core.production import BatchStatus, FleetDatagram
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
    from satellite_debug_tool.tests.test_production_fleet import _identity

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json"))
    workspace._batch_id_edit.setText("PILOT-ONE-DUT")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))
    workspace.create_batch()
    assert not workspace._start_button.isEnabled()

    now_ns = time.monotonic_ns()
    workspace._fleet._on_datagram(
        FleetDatagram(
            endpoint=("127.0.0.1", 4004),
            data=_identity("AFD01-ONLY"),
            wall_time_ns=time.time_ns(),
            monotonic_ns=now_ns,
        )
    )
    qapp.processEvents()
    _mark_analysis_capture_ready(workspace)

    assert workspace._start_button.isEnabled()
    batch = workspace.start_batch()
    assert batch["status"] == BatchStatus.RUNNING.value
    assert workspace._participant_serials == ("AFD01-ONLY",)
    assert workspace._abort_button.isEnabled()
    attempts = workspace.result_store.list_attempts("PILOT-ONE-DUT")
    from satellite_debug_tool.core.production.presentation import enabled_production_tests

    expected_attempts = len(enabled_production_tests(workspace.recipe))
    assert len(attempts) == expected_attempts
    assert {attempt["serial_number"] for attempt in attempts} == {"AFD01-ONLY"}

    workspace._fleet._on_datagram(
        FleetDatagram(
            endpoint=("127.0.0.2", 4004),
            data=_identity("AFD01-LATE"),
            wall_time_ns=time.time_ns(),
            monotonic_ns=time.monotonic_ns(),
        )
    )
    qapp.processEvents()
    assert [
        device["serial_number"]
        for device in workspace.result_store.list_devices("PILOT-ONE-DUT")
    ] == ["AFD01-ONLY"]
    assert not workspace._fleet.sessions()[1].recording_armed
    assert "AFD01-LATE" in workspace._ignored_after_start

    workspace.abort_batch()
    deadline = time.monotonic() + 5
    while (workspace._abort_coordinator.active or
           (workspace._abort_archive is not None and workspace._abort_archive.isRunning())) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.002)
    assert not workspace._abort_coordinator.active
    assert workspace._abort_archive is None or not workspace._abort_archive.isRunning()
    assert workspace.shutdown()
    workspace.close()


def test_unconfigured_external_ins_is_skipped_without_blocking_single_device_start(
    qapp,
    settings,
    tmp_path: Path,
) -> None:
    import struct
    import time

    from satellite_debug_tool.core.production import AttemptStatus, FleetDatagram
    from satellite_debug_tool.core.protocol import CmdType, build_frame
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
    from satellite_debug_tool.tests.test_production_fleet import _identity

    payload = valid_recipe()
    payload["tests"]["external_ins"] = {"enabled": True}
    recipe_path = tmp_path / "external-ins-na.json"
    recipe_path.write_text(json.dumps(payload), encoding="utf-8")

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(recipe_path)
    workspace._batch_id_edit.setText("PILOT-EXT-NA")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))
    workspace.create_batch()
    endpoint = ("127.0.0.1", 4004)
    now_ns = time.monotonic_ns()
    for frame in (
        _identity("AFD01-NO-EXT"),
        build_frame(
            CmdType.SERVICE_NAV_SOURCE_INFO,
            struct.pack("<BII7B", 1, 100, 0x7F, 2, 1, 0, 0, 0, 0x01, 0),
        ),
    ):
        workspace._fleet._on_datagram(
            FleetDatagram(
                endpoint=endpoint,
                data=frame,
                wall_time_ns=time.time_ns(),
                monotonic_ns=now_ns,
            )
        )
    qapp.processEvents()
    _mark_analysis_capture_ready(workspace)

    assert workspace._start_button.isEnabled()
    workspace.start_batch()
    external_attempt = next(
        attempt
        for attempt in workspace.result_store.list_attempts("PILOT-EXT-NA")
        if attempt["test_id"] == "external_ins"
    )
    assert external_attempt["status"] == AttemptStatus.SKIPPED.value
    assert external_attempt["result_json"]["reason"] == (
        "external_ins_not_configured"
    )
    workspace.close()


def test_invalid_recipe_does_not_replace_valid_recipe(
    qapp,
    settings,
    tmp_path: Path,
) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    valid_path = _write_recipe(tmp_path / "valid.json")
    assert workspace.load_recipe_file(valid_path)
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("{}", encoding="utf-8")

    assert not workspace.load_recipe_file(invalid_path)
    assert workspace.recipe is None
    assert workspace.batch is None
    workspace.close()


def test_workflow_separates_scenarios_from_navigation_summaries(
    qapp,
    settings,
    tmp_path: Path,
) -> None:
    from satellite_debug_tool.core.production import AttemptStatus
    from satellite_debug_tool.i18n import tr
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
    from satellite_debug_tool.tests.test_production_run_coordinator import _running_store

    payload = valid_recipe()
    payload["tests"]["gnss"] = {"enabled": True}
    payload["tests"]["imu_static"] = {"enabled": True}
    payload["tests"]["external_ins"] = {"enabled": True}
    payload["tests"]["whole_navigation"] = {"enabled": True}
    recipe_path = tmp_path / "navigation-recipe.json"
    recipe_path.write_text(json.dumps(payload), encoding="utf-8")

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(recipe_path)

    assert workspace._progress_panel.node_ids == (
        "static_acquisition",
        "locked_rocking",
        "gnss",
        "imu_static",
        "external_ins",
        "whole_navigation",
    )
    external_node = workspace._progress_panel._nodes["external_ins"]
    whole_node = workspace._progress_panel._nodes["whole_navigation"]
    assert external_node.title_label.text() == tr(
        "External INS performance summary"
    )
    assert external_node.state_label.text() == tr("Pending")
    assert whole_node.state_label.text() == tr("Pending")

    # 结果以真实逐设备 attempt 为来源，重翻译只改变呈现。
    store = _running_store(tmp_path, workspace._progress_panel.node_ids)
    workspace._batch_coordinator._store = store
    workspace._batch_coordinator._batch = store.get_batch("BATCH-AUTO")
    for attempt in store.list_attempts("BATCH-AUTO"):
        if attempt["test_id"] != "external_ins":
            continue
        for status in (AttemptStatus.WAITING_PREREQUISITE, AttemptStatus.ARMED,
                       AttemptStatus.RUNNING, AttemptStatus.ANALYZING):
            store.transition_attempt(attempt["attempt_id"], status)
        store.transition_attempt(attempt["attempt_id"], AttemptStatus.PASS, result={"reason": "Bynav"})
    workspace._render_steps()
    workspace.retranslate_ui()
    assert external_node.state_label.text() == tr("Pass")
    assert external_node.presentation.status is AttemptStatus.PASS
    assert "Bynav" in external_node.toolTip()
    assert whole_node.presentation.status is AttemptStatus.PENDING
    workspace.close()


def test_recipe_disables_missing_metric_views(qapp, settings, tmp_path: Path) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json"))

    for test_id in ("gnss", "imu_static", "external_ins", "whole_navigation",
                    "firmware_verification", "parameter_verification"):
        assert test_id not in workspace._progress_panel.node_ids
    assert workspace._progress_panel.node_ids == (
        "static_acquisition", "locked_rocking",
    )
    workspace.close()


def test_slot_and_fixture_updates_keep_typed_runtime_evidence(qapp, settings, tmp_path: Path) -> None:
    from satellite_debug_tool.core.production import FixtureRuntimeState
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json"))
    workspace.update_device_slot(
        1,
        serial_number="AFD01-001",
        endpoint="192.168.1.12:4004",
        connection="online",
        recording="armed",
    )
    assert workspace._snr_panels[1].serial_number == "AFD01-001"
    assert "192.168.1.12:4004" in workspace._snr_panels[1]._connection_label.full_text
    workspace.update_fixture_state(
        "motion", state=FixtureRuntimeState.CONNECTING, evidence="COMMAND_SENT"
    )
    motion = next(fixture for fixture in workspace._fixture_strip.fixtures if fixture.fixture_id == "motion")
    assert motion.evidence == "COMMAND_SENT"
    assert motion.level == "pending"
    workspace.update_fixture_state("motion", state=FixtureRuntimeState.READY, evidence="POSE_VERIFIED")
    motion = next(fixture for fixture in workspace._fixture_strip.fixtures if fixture.fixture_id == "motion")
    assert motion.level == "good"
    assert motion.evidence == "POSE_VERIFIED"
    vehicle = next(fixture for fixture in workspace._fixture_strip.fixtures if fixture.fixture_id == "vehicle")
    assert vehicle.level == "irrelevant"

    with pytest.raises(ValueError, match="slot"):
        workspace.update_device_slot(5)
    workspace.close()


def test_four_snr_panels_share_ranges_and_render_data_gaps(qapp, settings, tmp_path: Path) -> None:
    from satellite_debug_tool.core.production import FleetDatagram
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
    from satellite_debug_tool.tests.test_production_fleet import _fast_snr, _identity

    workspace = ProductionWorkspace(settings)
    workspace.activate_view()
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json", target_device_count=4))
    origin_ns = 10_000_000_000
    workspace._snr_origin_monotonic_ns = origin_ns
    for slot, snr_db in ((1, 12.5), (2, 24.0)):
        endpoint = (f"127.0.0.{slot}", 4004)
        workspace._fleet._on_datagram(
            FleetDatagram(
                endpoint=endpoint,
                data=_identity(f"AFD01-SNR-{slot}"),
                wall_time_ns=1_000_000_000,
                monotonic_ns=origin_ns,
            )
        )
        for offset_ns, value in (
            (50_000_000, snr_db - 1.0),
            (100_000_000, snr_db - 0.5),
            (2_000_000_000, snr_db),
        ):
            workspace._fleet._on_datagram(
                FleetDatagram(
                    endpoint=endpoint,
                    data=_fast_snr(offset_ns // 1_000_000, value),
                    wall_time_ns=1_000_000_000 + offset_ns,
                    monotonic_ns=origin_ns + offset_ns,
                )
            )

    workspace._refresh_session_rows()
    workspace._refresh_snr_charts(
        now_monotonic_ns=origin_ns + 2_000_000_000
    )

    assert workspace._visible_chart_slots == (1, 2, None, None)
    assert workspace._devices_layout.count() == 4
    assert len(workspace._snr_panels) == 2
    first_x, first_y = workspace._snr_panels[1].curve_data
    assert first_x.tolist() == pytest.approx([0.05, 0.1, 1.05, 2.0])
    assert math.isnan(float(first_y[2]))
    assert workspace._snr_panels[1].current_snr_db == pytest.approx(12.5)
    assert workspace._snr_panels[2].current_snr_db == pytest.approx(24.0)
    ranges = [workspace._devices_layout.itemAt(index).widget()._plot.viewRange()
              for index in range(workspace._devices_layout.count())]
    assert all(item[0] == pytest.approx(ranges[0][0]) for item in ranges)
    assert all(item[1] == pytest.approx(ranges[0][1]) for item in ranges)
    assert ranges[0][1] == pytest.approx([0.0, 25.0])
    workspace.close()


def test_production_layout_places_progress_and_fixtures_above_single_curve(
    qapp,
    settings,
    tmp_path: Path,
) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json"))
    workspace.show()
    workspace.resize(1024, 600)
    qapp.processEvents()
    assert workspace._visible_chart_slots == (None,)
    assert workspace._devices_layout.count() == 1
    assert len(workspace._pending_snr_panels) == 1
    assert workspace._progress_panel.geometry().bottom() < workspace._fixture_strip.geometry().top()
    assert workspace._fixture_strip.geometry().bottom() < workspace._charts_scroll.geometry().top()
    assert workspace._event_log.isHidden()
    assert workspace._abort_button.isVisible()
    assert "locked_drive" not in workspace._progress_panel.node_ids
    assert "gnss" not in workspace._progress_panel.node_ids

    workspace.resize(1600, 900)
    qapp.processEvents()
    assert workspace._devices_layout.count() == 1
    assert workspace._progress_panel.geometry().bottom() < workspace._charts_scroll.geometry().top()
    workspace._events_button.click()
    assert workspace._event_log.isVisible()
    workspace.close()


def test_only_participant_in_historical_slot_four_owns_the_single_curve(qapp, settings, tmp_path: Path) -> None:
    import time

    from satellite_debug_tool.core.production import FleetDatagram
    from satellite_debug_tool.tests.test_production_fleet import _identity
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    workspace.activate_view()
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json"))
    workspace._batch_id_edit.setText("PILOT-SLOT-FOUR")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))
    workspace.create_batch()
    for slot in range(1, 5):
        is_participant = slot == 4
        workspace._fleet._on_datagram(FleetDatagram(
            endpoint=(f"127.0.0.{slot}", 4004),
            data=_identity(f"AFD01-FOUR-{slot}", model="AFD01" if is_participant else "AFD01C",
                           service_protocol=2 if is_participant else 8),
            wall_time_ns=time.time_ns(),
            monotonic_ns=time.monotonic_ns() - (0 if is_participant else 10_000_000_000),
        ))
    qapp.processEvents()
    _mark_analysis_capture_ready(workspace)
    assert all(not session.is_online() for session in workspace._fleet.sessions()[:3])
    assert workspace._visible_chart_slots == (4,)
    assert workspace._devices_layout.count() == 1
    assert workspace._devices_layout.itemAt(0).widget() is workspace._snr_panels[4]
    assert workspace._snr_panels[4].serial_number == "AFD01-FOUR-4"
    workspace.start_batch()
    assert workspace._participant_serials == ("AFD01-FOUR-4",)
    assert workspace._visible_chart_slots == (4,)
    assert workspace._devices_layout.count() == 1
    assert [device["serial_number"] for device in workspace.result_store.list_devices("PILOT-SLOT-FOUR")] == [
        "AFD01-FOUR-4"
    ]
    assert all(slot not in workspace._snr_panels or workspace._snr_panels[slot].isHidden()
               for slot in (1, 2, 3))
    workspace.close()


def test_changing_device_quantity_reflows_existing_endpoint_panels(qapp, settings, tmp_path: Path) -> None:
    import time

    from satellite_debug_tool.core.production import FleetDatagram
    from satellite_debug_tool.tests.test_production_fleet import _identity
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    workspace.activate_view()
    workspace.resize(1600, 900)
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json", target_device_count=4))
    workspace.show()
    for slot in range(1, 5):
        workspace._fleet._on_datagram(FleetDatagram(
            endpoint=(f"127.0.0.{slot}", 4004), data=_identity(f"AFD01-REFLOW-{slot}"),
            wall_time_ns=time.time_ns(), monotonic_ns=time.monotonic_ns(),
        ))
    qapp.processEvents()
    panels = dict(workspace._snr_panels)
    assert workspace._visible_chart_slots == (1, 2, 3, 4)
    for count in (1, 3, 2):
        assert workspace.load_recipe_file(_write_recipe(tmp_path / f"recipe-{count}.json", target_device_count=count))
        qapp.processEvents()
        assert workspace._visible_chart_slots == tuple(range(1, count + 1))
        assert workspace._devices_layout.count() == count
        for slot, panel in panels.items():
            assert workspace._snr_panels[slot] is panel
            assert panel.isHidden() == (slot > count)
        if count == 3:
            assert workspace._devices_layout.getItemPosition(2) == (1, 0, 1, 2)
    assert workspace._devices_layout.getItemPosition(1) == (0, 1, 1, 1)
    workspace.resize(900, 800)
    qapp.processEvents()
    assert workspace._devices_layout.getItemPosition(1) == (1, 0, 1, 1)
    assert workspace._snr_panels[2] is panels[2]
    assert workspace._snr_panels[2].serial_number == "AFD01-REFLOW-2"
    workspace.close()


def test_hidden_production_workspace_toggles_without_changing_engineering_tabs(
    qapp,
    settings,
) -> None:
    from satellite_debug_tool.ui.main_window import MainWindow

    window = MainWindow(settings=settings)
    engineering_views = tuple(window._tabs.widget(index) for index in range(4))
    assert window._workspace.currentWidget() is window._customer

    window.unlock_production_for_session()
    qapp.processEvents()
    assert window._workspace.currentWidget() is window._production_host
    assert window._production is not None
    assert not window._tab_pillbar.isVisible()

    window._production_shortcut.activated.emit()
    qapp.processEvents()
    assert window._workspace.currentWidget() is window._customer

    window._production_shortcut.activated.emit()
    qapp.processEvents()
    assert window._workspace.currentWidget() is window._production_host
    assert tuple(window._tabs.widget(index) for index in range(4)) == engineering_views

    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_complete_batch_archives_reports_in_background(
    qapp, settings, tmp_path: Path
) -> None:
    """完成收尾立即返回；报告归档在后台 worker 完成，期间入口被守卫。"""

    from PySide6.QtCore import QCoreApplication

    from satellite_debug_tool.core.production import (
        AttemptStatus,
        BatchStatus,
        ProductionRecipe,
        ProductionResultStore,
        ResultStoreError,
    )
    from satellite_debug_tool.i18n import tr
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    output = tmp_path / "raw" / "BATCH-BG"
    output.mkdir(parents=True)
    store = ProductionResultStore(output / "batch.sqlite3")
    store.create_batch(
        "BATCH-BG",
        ProductionRecipe.from_mapping(valid_recipe()),
        operator="测试员甲",
        output_dir=output,
    )
    serial = "AFD01C-20260919-001"
    store.register_device(
        "BATCH-BG",
        serial_number=serial,
        slot=1,
        endpoint_ip="192.168.1.12",
        endpoint_port=4004,
        hardware_type="AFD01C",
        device_uid="111111112222222233333333",
        mac_address="02:00:00:00:00:01",
        metadata={"main_firmware": "1.2.3"},
    )
    store.start_batch("BATCH-BG", (serial,), ("static_acquisition",))
    attempt = store.list_attempts("BATCH-BG")[0]
    for status in (
        AttemptStatus.WAITING_PREREQUISITE,
        AttemptStatus.ARMED,
        AttemptStatus.RUNNING,
        AttemptStatus.ANALYZING,
        AttemptStatus.PASS,
    ):
        store.transition_attempt(
            int(attempt["attempt_id"]),
            status,
            result={"reason": "sample result"} if status is AttemptStatus.PASS else None,
        )
    evidence_dir = output / "devices" / serial / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "capture.sdb").write_bytes(b"SDB3-example")

    workspace = ProductionWorkspace(settings)
    workspace._batch_coordinator._store = store
    workspace._batch_coordinator._output_dir = output
    workspace._batch_coordinator._batch = store.get_batch("BATCH-BG")
    try:
        workspace._begin_complete_batch(source="manual")

        assert workspace._archive_worker is not None
        assert workspace._archive_worker.isRunning()
        with pytest.raises(ResultStoreError):
            workspace.complete_batch()

        deadline = __import__("time").monotonic() + 10
        while (
            workspace._archive_worker is not None
            and __import__("time").monotonic() < deadline
        ):
            QCoreApplication.processEvents()
        assert workspace._archive_worker is None
        from PySide6.QtCore import QEvent
        for _ in range(3):
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QCoreApplication.processEvents()

        expected = tr(
            "Batch completed; generated {count} device archive(s).", count=1
        )
        assert workspace._footer_status.text() == expected
        summary = json.loads((output / "batch_summary.json").read_text(encoding="utf-8"))
        assert summary["batch_id"] == "BATCH-BG"
        assert summary["status"] == BatchStatus.COMPLETED.value
        assert summary["reports"][0]["serial_number"] == serial
        assert summary["reports"][0]["document_path"].endswith(".docx")
    finally:
        # 先摘除并关闭批次存储；此后排队的 fleet 状态事件走空存储守卫。
        workspace._batch_coordinator.close()
