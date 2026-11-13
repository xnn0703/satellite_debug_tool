"""应用安装目录的单一平台合同。"""
from __future__ import annotations

import json
from pathlib import Path
import plistlib
import subprocess
import sys

from satellite_debug_tool.package_names import WINDOWS_EXECUTABLE


def installation_platform(root: Path) -> str:
    return "macos" if Path(root).suffix == ".app" else "windows"


def identity_path(root: Path, platform: str | None = None) -> Path:
    """返回冻结应用中唯一的构建身份路径。"""
    root = Path(root)
    selected = installation_platform(root) if platform is None else platform
    if selected == "macos":
        # PyInstaller BUNDLE 将 datas 放入 Contents/Resources；MacOS 仅承载可执行文件。
        return root / "Contents/Resources/satellite_debug_tool/build_identity.json"
    if selected == "windows":
        return root / "_internal/satellite_debug_tool/build_identity.json"
    raise ValueError(f"unsupported installation platform: {selected}")


def main_executable(root: Path) -> Path:
    root = Path(root)
    if installation_platform(root) == "windows":
        return root / WINDOWS_EXECUTABLE
    info = plistlib.loads((root / "Contents/Info.plist").read_bytes())
    name = info.get("CFBundleExecutable")
    if not isinstance(name, str) or not name:
        raise ValueError("macOS application requires CFBundleExecutable")
    return root / "Contents/MacOS" / name


def validate_macos_bundle(root: Path, *, version: str = "") -> None:
    root = Path(root)
    if not (root / "Contents/Info.plist").is_file():
        raise ValueError("macOS payload requires one complete application bundle")
    info = plistlib.loads((root / "Contents/Info.plist").read_bytes())
    executable = info.get("CFBundleExecutable")
    if not isinstance(executable, str) or not (root / "Contents/MacOS" / executable).is_file():
        raise ValueError("macOS payload requires its declared executable")
    if info.get("CFBundleIdentifier") != "com.satellite-debug-tool.desktop":
        raise ValueError("macOS bundle identifier differs from operations edition")
    if version and str(info.get("CFBundleShortVersionString", "")).lstrip("v") != version.lstrip("v"):
        raise ValueError("macOS bundle version differs from the requested release")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(root)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def installed_identity(root: Path) -> dict:
    data = json.loads(identity_path(root).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("installed build identity requires an object")
    return data


def host_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    raise ValueError("automatic installation requires Windows or macOS")
