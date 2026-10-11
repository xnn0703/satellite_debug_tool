"""Append-only evidence for one raw TCP shell / YMODEM session (M37)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Mapping, Optional
import time
import uuid


class TcpShellRecorderError(RuntimeError):
    pass


class TcpShellSessionRecorder:
    """Single-writer session evidence; the socket worker owns all writes."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        managed_session: bool,
        operator: str = "",
        root: Optional[Path] = None,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.managed_session = bool(managed_session)
        self.operator = str(operator)
        self.root = root or (Path.home() / ".satellite_debug_tool" / "tcp_shell_sessions")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.session_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        self.session_dir = self.root / self.session_id
        self._started = False
        self._closed = False
        self._event_sequence = 0

    def start(self) -> Path:
        if self._started:
            raise TcpShellRecorderError("tcp shell session is already started")
        self.session_dir.mkdir(parents=True, exist_ok=False)
        self._write_json(
            "config.json",
            {
                "result_class": "ENGINEERING_ONLY",
                "session_id": self.session_id,
                "operator": self.operator,
                "created_utc": _utc_now(),
                "host": self.host,
                "port": self.port,
                "managed_session": self.managed_session,
            },
        )
        self._write_manifest(status="incomplete", final_state="connected")
        self._started = True
        self.record_event("session_started", {})
        return self.session_dir

    def record_event(self, event_type: str, details: Mapping[str, object]) -> None:
        self._require_open()
        self._event_sequence += 1
        self._append_jsonl(
            "events.jsonl",
            {
                "sequence": self._event_sequence,
                "wall_time_utc": _utc_now(),
                "monotonic_ns": time.monotonic_ns(),
                "event_type": str(event_type),
                "details": dict(details),
            },
        )

    def record_transcript(self, direction: str, data: bytes) -> None:
        self._require_open()
        if direction not in ("tx", "rx"):
            raise TcpShellRecorderError(f"invalid transcript direction: {direction!r}")
        self._append_jsonl(
            "transcript.jsonl",
            {
                "wall_time_utc": _utc_now(),
                "monotonic_ns": time.monotonic_ns(),
                "direction": direction,
                "length": len(data),
                "hex": data.hex(),
            },
        )

    def record_ymodem(self, record: Mapping[str, object]) -> None:
        self._require_open()
        payload = dict(record)
        payload.setdefault("wall_time_utc", _utc_now())
        payload.setdefault("monotonic_ns", time.monotonic_ns())
        self._append_jsonl("ymodem_records.jsonl", payload)

    def finalize(
        self,
        *,
        status: str,
        final_state: str,
        incomplete_reason: str = "",
        ymodem: Optional[Mapping[str, object]] = None,
    ) -> None:
        if not self._started or self._closed:
            return
        self.record_event(
            "session_finished",
            {
                "final_state": str(final_state),
                "incomplete_reason": str(incomplete_reason),
            },
        )
        summary: dict[str, object] = {
            "session_id": self.session_id,
            "result_class": "ENGINEERING_ONLY",
            "status": str(status),
            "final_state": str(final_state),
            "incomplete_reason": str(incomplete_reason),
            "managed_session": self.managed_session,
            "host": self.host,
            "port": self.port,
            "finished_utc": _utc_now(),
        }
        if ymodem:
            summary["ymodem"] = dict(ymodem)
        self._write_json("summary.json", summary)
        self._write_manifest(status=str(status), final_state=str(final_state))
        self._closed = True

    def _write_manifest(self, *, status: str, final_state: str) -> None:
        self._write_json(
            "manifest.json",
            {
                "schema": "satellite-debug-tool/tcp-shell-session",
                "schema_version": 1,
                "session_id": self.session_id,
                "result_class": "ENGINEERING_ONLY",
                "status": str(status),
                "final_state": str(final_state),
                "updated_utc": _utc_now(),
            },
        )

    def _append_jsonl(self, name: str, payload: Mapping[str, object]) -> None:
        path = self.session_dir / name
        with path.open("a", encoding="utf-8") as file_object:
            file_object.write(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
            )
            file_object.flush()

    def _write_json(self, name: str, payload: Mapping[str, object]) -> None:
        path = self.session_dir / name
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _require_open(self) -> None:
        if not self._started or self._closed:
            raise TcpShellRecorderError("tcp shell session is not open")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["TcpShellRecorderError", "TcpShellSessionRecorder"]
