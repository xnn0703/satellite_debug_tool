"""Verify that a release 7z contains exactly one Windows updater payload."""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import sys

import py7zr


def find_payload_roots(names: list[str]) -> tuple[PurePosixPath, ...]:
    normalized = {PurePosixPath(name.replace("\\", "/")) for name in names}
    candidates: list[PurePosixPath] = []
    for entry in normalized:
        if entry.name != "SatelliteDebugTool.exe":
            continue
        root = entry.parent
        if root / "updater.exe" not in normalized:
            continue
        runtime = root / "_internal"
        if not any(path == runtime or runtime in path.parents for path in normalized):
            continue
        candidates.append(root)
    return tuple(sorted(set(candidates), key=str))


def verify_archive(path: Path) -> PurePosixPath:
    archive = Path(path)
    if not archive.is_file():
        raise ValueError(f"archive does not exist: {archive}")
    with py7zr.SevenZipFile(archive, mode="r") as seven_zip:
        roots = find_payload_roots(seven_zip.getnames())
    if not roots:
        raise ValueError("archive has no complete SatelliteDebugTool payload")
    if len(roots) != 1:
        raise ValueError(
            "archive has multiple SatelliteDebugTool payloads: "
            + ", ".join(str(root) for root in roots)
        )
    return roots[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        root = verify_archive(args.archive)
    except (OSError, ValueError, py7zr.Bad7zFile) as exc:
        print(f"update archive validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"update archive payload: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
