#!/usr/bin/env python3
"""Verify that a frozen application contains every registered built-in model."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys

from satellite_debug_tool.model_asset_manifest import BUILTIN_MODEL_ASSETS

EXPECTED_SUFFIX = ("satellite_debug_tool", "ui", "assets", "models")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matches_expected_location(path: Path) -> bool:
    parent_parts = tuple(part.lower() for part in path.parent.parts)
    return parent_parts[-len(EXPECTED_SUFFIX):] == EXPECTED_SUFFIX


def verify_package(package_root: Path) -> list[Path]:
    """Return verified model paths or raise ValueError on any mismatch."""
    if not package_root.is_dir():
        raise ValueError(f"package root does not exist: {package_root}")

    verified: list[Path] = []
    for filename, identity in BUILTIN_MODEL_ASSETS.items():
        matches = [
            path
            for path in package_root.rglob(filename)
            if _matches_expected_location(path)
        ]
        if not matches:
            raise ValueError(f"missing packaged model: {filename}")
        for path in matches:
            actual_size = path.stat().st_size
            if actual_size != identity.size:
                raise ValueError(
                    f"model size mismatch: {path} "
                    f"(expected {identity.size}, got {actual_size})"
                )
            actual_hash = _sha256(path)
            if actual_hash != identity.sha256:
                raise ValueError(
                    f"model hash mismatch: {path} "
                    f"(expected {identity.sha256}, got {actual_hash})"
                )
            verified.append(path)
    return verified


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package_root", type=Path)
    args = parser.parse_args()
    try:
        verified = verify_package(args.package_root.resolve())
    except ValueError as exc:
        print(f"[models] ERROR: {exc}", file=sys.stderr)
        return 1

    for path in verified:
        print(f"[models] OK: {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
