"""发行文件名称合同；应用信任身份与持久化路径继续由各自领域拥有。"""

from __future__ import annotations

from satellite_debug_tool.edition import Edition
from satellite_debug_tool.versioning import parse_version


APPLICATION_EXECUTABLE = "SatelliteTerminalManager"
WINDOWS_EXECUTABLE = f"{APPLICATION_EXECUTABLE}.exe"


def edition_directory(edition: str | Edition) -> str:
    return f"{APPLICATION_EXECUTABLE}{'Ops' if Edition(edition) is Edition.OPS else ''}"


def release_archive_name(*, edition: str | Edition, platform: str,
                         architecture: str, version: str, extension: str) -> str:
    """构建与候选归档共用完整、可辨识的平台/架构/版本名称。"""
    edition_suffix = "ops-" if Edition(edition) is Edition.OPS else ""
    system = {"win": "windows", "windows": "windows", "mac": "macos", "macos": "macos"}[platform]
    architecture = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(
        architecture.lower(), architecture.lower())
    if architecture not in ("x86_64", "arm64") or extension not in ("zip", "7z"):
        raise ValueError("archive requires a supported architecture and format")
    version = version.removeprefix("v")
    parse_version(version, complete=True)
    return f"satellite_terminal_manager-{edition_suffix}{system}-{architecture}-v{version}.{extension}"
