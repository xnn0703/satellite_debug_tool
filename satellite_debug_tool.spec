# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — Satellite Debug Tool 主程序。

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

block_cipher = None
APP_NAME = "SatelliteDebugTool"
ROOT = Path(SPECPATH)
ENTRY = str(ROOT / "satellite_debug_tool" / "main.py")

# 从 satellite_debug_tool/__init__.py 读取 __version__（单一来源）
_init_src = (ROOT / "satellite_debug_tool" / "__init__.py").read_text(encoding="utf-8")
_m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', _init_src)
APP_VERSION = _m.group(1) if _m else "0.0.0"
print(f"[spec] APP_VERSION = {APP_VERSION}")

# PySide6 / pyqtgraph / numpy 的所有数据文件、插件、dylib 一次性打进去，
# 否则运行时会随机缺 Qt plugin / pyqtgraph shader 导致启动失败。
from PyInstaller.utils.hooks import collect_all, collect_submodules

pyside6_datas, pyside6_binaries, pyside6_hidden = collect_all("PySide6")
pyqtgraph_datas, pyqtgraph_binaries, pyqtgraph_hidden = collect_all("pyqtgraph")

# 本项目包本身：tests 目录不要进发行包
extra_hidden = collect_submodules(
    "satellite_debug_tool",
    filter=lambda name: "tests" not in name.split("."),
)

a = Analysis(
    [ENTRY],
    pathex=[str(ROOT)],
    binaries=pyside6_binaries + pyqtgraph_binaries,
    datas=pyside6_datas + pyqtgraph_datas + [
        # M11：升级器需要知道去哪个仓库拉版本
        (str(ROOT / "release.config.json"), "."),
    ],
    hiddenimports=(
        pyside6_hidden
        + pyqtgraph_hidden
        + extra_hidden
        + [
            "serial.tools.list_ports",
            "OpenGL",              # pyqtgraph 3D 需要
            "OpenGL.GL",
        ]
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
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
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

# macOS：同步生成 .app bundle，方便双击
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=None,
        bundle_identifier="com.softhz.satellite-debug-tool",
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": "卫星调试工具",
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            # 允许应用读写用户目录下的 ~/.satellite_debug_tool/
            "NSDesktopFolderUsageDescription": "保存录制文件与 profile 缓存",
            "NSDocumentsFolderUsageDescription": "保存录制文件与 profile 缓存",
        },
    )
