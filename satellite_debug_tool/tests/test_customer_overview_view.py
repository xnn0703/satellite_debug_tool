"""AFD01 customer overview rendering tests."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from satellite_debug_tool.core.data import DataStore, GnssStore, StateStore
from satellite_debug_tool.core.product import ProductServiceStore
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    ChannelSample,
    DataReport,
    MetaInfo,
    ServiceIdentity,
    StateDefEntry,
    StateEnumItem,
    StateReport,
    StateSample,
)
from satellite_debug_tool.ui.customer_overview_view import CustomerOverviewView


class _LiveDouble(QObject):
    connection_state_changed = Signal(bool)
    profile_ready = Signal(str)
    recording_state_changed = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self.profiles = ProfileStore()
        self.data = DataStore(max_channels=64)
        self.states = StateStore()
        self.gnss = GnssStore()
        self.products = ProductServiceStore()
        self.connected = True
        self.recording = False
        self.connect_kwargs = None

    def profile_store(self):
        return self.profiles

    def data_store(self):
        return self.data

    def state_store(self):
        return self.states

    def gnss_store(self):
        return self.gnss

    def product_store(self):
        return self.products

    def is_connected(self):
        return self.connected

    def is_recording(self):
        return self.recording

    def connect_udp(self, *_args, **_kwargs):
        self.connect_kwargs = _kwargs
        self.connected = True
        return True

    def disconnect_device(self):
        self.connected = False

    def toggle_recording(self):
        self.recording = not self.recording

    def show_gnss_details(self):
        return None


class _SettingsDouble:
    _VALUES = {
        "udp.remote_ip": "192.168.1.12",
        "udp.remote_port": 4004,
        "udp.local_port": 45678,
    }

    def get(self, key: str, default=None):
        return self._VALUES.get(key, default)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _channel(cid: int, name: str):
    return ChannelDefEntry(cid, 1, 0, 0, name, "", -1000.0, 1000.0)


def test_customer_overview_renders_afd01_values_and_empty_components(app, monkeypatch):
    live = _LiveDouble()
    live.profiles.apply_meta(MetaInfo(2, "0.0.130", "afd01", "AFD01-0001"))
    live.profiles.apply_channel_define("afd01", 1, [
        _channel(0, "roll"), _channel(1, "pitch"), _channel(2, "yaw"),
        _channel(3, "ant_az"), _channel(4, "ant_el"), _channel(8, "snr"),
        _channel(20, "gps_lat"), _channel(21, "gps_lon"), _channel(22, "gps_alt"),
    ])
    live.profiles.apply_state_define("afd01", 1, [
        StateDefEntry(0, 1, 0, "TRACE_MODE", [StateEnumItem(3, 0, "LOCK")]),
        StateDefEntry(1, 0, 0, "LOCK_FLAG", []),
    ])
    monkeypatch.setattr("satellite_debug_tool.core.data.data_store.time.time", lambda: 10.0)
    monkeypatch.setattr("satellite_debug_tool.core.data.state_store.time.time", lambda: 10.0)
    live.data.update(DataReport(500, [
        ChannelSample(0, 1.0), ChannelSample(1, 2.0), ChannelSample(2, 3.0),
        ChannelSample(3, 120.0), ChannelSample(4, 35.0), ChannelSample(8, 18.5),
        ChannelSample(20, 31.8), ChannelSample(21, 118.8), ChannelSample(22, 12.0),
    ]))
    live.states.update("afd01", StateReport(500, [StateSample(0, 3), StateSample(1, 1)]))
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)

    monkeypatch.setattr("satellite_debug_tool.core.product.legacy_v2.time.time", lambda: 10.5)
    view.refresh()

    assert "afd01" in view._identity_label.text()
    assert "AFD01-0001" in view._identity_label.text()
    assert view._metrics["snr"].value.text() == "18.50 dB"
    assert view._metrics["longitude"].value.text().startswith("118.800")
    assert "LOCK" not in view._status_values["tracking"].text()
    assert view._component_table.item(0, 2).text() == "—"


def test_customer_connection_does_not_enable_engineering_debug(app) -> None:
    live = _LiveDouble()
    live.connected = False
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)

    view._toggle_connection()

    assert live.connect_kwargs == {"auto_debug": False}


def test_product_identity_loads_model_without_profile_ready(app) -> None:
    class _AttitudeDouble:
        def __init__(self) -> None:
            self.loaded: list[str] = []

        def try_load_device_model(self, hw_type: str) -> None:
            self.loaded.append(hw_type)

        def update_attitude(self, *_args) -> None:
            pass

        def update_pointing(self, *_args) -> None:
            pass

    live = _LiveDouble()
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)
    attitude = _AttitudeDouble()
    view._attitude = attitude
    live.products.feed(
        ServiceIdentity(1, 1, 0x17, "AFD01", "AFD01-0001", "0.0.130", "", 2)
    )

    view.refresh()

    assert attitude.loaded == ["afd01"]
