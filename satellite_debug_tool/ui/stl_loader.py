"""STL 模型加载器 —— 解析 binary / ASCII STL 为 pyqtgraph MeshData 用的顶点+面。

仅依赖 numpy。binary STL：80B 头 + uint32 三角数 + 每三角 50B（法线3f + 顶点9f + 2B attr）。
ASCII STL：`facet normal ... outer loop vertex×3 endloop endfacet`。

公开 API：
    load_stl(path) -> (verts: (N,3) f4, faces: (M,3) u4)
    normalize_mesh(verts, target_size, up_axis, nose_axis, nose_sign, left_sign) -> verts'
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Tuple

import numpy as np


def _is_binary_stl(path: Path) -> bool:
    """判断 binary 还是 ASCII：按文件大小是否匹配 binary 公式（更可靠，
    因为 binary 头也可能以 'solid' 开头）。"""
    size = path.stat().st_size
    if size < 84:
        return False
    with open(path, "rb") as f:
        f.seek(80)
        n = struct.unpack("<I", f.read(4))[0]
    return size == 84 + n * 50


def _load_binary_stl(path: Path) -> Tuple[np.ndarray, np.ndarray]:
    with open(path, "rb") as f:
        f.read(80)
        n = struct.unpack("<I", f.read(4))[0]
        rec = np.dtype([("normal", "<3f4"), ("v", "<9f4"), ("attr", "<u2")])
        tris = np.fromfile(f, dtype=rec, count=n)
    verts = tris["v"].reshape(-1, 3).astype(np.float32)        # (3n, 3)
    faces = np.arange(verts.shape[0], dtype=np.uint32).reshape(-1, 3)
    return verts, faces


def _load_ascii_stl(path: Path) -> Tuple[np.ndarray, np.ndarray]:
    coords = []
    with open(path, "r", errors="replace") as f:
        for line in f:
            s = line.strip()
            if s.startswith("vertex"):
                parts = s.split()
                coords.append((float(parts[1]), float(parts[2]), float(parts[3])))
    verts = np.asarray(coords, dtype=np.float32)
    faces = np.arange(verts.shape[0], dtype=np.uint32).reshape(-1, 3)
    return verts, faces


def load_stl(path) -> Tuple[np.ndarray, np.ndarray]:
    """加载 STL → (verts (N,3) f4, faces (M,3) u4)。自动判别 binary / ASCII。"""
    path = Path(path)
    if _is_binary_stl(path):
        return _load_binary_stl(path)
    return _load_ascii_stl(path)


def normalize_mesh(
    verts: np.ndarray,
    target_size: float = 3.6,
    up_axis: int = 2,
    nose_axis: int = 0,
    nose_sign: float = 1.0,
    left_sign: float = 1.0,
) -> np.ndarray:
    """把任意 STL 顶点居中 + 缩放 + 轴向重排，对齐 widget 的 FLU 视觉系。

    widget mesh 约定：+X 机头 / +Y 左翼 / +Z 天顶。STL 原始轴需映射过去。

    Args:
        verts:       原始顶点 (N,3)
        target_size: 缩放后最长边的目标尺寸（默认 3.6，对齐旧占位模型宽度）
        up_axis:     STL 中"厚度/朝天"对应的轴 index（0=X 1=Y 2=Z）→ 映射到 widget +Z
        nose_axis:   STL 中"机头方向"对应的轴 index → 映射到 widget +X
        nose_sign:   机头方向正负（+1/-1）
        left_sign:   剩余轴映射到 widget +Y（左侧）时的方向正负（+1/-1）
    """
    v = verts.astype(np.float64)
    # 居中
    v -= (v.min(0) + v.max(0)) * 0.5
    # 缩放：最长边 → target_size
    extent = (v.max(0) - v.min(0))
    scale = target_size / max(extent.max(), 1e-9)
    v *= scale

    # 轴重排：构造 3×3 置换，把 STL 轴搬到 widget (X=机头, Y=左翼, Z=天顶)
    axes = [0, 1, 2]
    third = [a for a in axes if a not in (up_axis, nose_axis)]
    left_axis = third[0] if third else 1
    out = np.empty_like(v)
    out[:, 0] = nose_sign * v[:, nose_axis]   # widget +X = 机头
    out[:, 1] = left_sign * v[:, left_axis]   # widget +Y = 左翼
    out[:, 2] = v[:, up_axis]                  # widget +Z = 天顶
    return out.astype(np.float32)
