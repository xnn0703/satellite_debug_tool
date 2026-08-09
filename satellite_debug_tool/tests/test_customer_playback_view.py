"""Customer playback projects full recordings onto a fixed curve set."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from satellite_debug_tool.core.product import CustomerPlaybackChannel
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.profile.cache import profile_to_dict
from satellite_debug_tool.core.protocol import ChannelDefEntry, CmdType, build_frame
from satellite_debug_tool.io.data_recorder import DataRecorder, SDB_VERSION_V3
from satellite_debug_tool.i18n import tr
from satellite_debug_tool.ui.customer_playback_view import CustomerPlaybackView


class _SettingsDouble:
    def get(self, _key: str, default=None):
        return default

    def set(self, _key: str, _value) -> None:
        return None


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _identity(timestamp: int = 10) -> bytes:
    payload = struct.pack("<BII", 1, timestamp, 0x17)
    for text in ("AFD01", "AFD01-PLAYBACK", "0.0.130", ""):
        encoded = text.encode("utf-8")
        payload += bytes([len(encoded)]) + encoded
    payload += b"\x02"
    return build_frame(CmdType.SERVICE_IDENTITY, payload)


def _fast(timestamp: int, snr: float, yaw: float) -> bytes:
    payload = struct.pack(
        "<BII6B6f",
        1,
        timestamp,
        0xFFF,
        0,
        3,
        1,
        3,
        3,
        0,
        1.0,
        2.0,
        yaw,
        120.0,
        35.0,
        snr,
    )
    return build_frame(CmdType.SERVICE_FAST_STATE, payload)


def _data(timestamp: int, samples: list[tuple[int, float]]) -> bytes:
    payload = struct.pack("<I", timestamp) + bytes([len(samples)])
    for channel_id, value in samples:
        payload += bytes([channel_id]) + struct.pack("<f", value)
    return build_frame(CmdType.DATA_REPORT, payload)


def _record_v3(path: Path) -> None:
    recorder = DataRecorder(
        path,
        format_version=SDB_VERSION_V3,
        metadata={"hardware_type": "afd01", "capture_profile": "support_full"},
    )
    assert recorder.start()
    assert recorder.write_frame(_identity(), host_timestamp_ns=1_000_000_000)
    assert recorder.write_frame(_fast(100, 12.5, 5.0), host_timestamp_ns=1_100_000_000)
    assert recorder.write_frame(
        _data(150, [(55, 999.0)]),
        host_timestamp_ns=1_150_000_000,
    )
    assert recorder.write_frame(_fast(2100, 18.5, 45.0), host_timestamp_ns=3_100_000_000)
    assert recorder.stop()


def test_v3_playback_is_static_customer_curve_analysis(app, tmp_path: Path) -> None:
    path = tmp_path / "customer-v3.sdb"
    _record_v3(path)
    view = CustomerPlaybackView(_SettingsDouble(), enable_3d=False)

    assert view.load_file(path)
    assert view._total_sec == pytest.approx(2.0)
    assert not hasattr(view, "_play_btn")
    assert "AFD01-PLAYBACK" in view._identity_label.text()
    assert view._quality_label.property("complete") is True
    assert view.chart._mode == "stacked"
    assert set(view.chart._curves) == set(range(9))
    assert view.chart._plots[0].getAxis("bottom").label.toPlainText().startswith(
        tr("Device uptime")
    )

    snr = view.data_store.get_channel_by_id(CustomerPlaybackChannel.SNR)
    yaw = view.data_store.get_channel_by_id(CustomerPlaybackChannel.YAW)
    assert snr is not None and snr.get_values().tolist() == pytest.approx([12.5, 18.5])
    assert yaw is not None and yaw.get_values().tolist() == pytest.approx([5.0, 45.0])
    assert set(view.data_store.get_all_channels()) <= {
        f"ch_{channel_id:02d}" for channel_id in range(9)
    }


def test_v2_product_service_recording_remains_readable(app, tmp_path: Path) -> None:
    path = tmp_path / "legacy-v2.sdb"
    recorder = DataRecorder(path)
    assert recorder.start()
    assert recorder.write_frame(_identity())
    assert recorder.write_frame(_fast(100, 11.0, 1.0))
    assert recorder.write_frame(_fast(1100, 13.0, 2.0))
    assert recorder.stop()

    view = CustomerPlaybackView(_SettingsDouble(), enable_3d=False)
    assert view.load_file(path)
    assert view._total_sec == pytest.approx(1.0)
    snr = view.data_store.get_channel_by_id(CustomerPlaybackChannel.SNR)
    assert snr is not None and snr.get_latest()[1] == pytest.approx(13.0)
    assert "SDB v2" in view._quality_label.text()


def test_playback_uses_unwrapped_device_uptime_across_rollover(
    app, tmp_path: Path
) -> None:
    path = tmp_path / "rollover-v2.sdb"
    recorder = DataRecorder(path)
    assert recorder.start()
    assert recorder.write_frame(_identity(0xFFFFFF00))
    assert recorder.write_frame(_fast(0xFFFFFF00, 10.0, 1.0))
    assert recorder.write_frame(_fast(0x00000100, 11.0, 2.0))
    assert recorder.stop()

    view = CustomerPlaybackView(_SettingsDouble(), enable_3d=False)
    assert view.load_file(path)

    assert view._total_sec == pytest.approx(0.512)
    snr = view.data_store.get_channel_by_id(CustomerPlaybackChannel.SNR)
    assert snr is not None
    assert snr.get_times().tolist() == pytest.approx([0xFFFFFF00, 0x100000100])


def test_legacy_debug_v2_is_projected_by_profile_roles(app, tmp_path: Path) -> None:
    path = tmp_path / "debug-v2.sdb"
    source = ProfileStore(cache=None)
    source.apply_channel_define(
        "afd01",
        1,
        [
            ChannelDefEntry(17, 1, 0, 0x02, "roll", "deg", -180.0, 180.0),
            ChannelDefEntry(23, 1, 2, 0x02, "snr", "dB", -10.0, 30.0),
            ChannelDefEntry(31, 1, 4, 0x02, "gps_lon", "deg", -180.0, 180.0),
        ],
    )
    recorder = DataRecorder(
        path,
        profile_dict=profile_to_dict(source.get_profile("afd01")),
    )
    assert recorder.start()
    assert recorder.write_frame(_data(5000, [(17, 3.5), (23, 16.0), (31, 118.8)]))
    assert recorder.stop()

    view = CustomerPlaybackView(_SettingsDouble(), enable_3d=False)
    assert view.load_file(path)

    roll = view.data_store.get_channel_by_id(CustomerPlaybackChannel.ROLL)
    snr = view.data_store.get_channel_by_id(CustomerPlaybackChannel.SNR)
    longitude = view.data_store.get_channel_by_id(CustomerPlaybackChannel.LONGITUDE)
    assert roll is not None and roll.get_latest()[1] == pytest.approx(3.5)
    assert snr is not None and snr.get_latest()[1] == pytest.approx(16.0)
    assert longitude is not None and longitude.get_latest()[1] == pytest.approx(118.8)
