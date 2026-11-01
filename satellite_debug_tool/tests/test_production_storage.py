from datetime import date
from pathlib import Path

import pytest

from satellite_debug_tool.core.production.storage import resolve_production_storage


class _Settings:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_follow_recording_directory_uses_automatic_test_date(tmp_path: Path) -> None:
    result = resolve_production_storage(
        _Settings({
            "production.storage_mode": "follow_recording",
            "paths.recording_dir": str(tmp_path / "recordings"),
        }),
        day=date(2026, 11, 1),
    )

    assert result.mode == "follow_recording"
    assert result.root == (tmp_path / "recordings" / "自动化测试" / "2026-11-01").resolve()


def test_legacy_custom_root_is_preserved(tmp_path: Path) -> None:
    result = resolve_production_storage(
        _Settings({
            "production.storage_mode": "",
            "paths.recording_dir": str(tmp_path / "recordings"),
            "paths.production_dir": str(tmp_path / "existing-production"),
        })
    )

    assert result.mode == "custom_root"
    assert result.migration_source == "legacy_custom_root"


def test_follow_mode_requires_recording_directory() -> None:
    with pytest.raises(ValueError, match="recording directory"):
        resolve_production_storage(_Settings({"production.storage_mode": "follow_recording"}))
