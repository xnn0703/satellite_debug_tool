"""测试环境明确关闭后台升级检查，避免访问发布服务。"""
import os

import pytest

os.environ.setdefault("SATELLITE_UPDATE_CHECK", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Historical UI assertions use the Chinese product copy. M16-specific tests
# explicitly switch to both locales and verify runtime retranslation.
os.environ.setdefault("SATELLITE_DEBUG_LOCALE", "zh_CN")


@pytest.fixture(scope="session", autouse=True)
def qapplication_session():
    """整个测试进程共享一个 QApplication，保证 Qt 对象与线程生命周期稳定。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
