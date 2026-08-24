"""stl_loader 单测：binary / ASCII 解析 + normalize_mesh 轴重排缩放。

解析与归一化单测使用合成小网格，避免依赖较大的内置设备 STL。
"""
from __future__ import annotations

import struct

import numpy as np
import pytest

from satellite_debug_tool.ui.stl_loader import (
    clear_stl_cache,
    load_stl,
    normalize_mesh,
    _is_binary_stl,
)


# 一个"扁平面板"似的两三角矩形：X 长 20，Y 宽 10，Z 厚 2
_TRIS = [
    # (normal, v0, v1, v2)
    ((0, 0, 1), (0, 0, 0), (20, 0, 0), (20, 10, 0)),
    ((0, 0, 1), (0, 0, 2), (20, 10, 2), (0, 10, 2)),
]


def _write_binary_stl(path) -> None:
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)               # 80B 头
        f.write(struct.pack("<I", len(_TRIS)))
        for normal, *verts in _TRIS:
            f.write(struct.pack("<3f", *normal))
            for v in verts:
                f.write(struct.pack("<3f", *v))
            f.write(struct.pack("<H", 0))   # attr byte count


def test_load_stl_reuses_read_only_process_cache(tmp_path, monkeypatch) -> None:
    from satellite_debug_tool.ui import stl_loader

    path = tmp_path / "cached.stl"
    _write_binary_stl(path)
    clear_stl_cache()
    calls = 0
    original = stl_loader._load_binary_stl

    def counted(model_path):
        nonlocal calls
        calls += 1
        return original(model_path)

    monkeypatch.setattr(stl_loader, "_load_binary_stl", counted)
    first = load_stl(path)
    second = load_stl(path)

    assert calls == 1
    assert first[0] is second[0]
    assert first[1] is second[1]
    assert not first[0].flags.writeable
    assert not first[1].flags.writeable
    clear_stl_cache()


def _write_ascii_stl(path) -> None:
    lines = ["solid test"]
    for normal, *verts in _TRIS:
        lines.append("  facet normal %g %g %g" % normal)
        lines.append("    outer loop")
        for v in verts:
            lines.append("      vertex %g %g %g" % v)
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid test")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def test_binary_detect_and_load(tmp_path):
    p = tmp_path / "panel.stl"
    _write_binary_stl(p)
    assert _is_binary_stl(p) is True
    verts, faces = load_stl(p)
    assert verts.shape == (6, 3)            # 2 三角 × 3 顶点
    assert faces.shape == (2, 3)
    assert verts.dtype == np.float32
    # bbox 还原原始尺寸
    np.testing.assert_allclose(verts.min(0), [0, 0, 0], atol=1e-5)
    np.testing.assert_allclose(verts.max(0), [20, 10, 2], atol=1e-5)


def test_ascii_load(tmp_path):
    p = tmp_path / "panel_ascii.stl"
    _write_ascii_stl(p)
    assert _is_binary_stl(p) is False       # 大小不匹配 binary 公式
    verts, faces = load_stl(p)
    assert verts.shape == (6, 3)
    np.testing.assert_allclose(verts.max(0), [20, 10, 2], atol=1e-5)


def test_normalize_centers_and_scales(tmp_path):
    p = tmp_path / "panel.stl"
    _write_binary_stl(p)
    verts, _ = load_stl(p)
    nv = normalize_mesh(verts, target_size=3.6, up_axis=2, nose_axis=0, nose_sign=1.0)
    extent = nv.max(0) - nv.min(0)
    # 最长边（原 X=20）缩到 3.6
    assert extent[0] == pytest.approx(3.6, abs=1e-4)
    # 比例保持：Y=10 → 1.8，Z=2 → 0.36
    assert extent[1] == pytest.approx(1.8, abs=1e-4)
    assert extent[2] == pytest.approx(0.36, abs=1e-4)
    # 居中：bbox 关于原点对称
    np.testing.assert_allclose(nv.min(0), -nv.max(0), atol=1e-5)


def test_normalize_nose_sign_flips(tmp_path):
    p = tmp_path / "panel.stl"
    _write_binary_stl(p)
    verts, _ = load_stl(p)
    pos = normalize_mesh(verts, nose_axis=0, nose_sign=1.0)
    neg = normalize_mesh(verts, nose_axis=0, nose_sign=-1.0)
    # X 轴整体取反
    np.testing.assert_allclose(pos[:, 0], -neg[:, 0], atol=1e-5)


def test_normalize_y_nose_rotates_without_mirroring():
    """AFD01/ESA01 的原始 +Y 机头映射到 +X，原始 +X 映射到右侧 -Y。"""
    probes = np.asarray([
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ], dtype=np.float32)
    out = normalize_mesh(
        probes,
        up_axis=2,
        nose_axis=1,
        nose_sign=1.0,
        left_sign=-1.0,
    )

    raw_x = out[1] - out[0]
    raw_y = out[2] - out[0]
    raw_z = out[3] - out[0]
    assert raw_y[0] > 0.0
    assert raw_x[1] < 0.0
    assert raw_z[2] > 0.0

    transform = np.column_stack((raw_x, raw_y, raw_z))
    assert np.linalg.det(transform) > 0.0
