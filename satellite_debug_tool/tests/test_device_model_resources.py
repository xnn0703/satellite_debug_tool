"""Built-in device model resolution and release-resource checks."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from pyqtgraph.opengl import MeshData

from satellite_debug_tool.model_asset_manifest import BUILTIN_MODEL_ASSETS
from satellite_debug_tool.ui.device_model_resources import (
    device_model_axes,
    device_model_candidates,
    load_first_device_model,
    normalize_model_key,
)


ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = ROOT / "satellite_debug_tool" / "ui" / "assets" / "models"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_builtin_models_are_present_and_identical():
    afd01 = MODELS_DIR / "afd01.stl"
    esa01 = MODELS_DIR / "esa01.stl"
    assert afd01.stat().st_size == 8_066_184
    assert esa01.stat().st_size == 8_066_184
    assert _sha256(afd01) == BUILTIN_MODEL_ASSETS["afd01.stl"].sha256
    assert _sha256(esa01) == BUILTIN_MODEL_ASSETS["esa01.stl"].sha256


def test_model_asset_manifest_covers_exact_bundled_files():
    """发行校验和源码回归必须消费同一份完整内置模型身份清单。"""

    actual_names = {path.name for path in MODELS_DIR.glob("*.stl")}
    assert actual_names == set(BUILTIN_MODEL_ASSETS)
    for name, identity in BUILTIN_MODEL_ASSETS.items():
        path = MODELS_DIR / name
        assert path.stat().st_size == identity.size
        assert _sha256(path) == identity.sha256


_REAL_DEVICE_MODELS = {
    # 2026-11 实机 STEP（gmsh 网格化导出，二进制 STL）
    "afd01b2.stl": {
        "vertices": 227_700,
        "faces": 75_900,
    },
    "afd01c.stl": {
        "vertices": 155_622,
        "faces": 51_874,
    },
    "afd01d.stl": {
        "vertices": 155_040,
        "faces": 51_680,
    },
}


def test_real_device_models_are_independent_builtins():
    """B2/C/D 各自的实机 STEP 模型：独立 hash、可加载、网格有效。"""
    from satellite_debug_tool.ui.stl_loader import load_stl

    for name, spec in _REAL_DEVICE_MODELS.items():
        path = MODELS_DIR / name
        identity = BUILTIN_MODEL_ASSETS[name]
        assert path.stat().st_size == identity.size
        assert _sha256(path) == identity.sha256
        vertices, faces = load_stl(path)
        assert vertices.shape == (spec["vertices"], 3)
        assert faces.shape == (spec["faces"], 3)
        assert np.isfinite(vertices).all()
        assert np.all(np.ptp(vertices, axis=0) > 0.0)


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


def test_afd01c_uses_its_independent_builtin_model(tmp_path):
    candidates = device_model_candidates(
        "AFD01C",
        home=tmp_path,
        builtin_dir=MODELS_DIR,
    )
    assert candidates == (
        tmp_path / ".satellite_debug_tool" / "models" / "afd01c.stl",
        MODELS_DIR / "afd01c.stl",
    )
    loaded = load_first_device_model(
        "AFD01C",
        lambda path: path.name,
        home=tmp_path,
        builtin_dir=MODELS_DIR,
    )
    assert loaded == (MODELS_DIR / "afd01c.stl", "afd01c.stl")
    assert (
        _sha256(MODELS_DIR / "afd01c.stl")
        == BUILTIN_MODEL_ASSETS["afd01c.stl"].sha256
    )
    assert device_model_axes("AFD01C") == device_model_axes("AFD01D")


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
    """B2 独立模型键保留（实机模型已内置，不再回退占位/其他型号）。"""
    candidates = device_model_candidates(
        "AFD01B2", home=tmp_path, builtin_dir=MODELS_DIR
    )
    assert candidates == (
        tmp_path / ".satellite_debug_tool" / "models" / "afd01b2.stl",
        MODELS_DIR / "afd01b2.stl",
    )
    loaded = load_first_device_model(
        "AFD01B2", lambda path: path.name, home=tmp_path, builtin_dir=MODELS_DIR
    )
    assert loaded == (MODELS_DIR / "afd01b2.stl", "afd01b2.stl")


def test_afd01d_uses_only_its_independent_model_and_axes(tmp_path):
    candidates = device_model_candidates(
        "AFD01D", home=tmp_path, builtin_dir=MODELS_DIR
    )
    assert candidates == (
        tmp_path / ".satellite_debug_tool" / "models" / "afd01d.stl",
        MODELS_DIR / "afd01d.stl",
    )
    loaded = load_first_device_model(
        "AFD01D", lambda path: path.name, home=tmp_path, builtin_dir=MODELS_DIR
    )
    assert loaded == (MODELS_DIR / "afd01d.stl", "afd01d.stl")

    axes = device_model_axes("AFD01D")
    assert (axes.up_axis, axes.nose_axis, axes.nose_sign, axes.left_sign) == (
        1,
        2,
        1.0,
        1.0,
    )


def test_attitude_widget_applies_afd01d_product_axes(qapplication_session) -> None:
    from satellite_debug_tool.ui.attitude_widget import AttitudeWidget

    widget = AttitudeWidget()
    assert widget.try_load_device_model("AFD01D")
    assert widget._loaded_model_hw == "afd01d"
    assert widget._device_verts is not None
    assert np.ptp(widget._device_verts, axis=0) == pytest.approx(
        (2.782136, 3.6, 0.761942), abs=1e-5
    )
    widget.close()


def test_attitude_widget_applies_shared_enclosure_axes_to_afd01c(
    qapplication_session,
) -> None:
    from satellite_debug_tool.ui.attitude_widget import AttitudeWidget

    widget = AttitudeWidget()
    assert widget.try_load_device_model("AFD01C")
    assert widget._loaded_model_hw == "afd01c"
    assert widget._device_verts is not None
    assert np.ptp(widget._device_verts, axis=0) == pytest.approx(
        (2.782136, 3.6, 0.761942), abs=1e-5
    )
    widget.close()


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


def test_afd01b2_has_independent_builtin_model_and_axes(tmp_path):
    """B2 实机模型独立成档；CAD 机头沿 X，与 C/D（沿 Z）轴向约定不同。"""
    candidates = device_model_candidates(
        "AFD01B2",
        home=tmp_path,
        builtin_dir=MODELS_DIR,
    )
    assert candidates == (
        tmp_path / ".satellite_debug_tool" / "models" / "afd01b2.stl",
        MODELS_DIR / "afd01b2.stl",
    )
    assert (
        _sha256(MODELS_DIR / "afd01b2.stl")
        == BUILTIN_MODEL_ASSETS["afd01b2.stl"].sha256
    )
    from satellite_debug_tool.ui.device_model_resources import device_model_axes

    axes = device_model_axes("afd01b2")
    assert (axes.up_axis, axes.nose_axis) == (1, 0)
    assert (axes.up_axis, axes.nose_axis) != (
        device_model_axes("afd01c").up_axis,
        device_model_axes("afd01c").nose_axis,
    )
