"""Persistent update transaction status shared by the app and updater."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


def write_update_status(path: Path, state: str, **details: Any) -> None:
    """Atomically publish one affirmative updater state."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "satellite_debug_tool_update_status/v1",
        "state": str(state),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)


__all__ = ["write_update_status"]
