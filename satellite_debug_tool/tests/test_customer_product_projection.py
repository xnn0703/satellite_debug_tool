"""Customer product model and legacy Debug v2 projection tests."""

from __future__ import annotations

import pytest

from satellite_debug_tool.core.data import DataStore, StateStore
from satellite_debug_tool.core.product import (
    Availability,
    ControlMode,
    LegacyV2Projector,
    NavigationState,
    ProductValue,
    TrackingPhase,
)
from satellite_debug_tool.core.profile import CHANNEL_ROLE_SNR, ProfileStore
from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    ChannelSample,
    DataReport,
    MetaInfo,
    StateDefEntry,
    StateEnumItem,
    StateReport,
    StateSample,
)


def _channel(cid: int, name: str) -> ChannelDefEntry:
    return ChannelDefEntry(cid, 1, 0, 0, name, "", -1000.0, 1000.0)


def _state(sid: int, name: str, enums=()) -> StateDefEntry:
    return StateDefEntry(sid, 1 if enums else 0, 0, name, list(enums))


def _afd_profile() -> ProfileStore:
    store = ProfileStore()
    store.apply_meta(MetaInfo(2, "0.0.130", "afd01", "AFD01-DEV"))
    store.apply_channel_define("afd01", 1, [
        _channel(0, "roll"),
        _channel(1, "pitch"),
        _channel(2, "yaw"),
        _channel(3, "ant_az"),
        _channel(4, "ant_el"),
        _channel(8, "snr"),
        _channel(20, "gps_lat"),
        _channel(21, "gps_lon"),
        _channel(22, "gps_alt"),
    ])
    store.apply_state_define("afd01", 1, [
        _state(0, "TRACE_MODE", [
            StateEnumItem(0, 3, "STANDBY"),
            StateEnumItem(1, 1, "SCAN_GLOBAL"),
            StateEnumItem(2, 1, "SCAN_WIDE"),
            StateEnumItem(3, 0, "LOCK"),
            StateEnumItem(4, 3, "MANUAL"),
        ]),
        _state(1, "LOCK_FLAG"),
        _state(2, "GPS_FIX", [StateEnumItem(3, 0, "RTK_FIXED")]),
        _state(12, "INTERNAL_INS_STATE", [
            StateEnumItem(3, 0, "NAVIGATION_READY"),
        ]),
    ])
    return store


def test_product_value_keeps_real_zero_distinct_from_unavailable():
    value = ProductValue.valid(0.0)
    assert value.value == 0.0
    assert value.availability == Availability.VALID
    assert ProductValue.unsupported().value is None


def test_afd01_legacy_projection_maps_customer_runtime_without_fake_serial(monkeypatch):
    profile = _afd_profile()
    data = DataStore(max_channels=64)
    states = StateStore()
    monkeypatch.setattr("satellite_debug_tool.core.data.data_store.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("satellite_debug_tool.core.data.state_store.time.monotonic", lambda: 100.0)
    data.update(DataReport(1234, [
        ChannelSample(0, 1.0), ChannelSample(1, 2.0), ChannelSample(2, 3.0),
        ChannelSample(3, 120.0), ChannelSample(4, 35.0), ChannelSample(8, 17.5),
        ChannelSample(20, 31.8), ChannelSample(21, 118.8), ChannelSample(22, 12.0),
    ]))
    states.update("afd01", StateReport(1234, [
        StateSample(0, 3), StateSample(1, 1), StateSample(2, 3), StateSample(12, 3),
    ]))

    snap = LegacyV2Projector(profile, data, states).snapshot(now_monotonic=101.0)

    assert snap.identity.model.value == "afd01"
    assert snap.identity.serial_number.availability == Availability.UNSUPPORTED
    assert snap.operation.control_mode.value == ControlMode.AUTO
    assert snap.operation.tracking_phase.value == TrackingPhase.LOCKED
    assert snap.operation.locked.value is True
    assert snap.operation.navigation.value == NavigationState.READY
    assert snap.operation.snr_db.value == 17.5
    assert snap.operation.longitude_deg.value == pytest.approx(118.8)


def test_projection_marks_supported_but_old_channel_stale(monkeypatch):
    profile = _afd_profile()
    data = DataStore(max_channels=64)
    states = StateStore()
    monkeypatch.setattr("satellite_debug_tool.core.data.data_store.time.monotonic", lambda: 10.0)
    data.update(DataReport(1, [ChannelSample(8, 0.0)]))

    snap = LegacyV2Projector(profile, data, states, stale_after_s=3.0).snapshot(
        now_monotonic=14.0
    )

    assert snap.operation.snr_db.value == 0.0
    assert snap.operation.snr_db.availability == Availability.STALE


def test_legacy_history_uses_device_uptime_seconds_across_rollover(monkeypatch):
    profile = _afd_profile()
    data = DataStore(max_channels=64)
    states = StateStore()
    monkeypatch.setattr("satellite_debug_tool.core.data.data_store.time.monotonic", lambda: 10.0)
    data.update(DataReport(0xFFFFFF00, [ChannelSample(8, 10.0)]))
    data.update(DataReport(0x00000100, [ChannelSample(8, 11.0)]))

    times, values = LegacyV2Projector(profile, data, states).channel_history(
        CHANNEL_ROLE_SNR, window_s=300.0
    )

    assert times.tolist() == pytest.approx([0xFFFFFF00 / 1000.0, 0x100000100 / 1000.0])
    assert values.tolist() == pytest.approx([10.0, 11.0])


def test_unknown_trace_mode_stays_unknown(monkeypatch):
    profile = _afd_profile()
    data = DataStore(max_channels=64)
    states = StateStore()
    monkeypatch.setattr(
        "satellite_debug_tool.core.data.state_store.time.monotonic",
        lambda: 10.0,
    )
    states.update("afd01", StateReport(1, [StateSample(0, 99)]))

    snapshot = LegacyV2Projector(profile, data, states).snapshot(now_monotonic=10.1)

    assert snapshot.operation.control_mode.value == ControlMode.UNKNOWN
