"""应用与窗口共用的品牌图标。"""

from pathlib import Path

from PySide6.QtGui import QIcon


ICON_PATH = Path(__file__).resolve().parent / "assets" / "icons" / "app_icon.ico"


def application_icon() -> QIcon:
    """读取多尺寸图标；源码、Python 包和 PyInstaller 保持同一路径。"""
    return QIcon(str(ICON_PATH))
