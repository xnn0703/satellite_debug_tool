"""Built-in device model resolution and release-resource checks."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
from pyqtgraph.opengl import MeshData

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


def test_afd01c_does_not_alias_the_afd01_model(tmp_path):
    candidates = device_model_candidates(
        "AFD01C",
        home=tmp_path,
        builtin_dir=MODELS_DIR,
    )
    assert candidates == (
        tmp_path / ".satellite_debug_tool" / "models" / "afd01c.stl",
        MODELS_DIR / "afd01c.stl",
    )
    assert load_first_device_model(
        "AFD01C",
        lambda path: path.read_bytes(),
        home=tmp_path,
        builtin_dir=MODELS_DIR,
    ) is None


def test_afd01a_prefers_own_override_then_original_builtin(tmp_path):
    candidates = device_model_candidates(
        "AFD01A", home=tmp_path, builtin_dir=MODELS_DIR
    )
    assert candidates == (
        tmp_path / ".satellite_debug_tool" / "models" / "afd01a.stl",
        MODELS_DIR / "afd01a.stl",
        tmp_path / ".satellite_debug_tool" / "models" / "afd01.stl",
        MODELS_DIR / "afd01.stl",
    )
    loaded = load_first_device_model(
        "AFD01A", lambda path: path.name, home=tmp_path, builtin_dir=MODELS_DIR
    )
    assert loaded == (MODELS_DIR / "afd01.stl", "afd01.stl")

    override = candidates[0]
    override.parent.mkdir(parents=True)
    override.write_bytes(b"afd01a-specific-model")
    loaded = load_first_device_model(
        "AFD01A", lambda path: path.read_bytes(), home=tmp_path, builtin_dir=MODELS_DIR
    )
    assert loaded == (override, b"afd01a-specific-model")

    override.unlink()
    original_override = candidates[2]
    original_override.write_bytes(b"original-afd01-model")
    loaded = load_first_device_model(
        "AFD01A", lambda path: path.read_bytes(), home=tmp_path, builtin_dir=MODELS_DIR
    )
    assert loaded == (original_override, b"original-afd01-model")


def test_afd01b2_keeps_independent_model_key(tmp_path):
    candidates = device_model_candidates(
        "AFD01B2", home=tmp_path, builtin_dir=MODELS_DIR
    )
    assert candidates == (
        tmp_path / ".satellite_debug_tool" / "models" / "afd01b2.stl",
        MODELS_DIR / "afd01b2.stl",
    )
    assert load_first_device_model(
        "AFD01B2", lambda path: path.read_bytes(), home=tmp_path, builtin_dir=MODELS_DIR
    ) is None


def test_model_change_clears_stale_mesh_when_target_has_no_model(
    monkeypatch,
) -> None:
    from satellite_debug_tool.ui import device_model_resources
    from satellite_debug_tool.ui.attitude_widget import AttitudeWidget

    mesh = MeshData(
        vertexes=np.asarray(
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            dtype=np.float32,
        ),
        faces=np.asarray(((0, 1, 2),), dtype=np.uint32),
    )

    def fake_load(model_key, _loader):
        if model_key == "afd01":
            return Path("afd01.stl"), mesh
        return None

    monkeypatch.setattr(
        device_model_resources,
        "load_first_device_model",
        fake_load,
    )
    widget = AttitudeWidget()

    assert widget.try_load_device_model("AFD01")
    afd01_body = widget._body
    assert widget._loaded_model_hw == "afd01"
    assert not widget._nose_arrow.visible()

    assert not widget.try_load_device_model("AFD01C")
    assert widget._loaded_model_hw is None
    assert widget._device_verts is None
    assert widget._device_faces is None
    assert widget._body is not afd01_body
    assert widget._nose_arrow.visible()
    widget.close()
