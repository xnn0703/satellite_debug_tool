"""Final Windows release archive layout validator regressions."""

from __future__ import annotations

from pathlib import PurePosixPath

import json
import pytest
import py7zr

from scripts.verify_update_archive import find_payload_roots


def test_flat_archive_payload_uses_manager_executable() -> None:
    roots = find_payload_roots(
        [
            "SatelliteTerminalManager.exe",
            "updater.exe",
            "_internal/runtime.bin",
        ]
    )

    assert roots == (PurePosixPath("."),)


def test_previous_product_executable_requires_manual_full_install() -> None:
    assert find_payload_roots([
        "SatelliteDebugTool.exe", "updater.exe", "_internal/runtime.bin",
    ]) == ()


def test_manager_edition_wrapper_is_readable() -> None:
    roots = find_payload_roots(
        [
            "SatelliteTerminalManagerOps/SatelliteTerminalManager.exe",
            "SatelliteTerminalManagerOps/updater.exe",
            "SatelliteTerminalManagerOps/_internal/runtime.bin",
        ]
    )

    assert roots == (PurePosixPath("SatelliteTerminalManagerOps"),)


def test_incomplete_payload_is_rejected() -> None:
    assert find_payload_roots(["SatelliteTerminalManager.exe", "_internal/runtime.bin"]) == ()


def test_user_manual_payload_has_no_updater():
    names = ["SatelliteTerminalManager.exe", "_internal/runtime.bin"]
    assert find_payload_roots(names, edition="user") == (PurePosixPath("."),)
    assert find_payload_roots(names, edition="ops") == ()


def _archive(tmp_path, *, edition="user", extra=""):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "SatelliteTerminalManager.exe").write_bytes(b"application")
    identity = payload / "_internal/satellite_debug_tool/build_identity.json"
    identity.parent.mkdir(parents=True)
    identity.write_text(json.dumps({"edition": edition, "platform": "win", "version": "1.8.1"}))
    if extra:
        (payload / extra).write_bytes(b"update")
    archive = tmp_path / "manual.7z"
    with py7zr.SevenZipFile(archive, "w") as output:
        for path in payload.rglob("*"):
            if path.is_file():
                output.write(path, str(path.relative_to(payload)))
    return archive


def test_user_manual_archive_checks_identity_without_updater(tmp_path):
    from scripts.verify_update_archive import verify_archive

    archive = _archive(tmp_path)
    assert verify_archive(archive, edition="user", version="1.8.1") == PurePosixPath(".")
    with pytest.raises(ValueError, match="no complete"):
        verify_archive(archive, edition="ops", version="1.8.1")
    with pytest.raises(ValueError, match="version"):
        verify_archive(archive, edition="user", version="1.8.2")


@pytest.mark.parametrize("extra", ["updater.exe", "release.config.json"])
def test_user_manual_archive_rejects_online_update_files(tmp_path, extra):
    from scripts.verify_update_archive import verify_archive

    with pytest.raises(ValueError, match="application update files"):
        verify_archive(_archive(tmp_path, extra=extra), edition="user", version="1.8.1")
