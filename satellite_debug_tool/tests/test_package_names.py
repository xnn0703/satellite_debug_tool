"""M53：构建、归档与冻结启动共用 Manager 文件名称。"""

import json
from pathlib import Path

import pytest

from satellite_debug_tool.edition import APP_ID
from satellite_debug_tool.package_names import (
    APPLICATION_EXECUTABLE, WINDOWS_EXECUTABLE, edition_directory, release_archive_name,
)


@pytest.mark.parametrize("edition,suffix", [("user", ""), ("ops", "Ops")])
@pytest.mark.parametrize("system,arch,expected,extension", [
    ("win", "AMD64", "windows-x86_64", "7z"),
    ("mac", "aarch64", "macos-arm64", "zip"),
])
def test_manager_names_preserve_trusted_identity(edition, suffix, system, arch, expected, extension):
    assert APPLICATION_EXECUTABLE == "SatelliteTerminalManager"
    assert WINDOWS_EXECUTABLE == "SatelliteTerminalManager.exe"
    assert edition_directory(edition) == f"SatelliteTerminalManager{suffix}"
    assert release_archive_name(edition=edition, platform=system, architecture=arch,
        version="v1.9.0", extension=extension) == (
        f"satellite_terminal_manager-{'ops-' if edition == 'ops' else ''}{expected}-v1.9.0.{extension}")
    assert APP_ID == "satellite-terminal-tool"


@pytest.mark.parametrize("edition,suffix", [("user", ""), ("ops", "Ops")])
@pytest.mark.parametrize("system", ["darwin", "win32"])
def test_frozen_smoke_starts_manager_edition_executable(tmp_path, monkeypatch, edition, suffix, system):
    from scripts import check_frozen_application as smoke

    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    monkeypatch.setattr(smoke.sys, "platform", system)
    monkeypatch.setattr(smoke.sys, "argv", ["smoke", "--edition", edition, "--version", "1.9.0"])
    folder = f"SatelliteTerminalManager{suffix}"
    executable = (tmp_path / "dist" / edition / f"{folder}.app" / "Contents/MacOS/SatelliteTerminalManager"
                  if system == "darwin" else tmp_path / "dist" / edition / folder / "SatelliteTerminalManager.exe")

    class Process:
        def __init__(self, arguments, **kwargs):
            assert Path(arguments[0]) == executable
            result = tmp_path / "release" / f"smoke-{edition}.json"
            result.write_text(json.dumps({"edition": edition, "version": "1.9.0",
                "overview_panels": "PASS", "shell_dialog": "PASS",
                "qpa_platform": kwargs["env"].get("QT_QPA_PLATFORM", ""),
                "application_update_policy": "manual" if edition == "user" else "online",
                "application_update_modules": []}))

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(smoke.subprocess, "Popen", Process)
    smoke.main()
