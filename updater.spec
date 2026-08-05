# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — updater 独立可执行（M11 P6）。

仅依赖标准库 + py7zr + psutil；无 PySide6/pyqtgraph/numpy，体积小 (~15-25MB)。

构建：
    pyinstaller --noconfirm updater.spec
产物：
    dist/updater(.exe)   ← 单文件；主程序触发它来完成 swap 后退出
"""

from pathlib import Path
import sys

block_cipher = None
APP_NAME = "updater"
ROOT = Path(SPECPATH)
ENTRY = str(ROOT / "satellite_debug_tool" / "updater" / "main.py")

from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    [ENTRY],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=(
        collect_submodules("py7zr")
        + ["psutil"]
        # updater.applier 用到 satellite_debug_tool.updater.applier；
        # main 入口已直接 import 子模块，PyInstaller 自动跟进
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6", "pyqtgraph", "numpy", "OpenGL",     # GUI 栈完全不需要
        "tkinter", "matplotlib",
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
    a.binaries,
    a.datas,
    [],
    exclude_binaries=False,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,    # updater 是命令行工具，保留控制台便于调试
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
