"""运动平台组件的自包含工程证据记录。"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
from typing import Any, Mapping, Optional


class MotionEvidenceRecorder:
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False

    def record(self, stream: str, event_type: str, payload: object) -> None:
        if self._closed:
            raise RuntimeError("motion evidence session is closed")
        safe_stream = _safe_stream(stream)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": str(event_type),
            "payload": _json_value(payload),
        }
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            with (self.session_dir / f"{safe_stream}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(line)

    def write_config(self, payload: Mapping[str, object]) -> Path:
        path = self.session_dir / "config.json"
        path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def close(self, *, summary: Optional[Mapping[str, object]] = None) -> Path:
        with self._lock:
            if self._closed:
                return self.session_dir / "manifest.json"
            if summary is not None:
                (self.session_dir / "summary.json").write_text(
                    json.dumps(dict(summary), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            files: list[dict[str, object]] = []
            for path in sorted(self.session_dir.iterdir()):
                if not path.is_file() or path.name == "manifest.json":
                    continue
                raw = path.read_bytes()
                files.append({
                    "path": path.name,
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                })
            manifest = {
                "schema": "satellite.motion-platform-evidence",
                "schema_version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "files": files,
            }
            manifest_path = self.session_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._closed = True
            return manifest_path


def _safe_stream(value: str) -> str:
    normalized = "".join(char for char in str(value) if char.isalnum() or char in "_-.")
    if not normalized:
        raise ValueError("motion evidence stream name is empty")
    return normalized


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, bytes):
        return {"hex": value.hex(), "size": len(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "value"):
        return _json_value(value.value)
    return value


__all__ = ["MotionEvidenceRecorder"]
