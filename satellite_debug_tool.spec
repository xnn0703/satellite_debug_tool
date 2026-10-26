# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — Satellite Terminal Tool 主程序。

构建：
    pyinstaller --noconfirm satellite_debug_tool.spec

产物：
    dist/SatelliteDebugTool/              （onedir 模式；mac 下同时产出 .app bundle）
        SatelliteDebugTool(.exe)
        _internal/ ...
"""

from pathlib import Path
import re
import sys
import os
import json
from satellite_debug_tool.edition import Edition

block_cipher = None
APP_NAME = "SatelliteDebugTool"
ROOT = Path(SPECPATH)
sys.path.insert(0, str(ROOT))
from scripts.edition_policy import operations_modules
ICON_DIR = ROOT / "satellite_debug_tool" / "ui" / "assets" / "icons"
EDITION = Edition(os.environ.get("SATELLITE_BUILD_EDITION", "ops"))
USER = EDITION is Edition.USER
ENTRY = str(ROOT / "satellite_debug_tool" / ("main_user.py" if USER else "main.py"))
BUNDLE_NAME = "SatelliteTerminalToolUser" if USER else APP_NAME
COLLECT_NAME = "SatelliteTerminalToolUser" if USER and sys.platform == "win32" else APP_NAME
OPERATIONS_MODULES = (
    "satellite_debug_tool.main", "satellite_debug_tool.ui.main_window",
    "satellite_debug_tool.ui.production_workspace", "satellite_debug_tool.ui.fixture_debug_workspace",
    "satellite_debug_tool.ui.customer_fixture_dialog",
    "satellite_debug_tool.ui.device_view",
    "satellite_debug_tool.ui.engineering_session_host", "satellite_debug_tool.ui.playback_view",
    "satellite_debug_tool.ui.map_widget",
    "satellite_debug_tool.ui.log_view", "satellite_debug_tool.ui.tracking_simulator_view",
    "satellite_debug_tool.ui.customer_iperf_view", "satellite_debug_tool.ui.power_supply_debug_workspace",
    "satellite_debug_tool.ui.external_power_window", "satellite_debug_tool.ui.ms6222_debug_workspace",
    "satellite_debug_tool.ui.channel_panel", "satellite_debug_tool.ui.control_panel_widget",
    "satellite_debug_tool.ui.dashboard_widget", "satellite_debug_tool.ui.event_timeline_widget",
    "satellite_debug_tool.ui.state_panel_widget", "satellite_debug_tool.ui.status_strip_widget",
    "satellite_debug_tool.core.external_power_monitor", "satellite_debug_tool.core.iperf_test",
    "satellite_debug_tool.core.motion_platform",
)


OPERATIONS_MODULES += operations_modules(ROOT)


def included_module(name):
    if "tests" in name.split("."):
        return False
    if USER:
        if any(name == item or name.startswith(item + ".") for item in OPERATIONS_MODULES):
            return False
        if name.startswith("satellite_debug_tool.core.production."):
            return name == "satellite_debug_tool.core.production.fixture_telemetry"
    return True


# 从 satellite_debug_tool/__init__.py 读取 __version__（单一来源）
_init_src = (ROOT / "satellite_debug_tool" / "__init__.py").read_text(encoding="utf-8")
_m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', _init_src)
APP_VERSION = _m.group(1) if _m else "0.0.0"
print(f"[spec] APP_VERSION = {APP_VERSION}")

# PySide6 / pyqtgraph / numpy 的所有数据文件、插件、dylib 一次性打进去，
# 否则运行时会随机缺 Qt plugin / pyqtgraph shader 导致启动失败。
from PyInstaller.utils.hooks import collect_all, collect_submodules

pyside6_datas, pyside6_binaries, pyside6_hidden = ([], [], []) if USER else collect_all("PySide6")
pyqtgraph_datas, pyqtgraph_binaries, pyqtgraph_hidden = collect_all("pyqtgraph")
# M11 fix: PyOpenGL（pyqtgraph.opengl 3D 姿态显示需要），全包确保 GLU/GL 子模块齐
opengl_datas, opengl_binaries, opengl_hidden = collect_all("OpenGL")
docx_datas, docx_binaries, docx_hidden = ([], [], []) if USER else collect_all("docx")
# AESGCM 在固件包路径内延迟导入；显式收集其绑定和后端，避免 Windows 冻结包遗漏。
cryptography_datas, cryptography_binaries, cryptography_hidden = collect_all("cryptography")

# ``collect_all("PySide6")`` also discovers standalone Qt development tools
# (Designer, Assistant and Linguist). They are not runtime dependencies, and
# some contain cross-bundle symlinks that prevent strict signing of the
# customer application. QtWebEngineProcess.app is the only nested application
# required at runtime.
def _is_unused_pyside_app(entry):
    source, destination = (str(value) for value in entry)
    combined = f"{source}/{destination}"
    return ".app/" in combined and "QtWebEngineProcess.app/" not in combined


pyside6_datas = [
    entry
    for entry in pyside6_datas
    if not _is_unused_pyside_app(entry)
]
pyside6_binaries = [
    entry
    for entry in pyside6_binaries
    if not _is_unused_pyside_app(entry)
]

# 本项目包本身：tests 目录不要进发行包
extra_hidden = collect_submodules(
    "satellite_debug_tool",
    filter=included_module,
)
identity_dir = ROOT / "build" / "identity" / EDITION.value
identity_dir.mkdir(parents=True, exist_ok=True)
identity_path = identity_dir / "build_identity.json"
identity_path.write_text(json.dumps({"edition": EDITION.value, "version": APP_VERSION,
                                    "platform": "mac" if sys.platform == "darwin" else "win"}), encoding="utf-8")
app_datas = [
    (str(identity_path), "satellite_debug_tool"),
    (
        str(ROOT / "satellite_debug_tool" / "i18n" / "translations"),
        "satellite_debug_tool/i18n/translations",
    ),
    (
        str(ROOT / "satellite_debug_tool" / "ui" / "assets"),
        "satellite_debug_tool/ui/assets",
    ),
    (
        str(ROOT / "satellite_debug_tool" / "resources" / "firmware_encryption_keys.json"),
        "satellite_debug_tool/resources",
    ),
]
if USER:
    app_datas = [entry for entry in app_datas if entry[1] != "satellite_debug_tool/ui/assets"]
    app_datas.extend((str(ROOT / "satellite_debug_tool/ui/assets" / name),
                      "satellite_debug_tool/ui/assets/" + name) for name in ("icons", "models", "fonts"))
if sys.platform == "darwin":
    app_datas.extend([
        (
            str(
                ROOT
                / "satellite_debug_tool"
                / "platform"
                / "macos"
                / "en.lproj"
            ),
            "en.lproj",
        ),
        (
            str(
                ROOT
                / "satellite_debug_tool"
                / "platform"
                / "macos"
                / "zh_CN.lproj"
            ),
            "zh_CN.lproj",
        ),
    ])

a = Analysis(
    [ENTRY],
    pathex=[str(ROOT)],
    binaries=(
        pyside6_binaries
        + pyqtgraph_binaries
        + opengl_binaries
        + docx_binaries
        + cryptography_binaries
    ),
    datas=(
        pyside6_datas
        + pyqtgraph_datas
        + opengl_datas
        + docx_datas
        + cryptography_datas
        + app_datas
        + [
            # M11：升级器需要知道去哪个仓库拉版本
            (str(ROOT / "release.config.json"), "."),
        ]
    ),
    hiddenimports=(
        pyside6_hidden
        + pyqtgraph_hidden
        + opengl_hidden
        + docx_hidden
        + cryptography_hidden
        + extra_hidden
        + [
            "serial.tools.list_ports",
        ]
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=(list(OPERATIONS_MODULES) + ["docx", "lxml", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick"] if USER else []) + [
        "tkinter",
        "matplotlib",
        "test",
        "unittest",
        "pytest",
        "IPython",
        "jupyter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                   # GUI，不要显示终端
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_DIR / "app_icon.ico") if sys.platform == "win32" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=COLLECT_NAME,
)

# macOS：同步生成 .app bundle，方便双击
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{BUNDLE_NAME}.app",
        icon=str(ICON_DIR / "app_icon.icns"),
        bundle_identifier="com.satellite-debug-tool.user" if USER else "com.satellite-debug-tool.desktop",
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": "Satellite Terminal Tool",
            "CFBundleDevelopmentRegion": "en",
            "CFBundleLocalizations": ["en", "zh_CN"],
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            # 允许应用读写用户目录下的 ~/.satellite_debug_tool/
            "NSDesktopFolderUsageDescription": "Save recordings and profile cache.",
            "NSDocumentsFolderUsageDescription": "Save recordings and profile cache.",
        },
    )
