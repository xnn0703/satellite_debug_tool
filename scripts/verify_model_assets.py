#!/usr/bin/env python3
"""Verify that a frozen application contains both built-in device models."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys


EXPECTED_MODELS = {
    "afd01.stl": "f58706d3b6fa1ac67f67fea50b8c9c4aa10b50716c8aedab8ef529290f0d5b37",
    "esa01.stl": "f58706d3b6fa1ac67f67fea50b8c9c4aa10b50716c8aedab8ef529290f0d5b37",
}
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
    for filename, expected_hash in EXPECTED_MODELS.items():
        matches = [
            path
            for path in package_root.rglob(filename)
            if _matches_expected_location(path)
        ]
        if not matches:
            raise ValueError(f"missing packaged model: {filename}")
        for path in matches:
            actual_hash = _sha256(path)
            if actual_hash != expected_hash:
                raise ValueError(
                    f"model hash mismatch: {path} "
                    f"(expected {expected_hash}, got {actual_hash})"
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
