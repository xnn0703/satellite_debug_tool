"""Per-device DOCX report, evidence archive, review, and manifest regression tests."""

from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import struct
import zipfile

from docx import Document
import pytest

from satellite_debug_tool.core.production import (
    AttemptStatus,
    BatchStatus,
    ProductionRecipe,
    ProductionReportService,
    ProductionResultStore,
    ReportBranding,
    ResultStoreError,
    sanitize_path_component,
)
from satellite_debug_tool.core.production.report_analysis import (
    build_report_analysis,
    project_attempt_outcomes,
    render_report_charts,
)
from satellite_debug_tool.core.protocol import CmdType, build_frame
from satellite_debug_tool.io.data_recorder import DataRecorder, SDB_VERSION_V3
from satellite_debug_tool.io.sdb_schema import SDB_FOOTER
from satellite_debug_tool.tests.test_production_recipe import valid_recipe


def _finish_attempt(
    store: ProductionResultStore,
    attempt_id: int,
    verdict: AttemptStatus,
    result: dict[str, object] | None = None,
) -> None:
    for status in (
        AttemptStatus.WAITING_PREREQUISITE,
        AttemptStatus.ARMED,
        AttemptStatus.RUNNING,
        AttemptStatus.ANALYZING,
        verdict,
    ):
        store.transition_attempt(
            attempt_id,
            status,
            result=(result if result is not None else {"reason": "sample result"}) if status == verdict else None,
        )


def _terminal_batch(
    tmp_path: Path,
    verdicts: tuple[AttemptStatus, ...],
    *,
    test_ids: tuple[str, ...] | None = None,
    result_payloads: tuple[dict[str, object], ...] | None = None,
    weather: str = "",
    ambient_temperature_c: float | None = None,
    ambient_humidity_percent: float | None = None,
) -> tuple[ProductionResultStore, Path, str]:
    output = tmp_path / "raw" / "BATCH-01"
    output.mkdir(parents=True)
    recipe = ProductionRecipe.from_mapping(valid_recipe())
    store = ProductionResultStore(output / "batch.sqlite3")
    store.create_batch(
        "BATCH-01",
        recipe,
        operator="测试员甲",
        output_dir=output,
        weather=weather,
        ambient_temperature_c=ambient_temperature_c,
        ambient_humidity_percent=ambient_humidity_percent,
    )
    serial = "AFD01C-20260919-001"
    store.register_device(
        "BATCH-01",
        serial_number=serial,
        slot=1,
        endpoint_ip="192.168.1.12",
        endpoint_port=4004,
        hardware_type="AFD01C",
        device_uid="111111112222222233333333",
        mac_address="02:00:00:00:00:01",
        metadata={"main_firmware": "1.2.3", "parameters": {"mode": 1}},
    )
    store.start_batch(
        "BATCH-01",
        (serial,),
        test_ids or tuple(f"test-{index}" for index in range(len(verdicts))),
    )
    attempts = store.list_attempts("BATCH-01")
    for index, (attempt, verdict) in enumerate(zip(attempts, verdicts)):
        _finish_attempt(store, int(attempt["attempt_id"]), verdict,
                        result_payloads[index] if result_payloads else None)
        store.record_metric(
            int(attempt["attempt_id"]),
            "snr_median",
            12.5,
            unit="dB",
            verdict=verdict.value,
            sample_count=100,
        )
    target_status = (
        BatchStatus.INCOMPLETE
        if any(value in {AttemptStatus.INCOMPLETE, AttemptStatus.ABORTED} for value in verdicts)
        else BatchStatus.COMPLETED
    )
    store.transition_batch("BATCH-01", target_status)
    evidence_dir = output / "devices" / serial / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "capture.sdb").write_bytes(b"SDB3-example")
    (output / "power_supply_records.jsonl").write_text(
        '{"command":"MEAS:VOLT?","response":"12.01"}\n', encoding="utf-8"
    )
    (output / "power_supply_summary.json").write_text(
        json.dumps({"identity": {"model": "PSW80-27"}}), encoding="utf-8"
    )
    (output / "production_configuration.json").write_text(
        json.dumps(
            {
                "product_template": {
                    "display_name": "AFD01C 卫星通信相控阵终端",
                    "product": "afd01c",
                },
                "power_profile": {"manufacturer": "GW-INSTEK", "model": "PSW80-27"},
                "station_profile": {
                    "display_name": "一号工位",
                    "motion_profile_id": "motion-main",
                    "reference_profile_id": "ms6222-main",
                },
                "combined_supply": {"voltage_v": 12.0, "current_a": 8.0},
                "test_environment": {
                    "location": "生产测试实验室",
                    "temperature_c": 23.5,
                    "relative_humidity_percent": 48,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return store, output, serial


def _document_text(path: Path) -> str:
    document = Document(path)
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def _service_fast(timestamp_ms: int, snr_db: float, roll_deg: float) -> bytes:
    payload = struct.pack(
        "<BII6B6f",
        1,
        timestamp_ms,
        0xFFF,
        0,
        3,
        1,
        3,
        3,
        0,
        roll_deg,
        2.0,
        3.0,
        120.0,
        35.0,
        snr_db,
    )
    return build_frame(CmdType.SERVICE_FAST_STATE, payload)


def _write_valid_device_sdb(output: Path, serial: str) -> Path:
    path = output / "devices" / serial / "evidence" / "capture.sdb"
    path.unlink()
    recorder = DataRecorder(path, format_version=SDB_VERSION_V3)
    assert recorder.start()
    assert recorder.write_frame(_service_fast(1000, 12.0, 0.0))
    assert recorder.stop()
    return path


def test_curve_analysis_uses_sdb_host_time_and_generates_stage_charts(tmp_path: Path) -> None:
    path = tmp_path / "capture.sdb"
    recorder = DataRecorder(path, format_version=SDB_VERSION_V3)
    assert recorder.start()
    wall_origin = 1_700_000_000_000_000_000
    for index, snr in enumerate((8.0, 10.0, 12.0, 14.0, 16.0)):
        assert recorder.write_frame(
            _service_fast(index * 1000, snr, float(index)),
            host_timestamp_ns=wall_origin + index * 1_000_000_000,
        )
    assert recorder.stop()

    monotonic_origin = 5_000_000_000
    events = [
        {
            "monotonic_ns": monotonic_origin,
            "wall_time_utc": "2023-11-14T22:13:20+00:00",
        }
    ]
    attempts = [
        {
            "attempt_id": 1,
            "test_id": "locked_rocking",
            "ordinal": 1,
            "status": "pass",
            "phase": "observing",
            "started_monotonic_ns": monotonic_origin,
            "ended_monotonic_ns": monotonic_origin + 4_000_000_000,
        }
    ]
    analysis = build_report_analysis((path,), attempts, events, tmp_path)
    stats = analysis["stages"][0]["statistics"]["snr"]

    assert analysis["source_kind"] == "product_service"
    assert stats["sample_count"] == 5
    assert stats["trend"] == "increasing"
    assert stats["start_median"] == pytest.approx(8.0)
    assert stats["end_median"] == pytest.approx(16.0)
    assert analysis["ms6222_comparison_available"] is False
    charts = render_report_charts(analysis)
    assert charts["analysis/snr_timeline.png"].startswith(b"\x89PNG")
    assert charts["analysis/attitude_timeline.png"].startswith(b"\x89PNG")


def test_report_groups_five_test_ids_into_four_operating_scenarios(tmp_path: Path) -> None:
    scenario_ids = (
        "static_acquisition",
        "locked_rocking",
        "power_on_rocking",
        "locked_drive",
        "power_on_drive",
        "gnss",
    )
    attempts = [
        {
            "attempt_id": index,
            "test_id": test_id,
            "ordinal": 1,
            "status": "pass",
            "phase": "observing",
            "started_monotonic_ns": index * 1_000_000_000,
            "ended_monotonic_ns": (index + 1) * 1_000_000_000,
        }
        for index, test_id in enumerate(scenario_ids, start=1)
    ]
    events = [{"monotonic_ns": 0, "wall_time_utc": "2023-11-14T22:13:20+00:00"}]
    analysis = build_report_analysis((), attempts, events, tmp_path)

    assert [item["scenario_id"] for item in analysis["scenario_groups"]] == [
        "static",
        "locked_rocking",
        "power_on_rocking",
        "vehicle_dynamic",
    ]
    vehicle = analysis["scenario_groups"][-1]
    assert [item["test_id"] for item in vehicle["stages"]] == [
        "locked_drive",
        "power_on_drive",
    ]
    assert [item["test_id"] for item in analysis["cross_scenario_stages"]] == ["gnss"]


def test_valid_process_recording_is_embedded_in_docx_and_evidence(tmp_path: Path) -> None:
    store, output, serial = _terminal_batch(
        tmp_path,
        (AttemptStatus.PASS,),
        test_ids=("static_acquisition",),
    )
    try:
        path = output / "devices" / serial / "evidence" / "capture.sdb"
        path.unlink()
        stored_attempt = store.list_attempts("BATCH-01")[0]
        stored_events = store.list_events("BATCH-01")
        offsets = [
            int(datetime.fromisoformat(event["wall_time_utc"]).timestamp() * 1_000_000_000)
            - int(event["monotonic_ns"])
            for event in stored_events
        ]
        wall_offset = sorted(offsets)[len(offsets) // 2]
        attempt_start = int(stored_attempt["started_monotonic_ns"])
        attempt_end = int(stored_attempt["ended_monotonic_ns"])
        recorder = DataRecorder(path, format_version=SDB_VERSION_V3)
        assert recorder.start()
        for index in range(6):
            fraction = index / 5
            sample_monotonic = int(
                attempt_start + (attempt_end - attempt_start) * fraction
            )
            assert recorder.write_frame(
                _service_fast(index * 1000, 10.0 + index, float(index)),
                host_timestamp_ns=sample_monotonic + wall_offset,
            )
        assert recorder.stop()
        fixture_dir = output / "fixture_capture"
        fixture_dir.mkdir()
        comparison_fields = (
            "monotonic_ns",
            "target_roll_deg",
            "target_pitch_deg",
            "target_yaw_deg",
            "measured_roll_deg",
            "measured_pitch_deg",
            "measured_yaw_deg",
            "error_roll_deg",
            "error_pitch_deg",
            "error_yaw_deg",
            "error_angle_deg",
        )
        event_origin = attempt_start
        with (fixture_dir / "attitude_comparison.csv").open(
            "w", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=comparison_fields)
            writer.writeheader()
            for index, error in enumerate((4.0, 3.0, 2.0, 1.0)):
                writer.writerow(
                    {
                        "monotonic_ns": event_origin + index * 1_000_000_000,
                        "target_roll_deg": 0,
                        "target_pitch_deg": 0,
                        "target_yaw_deg": 0,
                        "measured_roll_deg": error,
                        "measured_pitch_deg": 0,
                        "measured_yaw_deg": 0,
                        "error_roll_deg": error,
                        "error_pitch_deg": 0,
                        "error_yaw_deg": 0,
                        "error_angle_deg": error,
                    }
                )
        service = ProductionReportService(
            store,
            batch_output_dir=output,
            report_root=tmp_path / "reports",
        )
        artifact = service.archive_device("BATCH-01", serial)

        assert artifact.document_path is not None
        document = Document(artifact.document_path)
        assert len(document.inline_shapes) >= 3
        assert "二、测试结果" in _document_text(artifact.document_path)
        assert "工况：静置对星" in _document_text(artifact.document_path)
        assert "车载动态工况" not in _document_text(artifact.document_path)
        with zipfile.ZipFile(artifact.evidence_path) as archive:
            names = set(archive.namelist())
            assert "analysis/analysis.json" in names
            assert "analysis/snr_timeline.png" in names
            assert "analysis/attitude_timeline.png" in names
            assert "analysis/convergence_timeline.png" in names
            assert "analysis/scenarios/static/snr.png" in names
            assert "analysis/scenarios/static/attitude.png" in names
    finally:
        store.close()


def test_pass_generates_v1_self_contained_zip_and_valid_manifest(tmp_path: Path) -> None:
    store, output, serial = _terminal_batch(tmp_path, (AttemptStatus.PASS,))
    try:
        service = ProductionReportService(
            store,
            batch_output_dir=output,
            report_root=tmp_path / "reports",
            branding=ReportBranding(),
        )
        artifact = service.archive_device("BATCH-01", serial)

        assert artifact.status == "generated_v1"
        assert artifact.document_path is not None and artifact.document_path.is_file()
        assert artifact.directory.parts[-3:] == ("AFD01C", serial, "BATCH-01")
        with zipfile.ZipFile(artifact.evidence_path) as archive:
            names = set(archive.namelist())
            assert {
                "device/evidence.sdb",
                "device/identity.json",
                "device/metrics.json",
                "fixture/power_scpi.jsonl",
                "fixture/ms6222_raw.jsonl",
                "test/recipe.json",
                "test/software.json",
            } <= names
            assert archive.read("device/evidence.sdb") == b"SDB3-example"

        manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
        assert manifest["automatic_verdict"] == "PASS"
        assert manifest["evidence"]["sha256"] == hashlib.sha256(
            artifact.evidence_path.read_bytes()
        ).hexdigest()
        assert manifest["reports"][0]["sha256"] == hashlib.sha256(
            artifact.document_path.read_bytes()
        ).hexdigest()
        text = _document_text(artifact.document_path)
        assert "测试报告" in text
        assert "卫星通信相控阵设备自动化测试" in text
        assert "产品名称" in text
        assert "AFD01C 卫星通信相控阵终端" in text
        assert "测试结论" in text
        assert "自动生成" in text
        assert "附录 B  配置明细" in text
        assert serial in text
    finally:
        store.close()


def test_invalid_sdb_is_reported_as_incomplete_quality_and_all_fixture_files_are_archived(
    tmp_path: Path,
) -> None:
    store, output, serial = _terminal_batch(tmp_path, (AttemptStatus.PASS,))
    fixture_a = output / "fixtures" / "session-a"
    fixture_b = output / "fixtures" / "session-b"
    fixture_a.mkdir(parents=True)
    fixture_b.mkdir(parents=True)
    (fixture_a / "platform_feedback.jsonl").write_text('{"a":1}\n', encoding="utf-8")
    (fixture_b / "platform_feedback.jsonl").write_text('{"b":2}\n', encoding="utf-8")
    (fixture_a / "manifest.json").write_text('{"session":"a"}\n', encoding="utf-8")
    (fixture_b / "manifest.json").write_text('{"session":"b"}\n', encoding="utf-8")
    try:
        service = ProductionReportService(
            store,
            batch_output_dir=output,
            report_root=tmp_path / "reports",
        )
        artifact = service.archive_device("BATCH-01", serial)
        frozen = store.get_report_snapshot("BATCH-01", serial)["snapshot_json"]

        assert frozen["automatic_verdict"] == "PASS"
        assert frozen["report_verdict"] == "INCOMPLETE"
        assert frozen["data_quality"]["state"] == "INCOMPLETE"
        assert frozen["data_quality"]["analyzable_sample_count"] == 0
        with zipfile.ZipFile(artifact.evidence_path) as archive:
            assert {
                "fixture_sessions/session-a/platform_feedback.jsonl",
                "fixture_sessions/session-b/platform_feedback.jsonl",
                "fixture_sessions/session-a/manifest.json",
                "fixture_sessions/session-b/manifest.json",
            } <= set(archive.namelist())
    finally:
        store.close()


def test_fail_v1_can_create_one_audited_v2_without_overwriting_v1(tmp_path: Path) -> None:
    store, output, serial = _terminal_batch(tmp_path, (AttemptStatus.FAIL,))
    try:
        _write_valid_device_sdb(output, serial)
        service = ProductionReportService(
            store,
            batch_output_dir=output,
            report_root=tmp_path / "reports",
        )
        first = service.archive_device("BATCH-01", serial)
        first_bytes = first.document_path.read_bytes() if first.document_path else b""
        second = service.review(
            "BATCH-01",
            serial,
            reviewer="审核员乙",
            final_verdict="PASS",
            reason="完成线下复测并确认原始阈值配置错误。",
        )

        assert second.document_path is not None and second.document_path.is_file()
        assert first.document_path is not None and first.document_path.read_bytes() == first_bytes
        text = _document_text(second.document_path)
        assert "审核员乙" in text
        assert "完成线下复测" in text
        assert "FAIL" in text and "PASS" in text
        with pytest.raises(ResultStoreError, match="already exists"):
            service.review(
                "BATCH-01",
                serial,
                reviewer="另一审核员",
                final_verdict="FAIL",
                reason="重复审核",
            )
    finally:
        store.close()


def test_incomplete_generates_readable_v1_and_cannot_be_approved(tmp_path: Path) -> None:
    store, output, serial = _terminal_batch(tmp_path, (AttemptStatus.INCOMPLETE,))
    try:
        service = ProductionReportService(
            store,
            batch_output_dir=output,
            report_root=tmp_path / "reports",
        )
        artifact = service.archive_device("BATCH-01", serial)
        assert artifact.status == "generated_v1"
        assert artifact.document_path is not None and artifact.document_path.is_file()
        assert artifact.evidence_path.is_file()
        assert artifact.manifest_path.is_file()
        assert "INCOMPLETE" in _document_text(artifact.document_path)
        with pytest.raises(ResultStoreError, match="cannot be approved"):
            store.create_report_review(
                "BATCH-01",
                serial,
                reviewer="审核员",
                final_verdict="PASS",
                reason="不得通过",
            )
    finally:
        store.close()


@pytest.mark.parametrize("verdict", (AttemptStatus.PASS, AttemptStatus.FAIL))
def test_incomplete_recording_quality_blocks_review_pass_but_keeps_fail_review(
    tmp_path: Path, verdict: AttemptStatus,
) -> None:
    store, output, serial = _terminal_batch(tmp_path, (verdict,))
    try:
        service = ProductionReportService(store, batch_output_dir=output, report_root=tmp_path / "reports")
        service.archive_device("BATCH-01", serial)
        with pytest.raises(ResultStoreError, match="complete data quality"):
            service.review("BATCH-01", serial, reviewer="审核员", final_verdict="PASS", reason="缺少采集证据")
        review = service.review("BATCH-01", serial, reviewer="审核员", final_verdict="FAIL", reason="保留失败并记录证据不足")
        assert review.final_verdict == "FAIL"
        assert store.get_report_review("BATCH-01", serial)["reason"] == "保留失败并记录证据不足"
    finally:
        store.close()


@pytest.mark.parametrize("source", ("missing_sdb", "no_samples", "missing_footer", "tracking_gap"))
def test_specific_quality_gaps_cannot_be_approved_pass(tmp_path: Path, source: str) -> None:
    payload = {"data_quality": "INCOMPLETE", "tracking_criteria": {"quality_reasons": ["tracking_sample_gap"]}}
    store, output, serial = _terminal_batch(
        tmp_path, (AttemptStatus.PASS,), result_payloads=(payload,) if source == "tracking_gap" else None,
    )
    try:
        path = _write_valid_device_sdb(output, serial)
        if source == "missing_sdb":
            path.unlink()
        elif source == "no_samples":
            path.unlink()
            recorder = DataRecorder(path, format_version=SDB_VERSION_V3)
            assert recorder.start() and recorder.stop()
        elif source == "missing_footer":
            path.write_bytes(path.read_bytes()[:-len(SDB_FOOTER)])
        service = ProductionReportService(store, batch_output_dir=output, report_root=tmp_path / "reports")
        service.archive_device("BATCH-01", serial)
        snapshot = store.get_report_snapshot("BATCH-01", serial)["snapshot_json"]
        assert snapshot["data_quality"]["state"] == "INCOMPLETE"
        with pytest.raises(ResultStoreError, match="complete data quality"):
            service.review("BATCH-01", serial, reviewer="审核员", final_verdict="PASS", reason="证据不足")
    finally:
        store.close()


def test_tracking_report_preserves_failure_quality_and_readable_rule_statistics(tmp_path: Path) -> None:
    criteria = {
        "rule_version": 1,
        "policy": {
            "acquisition_stable_s": 5.0, "acquisition_timeout_s": 300.0,
            "interruption_max_s": 2.0, "interruptions_per_30min": 5,
            "interruption_ratio_max": 0.005, "recovery_stable_s": 3.0, "maximum_sample_gap_s": 2.0,
        },
        "duration_s": 1800.0, "acquisition_required": True,
        "acquisition_elapsed_s": 45.5, "observation_duration_s": 1754.5,
        "interruption_count": 1, "interruption_limit": 5,
        "interruption_max_s": 2.1, "interruption_total_s": 2.1,
        "interruption_ratio": 2.1 / 1754.5, "sample_count": 100, "valid_sample_count": 99,
        "maximum_observed_gap_s": 3.0, "recovery_pending": True,
        "failure_reasons": ["interruption_duration_exceeded"], "quality_reasons": ["tracking_sample_gap"],
        "interruptions": [],
    }
    result = {"tracking_criteria": criteria, "performance_verdict": "FAIL", "data_quality": "INCOMPLETE"}
    store, output, serial = _terminal_batch(
        tmp_path, (AttemptStatus.FAIL, AttemptStatus.INCOMPLETE),
        test_ids=("static_acquisition", "whole_navigation"), result_payloads=(result, {"reason": "criteria unavailable"}),
    )
    try:
        _write_valid_device_sdb(output, serial)
        service = ProductionReportService(store, batch_output_dir=output, report_root=tmp_path / "reports")
        artifact = service.archive_device("BATCH-01", serial)
        snapshot = store.get_report_snapshot("BATCH-01", serial)["snapshot_json"]
        assert snapshot["automatic_verdict"] == snapshot["report_verdict"] == "INCOMPLETE"
        assert snapshot["performance_verdict"] == "FAIL"
        assert snapshot["execution_completeness"] == "INCOMPLETE"
        assert snapshot["data_quality"]["state"] == "INCOMPLETE"
        stage = snapshot["analysis"]["stages"][0]
        assert stage["result_json"]["tracking_criteria"] == criteria
        assert stage["performance_verdict"] == "FAIL"
        manifest = json.loads(artifact.manifest_path.read_text())
        assert manifest["performance_verdict"] == "FAIL"
        assert manifest["execution_completeness"] == manifest["data_quality"] == "INCOMPLETE"
        text = _document_text(artifact.document_path)
        for expected in ("跟踪判据与事件统计", "45.500 s", "2.100 s", "0.500%", "恢复确认中",
                         "单次中断时长超过允许值", "采样间隔超过允许值", "性能结论", "执行完整性"):
            assert expected in text
        assert "interruption_duration_exceeded" not in text
        with zipfile.ZipFile(artifact.evidence_path) as archive:
            frozen_attempts = json.loads(archive.read("test/attempts.json"))
            assert frozen_attempts[0]["result_json"] == result
        legacy = dict(snapshot)
        for key in ("performance_verdict", "execution_completeness", "data_quality"):
            legacy.pop(key)
        legacy["attempts"] = [{**item, "result_json": {"reason": "legacy reason"}} for item in snapshot["attempts"]]
        legacy_path = tmp_path / "legacy.docx"
        service._write_docx(legacy, legacy_path, version=1, review=None)
        assert "该版本未记录" in _document_text(legacy_path)
        assert "legacy reason" in _document_text(legacy_path)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("attempts", "performance", "execution"),
    (
        ([{"status": "incomplete", "result_json": {"performance_verdict": "FAIL"}}], "FAIL", "INCOMPLETE"),
        ([{"status": "fail"}, {"status": "aborted"}], "FAIL", "INCOMPLETE"),
        ([{"status": "pass"}, {"status": "skipped"}], "PASS", "COMPLETE"),
        ([{"status": "fail"}], "FAIL", "COMPLETE"),
        ([], "INCOMPLETE", "INCOMPLETE"),
    ),
)
def test_outcomes_keep_performance_separate_from_execution(attempts, performance, execution) -> None:
    assert project_attempt_outcomes(attempts) == {
        "performance_verdict": performance, "execution_completeness": execution,
    }


def test_path_components_are_safe_and_bounded() -> None:
    assert sanitize_path_component(' AFD01C:/<>"\\|?* ') == "AFD01C"
    assert sanitize_path_component("..") == "unknown"
    assert len(sanitize_path_component("A" * 500)) == 128


def test_two_devices_receive_separate_self_contained_archives(tmp_path: Path) -> None:
    output = tmp_path / "raw" / "BATCH-02"
    output.mkdir(parents=True)
    recipe = ProductionRecipe.from_mapping(valid_recipe())
    store = ProductionResultStore(output / "batch.sqlite3")
    serials = ("AFD01C-A", "AFD01C-B")
    try:
        store.create_batch(
            "BATCH-02", recipe, operator="operator", output_dir=output
        )
        for slot, serial in enumerate(serials, start=1):
            store.register_device(
                "BATCH-02",
                serial_number=serial,
                slot=slot,
                hardware_type="AFD01C",
            )
        store.start_batch("BATCH-02", serials, ("identity",))
        for attempt in store.list_attempts("BATCH-02"):
            _finish_attempt(store, int(attempt["attempt_id"]), AttemptStatus.PASS)
        store.transition_batch("BATCH-02", BatchStatus.COMPLETED)
        for serial in serials:
            evidence = output / "devices" / serial / "evidence"
            evidence.mkdir(parents=True)
            (evidence / "capture.sdb").write_bytes(serial.encode("ascii"))

        service = ProductionReportService(
            store,
            batch_output_dir=output,
            report_root=tmp_path / "reports",
        )
        artifacts = service.archive_batch("BATCH-02")
        assert [item.serial_number for item in artifacts] == list(serials)
        assert artifacts[0].directory != artifacts[1].directory
        for artifact in artifacts:
            with zipfile.ZipFile(artifact.evidence_path) as archive:
                assert archive.read("device/evidence.sdb") == artifact.serial_number.encode(
                    "ascii"
                )
            assert artifact.document_path is not None
            report_text = _document_text(artifact.document_path)
            assert "自动生成" in report_text
    finally:
        store.close()


def test_docx_failure_records_generation_failure_without_changing_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, output, serial = _terminal_batch(tmp_path, (AttemptStatus.PASS,))
    try:
        service = ProductionReportService(
            store,
            batch_output_dir=output,
            report_root=tmp_path / "reports",
        )

        def fail_document(*_args, **_kwargs):
            raise OSError("simulated docx failure")

        monkeypatch.setattr(service, "_write_docx", fail_document)
        with pytest.raises(OSError, match="simulated"):
            service.archive_device("BATCH-01", serial)

        assert store.get_batch("BATCH-01")["status"] == BatchStatus.COMPLETED.value
        snapshot = store.get_report_snapshot("BATCH-01", serial)
        assert snapshot["automatic_verdict"] == "PASS"
        artifact = store.get_report_artifact("BATCH-01", serial, 1)
        assert artifact["status"] == "generation_failed"
        assert "simulated docx failure" in artifact["error"]
    finally:
        store.close()


def test_document_name_uses_model_serial_and_finished_time(tmp_path: Path) -> None:
    """V1 交付文档按 型号_SN_测试报告_批次结束时间 命名；V2 在其后追加 _V02。"""

    store, output, serial = _terminal_batch(tmp_path, (AttemptStatus.PASS,))
    try:
        service = ProductionReportService(
            store,
            batch_output_dir=output,
            report_root=tmp_path / "reports",
        )
        artifact = service.archive_device("BATCH-01", serial)

        assert artifact.document_path is not None
        batch = store.get_batch("BATCH-01")
        finished = datetime.fromisoformat(batch["finished_utc"]).astimezone()
        expected_stamp = finished.strftime("%Y%m%d%H%M")
        assert artifact.document_path.name == (
            f"AFD01C_{serial}_测试报告_{expected_stamp}.docx"
        )
    finally:
        store.close()

    fail_store, fail_output, fail_serial = _terminal_batch(
        tmp_path / "fail-case", (AttemptStatus.FAIL,), test_ids=("locked_rocking",)
    )
    try:
        service = ProductionReportService(
            fail_store,
            batch_output_dir=fail_output,
            report_root=tmp_path / "reports-fail",
        )
        service.archive_device("BATCH-01", fail_serial)
        service.review(
            "BATCH-01",
            fail_serial,
            reviewer="审核员乙",
            final_verdict="FAIL",
            reason="复核确认失败",
        )
        records = fail_store.list_report_artifacts("BATCH-01", fail_serial)
        v2_paths = [
            item["document_path"] for item in records if item["version"] == 2
        ]
        finished = datetime.fromisoformat(
            fail_store.get_batch("BATCH-01")["finished_utc"]
        ).astimezone()
        expected_stamp = finished.strftime("%Y%m%d%H%M")
        assert v2_paths and v2_paths[0].endswith(
            f"AFD01C_{fail_serial}_测试报告_{expected_stamp}_V02.docx"
        )
    finally:
        fail_store.close()


def test_archive_batch_reports_per_device_stage_progress(tmp_path: Path) -> None:
    """归档按设备与阶段回调进度：序号单调、阶段齐备、串号一致。"""

    store, output, serial = _terminal_batch(tmp_path, (AttemptStatus.PASS,))
    try:
        service = ProductionReportService(
            store,
            batch_output_dir=output,
            report_root=tmp_path / "reports",
        )
        events: list[tuple[int, int, str, str]] = []
        artifacts = service.archive_batch(
            "BATCH-01",
            progress=lambda done, total, current, stage: events.append(
                (done, total, current, stage)
            ),
        )

        assert len(artifacts) == 1
        assert events
        assert all(item[1] == 1 for item in events)
        assert all(item[0] == 1 for item in events)
        assert {item[2] for item in events} == {serial}
        assert {item[3] for item in events} >= {"analyzing", "packaging", "document"}
    finally:
        store.close()


def test_report_renders_conditions_temperatures_bias_and_environment(tmp_path: Path) -> None:
    """报告呈现：每项测试条件表、起止组件温度、出厂零偏、批次天气实测优先。"""
    store, output, serial = _terminal_batch(tmp_path, (AttemptStatus.PASS,), test_ids=("locked_rocking",))
    try:
        service = ProductionReportService(
            store,
            batch_output_dir=output,
            report_root=tmp_path / "reports",
        )
        artifact = service.archive_device("BATCH-01", serial)
        assert artifact.document_path is not None
        text = _document_text(artifact.document_path)
        # 条件表：冻结配方的三轴幅值与供电设定
        assert "测试时天气" in text
        assert "幅值 15° / 频率 0.25 Hz" in text
        assert "轨迹采样周期" in text
        # 环境实测缺省时回退工位静态值
        assert "测试时天气" in text and "未记录" in text
    finally:
        store.close()


def test_batch_weather_overrides_station_environment_in_report(tmp_path: Path) -> None:
    """批次创建时录入的当天天气/温湿度优先于工位档案静态环境。"""
    store, output, serial = _terminal_batch(
        tmp_path,
        (AttemptStatus.PASS,),
        test_ids=("locked_rocking",),
        weather="Sunny",
        ambient_temperature_c=24.0,
        ambient_humidity_percent=40,
    )
    try:
        service = ProductionReportService(
            store,
            batch_output_dir=output,
            report_root=tmp_path / "reports",
        )
        artifact = service.archive_device("BATCH-01", serial)
        assert artifact.document_path is not None
        text = _document_text(artifact.document_path)
        assert "晴 / 24.0 °C" in text
        assert "23.5 °C" not in text
    finally:
        store.close()
