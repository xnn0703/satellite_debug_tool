"""Resolve user-overridden and bundled 3D device models."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Callable, Optional, TypeVar


_MODEL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_BUILTIN_MODELS_DIR = Path(__file__).resolve().parent / "assets" / "models"

T = TypeVar("T")


def normalize_model_key(hw_type: str) -> str:
    """Return a safe lowercase model key, or an empty string when invalid."""
    key = str(hw_type or "").strip().lower()
    return key if _MODEL_KEY_RE.fullmatch(key) else ""


def device_model_candidates(
    hw_type: str,
    *,
    home: Optional[Path] = None,
    builtin_dir: Optional[Path] = None,
) -> tuple[Path, ...]:
    """Return model candidates in user-override then bundled order."""
    key = normalize_model_key(hw_type)
    if not key:
        return ()

    user_home = Path.home() if home is None else Path(home)
    packaged_models = _BUILTIN_MODELS_DIR if builtin_dir is None else Path(builtin_dir)
    filename = f"{key}.stl"
    candidates = (
        user_home / ".satellite_debug_tool" / "models" / filename,
        packaged_models / filename,
    )
    if key == "afd01a":
        return candidates + (
            user_home / ".satellite_debug_tool" / "models" / "afd01.stl",
            packaged_models / "afd01.stl",
        )
    return candidates


def load_first_device_model(
    hw_type: str,
    loader: Callable[[Path], T],
    *,
    home: Optional[Path] = None,
    builtin_dir: Optional[Path] = None,
) -> Optional[tuple[Path, T]]:
    """Load the first valid model, continuing after an invalid override."""
    for path in device_model_candidates(
        hw_type,
        home=home,
        builtin_dir=builtin_dir,
    ):
        if not path.is_file():
            continue
        try:
            return path, loader(path)
        except Exception:
            continue
    return None
