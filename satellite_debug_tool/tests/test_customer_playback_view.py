"""Customer playback applies a restricted product timeline from SDB v2/v3."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from satellite_debug_tool.core.protocol import CmdType, build_frame
from satellite_debug_tool.io.data_recorder import DataRecorder, SDB_VERSION_V3
from satellite_debug_tool.ui.customer_playback_view import CustomerPlaybackView


class _SettingsDouble:
    def get(self, _key: str, default=None):
        return default


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


def _record_v3(path: Path) -> None:
    recorder = DataRecorder(
        path,
        format_version=SDB_VERSION_V3,
        metadata={"hardware_type": "afd01", "capture_profile": "support_full"},
    )
    assert recorder.start()
    assert recorder.write_frame(_identity(), host_timestamp_ns=1_000_000_000)
    assert recorder.write_frame(_fast(100, 12.5, 5.0), host_timestamp_ns=1_100_000_000)
    assert recorder.write_frame(_fast(2100, 18.5, 45.0), host_timestamp_ns=3_100_000_000)
    assert recorder.stop()


def test_v3_playback_seek_uses_product_timeline_and_is_read_only(
    app, tmp_path: Path
) -> None:
    path = tmp_path / "customer-v3.sdb"
    _record_v3(path)
    view = CustomerPlaybackView(_SettingsDouble(), enable_3d=False)

    assert view.load_file(path)
    assert view._duration_s == pytest.approx(2.1)
    assert not view.overview._connect_btn.isVisible()
    assert not view.overview._record_btn.isVisible()
    assert "AFD01-PLAYBACK" in view.overview._identity_label.text()

    view.seek(view._duration_s)
    view.overview.refresh()
    assert view.overview._metrics["snr"].value.text() == "18.50 dB"
    assert view.overview._metrics["yaw"].value.text() == "45.00 °"

    view.seek(0.15)
    view.overview.refresh()
    assert view.overview._metrics["snr"].value.text() == "12.50 dB"
    assert view._quality_label.property("complete") is True


def test_v2_playback_remains_readable(app, tmp_path: Path) -> None:
    path = tmp_path / "legacy-v2.sdb"
    recorder = DataRecorder(path)
    assert recorder.start()
    assert recorder.write_frame(_identity())
    assert recorder.write_frame(_fast(100, 11.0, 1.0))
    assert recorder.write_frame(_fast(1100, 13.0, 2.0))
    assert recorder.stop()

    view = CustomerPlaybackView(_SettingsDouble(), enable_3d=False)
    assert view.load_file(path)
    assert view._duration_s == pytest.approx(1.09)
    view.seek(view._duration_s)
    view.overview.refresh()
    assert view.overview._metrics["snr"].value.text() == "13.00 dB"
    assert "SDB v2" in view._quality_label.text()
