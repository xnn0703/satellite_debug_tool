"""测试环境明确关闭后台升级检查，避免访问发布服务。"""
import os
from pathlib import Path

import pytest

os.environ.setdefault("SATELLITE_UPDATE_CHECK", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Historical UI assertions use the Chinese product copy. M16-specific tests
# explicitly switch to both locales and verify runtime retranslation.
os.environ.setdefault("SATELLITE_DEBUG_LOCALE", "zh_CN")


@pytest.fixture(autouse=True)
def isolated_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX/Windows 使用同一逐测试 home，配置和型号缓存由本项测试拥有。"""
    from satellite_debug_tool.core.profile import cache

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # 默认缓存路径在模块导入时计算；构造函数每次从此变量取得目录。
    monkeypatch.setattr(cache, "DEFAULT_CACHE_DIR", tmp_path / ".satellite_debug_tool" / "profiles")


@pytest.fixture(scope="session", autouse=True)
def qapplication_session():
    """整个测试进程共享一个 QApplication，保证 Qt 对象与线程生命周期稳定。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
