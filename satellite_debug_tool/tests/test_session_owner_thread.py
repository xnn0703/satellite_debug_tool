"""会话 owner 只观察线程归属，共享 Qt 线程的生命期由 Qt 管理。"""

import gc
from threading import Thread

import pytest
import shiboken6
from PySide6.QtCore import QThread

from satellite_debug_tool.core.comm import UdpEndpointBroker
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.customer.device_directory import CustomerDeviceDirectory
from satellite_debug_tool.core.session import EndpointSessionDirectory, EndpointSessionRuntime
from satellite_debug_tool.ui.application_icon import application_icon


def _make_owner(kind, tmp_path):
    broker = UdpEndpointBroker(local_port=0)
    sessions = EndpointSessionDirectory(broker)
    if kind == "runtime":
        owner = EndpointSessionRuntime(("192.168.1.13", 4004), broker=broker)
    else:
        settings = Settings(config_directory=tmp_path)
        settings.set("customer.devices", [])
        owner = CustomerDeviceDirectory(settings, sessions)
    return owner, sessions, broker


@pytest.mark.parametrize("kind", ["runtime", "customer_directory"])
def test_owner_destruction_preserves_shared_qt_thread(kind, tmp_path, qapplication_session):
    """旧实现 QObject.thread() 把主线程 wrapper 挂到短命 owner 并使其失效。"""
    owner_thread = QThread.currentThread()
    owner, sessions, broker = _make_owner(kind, tmp_path)
    owner._assert_owner_thread()

    shiboken6.delete(owner)
    del owner
    gc.collect()

    assert shiboken6.isValid(owner_thread)
    assert owner_thread.isCurrentThread()
    assert application_icon().availableSizes()
    qapplication_session.processEvents()


@pytest.mark.parametrize("kind", ["runtime", "customer_directory"])
def test_owner_rejects_calls_from_another_thread(kind, tmp_path, qapplication_session):
    owner, sessions, broker = _make_owner(kind, tmp_path)
    errors = []

    def check_owner():
        try:
            owner._assert_owner_thread()
        except RuntimeError as exc:
            errors.append(str(exc))

    thread = Thread(target=check_owner)
    thread.start()
    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert len(errors) == 1
    assert "Qt owner thread" in errors[0]
    shiboken6.delete(owner)
