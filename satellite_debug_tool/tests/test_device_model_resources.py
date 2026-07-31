"""Built-in device model resolution and release-resource checks."""

from __future__ import annotations

import hashlib
from pathlib import Path

from satellite_debug_tool.ui.device_model_resources import (
    device_model_candidates,
    load_first_device_model,
    normalize_model_key,
)


ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = ROOT / "satellite_debug_tool" / "ui" / "assets" / "models"
EXPECTED_SHA256 = "f58706d3b6fa1ac67f67fea50b8c9c4aa10b50716c8aedab8ef529290f0d5b37"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_builtin_models_are_present_and_identical():
    afd01 = MODELS_DIR / "afd01.stl"
    esa01 = MODELS_DIR / "esa01.stl"
    assert afd01.stat().st_size == 8_066_184
    assert esa01.stat().st_size == 8_066_184
    assert _sha256(afd01) == EXPECTED_SHA256
    assert _sha256(esa01) == EXPECTED_SHA256


def test_model_candidates_prefer_user_override(tmp_path):
    candidates = device_model_candidates(
        "AFD01",
        home=tmp_path,
        builtin_dir=MODELS_DIR,
    )
    assert candidates == (
        tmp_path / ".satellite_debug_tool" / "models" / "afd01.stl",
        MODELS_DIR / "afd01.stl",
    )


def test_valid_user_model_wins(tmp_path):
    user_model = tmp_path / ".satellite_debug_tool" / "models" / "afd01.stl"
    user_model.parent.mkdir(parents=True)
    user_model.write_bytes(b"user")

    loaded = load_first_device_model(
        "afd01",
        lambda path: path.read_bytes(),
        home=tmp_path,
        builtin_dir=MODELS_DIR,
    )
    assert loaded == (user_model, b"user")


def test_invalid_user_model_falls_back_to_builtin(tmp_path):
    user_model = tmp_path / ".satellite_debug_tool" / "models" / "esa01.stl"
    user_model.parent.mkdir(parents=True)
    user_model.write_bytes(b"broken")

    def loader(path: Path) -> str:
        if path == user_model:
            raise ValueError("invalid STL")
        return path.name

    loaded = load_first_device_model(
        "esa01",
        loader,
        home=tmp_path,
        builtin_dir=MODELS_DIR,
    )
    assert loaded == (MODELS_DIR / "esa01.stl", "esa01.stl")


def test_invalid_model_key_is_rejected(tmp_path):
    assert normalize_model_key("../afd01") == ""
    assert device_model_candidates("../afd01", home=tmp_path) == ()
