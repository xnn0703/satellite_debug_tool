"""Engineering-only PSW worker and durable diagnostic evidence."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
from queue import Empty, PriorityQueue
import threading
import time
from typing import Callable, Mapping, Optional, Sequence
import uuid

from PySide6.QtCore import QThread, Signal

from .power_supply import (
    GwInstekPswAdapter,
    PowerCommandRecord,
    PowerEvidenceLevel,
    PowerIdentity,
    PowerSupplyConfig,
    PowerSupplyState,
)


class PowerDebugError(RuntimeError):
    pass


class PowerDebugOperation(str, Enum):
    CONNECT = "connect"
    INSPECT = "inspect"
    PREPARE_OFF = "prepare_off"
    ENABLE = "enable"
    DISABLE = "disable"
    RELEASE_LOCAL = "release_local"
    DISCONNECT = "disconnect"


class PowerSupplyDebugWorker(QThread):
    """Own one adapter and execute all TCP operations serially."""

    operation_started = Signal(str)
    operation_succeeded = Signal(str, object, object, object)
    operation_failed = Signal(str, str, object, object)

    def __init__(
        self,
        config: PowerSupplyConfig,
        *,
        adapter_factory: Callable[[PowerSupplyConfig], GwInstekPswAdapter] = GwInstekPswAdapter,
        parent=None,
    ) -> None:
        super().__init__(parent)
        config.validate()
        self._config = config
        self._adapter_factory = adapter_factory
        self._requests: PriorityQueue[tuple[int, int, PowerDebugOperation, str]] = PriorityQueue()
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._adapter: Optional[GwInstekPswAdapter] = None
        self._record_index = 0

    def submit(
        self,
        operation: PowerDebugOperation | str,
        *,
        action_id: str = "",
    ) -> None:
        value = PowerDebugOperation(operation)
        if self._stop_event.is_set() and value != PowerDebugOperation.DISCONNECT:
            return
        with self._counter_lock:
            self._counter += 1
            sequence = self._counter
        if value == PowerDebugOperation.DISCONNECT:
            priority = -10
        elif value == PowerDebugOperation.DISABLE:
            priority = 0
        else:
            priority = 10
        self._requests.put((priority, sequence, value, str(action_id)))

    def stop(self) -> None:
        self._stop_event.set()
        self.submit(PowerDebugOperation.DISCONNECT)

    def run(self) -> None:
        try:
            while True:
                try:
                    _priority, _sequence, operation, action_id = self._requests.get(
                        timeout=0.1
                    )
                except Empty:
                    if self._stop_event.is_set():
                        break
                    continue
                if (
                    self._stop_event.is_set()
                    and operation != PowerDebugOperation.DISCONNECT
                ):
                    continue
                self.operation_started.emit(operation.value)
                try:
                    payload = self._execute(operation, action_id)
                    state, evidence, records = self._snapshot()
                    self.operation_succeeded.emit(
                        operation.value,
                        payload,
                        (state, evidence),
                        records,
                    )
                except Exception as exc:
                    state, evidence, records = self._snapshot()
                    self.operation_failed.emit(
                        operation.value,
                        f"{type(exc).__name__}: {exc}",
                        (state, evidence),
                        records,
                    )
                if operation == PowerDebugOperation.DISCONNECT:
                    break
        finally:
            adapter = self._adapter
            self._adapter = None
            if adapter is not None:
                adapter.close()

    def _execute(self, operation: PowerDebugOperation, action_id: str):
        if operation == PowerDebugOperation.CONNECT:
            if self._adapter is not None:
                self._adapter.close()
            self._adapter = self._adapter_factory(self._config)
            self._record_index = 0
            return self._adapter.connect()
        adapter = self._require_adapter()
        if operation == PowerDebugOperation.INSPECT:
            return adapter.inspect(fixture_action_id=action_id)
        if operation == PowerDebugOperation.PREPARE_OFF:
            return adapter.prepare_output_off(fixture_action_id=action_id)
        if operation == PowerDebugOperation.ENABLE:
            if adapter.state != PowerSupplyState.READY_OFF:
                adapter.prepare_output_off(fixture_action_id=action_id)
            return adapter.enable_output(fixture_action_id=action_id)
        if operation == PowerDebugOperation.DISABLE:
            return adapter.disable_output(fixture_action_id=action_id)
        if operation == PowerDebugOperation.RELEASE_LOCAL:
            adapter.release_local()
            return None
        if operation == PowerDebugOperation.DISCONNECT:
            adapter.close()
            return None
        raise PowerDebugError(f"unsupported power debug operation: {operation.value}")

    def _require_adapter(self) -> GwInstekPswAdapter:
        if self._adapter is None:
            raise PowerDebugError("power supply is not connected")
        return self._adapter

    def _snapshot(
        self,
    ) -> tuple[PowerSupplyState, PowerEvidenceLevel, tuple[PowerCommandRecord, ...]]:
        adapter = self._adapter
        if adapter is None:
            return PowerSupplyState.DISCONNECTED, PowerEvidenceLevel.NONE, ()
        records = adapter.records
        new_records = records[self._record_index :]
        self._record_index = len(records)
        return adapter.state, adapter.evidence_level, tuple(new_records)


class PowerDebugSessionRecorder:
    """Append-only evidence for one standalone engineering power session."""

    def __init__(
        self,
        config: PowerSupplyConfig,
        *,
        operator: str,
        root: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.operator = str(operator)
        self.root = root or (
            Path.home() / ".satellite_debug_tool" / "power_supply_sessions"
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.session_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        self.session_dir = self.root / self.session_id
        self._started = False
        self._closed = False
        self._event_sequence = 0

    def start(self) -> Path:
        if self._started:
            raise PowerDebugError("power debug session is already started")
        self.session_dir.mkdir(parents=True, exist_ok=False)
        self._write_json(
            "config.json",
            {
                "result_class": "ENGINEERING_ONLY",
                "session_id": self.session_id,
                "operator": self.operator,
                "created_utc": _utc_now(),
                "config": asdict(self.config),
            },
        )
        self._write_manifest(status="incomplete", final_state="disconnected")
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

    def record_commands(self, records: Sequence[PowerCommandRecord]) -> None:
        self._require_open()
        for record in records:
            self._append_jsonl("scpi_records.jsonl", asdict(record))

    def record_identity(self, identity: PowerIdentity) -> None:
        self._require_open()
        self._write_json("identity.json", asdict(identity))

    def finalize(
        self,
        *,
        final_state: PowerSupplyState,
        evidence_level: PowerEvidenceLevel,
        output_preserved: bool,
        incomplete_reason: str = "",
    ) -> None:
        if not self._started or self._closed:
            return
        self.record_event(
            "session_finished",
            {
                "final_state": final_state.value,
                "evidence_level": evidence_level.value,
                "output_preserved": bool(output_preserved),
                "incomplete_reason": str(incomplete_reason),
            },
        )
        status = "incomplete" if incomplete_reason else "complete"
        self._write_json(
            "summary.json",
            {
                "session_id": self.session_id,
                "result_class": "ENGINEERING_ONLY",
                "status": status,
                "final_state": final_state.value,
                "evidence_level": evidence_level.value,
                "output_preserved": bool(output_preserved),
                "incomplete_reason": str(incomplete_reason),
                "finished_utc": _utc_now(),
            },
        )
        self._write_manifest(status=status, final_state=final_state.value)
        self._closed = True

    def _write_manifest(self, *, status: str, final_state: str) -> None:
        self._write_json(
            "manifest.json",
            {
                "schema": "satellite-debug-tool/power-supply-session",
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
            file_object.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
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
            raise PowerDebugError("power debug session is not open")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "PowerDebugError",
    "PowerDebugOperation",
    "PowerDebugSessionRecorder",
    "PowerSupplyDebugWorker",
]
