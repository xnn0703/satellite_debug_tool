"""包内可信更新源配置；部署信息缺失时保持明确的待配置状态。"""
from __future__ import annotations

from copy import deepcopy
import json
import sys
from pathlib import Path
from typing import Optional


DEFAULTS = {
    "schema": "satellite.update-config/v1",
    "app_id": "satellite-terminal-tool",
    "repo_id": "satellite-terminal-tool-production",
    "channel": "stable",
    "metadata_base_url": "",
    "target_base_url": "",
    "trusted_root": None,
}


def _candidate_paths() -> list[Path]:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        return [Path(meipass or Path(sys.executable).parent) / "release.config.json"]
    return [Path(__file__).resolve().parent.parent / "release.config.json"]


def load_release_config(override_path: Optional[Path] = None) -> dict:
    """加载固定包内源；畸形文件保留错误，不改用其他信任源。"""
    paths = [override_path] if override_path is not None else _candidate_paths()
    for p in paths:
        if p is None or not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != DEFAULTS["schema"]:
                raise ValueError("release configuration requires satellite.update-config/v1")
            return data
        except (ValueError, OSError) as exc:
            return {**deepcopy(DEFAULTS), "configuration_error": str(exc)}
    return deepcopy(DEFAULTS)
