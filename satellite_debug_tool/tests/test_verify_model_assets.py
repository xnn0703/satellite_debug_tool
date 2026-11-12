"""Frozen model-package integrity checks."""

from __future__ import annotations

import hashlib

import pytest

from satellite_debug_tool.model_asset_manifest import ModelAssetIdentity
from scripts import verify_model_assets


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
