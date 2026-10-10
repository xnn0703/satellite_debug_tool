"""Final Windows release archive layout validator regressions."""

from __future__ import annotations

from pathlib import PurePosixPath

from scripts.verify_update_archive import find_payload_roots


def test_flat_archive_payload_is_valid_for_legacy_updater() -> None:
    roots = find_payload_roots(
        [
            "SatelliteDebugTool.exe",
            "updater.exe",
            "_internal/runtime.bin",
        ]
    )

    assert roots == (PurePosixPath("."),)


def test_versioned_wrapper_remains_readable_by_new_updater() -> None:
    roots = find_payload_roots(
        [
            "release/satellite_debug_tool-win-v1.4.0/SatelliteDebugTool.exe",
            "release/satellite_debug_tool-win-v1.4.0/updater.exe",
            "release/satellite_debug_tool-win-v1.4.0/_internal/runtime.bin",
        ]
    )

    assert roots == (PurePosixPath("release/satellite_debug_tool-win-v1.4.0"),)


def test_incomplete_payload_is_rejected() -> None:
    assert find_payload_roots(["SatelliteDebugTool.exe", "_internal/runtime.bin"]) == ()
