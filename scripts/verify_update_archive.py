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


def verify_archive(path: Path, *, edition: str | None = None, version: str = "") -> PurePosixPath:
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
    if edition is not None:
        import tempfile
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from satellite_debug_tool.edition import Edition, validate_payload_identity
        identity = roots[0] / "_internal/satellite_debug_tool/build_identity.json"
        with tempfile.TemporaryDirectory(prefix="sdt-edition-verify-") as temporary:
            with py7zr.SevenZipFile(archive, mode="r") as seven_zip:
                seven_zip.extract(path=temporary, targets=[str(identity)])
            validate_payload_identity(Path(temporary) / roots[0], Edition(edition), version)
    return roots[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--edition", choices=("user", "ops"))
    parser.add_argument("--version", default="")
    args = parser.parse_args()
    try:
        root = verify_archive(args.archive, edition=args.edition, version=args.version)
    except (OSError, ValueError, py7zr.Bad7zFile) as exc:
        print(f"update archive validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"update archive payload: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
