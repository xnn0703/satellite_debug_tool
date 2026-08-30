"""M19 production workspace batch setup and hidden-entry tests."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from satellite_debug_tool.tests.test_production_recipe import valid_recipe


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
) -> Path:
    payload = valid_recipe(duration_s=duration_s)
    payload["product"] = product
    if product == "afd01c":
        payload["recipe_id"] = "AFD01C-PILOT-R1"
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_workspace_explicitly_marks_engineering_preview(qapp, settings) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)

    notice = workspace._preview_notice.text()
    assert "M19-A" in notice
    assert "engineering preview" in notice or "工程预览" in notice
    assert (
        "not approved for formal production release" in notice
        or "尚未批准用于正式试产放行" in notice
    )
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
    assert tr("Evidence recording active") in (
        workspace._snr_panels[1]._connection_label.full_text
    )
    workspace.close()


def test_afd01c_recipe_registers_matching_device(
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
    workspace._batch_id_edit.setText("PILOT-AFD01C")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))
    workspace.create_batch()
    workspace._fleet._on_datagram(
        FleetDatagram(
            endpoint=("127.0.0.1", 4004),
            data=_identity(
                "AFD01C-UI-001",
                model="AFD01C",
                service_protocol=8,
            ),
            wall_time_ns=time.time_ns(),
            monotonic_ns=time.monotonic_ns(),
        )
    )
    qapp.processEvents()

    devices = workspace.result_store.list_devices("PILOT-AFD01C")
    assert [(item["serial_number"], item["hardware_type"]) for item in devices] == [
        ("AFD01C-UI-001", "AFD01C")
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

    assert workspace._start_button.isEnabled()
    batch = workspace.start_batch()
    assert batch["status"] == BatchStatus.RUNNING.value
    assert workspace._participant_serials == ("AFD01-ONLY",)
    assert workspace._abort_button.isEnabled()
    attempts = workspace.result_store.list_attempts("PILOT-ONE-DUT")
    expected_attempts = sum(workspace._workflow_default_states.values())
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
    from satellite_debug_tool.i18n import tr
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    payload = valid_recipe()
    payload["tests"]["gnss"] = {"enabled": True}
    payload["tests"]["imu_static"] = {"enabled": True}
    payload["tests"]["external_ins"] = {"enabled": True}
    recipe_path = tmp_path / "navigation-recipe.json"
    recipe_path.write_text(json.dumps(payload), encoding="utf-8")

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(recipe_path)

    assert tuple(workspace._test_rows) == (
        "firmware_verification",
        "parameter_verification",
        "static_acquisition",
        "locked_rocking",
        "power_on_rocking",
        "locked_drive",
        "power_on_drive",
        "gnss",
        "imu_static",
        "external_ins",
        "whole_navigation",
    )
    external_row = workspace._test_rows["external_ins"]
    whole_row = workspace._test_rows["whole_navigation"]
    assert workspace._workflow_table.item(external_row, 1).text() == tr(
        "External INS performance summary"
    )
    assert workspace._workflow_table.item(external_row, 3).text() == tr("Pending")
    assert workspace._workflow_table.item(external_row, 4).text() == tr(
        "Shared windows"
    )
    assert workspace._workflow_table.item(whole_row, 3).text() == tr("Pending")

    workspace.update_test_status(
        "external_ins",
        status="pass",
        result="Bynav",
    )
    workspace.retranslate_ui()
    assert workspace._workflow_table.item(external_row, 3).text() == "pass"
    assert workspace._workflow_table.item(external_row, 5).text() == "Bynav"
    workspace.close()


def test_recipe_disables_missing_metric_views(qapp, settings, tmp_path: Path) -> None:
    from satellite_debug_tool.i18n import tr
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    assert workspace.load_recipe_file(_write_recipe(tmp_path / "recipe.json"))

    for test_id in ("gnss", "imu_static", "external_ins"):
        row = workspace._test_rows[test_id]
        assert workspace._workflow_table.item(row, 3).text() == tr(
            "Not applicable"
        )
        assert workspace._workflow_table.item(row, 5).text() == tr(
            "Disabled by recipe"
        )
    whole_row = workspace._test_rows["whole_navigation"]
    assert workspace._workflow_table.item(whole_row, 3).text() == tr("Pending")
    workspace.close()


def test_slot_test_and_fixture_updates_are_bounded(qapp, settings) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    workspace.update_device_slot(
        1,
        serial_number="AFD01-001",
        endpoint="192.168.1.12:4004",
        connection="online",
        recording="armed",
    )
    assert workspace._snr_panels[1].serial_number == "AFD01-001"
    assert "192.168.1.12:4004" in workspace._snr_panels[1]._connection_label.full_text
    workspace.update_test_status(
        "firmware_verification", status="pass", result="0.0.130 beta"
    )
    assert workspace._workflow_table.item(0, 3).text() == "pass"
    workspace.update_fixture_state(
        "motion", state="command sent", evidence="COMMAND_SENT"
    )
    assert workspace._fixture_table.item(1, 2).text() == "COMMAND_SENT"

    with pytest.raises(ValueError, match="slot"):
        workspace.update_device_slot(5)
    with pytest.raises(ValueError, match="unknown production test"):
        workspace.update_test_status("unknown", status="pending")
    workspace.close()


def test_four_snr_panels_share_ranges_and_render_data_gaps(qapp, settings) -> None:
    from satellite_debug_tool.core.production import FleetDatagram
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
    from satellite_debug_tool.tests.test_production_fleet import _fast_snr, _identity

    workspace = ProductionWorkspace(settings)
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

    assert len(workspace._snr_panels) == 4
    first_x, first_y = workspace._snr_panels[1].curve_data
    assert first_x.tolist() == pytest.approx([0.05, 0.1, 1.05, 2.0])
    assert math.isnan(float(first_y[2]))
    assert workspace._snr_panels[1].current_snr_db == pytest.approx(12.5)
    assert workspace._snr_panels[2].current_snr_db == pytest.approx(24.0)
    ranges = [panel._plot.viewRange() for panel in workspace._snr_panels.values()]
    assert all(item[0] == pytest.approx(ranges[0][0]) for item in ranges)
    assert all(item[1] == pytest.approx(ranges[0][1]) for item in ranges)
    assert ranges[0][1] == pytest.approx([0.0, 25.0])
    workspace.close()


def test_production_layout_uses_compact_workflow_columns_on_small_screens(
    qapp,
    settings,
) -> None:
    from PySide6.QtCore import Qt

    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(settings)
    workspace.show()
    workspace.resize(1024, 600)
    qapp.processEvents()
    assert workspace._main_splitter.orientation() == Qt.Orientation.Horizontal
    assert workspace._content_splitter.orientation() == Qt.Orientation.Vertical
    assert workspace._workflow_table.isColumnHidden(0)
    assert workspace._workflow_table.isColumnHidden(2)
    assert workspace._workflow_table.isColumnHidden(4)

    workspace.resize(1600, 900)
    qapp.processEvents()
    assert not workspace._workflow_table.isColumnHidden(0)
    assert not workspace._workflow_table.isColumnHidden(2)
    assert not workspace._workflow_table.isColumnHidden(4)
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
