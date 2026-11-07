"""Immutable per-device production reports and self-contained evidence archives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import csv
import hashlib
from io import BytesIO
import json
from pathlib import Path
import platform
import re
from typing import Any, Mapping, Optional, Sequence
import zipfile

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Mm, Pt, RGBColor

from satellite_debug_tool import __app_name_cn__, __version__

from .models import AttemptStatus, BatchStatus
from .presentation import tracking_reason_text
from .report_analysis import build_report_analysis, project_attempt_outcomes, render_report_charts
from .result_store import ProductionResultStore, ResultStoreError


_TERMINAL_ATTEMPT_STATUSES = {
    AttemptStatus.PASS.value,
    AttemptStatus.FAIL.value,
    AttemptStatus.INCOMPLETE.value,
    AttemptStatus.SKIPPED.value,
    AttemptStatus.ABORTED.value,
}
_FORMAL_VERDICTS = {"PASS", "FAIL", "INCOMPLETE", "ABORTED"}
_INVALID_PATH_CHARACTERS = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")



_SOFTHERTZ_LOGO_PATH = Path(__file__).resolve().parents[2] / "ui" / "assets" / "soft_hertz_logo_transparent.png"
@dataclass(frozen=True)
class ReportBranding:
    """出厂报告品牌为 SoftHertz 固定标识；不再由工位配置提供。"""

    company_name: str = "SoftHertz"
    logo_path: str = str(_SOFTHERTZ_LOGO_PATH)

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]] = None) -> "ReportBranding":
        return cls()

    def to_mapping(self) -> dict[str, str]:
        return {"company_name": self.company_name, "logo_path": self.logo_path}

    @property
    def missing_fields(self) -> tuple[str, ...]:
        return () if _SOFTHERTZ_LOGO_PATH.is_file() else ("Logo",)


@dataclass(frozen=True)
class ReportArtifact:
    batch_id: str
    serial_number: str
    report_id: str
    version: int
    status: str
    automatic_verdict: str
    final_verdict: str
    directory: Path
    document_path: Optional[Path]
    evidence_path: Path
    manifest_path: Path




def sanitize_path_component(value: object, *, fallback: str = "unknown") -> str:
    text = _INVALID_PATH_CHARACTERS.sub("_", str(value or "").strip())
    text = re.sub(r"\s+", "_", text).strip(" ._")
    if text in {"", ".", ".."}:
        return fallback
    return text[:128]


def automatic_device_verdict(attempts: Sequence[Mapping[str, Any]]) -> str:
    statuses = {str(item.get("status", "")) for item in attempts}
    if not statuses or any(status not in _TERMINAL_ATTEMPT_STATUSES for status in statuses):
        return "INCOMPLETE"
    if AttemptStatus.ABORTED.value in statuses:
        return "ABORTED"
    if AttemptStatus.INCOMPLETE.value in statuses:
        return "INCOMPLETE"
    if AttemptStatus.FAIL.value in statuses:
        return "FAIL"
    if AttemptStatus.PASS.value in statuses:
        return "PASS"
    return "INCOMPLETE"


def _report_document_name(snapshot: Mapping[str, Any], suffix: str) -> str:
    """V1 交付文档按 型号_SN_测试报告_结束时间 命名；旧冻结快照回退 report_id。"""

    basename = str(snapshot.get("document_basename") or "").strip()
    if not basename:
        basename = str(snapshot["report_id"])
    return f"{basename}{suffix}.docx"


class ProductionReportService:
    """Create V1/V2 reports exclusively from a frozen device snapshot."""

    def __init__(
        self,
        store: ProductionResultStore,
        *,
        batch_output_dir: str | Path,
        report_root: str | Path,
        branding: ReportBranding = ReportBranding(),
    ) -> None:
        self.store = store
        self.batch_output_dir = Path(batch_output_dir).expanduser().resolve()
        self.report_root = Path(report_root).expanduser().resolve()
        self.branding = branding

    def archive_batch(self, batch_id: str, *, progress=None) -> tuple[ReportArtifact, ...]:
        batch = self.store.get_batch(batch_id)
        if batch["status"] not in {
            BatchStatus.COMPLETED.value,
            BatchStatus.INCOMPLETE.value,
            BatchStatus.ABORTED.value,
        }:
            raise ResultStoreError("reports require a terminal batch")
        participants = [
            device["serial_number"]
            for device in self.store.list_devices(batch_id)
            if device["status"] == "participant"
        ]
        total = len(participants)
        artifacts = []
        for index, serial_number in enumerate(participants, start=1):

            def device_progress(stage: str, serial=serial_number, done=index) -> None:
                if progress is not None:
                    progress(done, total, serial, stage)

            artifacts.append(
                self.archive_device(batch_id, serial_number, progress=device_progress)
            )
        return tuple(artifacts)

    def archive_device(
        self,
        batch_id: str,
        serial_number: str,
        *,
        progress=None,
    ) -> ReportArtifact:
        def report_stage(stage: str) -> None:
            if progress is not None:
                progress(stage)

        try:
            report_stage("analyzing")
            frozen = self.store.get_report_snapshot(batch_id, serial_number)
            snapshot = frozen["snapshot_json"]
        except ResultStoreError:
            snapshot = self._build_snapshot(batch_id, serial_number)
            frozen = self.store.freeze_report_snapshot(
                batch_id,
                serial_number,
                snapshot,
                automatic_verdict=snapshot["automatic_verdict"],
                report_id=snapshot["report_id"],
            )
        verdict = str(frozen["automatic_verdict"])
        final_verdict = str(snapshot.get("report_verdict", verdict))
        directory = self._artifact_directory(snapshot)
        directory.mkdir(parents=True, exist_ok=True)
        report_stage("packaging")
        evidence_path, evidence_entries = self._write_evidence_zip(snapshot, directory)
        document_path: Optional[Path] = None
        status = "generated_v1" if verdict in _FORMAL_VERDICTS else "not_generated"
        try:
            if verdict in _FORMAL_VERDICTS:
                document_path = directory / _report_document_name(snapshot, "")
                if document_path.exists():
                    records = self.store.list_report_artifacts(batch_id, serial_number)
                    failed = any(
                        item["version"] == 1 and item["status"] == "generation_failed"
                        for item in records
                    )
                    if not failed:
                        raise ResultStoreError(
                            f"report artifact already exists: {document_path.name}"
                        )
                    document_path.unlink()
                report_stage("document")
                self._write_docx(snapshot, document_path, version=1, review=None)
            manifest_path = self._write_manifest(
                snapshot,
                directory,
                evidence_path,
                evidence_entries,
            )
            if verdict in _FORMAL_VERDICTS:
                self.store.record_report_artifact(
                    batch_id,
                    serial_number,
                    version=1,
                    status="generated_v1",
                    final_verdict=final_verdict,
                    document_path=str(document_path),
                    document_sha256=_sha256_file(document_path),
                    evidence_path=str(evidence_path),
                    evidence_sha256=_sha256_file(evidence_path),
                    manifest_path=str(manifest_path),
                )
            else:
                self.store.record_report_artifact(
                    batch_id,
                    serial_number,
                    version=1,
                    status="not_generated",
                    final_verdict=final_verdict,
                    evidence_path=str(evidence_path),
                    evidence_sha256=_sha256_file(evidence_path),
                    manifest_path=str(manifest_path),
                )
        except Exception as exc:
            if verdict in _FORMAL_VERDICTS:
                self.store.record_report_artifact(
                    batch_id,
                    serial_number,
                    version=1,
                    status="generation_failed",
                    final_verdict=final_verdict,
                    evidence_path=str(evidence_path),
                    evidence_sha256=_sha256_file(evidence_path),
                    error=str(exc),
                )
            raise
        return ReportArtifact(
            batch_id=batch_id,
            serial_number=serial_number,
            report_id=snapshot["report_id"],
            version=1,
            status=status,
            automatic_verdict=verdict,
            final_verdict=final_verdict,
            directory=directory,
            document_path=document_path,
            evidence_path=evidence_path,
            manifest_path=manifest_path,
        )

    def review(
        self,
        batch_id: str,
        serial_number: str,
        *,
        reviewer: str,
        final_verdict: str,
        reason: str,
    ) -> ReportArtifact:
        try:
            review = self.store.get_report_review(batch_id, serial_number)
        except ResultStoreError:
            review = self.store.create_report_review(
                batch_id,
                serial_number,
                reviewer=reviewer,
                final_verdict=final_verdict,
                reason=reason,
            )
        else:
            if (
                review["reviewer"] != str(reviewer).strip()
                or review["final_verdict"] != str(final_verdict).upper()
                or review["reason"] != str(reason).strip()
            ):
                raise ResultStoreError("report review already exists with different content")
        frozen = self.store.get_report_snapshot(batch_id, serial_number)
        snapshot = frozen["snapshot_json"]
        directory = self._artifact_directory(snapshot)
        evidence_path = directory / "evidence.zip"
        if not evidence_path.is_file():
            raise ResultStoreError("review requires the original evidence.zip")
        document_path = directory / _report_document_name(snapshot, "_V02")
        if document_path.exists():
            records = self.store.list_report_artifacts(batch_id, serial_number)
            failed = any(
                item["version"] == 2 and item["status"] == "generation_failed"
                for item in records
            )
            if not failed:
                raise ResultStoreError(f"report artifact already exists: {document_path.name}")
            document_path.unlink()
        try:
            self._write_docx(snapshot, document_path, version=2, review=review)
            manifest_path = self._write_manifest(
                snapshot,
                directory,
                evidence_path,
                _zip_entry_hashes(evidence_path),
                review=review,
            )
            self.store.record_report_artifact(
                batch_id,
                serial_number,
                version=2,
                status="reviewed_v2",
                final_verdict=review["final_verdict"],
                document_path=str(document_path),
                document_sha256=_sha256_file(document_path),
                evidence_path=str(evidence_path),
                evidence_sha256=_sha256_file(evidence_path),
                manifest_path=str(manifest_path),
            )
        except Exception as exc:
            self.store.record_report_artifact(
                batch_id,
                serial_number,
                version=2,
                status="generation_failed",
                final_verdict=review["final_verdict"],
                evidence_path=str(evidence_path),
                evidence_sha256=_sha256_file(evidence_path),
                error=str(exc),
            )
            raise
        return ReportArtifact(
            batch_id=batch_id,
            serial_number=serial_number,
            report_id=snapshot["report_id"],
            version=2,
            status="reviewed_v2",
            automatic_verdict=snapshot["automatic_verdict"],
            final_verdict=review["final_verdict"],
            directory=directory,
            document_path=document_path,
            evidence_path=evidence_path,
            manifest_path=manifest_path,
        )

    def _build_snapshot(self, batch_id: str, serial_number: str) -> dict[str, Any]:
        batch = self.store.get_batch(batch_id)
        device = self.store.get_device(batch_id, serial_number)
        attempts = [
            item
            for item in self.store.list_attempts(batch_id)
            if item["serial_number"] == serial_number
        ]
        for attempt in attempts:
            attempt["metrics"] = self.store.list_metrics(int(attempt["attempt_id"]))
        verdict = automatic_device_verdict(attempts)
        finished = _parse_datetime(batch.get("finished_utc") or batch["created_utc"])
        date_text = finished.astimezone().strftime("%Y%m%d")
        model = str(device.get("hardware_type") or batch["recipe_json"].get("product") or "unknown").upper()
        safe_model = sanitize_path_component(model)
        safe_serial = sanitize_path_component(serial_number)
        safe_batch = sanitize_path_component(batch_id)
        report_id = f"RPT-{date_text}-{safe_model}-{safe_serial}-{safe_batch}"
        document_basename = (
            f"{safe_model}_{safe_serial}_测试报告_{finished.astimezone().strftime('%Y%m%d%H%M')}"
        )
        configuration = _read_json_file(self.batch_output_dir / "production_configuration.json")
        branding = self.branding.to_mapping()
        configured_branding = configuration.get("station_profile", {}).get("report_branding")
        if isinstance(configured_branding, Mapping):
            branding.update(
                {
                    str(key): str(value)
                    for key, value in configured_branding.items()
                    if value
                }
            )
        events = self.store.list_events(batch_id)
        sdb_files = self._device_sdb_files_for_serial(serial_number)
        analysis = build_report_analysis(
            sdb_files,
            attempts,
            events,
            self.batch_output_dir,
        )
        analyzable_samples = sum(
            int(item.get("product_samples", 0)) + int(item.get("legacy_samples", 0))
            for item in analysis.get("source_files", [])
        )
        quality_reasons: list[str] = []
        if not sdb_files:
            quality_reasons.append("device SDB is missing")
        if analysis.get("source_errors"):
            quality_reasons.extend(str(item) for item in analysis["source_errors"])
        if analyzable_samples <= 0:
            quality_reasons.append("SDB contains no analyzable telemetry frames")
        for source in analysis.get("source_files", []):
            recording_quality = source.get("recording_quality", {})
            if recording_quality.get("complete") is not True:
                quality_reasons.append(f"{source['file']}：录制收尾证据不完整")
            if any(int(recording_quality.get(key, 0)) > 0 for key in ("gap_markers", "gap_chunks", "dropped_chunks")):
                quality_reasons.append(f"{source['file']}：录制存在数据缺口")
        for attempt in attempts:
            result = attempt.get("result_json", {})
            reasons = result.get("tracking_criteria", {}).get("quality_reasons", [])
            if result.get("data_quality") == "INCOMPLETE" or reasons:
                detail = "、".join(tracking_reason_text(reason) for reason in reasons) or "判定数据不完整"
                quality_reasons.append(f"{attempt['test_id']} 第 {attempt['ordinal']} 次：{detail}")
        data_quality = {
            "schema": "satellite-debug-tool/production-data-quality",
            "schema_version": 1,
            "state": "COMPLETE" if not quality_reasons else "INCOMPLETE",
            "sdb_file_count": len(sdb_files),
            "analyzable_sample_count": analyzable_samples,
            "source_errors": list(analysis.get("source_errors", [])),
            "reasons": quality_reasons,
        }
        report_verdict = verdict
        if verdict == "PASS" and quality_reasons:
            report_verdict = "INCOMPLETE"
        local_now = datetime.now().astimezone()
        return {
            "schema": "satellite-debug-tool/device-report-snapshot",
            "schema_version": 3,
            "report_id": report_id,
            "document_basename": document_basename,
            "automatic_verdict": verdict,
            "report_verdict": report_verdict,
            **project_attempt_outcomes(attempts),
            "data_quality": data_quality,
            "frozen_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "batch": batch,
            "device": device,
            "attempts": attempts,
            "fixture_actions": self.store.list_fixture_actions(batch_id),
            "events": events,
            "analysis": analysis,
            "configuration": configuration,
            "branding": branding,
            "application": {
                "version": __version__,
                "python": platform.python_version(),
                "platform": platform.platform(),
                "hostname": platform.node(),
                "timezone": str(local_now.tzinfo or "未配置"),
                "utc_offset": local_now.strftime("%z") or "未配置",
            },
        }

    def _artifact_directory(self, snapshot: Mapping[str, Any]) -> Path:
        batch = snapshot["batch"]
        device = snapshot["device"]
        finished = _parse_datetime(batch.get("finished_utc") or batch["created_utc"])
        date_dir = finished.astimezone().strftime("%Y-%m-%d")
        model = str(device.get("hardware_type") or batch["recipe_json"].get("product") or "unknown").upper()
        return (
            self.report_root
            / date_dir
            / sanitize_path_component(model)
            / sanitize_path_component(device["serial_number"])
            / sanitize_path_component(batch["batch_id"])
        )

    def _write_evidence_zip(
        self, snapshot: Mapping[str, Any], directory: Path
    ) -> tuple[Path, dict[str, str]]:
        entries = self._evidence_entries(snapshot)
        target = directory / "evidence.zip"
        temporary = target.with_name(f".{target.name}.tmp")
        hashes: dict[str, str] = {}
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in entries.items():
                data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
                archive.writestr(name, data)
                hashes[name] = hashlib.sha256(data).hexdigest()
            for index, source in enumerate(self._device_sdb_files(snapshot), start=1):
                name = "device/evidence.sdb" if index == 1 else f"device/evidence-{index:02d}.sdb"
                archive.write(source, name)
                hashes[name] = _sha256_file(source)
            fixture_root = self.batch_output_dir / "fixtures"
            if fixture_root.is_dir():
                for source in sorted(path for path in fixture_root.rglob("*") if path.is_file()):
                    relative = source.relative_to(fixture_root).as_posix()
                    name = f"fixture_sessions/{relative}"
                    archive.write(source, name)
                    hashes[name] = _sha256_file(source)
        temporary.replace(target)
        return target, hashes

    def _evidence_entries(self, snapshot: Mapping[str, Any]) -> dict[str, bytes | str]:
        device = snapshot["device"]
        attempts = snapshot["attempts"]
        metrics = [metric for attempt in attempts for metric in attempt.get("metrics", [])]
        configuration = snapshot.get("configuration", {})
        event_lines = "".join(_json_line(item) for item in snapshot["events"])
        fixture_actions = snapshot.get("fixture_actions", [])
        power_records = _read_text_file(self.batch_output_dir / "power_supply_records.jsonl")
        power_summary = _read_json_file(self.batch_output_dir / "power_supply_summary.json")
        return {
            "device/identity.json": _pretty_json(device),
            "device/parameters.json": _pretty_json(
                device.get("metadata_json", {}).get("parameters", {"status": "not_recorded"})
            ),
            "device/metrics.json": _pretty_json(metrics),
            "device/events.jsonl": event_lines,
            "fixture/power_config.json": _pretty_json(configuration.get("power_profile", {})),
            "fixture/power_identity.json": _pretty_json(power_summary.get("identity", {"status": "unavailable"})),
            "fixture/power_scpi.jsonl": power_records or _json_line({"status": "unavailable"}),
            "fixture/motion_commands.jsonl": _fixture_lines(fixture_actions, "motion"),
            "fixture/ms6222_raw.jsonl": _first_text(
                self.batch_output_dir,
                ("ms6222_raw.jsonl", "raw_frames.jsonl"),
            ),
            "fixture/ms6222_parsed.jsonl": _first_text_or_csv_jsonl(
                self.batch_output_dir,
                ("ms6222_parsed.jsonl", "parsed_samples.jsonl", "comparisons.jsonl"),
                ("ms6222_parsed.csv", "parsed_frames.csv", "attitude_comparison.csv"),
            ),
            "fixture/attitude_comparison.csv": _first_text(
                self.batch_output_dir,
                ("attitude_comparison.csv",),
            ),
            "fixture/calibration.json": _pretty_json(
                _first_json(self.batch_output_dir, ("calibration.json", "ms6222_calibration.json"))
            ),
            "test/recipe.json": _pretty_json(snapshot["batch"]["recipe_json"]),
            "test/attempts.json": _pretty_json(attempts),
            "test/timeline.json": _pretty_json(snapshot["events"]),
            "test/software.json": _pretty_json(
                {
                    **snapshot["application"],
                    "batch_id": snapshot["batch"]["batch_id"],
                    "report_id": snapshot["report_id"],
                    "snapshot_frozen_utc": snapshot["frozen_utc"],
                }
            ),
            "analysis/analysis.json": _pretty_json(snapshot.get("analysis", {})),
            "analysis/data_quality.json": _pretty_json(snapshot.get("data_quality", {})),
            **render_report_charts(snapshot.get("analysis", {})),
        }

    def _device_sdb_files(self, snapshot: Mapping[str, Any]) -> tuple[Path, ...]:
        return self._device_sdb_files_for_serial(str(snapshot["device"]["serial_number"]))

    def _device_sdb_files_for_serial(self, serial: str) -> tuple[Path, ...]:
        candidates = self.batch_output_dir / "devices" / serial / "evidence"
        if not candidates.is_dir():
            return ()
        return tuple(sorted(path for path in candidates.glob("*.sdb") if path.is_file()))

    def _write_manifest(
        self,
        snapshot: Mapping[str, Any],
        directory: Path,
        evidence_path: Path,
        evidence_entries: Mapping[str, str],
        *,
        review: Optional[Mapping[str, Any]] = None,
    ) -> Path:
        reports = []
        document_paths = set()
        document_paths.update(directory.glob(f"{snapshot['report_id']}_V*.docx"))
        document_paths.update(
            directory.glob(f"{snapshot.get('document_basename') or snapshot['report_id']}*.docx")
        )
        for path in sorted(document_paths):
            reports.append({"name": path.name, "sha256": _sha256_file(path)})
        payload = {
            "schema": "satellite-debug-tool/device-report-manifest",
            "schema_version": 1,
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "application_version": __version__,
            "batch_id": snapshot["batch"]["batch_id"],
            "serial_number": snapshot["device"]["serial_number"],
            "report_id": snapshot["report_id"],
            "automatic_verdict": snapshot["automatic_verdict"],
            "performance_verdict": snapshot.get("performance_verdict"),
            "execution_completeness": snapshot.get("execution_completeness"),
            "data_quality": snapshot.get("data_quality", {}).get("state"),
            "final_verdict": (
                review["final_verdict"]
                if review is not None
                else snapshot.get("report_verdict", snapshot["automatic_verdict"])
            ),
            "snapshot_sha256": hashlib.sha256(
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "evidence": {
                "name": evidence_path.name,
                "sha256": _sha256_file(evidence_path),
                "entries": dict(sorted(evidence_entries.items())),
            },
            "reports": reports,
            "review": None if review is None else dict(review),
        }
        target = directory / "manifest.json"
        _atomic_write_text(target, _pretty_json(payload))
        return target

    def _write_docx(
        self,
        snapshot: Mapping[str, Any],
        destination: Path,
        *,
        version: int,
        review: Optional[Mapping[str, Any]],
    ) -> None:
        document = Document()
        _configure_document(document, snapshot.get("branding", {}), snapshot["device"]["serial_number"])
        _add_cover(document, snapshot, version, review)
        document.add_page_break()
        _add_configuration(document, snapshot)
        document.add_page_break()
        _add_curve_analysis(document, snapshot)
        _add_integrity(document, snapshot)
        _add_review(document, snapshot, version, review)
        document.add_page_break()
        _add_conclusion(document, snapshot, review)
        document.add_page_break()
        _add_appendix(document, snapshot)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        document.save(temporary)
        temporary.replace(destination)


def _configure_document(document: Document, branding: Mapping[str, Any], serial_number: str = "") -> None:
    section = document.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width = Mm(210)
    section.page_height = Mm(297)
    section.top_margin = Mm(18)
    section.bottom_margin = Mm(18)
    section.left_margin = Mm(20)
    section.right_margin = Mm(20)
    styles = document.styles
    # 中文固定宋体；西文与数字固定 Times New Roman。
    cjk_font = "宋体"
    latin_font = "Times New Roman"
    normal = styles["Normal"]
    normal.font.name = latin_font
    normal.font.size = Pt(9)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), cjk_font)
    normal._element.rPr.rFonts.set(qn("w:ascii"), latin_font)
    normal._element.rPr.rFonts.set(qn("w:hAnsi"), latin_font)
    for style_name, size in (("Title", 22), ("Heading 1", 16), ("Heading 2", 12), ("Heading 3", 10)):
        style = styles[style_name]
        style.font.name = latin_font
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor(0, 0, 0)
        style._element.rPr.rFonts.set(qn("w:eastAsia"), cjk_font)
        style._element.rPr.rFonts.set(qn("w:ascii"), latin_font)
        style._element.rPr.rFonts.set(qn("w:hAnsi"), latin_font)
    header = section.header.paragraphs[0]
    if _SOFTHERTZ_LOGO_PATH.is_file():
        header.add_run().add_picture(str(_SOFTHERTZ_LOGO_PATH), height=Cm(0.9))
    header.add_run(f"\t{serial_number}")
    header.alignment = WD_ALIGN_PARAGRAPH.LEFT
    right_edge = section.page_width - section.left_margin - section.right_margin
    header.paragraph_format.tab_stops.add_tab_stop(right_edge, WD_TAB_ALIGNMENT.RIGHT)


def _add_cover(
    document: Document,
    snapshot: Mapping[str, Any],
    version: int,
    review: Optional[Mapping[str, Any]],
) -> None:
    branding = snapshot.get("branding", {})
    batch = snapshot["batch"]
    device = snapshot["device"]
    configuration = snapshot.get("configuration", {})
    product = configuration.get("product_template", {})
    verdict = review["final_verdict"] if review else snapshot.get("report_verdict", snapshot["automatic_verdict"])

    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_before = Pt(120)
    title.paragraph_format.space_after = Pt(20)
    run = title.add_run("测试报告")
    run.bold = True
    run.font.size = Pt(26)

    subtitle = document.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle.paragraph_format.space_after = Pt(70)
    subtitle_run = subtitle.add_run("卫星通信相控阵设备自动化测试")
    subtitle_run.font.size = Pt(11)

    product_name = product.get("display_name") or device.get("hardware_type") or "未配置"
    product_model = (
        device.get("hardware_type")
        or product.get("product")
        or batch["recipe_json"].get("product")
        or "未配置"
    )
    document.add_paragraph().paragraph_format.space_after = Pt(30)
    for label, value in (
        ("产品名称", product_name),
        ("产品型号", product_model),
        ("产品编号", device["serial_number"]),
        ("批次编号", batch["batch_id"]),
        ("报告版本", f"V{version:02d}"),
    ):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = Cm(4.0)
        paragraph.paragraph_format.space_after = Pt(16)
        label_run = paragraph.add_run(f"{label}：  ")
        label_run.bold = True
        label_run.font.size = Pt(13)
        value_run = paragraph.add_run(str(value))
        value_run.bold = True
        value_run.font.size = Pt(13)
        value_run.font.underline = True

    document.add_paragraph().paragraph_format.space_after = Pt(40)
    for label in ("检验：", "质量审核：", "日期："):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = Cm(4.0)
        paragraph.paragraph_format.space_after = Pt(16)
        run = paragraph.add_run(label)
        run.bold = True
        run.font.size = Pt(13)
        paragraph.add_run("_" * 24)
    date_paragraph = document.add_paragraph()
    date_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    date_paragraph.paragraph_format.space_before = Pt(26)
    date_run = date_paragraph.add_run(_cover_date(batch))
    date_run.bold = True
    date_run.font.size = Pt(12)




def _conclusion_text(status: str) -> str:
    return {"PASS": "PASS", "FAIL": "FAIL"}.get(str(status).upper(), "INCOMPLETE")


def _conclusion_color(status: str) -> RGBColor:
    upper = str(status).upper()
    if upper == "PASS":
        return RGBColor(0x1B, 0x7A, 0x43)
    if upper == "FAIL":
        return RGBColor(0xC6, 0x28, 0x28)
    return RGBColor(0xE6, 0x8A, 0x00)


def _add_conclusion(
    document: Document,
    snapshot: Mapping[str, Any],
    review: Optional[Mapping[str, Any]],
) -> None:
    _heading(document, "五、测试结论", level=1)
    attempts = snapshot["attempts"]
    rows = [("编号", "测试项目", "测试结论")]
    for index, attempt in enumerate(attempts, start=1):
        latest = attempt
        rows.append((
            f"{index}.",
            str(attempt["test_id"]),
            str(latest["status"]).upper(),
        ))
    table = document.add_table(rows=len(rows), cols=3)
    table.style = "Table Grid"
    widths = (Cm(1.6), Cm(7.0), Cm(7.4))
    for row_index, row_values in enumerate(rows):
        for column, value in enumerate(row_values):
            cell = table.cell(row_index, column)
            cell.width = widths[column]
            paragraph = cell.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = paragraph.add_run(str(value))
            run.bold = row_index == 0 or column == 2
            if row_index > 0 and column == 2:
                run.font.size = Pt(12)
                run.font.color.rgb = _conclusion_color(value)

    final_verdict = review["final_verdict"] if review else snapshot.get(
        "report_verdict", snapshot["automatic_verdict"]
    )
    summary = document.add_paragraph()
    run = summary.add_run("测试结论：")
    run.bold = True
    verdict_run = summary.add_run(_conclusion_text(final_verdict))
    verdict_run.bold = True
    verdict_run.font.size = Pt(14)
    verdict_run.font.color.rgb = _conclusion_color(final_verdict)

    # 整机关键性能指标：三类场景 × 三个计时起点的锁定时间
    by_test = {str(a["test_id"]): a for a in snapshot["attempts"]}
    scenario_rows = [("场景", "从上电起", "从 GPS 锁定起", "从 modem 下发对星数据起")]

    def _fmt(value):
        return "未记录" if value is None else f"{float(value):.1f} s"

    for label, test_id in (
        ("静止锁定时间", "static_acquisition"),
        ("动中通锁定时间", "locked_drive"),
        ("摇摆台锁定时间", "locked_rocking"),
    ):
        criteria = (by_test.get(test_id, {}).get("result_json", {}) or {}).get("tracking_criteria", {})
        acquired = criteria.get("acquired_at")
        anchor = criteria.get("power_on_monotonic_s") or criteria.get("started_at")
        gnss = criteria.get("gnss_fixed_at")
        modem = criteria.get("modem_online_at")
        scenario_rows.append((
            label,
            _fmt(criteria.get("lock_elapsed_s")),
            _fmt(None if acquired is None or gnss is None else acquired - gnss),
            _fmt(None if acquired is None or modem is None else acquired - modem),
        ))
    _TABLE_COUNTER["n"] += 1
    _add_table(document, scenario_rows, widths=(4.6, 3.6, 3.8, 5.0))
    cap = document.add_paragraph(f"表 {_TABLE_COUNTER['n']}  整机关键性能指标（锁定时间）")
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in cap.runs:
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(89, 89, 89)
    _spacer(document)
    document.add_paragraph(
        "锁定时间判定口径与各工况判据一致（连续锁定 5 s 确认）；"
        "「从 modem 上线起」以设备 modem 上线时刻为计时零点。"
    )

    document.add_paragraph()
    end_line = document.add_paragraph("-------------------------------报告结束------------------------------")
    end_line.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _add_configuration_detail_table(document: Document, snapshot: Mapping[str, Any]) -> None:
    batch = snapshot["batch"]
    device = snapshot["device"]
    metadata = device.get("metadata_json", {})
    configuration = snapshot.get("configuration", {})
    power = configuration.get("power_profile", {})
    station = configuration.get("station_profile", {})
    combined = configuration.get("combined_supply", {})
    _add_key_value_table(
        document,
        (
            ("设备 UID", device.get("device_uid") or "未记录"),
            ("MAC", device.get("mac_address") or "未记录"),
            ("网络端点", _network_endpoint(device.get("endpoint_ip"), device.get("endpoint_port"))),
            ("配方 SHA-256", batch.get("recipe_sha256", "")),
            ("电源", f"{power.get('manufacturer', '未记录')} {power.get('model', '')}（{power.get('serial_number') or '未记录'}）"),
            ("批次电压 / 限流", f"{combined.get('voltage_v', '未记录')} V / {combined.get('current_a', '未记录')} A"),
            ("摇摆台配置", station.get("motion_profile_id") or "未记录"),
            ("MS6222 配置", station.get("reference_profile_id") or "未记录"),
            ("测试工位", station.get("display_name") or "未记录"),
            ("参数回读", _pretty_json(metadata.get("parameters")) if metadata.get("parameters") else "本批次未配置参数核验"),
        ),
        caption="配置明细",
    )


def _weather_label(batch: Mapping[str, Any]) -> str:
    _WEATHER_ZH = {
        "Sunny": "晴", "Cloudy": "多云", "Overcast": "阴",
        "Rain": "雨", "Snow": "雪", "Fog": "雾",
    }
    token = str(batch.get("weather") or "")
    ambient = batch.get("ambient_temperature_c")
    parts = []
    if token:
        parts.append(_WEATHER_ZH.get(token, token))
    if ambient is not None:
        parts.append(f"{float(ambient):.1f} °C")
    return " / ".join(parts) if parts else "未记录"


def _add_configuration(document: Document, snapshot: Mapping[str, Any]) -> None:
    _heading(document, "一、测试环境", level=1)
    batch = snapshot["batch"]
    device = snapshot["device"]
    metadata = device.get("metadata_json", {})
    configuration = snapshot.get("configuration", {})
    station = configuration.get("station_profile", {})
    application = snapshot.get("application", {})
    combined = configuration.get("combined_supply", {})
    _add_captioned_table(
        document,
        "测试环境",
        (
            ("设备型号", device.get("hardware_type") or "未记录"),
            ("设备 SN", device["serial_number"]),
            ("主固件", metadata.get("main_firmware") or "未记录"),
            ("网络端点", _network_endpoint(device.get("endpoint_ip"), device.get("endpoint_port"))),
            ("测试工位", station.get("display_name") or "未记录"),
            ("供电设定", f"{combined.get('voltage_v', '未记录')} V / {combined.get('current_a', '未记录')} A" if combined else "未记录"),
            ("配方", batch["recipe_json"].get("recipe_id", "未记录")),
            ("上位机版本", f"{__app_name_cn__} {application.get('version', '未记录')}"),
            ("测试时天气", _weather_label(batch)),
        ),
    )
    document.add_paragraph(
        "整机姿态、波束、原始 SNR 与跟踪阶段按统一遥测口径连续记录，"
        "原始 SDB 与配置快照随证据包归档。"
    )


_TEST_CONDITION_LABELS = {
    "firmware_verification": "固件核验",
    "parameter_verification": "参数核验",
    "static_acquisition": "静置对星",
    "locked_rocking": "锁定后摇摆跟踪",
    "power_on_rocking": "摇摆中上电跟踪",
    "locked_drive": "锁定中行驶跟踪",
    "power_on_drive": "行驶中上电跟踪",
}


def _test_conditions_table(recipe: Mapping[str, Any], attempt: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """测试条件来自批次冻结的配方与工位配置，不由报告推断。"""

    test_id = str(attempt["test_id"])
    test_config = recipe.get("tests", {}).get(test_id, {})
    rows: list[tuple[str, str]] = []
    duration = test_config.get("duration_s")
    rows.append(("本项时长（含搜星）", f"{duration} s" if duration else "未记录"))
    motion = recipe.get("fixtures", {}).get("motion_platform", {})
    profile = motion.get("profile", {})
    if profile and test_id in {"locked_rocking", "power_on_rocking"}:
        for axis in ("roll", "pitch", "yaw"):
            axis_config = profile.get(axis, {})
            if axis_config:
                rows.append((
                    f"摇摆台 {axis} 轴设定",
                    f"幅值 {axis_config.get('amplitude_deg', '未记录')}° / "
                    f"频率 {axis_config.get('frequency_hz', '未记录')} Hz / "
                    f"相位 {axis_config.get('phase_deg', '未记录')}°",
                ))
        if profile.get("sample_period_ms") is not None:
            rows.append(("轨迹采样周期", f"{profile['sample_period_ms']} ms"))
        limits = motion.get("limits", {})
        if limits:
            rows.append((
                "运动限幅",
                f"Roll {limits.get('roll_abs_deg', '未记录')}° / "
                f"Pitch {limits.get('pitch_abs_deg', '未记录')}° / "
                f"Yaw {limits.get('yaw_abs_deg', '未记录')}°",
            ))
    firmware = recipe.get("expected", {}).get("main_firmware", {})
    if test_id == "firmware_verification" and firmware:
        rows.append(("固件期望", f"{firmware.get('value', '未记录')}（{firmware.get('match', 'exact')}）"))
    parameters = recipe.get("expected", {}).get("parameters", {})
    if test_id == "parameter_verification" and parameters:
        rows.append(("参数核验规则数", str(len(parameters))))
    return tuple(rows)


_HEADING_SPACING = {1: (18, 10), 2: (14, 8), 3: (10, 6)}


def _heading(document: Document, text_value: str, level: int) -> None:
    paragraph = document.add_heading(text_value, level=level)
    before, after = _HEADING_SPACING.get(level, (10, 6))
    paragraph.paragraph_format.space_before = Pt(before)
    paragraph.paragraph_format.space_after = Pt(after)
    for run in paragraph.runs:
        run.font.color.rgb = RGBColor(0, 0, 0)
        run.font.name = "Times New Roman"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    return paragraph


_TABLE_COUNTER = {"n": 0}


def _spacer(document: Document) -> None:
    document.add_paragraph()


def _add_captioned_table(document: Document, caption: str, values):
    _add_key_value_table(document, values, caption=caption)


def _tracking_number(value: object, *, percent: bool = False) -> str:
    if value is None:
        return "未记录"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number * 100:.3f}%" if percent else f"{number:.3f} s"


def _add_tracking_criteria(
    document: Document, criteria: Mapping[str, Any], result: Mapping[str, Any], numbering: str = ""
) -> None:
    policy = criteria.get("policy", {})
    _heading(document, f"{numbering} 跟踪判据与事件统计".strip(), level=3)
    _add_key_value_table(
        document,
        (
            ("判据版本", criteria.get("rule_version", "未记录")),
            ("工况总时长（含搜星）", _tracking_number(criteria.get("duration_s"))),
            ("有效跟踪条件", (
                "设备姿态相对 MS-6222 参考的三轴最大误差 ≤ "
                f"{_tracking_number(policy.get('attitude_error_max_deg'))}°；SNR 与锁定仅记录"
            ) if criteria.get("attitude_mode") else "跟踪阶段为 LOCKED、锁定标志为 true、SNR > 0；三项数据均有效"),
            ("连续锁定确认时间", _tracking_number(policy.get("acquisition_stable_s"))),
            ("搜星时限", _tracking_number(policy.get("acquisition_timeout_s"))),
            ("单次中断上限", _tracking_number(policy.get("interruption_max_s"))),
            ("次数配额", f"每 30 分钟 {policy.get('interruptions_per_30min', '未记录')} 次；本项 {criteria.get('interruption_limit', '未记录')} 次"),
            ("累计中断占比上限", _tracking_number(policy.get("interruption_ratio_max"), percent=True)),
            ("连续恢复确认时间", _tracking_number(policy.get("recovery_stable_s"))),
            ("最大允许采样间隔", _tracking_number(policy.get("maximum_sample_gap_s"))),
        ),
        caption="判定门限",
    )
    acquisition = (
        _tracking_number(criteria.get("acquisition_elapsed_s"))
        if criteria.get("acquisition_required") is True else "前置步骤已确认稳定锁定"
        if criteria.get("acquisition_required") is False else "未记录"
    )
    _add_key_value_table(
        document,
        (
            ("性能结论", result.get("performance_verdict", "未记录")),
            ("数据质量", result.get("data_quality", "未记录")),
            ("初次稳定锁定耗时", acquisition),
            ("上电至跟踪锁定耗时" if criteria.get("attitude_mode") else "上电至锁定耗时",
             _tracking_number(criteria.get("tracking_lock_elapsed_s") if criteria.get("attitude_mode") else criteria.get("lock_elapsed_s"))),
            ("上电至姿态对准耗时", "不适用" if not criteria.get("attitude_mode") else _tracking_number(criteria.get("attitude_aligned_elapsed_s"))),
            ("上电至 GNSS 定位耗时", _tracking_number(criteria.get("gnss_lock_elapsed_s"))),
            ("上电至 modem 在线耗时", _tracking_number(criteria.get("modem_online_elapsed_s"))),
            ("正式观察时长", _tracking_number(criteria.get("observation_duration_s"))),
            ("中断次数", criteria.get("interruption_count", "未记录")),
            ("最长已观测中断时长", _tracking_number(criteria.get("interruption_max_s"))),
            ("累计已观测中断时长", _tracking_number(criteria.get("interruption_total_s"))),
            ("累计中断占比", _tracking_number(criteria.get("interruption_ratio"), percent=True)),
            ("窗口结束恢复状态", "恢复确认中" if criteria.get("recovery_pending") else "无待确认恢复"),
            ("判定样本数", criteria.get("sample_count", "未记录")),
            ("有效判定样本数", criteria.get("valid_sample_count", "未记录")),
            ("最大已观测采样间隔", _tracking_number(criteria.get("maximum_observed_gap_s"))),
        ),
        caption="判定结果与耗时",
    )
    if criteria.get("attitude_mode"):
        _add_key_value_table(
            document,
            (
                ("姿态误差最大值", "未记录" if criteria.get("attitude_error_max_deg") is None else f"{float(criteria['attitude_error_max_deg']):.2f}°"),
                ("姿态误差均值", "未记录" if criteria.get("attitude_error_mean_deg") is None else f"{float(criteria['attitude_error_mean_deg']):.2f}°"),
                ("姿态判定样本数", criteria.get("attitude_sample_count", "未记录")),
            ),
            caption="姿态统计",
        )
    for field, label in (("failure_reasons", "性能失败原因"), ("quality_reasons", "数据质量原因")):
        reasons = criteria.get(field, [])
        document.add_paragraph(f"{label}：" + ("；".join(tracking_reason_text(value) for value in reasons) or "未记录此类原因"))
    document.add_paragraph(
        "统计依据有效采样证据；未采样区间的物理状态未作推断。中断明细及原始判据保存在 test/attempts.json。"
    )




_TEST_SCENARIO_IDS = {
    "static_acquisition": "static",
    "locked_rocking": "locked_rocking",
    "power_on_rocking": "power_on_rocking",
    "locked_drive": "vehicle_dynamic",
    "power_on_drive": "vehicle_dynamic",
}
_VEHICLE_TEST_SET = frozenset({"locked_drive", "power_on_drive"})


def _scenario_label(test_id: str) -> str:
    return {
        "firmware_verification": "固件核验",
        "parameter_verification": "参数核验",
        "static_acquisition": "工况：静置对星",
        "locked_rocking": "工况：锁定后摇摆跟踪",
        "power_on_rocking": "工况：摇摆中上电跟踪",
        "locked_drive": "工况：锁定中行驶跟踪",
        "power_on_drive": "工况：行驶中上电跟踪",
    }.get(str(test_id), f"工况：{test_id}")


def _attempt_narrative(attempt: Mapping[str, Any]) -> str:
    """用真实判定数字写一句连贯的工况叙述；缺证据时如实说明。"""
    criteria = attempt.get("result_json", {}).get("tracking_criteria")
    if not isinstance(criteria, Mapping):
        result_json = attempt.get("result_json", {})
        detail = str(result_json.get("reason") or "").strip()
        status = str(attempt.get("status") or "未执行").upper()
        test_id = str(attempt.get("test_id"))
        if test_id == "firmware_verification":
            actual = str(result_json.get("actual") or "未记录")
            if status == "PASS":
                return f"设备固件版本回读为 {actual}，与配方期望一致，核验通过。"
            return f"设备固件版本回读为 {actual}，与配方期望不一致，核验未通过。"
        if test_id == "parameter_verification":
            matches = result_json.get("matches", {})
            expected = result_json.get("expected_per_device", {})
            if status == "PASS":
                detail_text = "、".join(
                    f"{name}={expected.get(name, '配方期望值')}" for name in matches
                ) or "全部规则"
                return f"参数表回读逐项比对通过（{detail_text}），核验通过。"
            failed = [name for name, ok in matches.items() if not ok]
            return f"参数回读比对未通过：{'、'.join(failed) or '存在不一致项'}。"
        suffix = f"（{detail}）" if detail else ""
        return f"本工况记录状态为 {status}，未形成跟踪判据统计{suffix}。"
    duration = criteria.get("duration_s")
    sentences = []
    if duration:
        sentences.append(f"本工况按冻结配方连续记录 {float(duration):.0f} s")
    lock = criteria.get("tracking_lock_elapsed_s") or criteria.get("acquisition_elapsed_s")
    if lock is not None:
        sentences.append(f"约 {float(lock):.1f} s 后确认稳定跟踪")
    gnss = criteria.get("gnss_lock_elapsed_s")
    if gnss is not None:
        sentences.append(f"GNSS 定位在记录开始后 {float(gnss):.1f} s 有效")
    modem = criteria.get("modem_online_elapsed_s")
    if modem is not None:
        sentences.append(f"modem 在 {float(modem):.1f} s 内上线")
    if sentences:
        narrative = "；".join(sentences) + "。"
    else:
        narrative = "本工况未取得有效采样证据。"
    interruptions = criteria.get("interruption_count")
    if interruptions is not None:
        longest = criteria.get("interruption_max_s") or 0.0
        narrative += (
            f"正式观察期内中断 {int(interruptions)} 次，最长单次 {float(longest):.1f} s，"
            f"累计占比 {float(criteria.get('interruption_ratio') or 0.0) * 100:.2f}%。"
        )
    attitude_max = criteria.get("attitude_error_max_deg")
    if attitude_max is not None:
        mean = criteria.get("attitude_error_mean_deg")
        narrative += (
            f"设备姿态相对 MS-6222 参考的三轴最大误差 {float(attitude_max):.2f}°"
            f"（均值 {float(mean):.2f}°），门限 3°。"
        )
    elif criteria.get("attitude_mode"):
        narrative += "本次记录未接入 MS-6222 姿态参考，姿态误差统计不可用。"
    reasons = list(criteria.get("quality_reasons") or ())
    if reasons:
        narrative += "数据质量记录：" + "；".join(
            tracking_reason_text(reason) for reason in reasons
        ) + "。"
    return narrative


def _add_curve_analysis(document: Document, snapshot: Mapping[str, Any]) -> None:
    _heading(document, "二、测试结果", level=1)
    analysis = snapshot.get("analysis", {})
    series = analysis.get("series", {})
    charts = render_report_charts(analysis)
    figure_no = 0
    for name, caption in (
        ("analysis/snr_timeline.png", "全过程 SNR 与锁定状态"),
        ("analysis/attitude_timeline.png", "全过程设备姿态"),
    ):
        payload = charts.get(name)
        if not payload:
            continue
        figure_no += 1
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.add_run().add_picture(BytesIO(payload), width=Cm(16.5))
        caption_paragraph = document.add_paragraph(f"图 {figure_no}  {caption}")
        caption_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    attempts = snapshot["attempts"]
    if not attempts:
        document.add_paragraph("未记录测试。")
        return
    _ORDER = ("firmware_verification", "parameter_verification", "static_acquisition",
              "locked_rocking", "power_on_rocking", "locked_drive", "power_on_drive")
    attempts = sorted(
        attempts,
        key=lambda item: (
            _ORDER.index(item["test_id"]) if item["test_id"] in _ORDER else len(_ORDER),
            int(item["ordinal"]),
        ),
    )
    recipe = snapshot["batch"].get("recipe_json", {})
    started_at = analysis.get("clock_alignment", {}).get("offset_ns")
    for index, attempt in enumerate(attempts, start=1):
        _heading(document, f"{index} {_scenario_label(attempt['test_id'])}", level=2)
        narrative = _attempt_narrative(attempt)
        document.add_paragraph(narrative)
        result_json = attempt.get("result_json", {})
        if attempt["test_id"] == "firmware_verification":
            expected = str(recipe.get("expected", {}).get("main_firmware", {}).get("value") or "未记录")
            actual = str(result_json.get("actual") or "未记录")
            _add_key_value_table(
                document,
                (("配方期望版本", expected), ("实际回读版本", actual)),
                caption="固件版本核验",
            )
        elif attempt["test_id"] == "parameter_verification":
            actual_params = result_json.get("parameters", {})
            expected_rules = result_json.get("expected", {})
            rows = [("参数编号", "参数名称", "参数值", "参数说明")]
            for order, (name, value) in enumerate(actual_params.items(), start=1):
                rule = expected_rules.get(name, {})
                explanation = str(rule.get("description") or "—")
                rows.append((str(order), str(name), str(value), explanation))
            _TABLE_COUNTER["n"] += 1
            _add_table(document, rows, widths=(2.0, 4.6, 4.0, 5.8))
            cap = document.add_paragraph(f"表 {_TABLE_COUNTER['n']}  参数回读留档")
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in cap.runs:
                run.font.size = Pt(9)
                run.font.color.rgb = RGBColor(89, 89, 89)
            _spacer(document)
        conditions = list(_test_conditions_table(
            recipe if isinstance(recipe, Mapping) else {}, attempt
        ))
        conditions.insert(1, ("测试时天气", _weather_label(snapshot["batch"])))
        _add_key_value_table(document, tuple(conditions))
        test_id_now = str(attempt["test_id"])
        scenario_id = _TEST_SCENARIO_IDS.get(test_id_now)
        for chart_name, chart_caption in (
            (f"analysis/scenarios/{scenario_id}/snr.png", "本工况 SNR 与锁定状态"),
            (f"analysis/scenarios/{scenario_id}/attitude.png", "本工况设备姿态"),
        ):
            payload = charts.get(chart_name)
            if not payload:
                continue
            figure_no += 1
            paragraph = document.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.add_run().add_picture(BytesIO(payload), width=Cm(16.0))
            caption_paragraph = document.add_paragraph(f"图 {figure_no}  {chart_caption}")
            caption_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if test_id_now in _VEHICLE_TEST_SET:
            track = charts.get("analysis/vehicle_track.png")
            if track:
                figure_no += 1
                paragraph = document.add_paragraph()
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                paragraph.add_run().add_picture(BytesIO(track), width=Cm(16.0))
                caption_paragraph = document.add_paragraph(
                    f"图 {figure_no}  车载测试轨迹（绿=起点，红=终点）"
                )
                caption_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            dynamics = analysis.get("vehicle_dynamics")
            if dynamics and (
                dynamics.get("max_speed_ms") is not None
                or dynamics.get("max_yaw_rate_deg_s") is not None
            ):
                parts = []
                speed = dynamics.get("max_speed_ms")
                yaw_rate = dynamics.get("max_yaw_rate_deg_s")
                if speed is not None:
                    parts.append(f"实际最大速度 {float(speed) * 3.6:.1f} km/h")
                if yaw_rate is not None:
                    parts.append(f"实际最大角速度 {float(yaw_rate):.2f} °/s")
                document.add_paragraph(
                    "、".join(parts) + "（取自记录期间逐帧 GNSS 速度与航向变化率）。"
                )
        criteria = attempt.get("result_json", {}).get("tracking_criteria")
        if isinstance(criteria, Mapping):
            _heading(document, f"{index}.1 主要性能结果", level=3)
            _add_tracking_criteria(document, criteria, attempt.get("result_json", {}), numbering=f"{index}.1.1")
    if not series.get("snr", {}).get("sample_count"):
        document.add_paragraph("SNR 曲线不可用：记录中未发现有效 SNR 通道。")


def _add_integrity(document: Document, snapshot: Mapping[str, Any]) -> None:
    _heading(document, "三、数据完整性与出厂零偏", level=1)
    device = snapshot["device"]
    attempts = snapshot["attempts"]
    events = snapshot["events"]
    incomplete = [item for item in attempts if item["status"] in {"incomplete", "aborted"}]
    quality = snapshot.get("data_quality", {})
    sdb_present = int(quality.get("sdb_file_count", 0)) > 0
    disconnects = sum(
        1 for event in events if "disconnect" in str(event.get("event_type", "")).lower()
    )
    _add_key_value_table(
        document,
        (
            ("身份确认", "已记录" if device.get("device_uid") or device.get("serial_number") else "缺失"),
            ("原始 SDB", "已归档" if sdb_present else "未记录"),
            ("可分析遥测样本", quality.get("analyzable_sample_count", 0)),
            ("断线相关事件", disconnects),
            ("事件总数", len(events)),
            ("不完整测试项", len(incomplete)),
            ("时间对齐", "使用主机 UTC 与单调时钟"),
            ("执行完整性", snapshot.get("execution_completeness", "该版本未记录")),
            ("采集完整性", "完整" if quality.get("state") == "COMPLETE" else "证据不完整"),
        ),
        caption="数据完整性",
    )
    for reason in quality.get("reasons", []):
        document.add_paragraph(f"数据质量：{reason}")
    bias = (snapshot.get("analysis", {}) or {}).get("final_imu_bias")
    _heading(document, "出厂零偏（整批测试完成时的最新 INS 估计）", level=2)
    if bias:
        def bias_row(label, values, unit):
            text = "、".join(f"{float(item):+.4f}" for item in values) if values else "未记录"
            return (label, f"{text} {unit}")

        _add_key_value_table(
            document,
            (
                bias_row("陀螺零偏 X/Y/Z", bias.get("gyro_bias_dps"), "°/s"),
                bias_row("陀螺零偏标准差", bias.get("gyro_bias_std_dps"), "°/s"),
                bias_row("加计零偏 X/Y/Z", bias.get("accel_bias_m_s2"), "m/s²"),
                ("采样时刻（主机时间）", str(bias.get("host_timestamp_ns", "未记录"))),
            ),
            caption="出厂零偏",
        )
        document.add_paragraph(
            "零偏取自本设备录制 SDB 中最后一条 INS TRACE kind3 记录，代表整批测试完成时的滤波估计值。"
        )
    else:
        document.add_paragraph("录制中未包含 INS TRACE kind3 零偏记录。")
    document.add_paragraph(
        "共享夹具证据按本设备测试窗口归档；当前批次未提供的证据文件在 ZIP 中以 status=unavailable 明确标识。"
    )


def _add_review(
    document: Document,
    snapshot: Mapping[str, Any],
    version: int,
    review: Optional[Mapping[str, Any]],
) -> None:
    _heading(document, "四、审核与版本记录", level=1)
    rows = [("版本", "结论", "生成/审核时间", "审核人", "原因")]
    rows.append((
        "V01",
        snapshot.get("report_verdict", snapshot["automatic_verdict"]),
        snapshot["frozen_utc"],
        "自动生成",
        "执行结论与采集质量综合结果",
    ))
    if review is not None:
        rows.append(
            (
                "V02",
                review["final_verdict"],
                review["reviewed_utc"],
                review["reviewer"],
                review["reason"],
            )
        )
    else:
        rows.append(("V02", "未生成", "-", "未审核", "-"))
    _add_table(document, rows)
    document.add_paragraph(
        f"当前文档版本：V{version:02d}。自动结论始终保留，审核仅形成新的正式版本。"
    )


def _add_appendix(document: Document, snapshot: Mapping[str, Any]) -> None:
    _heading(document, "附录 A  数据处理说明", level=1)
    document.add_paragraph(
        "原始记录为整机连续调试数据（SDB v3）。报告曲线使用各展示窗口内全部有效实测点，"
        "不插值、不平滑；判据统计仅使用遥测字段完整、数值有限的有效样本，"
        "采样缺口作为数据质量事实单独记录，不推断为性能失败。"
        "证据包内含原始 SDB、配置快照与逐项判定明细，SHA-256 见 manifest.json。"
    )
    _heading(document, "附录 B  配置明细", level=1)
    _add_configuration_detail_table(document, snapshot)

    _heading(document, "附录 C  证据包索引", level=1)
    _heading(document, "A. 完整指标", level=2)
    metric_rows = [("测试项目", "次数", "指标", "值", "单位", "判定", "算法版本")]
    for attempt in snapshot["attempts"]:
        for metric in attempt.get("metrics", []):
            metric_rows.append(
                (
                    attempt["test_id"],
                    attempt["ordinal"],
                    metric["name"],
                    _compact_json(metric["value_json"]),
                    metric.get("unit", ""),
                    metric.get("verdict", ""),
                    metric.get("algorithm_version", ""),
                )
            )
    if len(metric_rows) == 1:
        metric_rows.append(("未记录", "-", "-", "-", "-", "-", "-"))
    _add_table(document, metric_rows)
    _heading(document, "B. 异常事件索引", level=2)
    event_rows = [("ID", "UTC", "类型", "详情")]
    exception_events = _report_exception_events(snapshot["events"])
    for event in exception_events:
        event_rows.append(
            (
                event["event_id"],
                event["wall_time_utc"],
                event["event_type"],
                _compact_json(event["payload_json"]),
            )
        )
    if len(event_rows) == 1:
        event_rows.append(("-", "-", "无异常事件", "完整事件流见 device/events.jsonl 和 test/timeline.json"))
    _add_table(document, event_rows)
    document.add_paragraph(
        f"批次共记录 {len(snapshot['events'])} 条事件，报告列出 {len(exception_events)} 条异常相关事件。"
    )
    _heading(document, "C. 证据包索引", level=2)
    evidence_paths = [
        "device/evidence.sdb",
        "device/identity.json",
        "device/parameters.json",
        "device/metrics.json",
        "device/events.jsonl",
        "fixture/power_config.json",
        "fixture/power_identity.json",
        "fixture/power_scpi.jsonl",
        "fixture/motion_commands.jsonl",
        "fixture/ms6222_raw.jsonl",
        "fixture/ms6222_parsed.jsonl",
        "fixture/attitude_comparison.csv",
        "fixture/calibration.json",
        "test/recipe.json",
        "test/attempts.json",
        "test/timeline.json",
        "test/software.json",
        "analysis/analysis.json",
        "analysis/snr_timeline.png",
        "analysis/attitude_timeline.png",
        "analysis/convergence_timeline.png",
    ]
    evidence_paths.extend(
        f"analysis/scenarios/{scenario['scenario_id']}/snr.png；"
        f"analysis/scenarios/{scenario['scenario_id']}/attitude.png"
        for scenario in snapshot.get("analysis", {}).get("scenario_groups", [])
    )
    _add_table(
        document,
        [("序号", "归档路径")]
        + [(str(index), path) for index, path in enumerate(evidence_paths, start=1)],
    )


def _set_paragraph_bottom_border(paragraph: Any) -> None:
    properties = paragraph._p.get_or_add_pPr()
    borders = properties.find(qn("w:pBdr"))
    if borders is None:
        borders = OxmlElement("w:pBdr")
        properties.append(borders)
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), "7F7F7F")
    borders.append(bottom)


def _add_signature_table(
    document: Document,
    values: Sequence[tuple[object, object]],
) -> None:
    table = document.add_table(rows=len(values), cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    table.autofit = False
    table.columns[0].width = Cm(3.2)
    table.columns[1].width = Cm(9.5)
    for row_index, (label, value) in enumerate(values):
        row = table.rows[row_index]
        row_properties = row._tr.get_or_add_trPr()
        cannot_split = OxmlElement("w:cantSplit")
        row_properties.append(cannot_split)
        for column_index, content in enumerate((label, value)):
            cell = row.cells[column_index]
            cell.width = Cm(3.2 if column_index == 0 else 9.5)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_before = Pt(6)
            paragraph.paragraph_format.space_after = Pt(6)
            run = paragraph.add_run(str(content))
            run.font.size = Pt(10)
            if column_index == 0:
                run.bold = True


def _cover_date(batch: Mapping[str, Any]) -> str:
    value = batch.get("finished_utc") or batch.get("created_utc")
    local_date = _parse_datetime(value).astimezone()
    return f"{local_date.year} 年 {local_date.month} 月 {local_date.day} 日"


def _network_endpoint(host: object, port: object) -> str:
    host_text = str(host or "").strip()
    port_text = str(port or "").strip()
    if not host_text:
        return "未配置"
    return f"{host_text}:{port_text}" if port_text else host_text


def _environment_value(value: object, unit: str) -> str:
    if value is None or str(value).strip() == "":
        return "未记录"
    return f"{value} {unit}"


def _format_utc_offset(value: object) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[+-]\d{4}", text):
        return f"{text[:3]}:{text[3:]}"
    return text or "未配置"


def _add_key_value_table(document: Document, values, caption: str = "") -> None:
    _spacer(document)
    rows = [("项目", "内容"), *((str(key), str(value)) for key, value in values)]
    _add_table(document, rows, widths=(4.2, 12.2))
    _TABLE_COUNTER["n"] += 1
    cap = document.add_paragraph(f"表 {_TABLE_COUNTER['n']}  {caption or '测试记录'}")
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in cap.runs:
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(89, 89, 89)
    _spacer(document)


def _add_table(
    document: Document,
    rows: Sequence[Sequence[object]],
    *,
    widths: Sequence[float] = (),
) -> None:
    if not rows:
        return
    table = document.add_table(rows=len(rows), cols=len(rows[0]))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    table.autofit = not bool(widths)
    for row_index, row in enumerate(rows):
        row_properties = table.rows[row_index]._tr.get_or_add_trPr()
        cannot_split = OxmlElement("w:cantSplit")
        row_properties.append(cannot_split)
        if row_index == 0:
            repeat_header = OxmlElement("w:tblHeader")
            repeat_header.set(qn("w:val"), "true")
            row_properties.append(repeat_header)
        for column_index, value in enumerate(row):
            cell = table.cell(row_index, column_index)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            cell.text = str(value)
            if widths and column_index < len(widths):
                cell.width = Cm(widths[column_index])
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_after = Pt(0)
                for run in paragraph.runs:
                    run.font.size = Pt(8)
                    if row_index == 0:
                        run.bold = True
            if row_index == 0:
                shading = OxmlElement("w:shd")
                shading.set(qn("w:fill"), "D9E2F3")
                cell._tc.get_or_add_tcPr().append(shading)


def _parse_datetime(value: object) -> datetime:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _time_range(batch: Mapping[str, Any]) -> str:
    return f"{batch.get('started_utc') or '未记录'} ～ {batch.get('finished_utc') or '未记录'}"


def _pretty_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _json_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _first_text(root: Path, names: Sequence[str]) -> str:
    for name in names:
        direct = root / name
        if direct.is_file():
            return _read_text_file(direct)
        matches = tuple(root.glob(f"**/{name}"))
        if matches:
            return _read_text_file(matches[0])
    return _json_line({"status": "unavailable"})


def _first_text_or_csv_jsonl(
    root: Path,
    text_names: Sequence[str],
    csv_names: Sequence[str],
) -> str:
    text = _first_text(root, text_names)
    if text != _json_line({"status": "unavailable"}):
        return text
    for name in csv_names:
        matches = (root / name, *sorted(root.glob(f"**/{name}")))
        for path in matches:
            if not path.is_file():
                continue
            try:
                with path.open("r", encoding="utf-8", newline="") as stream:
                    rows = list(csv.DictReader(stream))
            except (OSError, UnicodeError, csv.Error):
                continue
            if rows:
                return "".join(_json_line(row) for row in rows)
    return text


def _first_json(root: Path, names: Sequence[str]) -> dict[str, Any]:
    for name in names:
        direct = root / name
        if direct.is_file():
            return _read_json_file(direct)
        matches = tuple(root.glob(f"**/{name}"))
        if matches:
            return _read_json_file(matches[0])
    return {"status": "unavailable"}


def _fixture_lines(actions: Sequence[Mapping[str, Any]], keyword: str) -> str:
    selected = [
        item for item in actions if keyword in str(item.get("action_type", "")).lower()
    ]
    if not selected:
        return _json_line({"status": "unavailable"})
    return "".join(_json_line(item) for item in selected)


def _report_exception_events(
    events: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    markers = (
        "fail",
        "error",
        "disconnect",
        "gap",
        "abort",
        "warning",
        "timeout",
        "conflict",
        "missing",
        "protect",
        "incomplete",
    )
    selected = []
    for event in events:
        event_type = str(event.get("event_type", "")).lower()
        payload = event.get("payload_json", {})
        payload_values = (
            " ".join(str(value) for value in payload.values()).lower()
            if isinstance(payload, Mapping)
            else str(payload).lower()
        )
        has_explicit_failure = any(
            value in payload_values.split()
            for value in ("fail", "failed", "error", "aborted", "incomplete")
        )
        if any(marker in event_type for marker in markers) or has_explicit_failure:
            selected.append(event)
    return selected


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _zip_entry_hashes(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    with zipfile.ZipFile(path, "r") as archive:
        for name in archive.namelist():
            result[name] = hashlib.sha256(archive.read(name)).hexdigest()
    return result


def _atomic_write_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


__all__ = [
    "ProductionReportService",
    "ReportArtifact",
    "ReportBranding",
    "automatic_device_verdict",
    "sanitize_path_component",
]
