"""Persistent update transaction status shared by the app and updater."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


def sync_file(path: Path) -> None:
    """副本内容先持久化，再让 journal 引用该文件。"""
    with Path(path).open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """先持久化内容，再原子替换；journal 与启动回执共用同一写入合同。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if os.name != "nt":
            descriptor = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def write_update_status(path: Path, state: str, **details: Any) -> None:
    """Atomically publish one affirmative updater state."""
    payload = {
        "schema": "satellite_debug_tool_update_status/v1",
        "state": str(state),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    write_atomic_json(path, payload)


__all__ = ["sync_file", "write_atomic_json", "write_update_status"]
