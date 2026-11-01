"""试产测试夹具只管理自己构造的控件。"""

import gc
import weakref

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from satellite_debug_tool.core.config import Settings
import satellite_debug_tool.tests.test_production_workspace as workspace_tests
from satellite_debug_tool.ui.production_workspace import ProductionWorkspace


def test_production_widget_fixture_owns_created_instances_only(
    qapplication_session, tmp_path, monkeypatch,
):
    settings = Settings(config_directory=tmp_path)
    settings.set("production.discovery_cidr", "127.0.0.1/32")
    settings.set("production.local_port", 0)
    existing = ProductionWorkspace(settings)

    def reject_global_enumeration(*args):
        raise AssertionError("fixture must own its instances directly")

    try:
        with monkeypatch.context() as scope:
            scope.setattr(QApplication, "allWidgets", reject_global_enumeration)
            lifecycle = workspace_tests._dispose_created_production_widgets.__wrapped__(
                qapplication_session,
            )
            next(lifecycle)
            try:
                created = ProductionWorkspace(settings)
                created_ref = weakref.ref(created)
                del created
                gc.collect()
                assert created_ref() is not None
                assert shiboken6.isValid(created_ref())
            finally:
                lifecycle.close()
            assert shiboken6.isValid(existing)
            assert created_ref() is None or shiboken6.isValid(created_ref()) is False
    finally:
        existing.close()
        existing.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapplication_session.processEvents()
