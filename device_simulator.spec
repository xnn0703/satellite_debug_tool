# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — device_simulator 命令行工具。

构建：
    pyinstaller --noconfirm device_simulator.spec
产物：
    dist/DeviceSimulator/DeviceSimulator(.exe)
"""

from pathlib import Path

block_cipher = None
APP_NAME = "DeviceSimulator"
ROOT = Path(SPECPATH)
ENTRY = str(ROOT / "tools" / "device_simulator.py")

from PyInstaller.utils.hooks import collect_submodules

# 模拟器复用 satellite_debug_tool.core.protocol 做编解码
extra_hidden = collect_submodules(
    "satellite_debug_tool.core",
    filter=lambda name: "tests" not in name.split("."),
)

a = Analysis(
    [ENTRY],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=extra_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "PySide6", "pyqtgraph", "OpenGL",
        "test", "unittest", "pytest", "IPython", "jupyter",
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
    console=True,                    # 控制台工具，保留终端输出
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
