"""Frozen model-package integrity checks."""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from satellite_debug_tool.model_asset_manifest import ModelAssetIdentity
from scripts import verify_model_assets


ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = ROOT / "satellite_debug_tool" / "ui" / "assets" / "models"


def test_verify_script_resolves_project_package_in_isolated_python() -> None:
    """Windows 构建入口不依赖调用方预设 PYTHONPATH。"""

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / "scripts" / "verify_model_assets.py"),
            str(MODELS_DIR),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_verify_package_consumes_authoritative_model_identity(
    tmp_path, monkeypatch
) -> None:
    payload = tmp_path / "satellite_debug_tool" / "ui" / "assets" / "models"
    payload.mkdir(parents=True)
    model = payload / "fixture.stl"
    model.write_bytes(b"current-model")
    identity = ModelAssetIdentity(
        size=model.stat().st_size,
        sha256=hashlib.sha256(model.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        verify_model_assets,
        "BUILTIN_MODEL_ASSETS",
        {model.name: identity},
    )

    assert verify_model_assets.verify_package(tmp_path) == [model]

    model.write_bytes(b"stale-model")
    with pytest.raises(ValueError, match="model (size|hash) mismatch"):
        verify_model_assets.verify_package(tmp_path)
