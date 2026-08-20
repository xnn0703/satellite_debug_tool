"""AFD01 customer overview rendering tests."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from satellite_debug_tool.core.data import DataStore, GnssStore, OrbitStore, StateStore
from satellite_debug_tool.core.product import (
    Availability,
    CustomerRecordingState,
    ProductServiceStore,
    ProductValue,
)
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    ChannelSample,
    DataReport,
    MetaInfo,
    OrbitOperation,
    OrbitSkyReport,
    OrbitSkySample,
    OrbitStatus,
    ServiceIdentity,
    ServiceComponentHealth,
    ServiceComponentValue,
    ServiceLinkDetail,
    ServiceFastState,
    ServiceRfLockStatus,
    ServiceSlowState,
    StateDefEntry,
    StateEnumItem,
    StateReport,
    StateSample,
)
from satellite_debug_tool.ui.customer_overview_view import (
    CustomerOverviewView,
    combined_polarization,
    format_polarization,
    pll_lock_summary,
)
from satellite_debug_tool.i18n import tr


class _LiveDouble(QObject):
    connection_state_changed = Signal(bool)
    profile_ready = Signal(str)
    recording_state_changed = Signal(bool, str)
    customer_recording_state_changed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.profiles = ProfileStore()
        self.data = DataStore(max_channels=64)
        self.states = StateStore()
        self.gnss = GnssStore()
        self.orbits = OrbitStore()
        self.products = ProductServiceStore()
        self.connected = True
        self.recording = False
        self.customer_state = CustomerRecordingState.IDLE
        self.connect_kwargs = None
        self.sky_consumers: list[tuple[str, bool]] = []
        self.selected_target = 0

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

    def orbit_store(self):
        return self.orbits

    def is_connected(self):
        return self.connected

    def is_recording(self):
        return self.recording

    def customer_recording_state(self):
        return self.customer_state

    def connect_udp(self, *_args, **_kwargs):
        self.connect_kwargs = _kwargs
        self.connected = True
        return True

    def disconnect_device(self):
        self.connected = False

    def toggle_recording(self):
        self.recording = not self.recording

    def toggle_customer_recording(self):
        self.customer_state = (
            CustomerRecordingState.IDLE
            if self.customer_state != CustomerRecordingState.IDLE
            else CustomerRecordingState.ARMED
        )
        self.customer_recording_state_changed.emit(self.customer_state.value, "")

    def show_gnss_details(self):
        return None

    def set_orbit_sky_consumer(self, name: str, active: bool):
        self.sky_consumers.append((name, active))

    def select_orbit_tracking_target(self, norad_id: int):
        self.selected_target = norad_id
        return True


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
    assert tuple(view._data_values) == (
        "longitude", "latitude", "altitude", "rx_rf",
        "tx_rf", "rx_lo", "tx_lo", "satellite",
    )
    assert view._snr_readout.value.text() == "18.50 dB"
    assert "118.800" in view._data_values["longitude"].text()
    assert "LOCK" not in view._status_values["tracking"].text()
    assert view._component_details["converter"].text() == "— · — · — · —"


def test_customer_overview_overlays_array_sky_and_converts_native_beam(app) -> None:
    live = _LiveDouble()
    live.products.feed(
        ServiceFastState(
            1,
            100,
            (1 << 9) | (1 << 10),
            0,
            0,
            False,
            0,
            0,
            False,
            0.0,
            0.0,
            0.0,
            244.0,
            60.0,
            0.0,
        )
    )
    sample = OrbitSkySample(
        25544,
        1.0,
        2.0,
        400000.0,
        90.0,
        45.0,
        700000.0,
        120.0,
        30.0,
        2.5,
        False,
        True,
        True,
        True,
        True,
    )
    live.orbits.feed(
        OrbitSkyReport(
            1,
            OrbitOperation.SKY_SNAPSHOT,
            OrbitStatus.OK,
            7,
            42,
            4,
            1723939200123,
            1,
            0,
            70.0,
            60.0,
            1.0,
            2.0,
            3.0,
            4.0,
            -1,
            1,
            25544,
            False,
            (sample,),
        )
    )
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)

    view.refresh()

    assert view._beam_polar._azimuth_deg == pytest.approx(120.0)
    assert view._beam_polar._off_axis_deg == pytest.approx(30.0)
    assert view._beam_polar.satellites()[0].active_target


def test_customer_overview_requests_sky_only_while_visible_and_confirms_selection(
    app, monkeypatch
) -> None:
    live = _LiveDouble()
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)

    view.show()
    app.processEvents()
    assert live.sky_consumers[-1] == ("customer_overview", True)

    monkeypatch.setattr(
        "satellite_debug_tool.ui.customer_overview_view.QMessageBox.question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    view._on_satellite_clicked(25544)
    assert live.selected_target == 25544

    view.hide()
    app.processEvents()
    assert live.sky_consumers[-1] == ("customer_overview", False)


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


def test_customer_overview_shows_independent_rx_tx_polarization(app) -> None:
    live = _LiveDouble()
    live.products.feed(
        ServiceIdentity(1, 100, 0x17, "AFD01", "AFD01-0001", "0.0.130", "", 2)
    )
    live.products.feed(
        ServiceSlowState(
            1, 100, (1 << 5) | (1 << 6),
            0.0, 0.0, 0.0, 0.0, 0.0, 2, 3, False,
        )
    )
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)

    view.refresh()

    text = view._beam_values["polarization"].value.text()
    assert "/" in text
    assert format_polarization(2) in text
    assert format_polarization(3) in text


def test_pll_lock_summary_preserves_each_path_and_availability() -> None:
    summary, status = pll_lock_summary(
        ProductValue.valid(True, 10),
        ProductValue.valid(False, 10),
        ProductValue.valid(True, 10),
    )

    assert summary.value is not None
    assert "CLK ✓" in summary.value
    assert "TX ×" in summary.value
    assert "RX ✓" in summary.value
    assert status == "warn"

    unsupported, status = pll_lock_summary(
        ProductValue.unsupported(),
        ProductValue.unsupported(),
        ProductValue.unsupported(),
    )
    assert unsupported.availability == Availability.UNSUPPORTED
    assert status == "neutral"

    stale, status = pll_lock_summary(
        ProductValue.stale(True, 10),
        ProductValue.valid(True, 10),
        ProductValue.valid(True, 10),
    )
    assert stale.availability == Availability.STALE
    assert status == "neutral"


def test_customer_overview_shows_compact_pll_lock_summary(app) -> None:
    live = _LiveDouble()
    live.products.feed(ServiceRfLockStatus(1, 100, 0x07, 0x05))
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)

    view.refresh()

    assert "CLK ✓" in view._pll_lock_summary.text()
    assert "TX ×" in view._pll_lock_summary.text()
    assert view._pll_lock_summary.property("status") == "warn"
    assert tuple(view._data_values) == (
        "longitude", "latitude", "altitude", "rx_rf",
        "tx_rf", "rx_lo", "tx_lo", "satellite",
    )


def test_polarization_formatter_preserves_future_linear_angle() -> None:
    assert "27.2°" in format_polarization(None, linear_angle_deg=27.25)
    same = combined_polarization(ProductValue.valid(2), ProductValue.valid(2))
    assert same.value == format_polarization(2)
    stale = combined_polarization(ProductValue.stale(0), ProductValue.valid(0))
    assert stale.availability == Availability.STALE


def test_customer_overview_splits_compact_state_and_runtime_data(app) -> None:
    view = CustomerOverviewView(_LiveDouble(), _SettingsDouble(), enable_3d=False)

    assert tuple(view._status_values) == (
        "link", "mode", "tracking", "lock", "navigation", "gnss", "tx", "modem"
    )
    assert view._info_layout.count() == 2
    assert view._status_grid.getItemPosition(
        view._status_grid.indexOf(view._status_values["modem"])
    ) == (1, 3, 1, 1)
    assert view._data_grid.getItemPosition(
        view._data_grid.indexOf(view._data_values["satellite"])
    ) == (1, 3, 1, 1)


def test_customer_overview_does_not_mark_tx_on_when_array_is_offline(app) -> None:
    live = _LiveDouble()
    live.products.feed(
        ServiceFastState(
            1, 100, 1 << 5, 0, 0, False, 0, 0, True,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        )
    )
    unavailable = ServiceComponentValue(0x01, False, 0.0, 0.0, 0)
    live.products.feed(
        ServiceComponentHealth(1, 100, unavailable, unavailable, unavailable)
    )
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)

    view.refresh()

    assert tr("Unavailable") in view._status_values["tx"].text()
    assert view._status_values["tx"].property("status") == "warn"
    assert tr("Offline") in view._component_details["tx_array"].text()


def test_customer_overview_renders_link_detail_and_satellite_modes(app) -> None:
    live = _LiveDouble()
    live.products.feed(
        ServiceLinkDetail(
            1, 100, 0x3F, True, 18250.0, 28050.0, 2, 0.0, 25544, ""
        )
    )
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)

    view.refresh()

    assert tr("Online") in view._status_values["modem"].text()
    assert "18250.00 MHz" in view._data_values["rx_lo"].text()
    assert "NORAD 25544" in view._data_values["satellite"].text()

    live.products.feed(
        ServiceLinkDetail(
            1, 200, 0x1F, False, 19250.0, 29050.0, 1, 125.0, 0, ""
        )
    )
    view.refresh()

    assert tr("Offline") in view._status_values["modem"].text()
    assert "GEO 125.00°E" in view._data_values["satellite"].text()


def test_customer_snr_plot_uses_five_minute_device_uptime_axis(app) -> None:
    live = _LiveDouble()
    live.products.feed(
        ServiceIdentity(1, 1_230_000, 0x17, "AFD01", "AFD01-0001", "0.0.130", "", 2)
    )
    for timestamp, snr in ((1_230_000, 10.0), (1_530_000, 12.0)):
        live.products.feed(
            ServiceFastState(
                1, timestamp, 1 << 11, 0, 0, False, 0, 0, False,
                0.0, 0.0, 0.0, 0.0, 0.0, snr,
            )
        )
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)

    view.refresh()

    times, values = view._snr_curve.getData()
    assert times.tolist() == pytest.approx([1230.0, 1530.0])
    assert values.tolist() == pytest.approx([10.0, 12.0])
    x_range = view._snr_plot.getViewBox().viewRange()[0]
    assert x_range == pytest.approx([1230.0, 1530.0])


def test_customer_snr_plot_keeps_full_window_for_single_sample(app) -> None:
    live = _LiveDouble()
    live.products.feed(
        ServiceIdentity(1, 1_230_000, 0x17, "AFD01", "AFD01-0001", "0.0.130", "", 3)
    )
    live.products.feed(
        ServiceFastState(
            1, 1_230_000, 1 << 11, 0, 0, False, 0, 0, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 12.4,
        )
    )
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)

    view.refresh()

    x_range = view._snr_plot.getViewBox().viewRange()[0]
    y_range = view._snr_plot.getViewBox().viewRange()[1]
    assert x_range == pytest.approx([930.0, 1230.0])
    assert y_range == pytest.approx([10.4, 14.4])


def test_customer_overview_density_controls_bottom_band_height(app) -> None:
    view = CustomerOverviewView(_LiveDouble(), _SettingsDouble(), enable_3d=False)

    view._apply_density("regular")
    assert view._signal_panel.height() == 240
    assert view._snr_plot.minimumHeight() == 180
    assert view._component_panel.height() == 60

    view._apply_density("compact")
    assert view._signal_panel.height() == 175
    assert view._snr_plot.minimumHeight() == 126
    assert view._component_panel.height() == 52

    view._apply_density("dense")
    assert view._signal_panel.height() == 122
    assert view._snr_plot.minimumHeight() == 86
    assert view._component_panel.height() == 44
    last = view._status_values["modem"]
    index = view._status_grid.indexOf(last)
    assert view._status_grid.getItemPosition(index) == (1, 3, 1, 1)


def test_customer_overview_component_strip_is_last(app) -> None:
    view = CustomerOverviewView(_LiveDouble(), _SettingsDouble(), enable_3d=False)

    assert view._root_layout.indexOf(view._signal_panel) < view._root_layout.indexOf(
        view._component_panel
    )
    assert view._root_layout.indexOf(view._component_panel) == view._root_layout.count() - 1


def test_customer_record_button_renders_armed_active_and_restoring_states(app) -> None:
    live = _LiveDouble()
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)

    live.customer_state = CustomerRecordingState.ARMED
    live.customer_recording_state_changed.emit("armed", "/tmp/support.sdb")
    assert view._record_btn.text() == tr("Cancel recording")
    assert view._record_btn.isEnabled()

    live.customer_state = CustomerRecordingState.ACTIVE
    live.customer_recording_state_changed.emit("active", "/tmp/support.sdb")
    assert view._record_btn.text() == tr("Stop recording")

    live.customer_state = CustomerRecordingState.RESTORING
    live.customer_recording_state_changed.emit("restoring", "/tmp/support.sdb")
    assert view._record_btn.text() == tr("Restoring...")
    assert not view._record_btn.isEnabled()
