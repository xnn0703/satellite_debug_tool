"""Crash-tolerant engineering session recording for fixture diagnostics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import csv
import hashlib
import json
from pathlib import Path
import queue
import threading
from typing import Any, Mapping, Optional
import uuid
import zipfile

from .fixture_analysis import (
    AttitudeComparison,
    FixtureCalibration,
    TimedAttitude,
    iter_attitude_comparisons,
)
from .fixture_profile import WorkstationFixtureProfile
from .motion_platform import PlatformSendResult
from .ms6222_protocol import Ms6222FrameEnvelope


class FixtureSessionError(RuntimeError):
    pass


class FixtureSessionConclusion(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class FixtureSessionResult:
    session_id: str
    session_dir: Path
    status: str
    conclusion: FixtureSessionConclusion
    file_hashes: Mapping[str, str]


class FixtureSessionRecorder:
    """Append-only recorder with one dedicated file-writer thread."""

    DEFAULT_QUEUE_SIZE = 8192
    HASH_CHUNK_SIZE = 1024 * 1024

    def __init__(
        self,
        profile: WorkstationFixtureProfile,
        *,
        operator: str = "",
        root: Optional[Path] = None,
        session_id: Optional[str] = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        profile.validate()
        if int(queue_size) <= 0:
            raise FixtureSessionError("fixture evidence queue size must be positive")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.session_id = session_id or f"{timestamp}-{uuid.uuid4().hex[:8]}"
        self.root = root or (Path.home() / ".satellite_debug_tool" / "fixture_sessions")
        self.session_dir = self.root / self.session_id
        self.profile = profile
        self.operator = str(operator)
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(
            maxsize=int(queue_size)
        )
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._started = False
        self._closed = False
        self._writer_error = ""
        self._started_utc = datetime.now(timezone.utc).isoformat()
        self._counts = {
            "commands": 0,
            "ms_raw": 0,
            "ms_valid": 0,
            "ms_invalid": 0,
            "targets": 0,
            "measurements": 0,
            "comparisons": 0,
            "events": 0,
        }

    @property
    def writer_error(self) -> str:
        return self._writer_error

    @property
    def queue_capacity(self) -> int:
        return self._queue.maxsize

    @property
    def pending_writes(self) -> int:
        return self._queue.qsize()

    def start(self) -> Path:
        if self._started:
            raise FixtureSessionError("fixture session recorder is already started")
        if self.session_dir.exists():
            raise FixtureSessionError("fixture session directory already exists")
        self.session_dir.mkdir(parents=True)
        (self.session_dir / "fixture_profile.json").write_text(
            json.dumps(
                self.profile.to_payload(include_hash=True),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self._write_manifest(status="incomplete", conclusion=FixtureSessionConclusion.INCONCLUSIVE)
        self._thread = threading.Thread(
            target=self._writer_main,
            name=f"fixture-rec-{self.session_id}",
            daemon=True,
        )
        self._started = True
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise FixtureSessionError("fixture session writer did not start")
        if self._writer_error:
            raise FixtureSessionError(self._writer_error)
        return self.session_dir

    def record_command(
        self,
        result: PlatformSendResult,
        *,
        action: str,
        sequence: Optional[int] = None,
    ) -> None:
        self._enqueue(
            "command",
            {
                "action": str(action),
                "sequence": sequence,
                "host_monotonic_ns": result.monotonic_ns,
                "planned_completion_monotonic_ns": (
                    result.monotonic_ns
                    + int(result.duration_ms or 0) * 1_000_000
                ),
                "sent": result.sent,
                "duration_ms": result.duration_ms,
                "logical_pose": asdict(result.logical_pose),
                "command_pose": asdict(result.command_pose),
                "raw_command_ascii": result.raw_command.decode("ascii", errors="replace"),
                "evidence_level": result.evidence_level.value,
                "error": result.error,
            },
        )
        self._counts["commands"] += 1

    def record_ms_frame(self, envelope: Ms6222FrameEnvelope) -> None:
        payload = {
            "host_monotonic_ns": envelope.host_monotonic_ns,
            "host_wall_time_ns": envelope.host_wall_time_ns,
            "frame_type": envelope.frame_type.value,
            "protocol_version": envelope.protocol_version,
            "crc_valid": envelope.crc_valid,
            "valid": envelope.valid,
            "gps_time_s": envelope.gps_time_s,
            "error": envelope.error,
            "raw_hex": envelope.raw.hex(),
            "record": asdict(envelope.record) if envelope.record is not None else None,
        }
        self._enqueue("ms", payload)
        self._counts["ms_raw"] += 1
        self._counts["ms_valid" if envelope.valid else "ms_invalid"] += 1

    def record_comparison(self, comparison: AttitudeComparison) -> None:
        self._enqueue("comparison", asdict(comparison))
        self._counts["comparisons"] += 1

    def record_target(self, target: TimedAttitude) -> None:
        self._enqueue("target", asdict(target))
        self._counts["targets"] += 1

    def record_measurement(self, measurement: TimedAttitude) -> None:
        self._enqueue("measurement", asdict(measurement))
        self._counts["measurements"] += 1

    def record_comparisons(
        self,
        comparisons: tuple[AttitudeComparison, ...],
    ) -> None:
        for comparison in comparisons:
            self.record_comparison(comparison)

    def flush(self, *, timeout_s: float = 10.0) -> None:
        """Wait until all evidence submitted before this call is durable."""
        if not self._started or self._closed:
            raise FixtureSessionError("fixture session recorder is not active")
        if self._writer_error:
            raise FixtureSessionError(self._writer_error)
        completed = threading.Event()
        try:
            self._queue.put(("flush", completed), timeout=max(0.1, float(timeout_s)))
        except queue.Full as exc:
            self._set_writer_error("fixture evidence queue is full")
            raise FixtureSessionError(self._writer_error) from exc
        if not completed.wait(timeout=max(0.1, float(timeout_s))):
            self._set_writer_error("fixture evidence flush timed out")
            raise FixtureSessionError(self._writer_error)
        if self._writer_error:
            raise FixtureSessionError(self._writer_error)

    def load_comparison_sample(
        self,
        *,
        max_samples: int = 120_000,
    ) -> tuple[AttitudeComparison, ...]:
        """Load a deterministic bounded sample from the persisted comparison CSV."""
        if int(max_samples) <= 0:
            raise FixtureSessionError("comparison sample limit must be positive")
        self.flush()
        total = int(self._counts["comparisons"])
        stride = max(1, (total + int(max_samples) - 1) // int(max_samples))
        path = self.session_dir / "attitude_comparison.csv"
        sampled: list[AttitudeComparison] = []
        last: Optional[AttitudeComparison] = None
        with path.open("r", encoding="utf-8", newline="") as file_object:
            for index, row in enumerate(csv.DictReader(file_object)):
                current = _comparison_from_csv(row)
                last = current
                if index % stride == 0:
                    sampled.append(current)
        if last is not None and (not sampled or sampled[-1] != last):
            if len(sampled) >= int(max_samples):
                sampled[-1] = last
            else:
                sampled.append(last)
        return tuple(sampled)

    def generate_comparison_sample(
        self,
        *,
        calibration: FixtureCalibration,
        max_target_gap_s: float,
        max_samples: int = 120_000,
    ) -> tuple[AttitudeComparison, ...]:
        """Generate full comparison evidence while retaining a bounded metric sample."""
        if int(max_samples) <= 0:
            raise FixtureSessionError("comparison sample limit must be positive")
        self.flush()
        sampled: list[AttitudeComparison] = []
        stride = 1
        comparison_count = 0
        latest: Optional[AttitudeComparison] = None
        for comparison in iter_attitude_comparisons(
            self._iter_attitudes("target_attitude.csv"),
            self._iter_attitudes("measurement_attitude.csv"),
            calibration=calibration,
            max_target_gap_s=max_target_gap_s,
        ):
            self._enqueue("comparison", asdict(comparison), wait=True)
            self._counts["comparisons"] += 1
            latest = comparison
            if comparison_count % stride == 0:
                sampled.append(comparison)
            comparison_count += 1
            if len(sampled) > int(max_samples):
                sampled = sampled[::2]
                stride *= 2
        self.flush()
        if latest is not None and (not sampled or sampled[-1] != latest):
            if len(sampled) >= int(max_samples):
                sampled[-1] = latest
            else:
                sampled.append(latest)
        return tuple(sampled)

    def _iter_attitudes(self, filename: str):
        path = self.session_dir / filename
        with path.open("r", encoding="utf-8", newline="") as file_object:
            for row in csv.DictReader(file_object):
                yield TimedAttitude(
                    monotonic_ns=int(row["monotonic_ns"]),
                    roll_deg=float(row["roll_deg"]),
                    pitch_deg=float(row["pitch_deg"]),
                    yaw_deg=float(row["yaw_deg"]),
                    valid=str(row["valid"]).strip().lower() in {"1", "true"},
                )

    def record_event(
        self,
        event_type: str,
        message: str,
        *,
        monotonic_ns: Optional[int] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        import time

        self._enqueue(
            "event",
            {
                "host_monotonic_ns": time.monotonic_ns()
                if monotonic_ns is None
                else int(monotonic_ns),
                "host_wall_time_ns": time.time_ns(),
                "event_type": str(event_type),
                "message": str(message),
                "details": dict(details or {}),
            },
        )
        self._counts["events"] += 1

    def complete(
        self,
        *,
        conclusion: FixtureSessionConclusion | str,
        notes: str,
        summary: Optional[Mapping[str, Any]] = None,
    ) -> FixtureSessionResult:
        return self._finish(
            status="complete",
            conclusion=FixtureSessionConclusion(conclusion),
            notes=notes,
            summary=summary,
        )

    def abort(
        self,
        *,
        reason: str,
        summary: Optional[Mapping[str, Any]] = None,
    ) -> FixtureSessionResult:
        payload = dict(summary or {})
        payload["abort_reason"] = str(reason)
        return self._finish(
            status="incomplete",
            conclusion=FixtureSessionConclusion.INCONCLUSIVE,
            notes=str(reason),
            summary=payload,
        )

    def _finish(
        self,
        *,
        status: str,
        conclusion: FixtureSessionConclusion,
        notes: str,
        summary: Optional[Mapping[str, Any]],
    ) -> FixtureSessionResult:
        if not self._started:
            raise FixtureSessionError("fixture session recorder is not started")
        if self._closed:
            raise FixtureSessionError("fixture session recorder is already closed")
        self._closed = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            try:
                self._queue.put(("stop", None), timeout=10.0)
            except queue.Full:
                self._set_writer_error("fixture evidence queue did not drain")
        if thread is not None:
            thread.join(timeout=10.0)
            if thread.is_alive():
                raise FixtureSessionError("fixture session writer did not stop")
        if self._writer_error:
            status = "incomplete"
            conclusion = FixtureSessionConclusion.INCONCLUSIVE
        summary_payload = {
            "schema": "satellite.fixture-session-summary",
            "schema_version": 1,
            "session_id": self.session_id,
            "result_class": "ENGINEERING_ONLY",
            "status": status,
            "conclusion": conclusion.value,
            "notes": str(notes),
            "counts": dict(self._counts),
            "writer_error": self._writer_error,
            "metrics": dict(summary or {}),
        }
        (self.session_dir / "summary.json").write_text(
            json.dumps(summary_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        hashes = self._file_hashes()
        self._write_manifest(status=status, conclusion=conclusion, file_hashes=hashes)
        return FixtureSessionResult(
            session_id=self.session_id,
            session_dir=self.session_dir,
            status=status,
            conclusion=conclusion,
            file_hashes=hashes,
        )

    def _enqueue(self, kind: str, payload: Any, *, wait: bool = False) -> None:
        if not self._started or self._closed:
            raise FixtureSessionError("fixture session recorder is not active")
        if self._writer_error:
            raise FixtureSessionError(self._writer_error)
        try:
            if wait:
                self._queue.put((kind, payload), timeout=10.0)
            else:
                self._queue.put_nowait((kind, payload))
        except queue.Full as exc:
            self._set_writer_error("fixture evidence queue is full")
            raise FixtureSessionError(self._writer_error) from exc

    def _set_writer_error(self, details: str) -> None:
        if not self._writer_error:
            self._writer_error = str(details)

    def _writer_main(self) -> None:
        try:
            with (
                (self.session_dir / "platform_commands.jsonl").open(
                    "a", encoding="utf-8", buffering=1
                ) as command_file,
                (self.session_dir / "ms6222_raw.jsonl").open(
                    "a", encoding="utf-8", buffering=1
                ) as raw_file,
                (self.session_dir / "ms6222_parsed.csv").open(
                    "a", encoding="utf-8", newline="", buffering=1
                ) as parsed_file,
                (self.session_dir / "attitude_comparison.csv").open(
                    "a", encoding="utf-8", newline="", buffering=1
                ) as comparison_file,
                (self.session_dir / "target_attitude.csv").open(
                    "a", encoding="utf-8", newline="", buffering=1
                ) as target_file,
                (self.session_dir / "measurement_attitude.csv").open(
                    "a", encoding="utf-8", newline="", buffering=1
                ) as measurement_file,
                (self.session_dir / "events.jsonl").open(
                    "a", encoding="utf-8", buffering=1
                ) as event_file,
            ):
                parsed_writer = csv.DictWriter(
                    parsed_file,
                    fieldnames=(
                        "host_monotonic_ns",
                        "host_wall_time_ns",
                        "frame_type",
                        "protocol_version",
                        "gps_time_s",
                        "record_json",
                    ),
                )
                parsed_writer.writeheader()
                comparison_fields = tuple(AttitudeComparison.__dataclass_fields__)
                comparison_writer = csv.DictWriter(
                    comparison_file,
                    fieldnames=comparison_fields,
                )
                comparison_writer.writeheader()
                attitude_fields = tuple(TimedAttitude.__dataclass_fields__)
                target_writer = csv.DictWriter(target_file, fieldnames=attitude_fields)
                measurement_writer = csv.DictWriter(
                    measurement_file,
                    fieldnames=attitude_fields,
                )
                target_writer.writeheader()
                measurement_writer.writeheader()
                self._ready.set()
                while True:
                    kind, payload = self._queue.get()
                    if kind == "stop":
                        break
                    if kind == "command":
                        _write_json_line(command_file, payload)
                    elif kind == "ms":
                        _write_json_line(raw_file, payload)
                        if payload["valid"] and payload["record"] is not None:
                            parsed_writer.writerow(
                                {
                                    "host_monotonic_ns": payload["host_monotonic_ns"],
                                    "host_wall_time_ns": payload["host_wall_time_ns"],
                                    "frame_type": payload["frame_type"],
                                    "protocol_version": payload["protocol_version"],
                                    "gps_time_s": payload["gps_time_s"],
                                    "record_json": json.dumps(
                                        payload["record"],
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    ),
                                }
                            )
                    elif kind == "comparison":
                        comparison_writer.writerow(payload)
                    elif kind == "target":
                        target_writer.writerow(payload)
                    elif kind == "measurement":
                        measurement_writer.writerow(payload)
                    elif kind == "event":
                        _write_json_line(event_file, payload)
                    elif kind == "flush":
                        for file_object in (
                            command_file,
                            raw_file,
                            parsed_file,
                            comparison_file,
                            target_file,
                            measurement_file,
                            event_file,
                        ):
                            file_object.flush()
                        payload.set()
        except Exception as exc:  # keep the incomplete manifest for field recovery
            self._set_writer_error(str(exc))
            self._ready.set()

    def _write_manifest(
        self,
        *,
        status: str,
        conclusion: FixtureSessionConclusion,
        file_hashes: Optional[Mapping[str, str]] = None,
    ) -> None:
        payload = {
            "schema": "satellite.fixture-session",
            "schema_version": 1,
            "session_id": self.session_id,
            "result_class": "ENGINEERING_ONLY",
            "status": status,
            "conclusion": conclusion.value,
            "operator": self.operator,
            "started_utc": self._started_utc,
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "fixture_profile_id": self.profile.profile_id,
            "fixture_profile_revision": self.profile.revision,
            "fixture_profile_sha256": self.profile.sha256,
            "timing_mode": "HOST_ARRIVAL_ONLY_NO_PPS",
            "file_sha256": dict(file_hashes or {}),
        }
        path = self.session_dir / "manifest.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)

    def _file_hashes(self) -> dict[str, str]:
        hashes = {}
        for path in sorted(self.session_dir.iterdir()):
            if not path.is_file() or path.name == "manifest.json":
                continue
            digest = hashlib.sha256()
            with path.open("rb") as file_object:
                while chunk := file_object.read(self.HASH_CHUNK_SIZE):
                    digest.update(chunk)
            hashes[path.name] = digest.hexdigest()
        return hashes


def _write_json_line(file_object, payload: Mapping[str, Any]) -> None:
    file_object.write(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    )


def _comparison_from_csv(row: Mapping[str, str]) -> AttitudeComparison:
    return AttitudeComparison(
        monotonic_ns=int(row["monotonic_ns"]),
        target_roll_deg=float(row["target_roll_deg"]),
        target_pitch_deg=float(row["target_pitch_deg"]),
        target_yaw_deg=float(row["target_yaw_deg"]),
        measured_roll_deg=float(row["measured_roll_deg"]),
        measured_pitch_deg=float(row["measured_pitch_deg"]),
        measured_yaw_deg=float(row["measured_yaw_deg"]),
        error_roll_deg=float(row["error_roll_deg"]),
        error_pitch_deg=float(row["error_pitch_deg"]),
        error_yaw_deg=float(row["error_yaw_deg"]),
        error_angle_deg=float(row["error_angle_deg"]),
    )


def create_fixture_session_zip(session_dir: Path) -> Path:
    """Create a self-contained field diagnostic archive beside a finalized session."""
    source = Path(session_dir)
    if not source.is_dir():
        raise FixtureSessionError("fixture session directory does not exist")
    manifest = source / "manifest.json"
    if not manifest.is_file():
        raise FixtureSessionError("fixture session manifest is missing")
    output = source.with_name(f"{source.name}-diagnostics.zip")
    temporary = output.with_suffix(".tmp")
    with zipfile.ZipFile(
        temporary,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source.parent))
    temporary.replace(output)
    return output


__all__ = [
    "FixtureSessionConclusion",
    "FixtureSessionError",
    "FixtureSessionRecorder",
    "FixtureSessionResult",
    "create_fixture_session_zip",
]
