"""Authoritative production storage selection and legacy migration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol


class _SettingsLike(Protocol):
    def get(self, key: str, default=None): ...


@dataclass(frozen=True)
class ProductionStorageSelection:
    mode: str
    root: Path
    migration_source: str


def resolve_production_storage(
    settings: _SettingsLike,
    *,
    day: date | None = None,
) -> ProductionStorageSelection:
    """Resolve the root used by the next batch without mutating active batches."""

    configured_mode = str(settings.get("production.storage_mode", "") or "").strip()
    recording = str(settings.get("paths.recording_dir", "") or "").strip()
    legacy = str(settings.get("paths.production_dir", "") or "").strip()
    legacy_path = Path(legacy).expanduser() if legacy else None
    old_default = Path.home() / "SatelliteProduction"

    if configured_mode == "custom_root":
        if legacy_path is None:
            raise ValueError("custom production root is not configured")
        return ProductionStorageSelection("custom_root", legacy_path.resolve(), "explicit")
    if configured_mode not in {"", "follow_recording"}:
        raise ValueError(f"unsupported production storage mode: {configured_mode}")

    if configured_mode == "" and legacy_path is not None and legacy_path.resolve() != old_default.resolve():
        return ProductionStorageSelection("custom_root", legacy_path.resolve(), "legacy_custom_root")
    if not recording:
        raise ValueError("recording directory is not configured")
    target_day = day or date.today()
    root = Path(recording).expanduser().resolve() / "自动化测试" / target_day.isoformat()
    source = "legacy_default" if configured_mode == "" else "explicit"
    return ProductionStorageSelection("follow_recording", root, source)


__all__ = ["ProductionStorageSelection", "resolve_production_storage"]
