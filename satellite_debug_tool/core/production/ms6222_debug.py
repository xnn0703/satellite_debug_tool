"""Independent MS-6222 ownership and crash-tolerant engineering evidence."""

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

from .ms6222_protocol import Ms6222FrameEnvelope
from .ms6222_worker import Ms6222WorkerStatistics


class Ms6222DebugError(RuntimeError):
    pass


class Ms6222Conclusion(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class Ms6222LeaseHandle:
    token: str
    owner: str


class Ms6222ControlLease:
    """Process-local single owner for the physical MS-6222 serial stream."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handle: Optional[Ms6222LeaseHandle] = None

    def acquire(self, owner: str) -> Ms6222LeaseHandle:
        owner_name = str(owner).strip()
        if not owner_name:
            raise Ms6222DebugError("MS-6222 owner is required")
        with self._lock:
            if self._handle is not None:
                raise Ms6222DebugError(
                    f"MS-6222 is already controlled by {self._handle.owner}"
                )
            self._handle = Ms6222LeaseHandle(uuid.uuid4().hex, owner_name)
            return self._handle

    def release(self, handle: Optional[Ms6222LeaseHandle]) -> None:
        if handle is None:
            return
        with self._lock:
            if self._handle == handle:
                self._handle = None

    def held_by(self, handle: Optional[Ms6222LeaseHandle]) -> bool:
        with self._lock:
            return handle is not None and self._handle == handle

    @property
    def owner(self) -> str:
        with self._lock:
            return "" if self._handle is None else self._handle.owner


@dataclass(frozen=True)
class Ms6222SessionResult:
    session_id: str
    session_dir: Path
    status: str
    conclusion: Ms6222Conclusion
    file_hashes: Mapping[str, str]


class Ms6222SessionRecorder:
    """Append-only recorder dedicated to one reference-sensor session."""

    HASH_CHUNK_SIZE = 1024 * 1024

    def __init__(
        self,
        *,
        port: str,
        operator: str = "",
        notes: str = "",
        root: Optional[Path] = None,
        session_id: Optional[str] = None,
        queue_size: int = 8192,
    ) -> None:
        if not str(port).strip():
            raise Ms6222DebugError("MS-6222 serial port is required")
        if int(queue_size) <= 0:
            raise Ms6222DebugError("MS-6222 evidence queue size must be positive")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.session_id = session_id or f"{timestamp}-{uuid.uuid4().hex[:8]}"
        self.root = root or (Path.home() / ".satellite_debug_tool" / "ms6222_sessions")
        self.session_dir = self.root / self.session_id
        self.port = str(port).strip()
        self.operator = str(operator)
        self.notes = str(notes)
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=int(queue_size))
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._started = False
        self._closed = False
        self._writer_error = ""
        self._started_utc = datetime.now(timezone.utc).isoformat()
        self._counts = {"raw": 0, "valid": 0, "invalid": 0, "statistics": 0, "events": 0}

    @property
    def writer_error(self) -> str:
        return self._writer_error

    def start(self) -> Path:
        if self._started:
            raise Ms6222DebugError("MS-6222 session is already started")
        if self.session_dir.exists():
            raise Ms6222DebugError("MS-6222 session directory already exists")
        self.session_dir.mkdir(parents=True)
        (self.session_dir / "configuration.json").write_text(
            json.dumps(
                {
                    "schema": "satellite.ms6222-debug-configuration",
                    "schema_version": 1,
                    "port": self.port,
                    "baudrate": 460800,
                    "bytesize": 8,
                    "parity": "N",
                    "stopbits": 1,
                    "operator": self.operator,
                    "initial_notes": self.notes,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
        self._write_manifest("incomplete", Ms6222Conclusion.INCONCLUSIVE, {})
        self._thread = threading.Thread(
            target=self._writer_main,
            name=f"ms6222-rec-{self.session_id}",
            daemon=True,
        )
        self._started = True
        self._thread.start()
        if not self._ready.wait(5.0):
            raise Ms6222DebugError("MS-6222 session writer did not start")
        if self._writer_error:
            raise Ms6222DebugError(self._writer_error)
        return self.session_dir

    def record_frame(self, envelope: Ms6222FrameEnvelope) -> None:
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
        self._enqueue("frame", payload)
        self._counts["raw"] += 1
        self._counts["valid" if envelope.valid else "invalid"] += 1

    def record_statistics(self, statistics: Ms6222WorkerStatistics) -> None:
        self._enqueue("statistics", asdict(statistics))
        self._counts["statistics"] += 1

    def record_event(self, event_type: str, message: str) -> None:
        import time

        self._enqueue(
            "event",
            {
                "host_monotonic_ns": time.monotonic_ns(),
                "host_wall_time_ns": time.time_ns(),
                "event_type": str(event_type),
                "message": str(message),
            },
        )
        self._counts["events"] += 1

    def complete(
        self,
        *,
        conclusion: Ms6222Conclusion | str,
        notes: str,
        statistics: Optional[Ms6222WorkerStatistics],
    ) -> Ms6222SessionResult:
        return self._finish(
            "complete",
            Ms6222Conclusion(conclusion),
            str(notes),
            statistics,
        )

    def abort(
        self,
        *,
        reason: str,
        statistics: Optional[Ms6222WorkerStatistics],
    ) -> Ms6222SessionResult:
        return self._finish(
            "incomplete",
            Ms6222Conclusion.INCONCLUSIVE,
            str(reason),
            statistics,
        )

    def _finish(
        self,
        status: str,
        conclusion: Ms6222Conclusion,
        notes: str,
        statistics: Optional[Ms6222WorkerStatistics],
    ) -> Ms6222SessionResult:
        if not self._started or self._closed:
            raise Ms6222DebugError("MS-6222 session is not active")
        self._closed = True
        try:
            self._queue.put(("stop", None), timeout=10.0)
        except queue.Full:
            self._writer_error = "MS-6222 evidence queue did not drain"
        thread = self._thread
        if thread is not None:
            thread.join(10.0)
            if thread.is_alive():
                raise Ms6222DebugError("MS-6222 session writer did not stop")
        if self._writer_error:
            status = "incomplete"
            conclusion = Ms6222Conclusion.INCONCLUSIVE
        summary = {
            "schema": "satellite.ms6222-debug-summary",
            "schema_version": 1,
            "session_id": self.session_id,
            "result_class": "ENGINEERING_ONLY",
            "status": status,
            "conclusion": conclusion.value,
            "notes": notes,
            "counts": dict(self._counts),
            "writer_error": self._writer_error,
            "final_statistics": None if statistics is None else asdict(statistics),
        }
        (self.session_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        hashes = self._hash_files()
        self._write_manifest(status, conclusion, hashes)
        return Ms6222SessionResult(
            self.session_id, self.session_dir, status, conclusion, hashes
        )

    def _enqueue(self, kind: str, payload: Any) -> None:
        if not self._started or self._closed:
            raise Ms6222DebugError("MS-6222 session is not active")
        if self._writer_error:
            raise Ms6222DebugError(self._writer_error)
        try:
            self._queue.put_nowait((kind, payload))
        except queue.Full as exc:
            self._writer_error = "MS-6222 evidence queue is full"
            raise Ms6222DebugError(self._writer_error) from exc

    def _writer_main(self) -> None:
        try:
            with (
                (self.session_dir / "raw_frames.jsonl").open("a", encoding="utf-8") as raw,
                (self.session_dir / "parsed_frames.csv").open("a", encoding="utf-8", newline="") as parsed_file,
                (self.session_dir / "statistics.jsonl").open("a", encoding="utf-8") as stats,
                (self.session_dir / "events.jsonl").open("a", encoding="utf-8") as events,
            ):
                parsed = csv.writer(parsed_file)
                parsed.writerow(("host_monotonic_ns", "host_wall_time_ns", "frame_type", "protocol_version", "gps_time_s", "record_json"))
                self._ready.set()
                while True:
                    kind, payload = self._queue.get()
                    if kind == "stop":
                        break
                    if kind == "frame":
                        raw.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
                        if payload["valid"]:
                            parsed.writerow(
                                (
                                    payload["host_monotonic_ns"],
                                    payload["host_wall_time_ns"],
                                    payload["frame_type"],
                                    payload["protocol_version"],
                                    payload["gps_time_s"],
                                    json.dumps(payload["record"], ensure_ascii=False, sort_keys=True),
                                )
                            )
                    elif kind == "statistics":
                        stats.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
                    elif kind == "event":
                        events.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
                for stream in (raw, parsed_file, stats, events):
                    stream.flush()
        except Exception as exc:
            self._writer_error = f"MS-6222 evidence writer failed: {exc}"
            self._ready.set()

    def _hash_files(self) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for path in sorted(self.session_dir.iterdir()):
            if not path.is_file() or path.name == "manifest.json":
                continue
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(self.HASH_CHUNK_SIZE):
                    digest.update(chunk)
            hashes[path.name] = digest.hexdigest()
        return hashes

    def _write_manifest(
        self,
        status: str,
        conclusion: Ms6222Conclusion,
        hashes: Mapping[str, str],
    ) -> None:
        payload = {
            "schema": "satellite.ms6222-debug-session",
            "schema_version": 1,
            "session_id": self.session_id,
            "result_class": "ENGINEERING_ONLY",
            "status": status,
            "conclusion": conclusion.value,
            "started_utc": self._started_utc,
            "finished_utc": None if status == "incomplete" and not hashes else datetime.now(timezone.utc).isoformat(),
            "files": dict(hashes),
        }
        temporary = self.session_dir / ".manifest.json.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.session_dir / "manifest.json")


__all__ = [
    "Ms6222Conclusion",
    "Ms6222ControlLease",
    "Ms6222DebugError",
    "Ms6222LeaseHandle",
    "Ms6222SessionRecorder",
    "Ms6222SessionResult",
]
