"""GNSS debug payload、分片重组与 UG016 频段映射。"""

import os
import pytest

import satellite_debug_tool.core.data.gnss_store as gnss_store_module
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.data.gnss_store import (
    GnssStore,
    infer_sky_system,
    observation_locked,
    satellite_label,
    signal_band,
)
from satellite_debug_tool.core.protocol import (
    CmdType,
    FrameReceiverV2,
    GnssCnrObservation,
    GnssCnrReport,
    GnssSkyReport,
    GnssSkySatellite,
    build_frame,
    decode_gnss_cnr_report,
    decode_gnss_sky_report,
)
from satellite_debug_tool.ui.gnss_widget import GnssWidget, build_frequency_bars, sky_point
from PySide6.QtCore import QPointF
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.io.data_recorder import DataRecorder
from satellite_debug_tool.ui.live_view import LiveView
from satellite_debug_tool.ui.playback_view import PlaybackView


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


SKY_GOLDEN = bytes([
    0x01, 0x04, 0x03, 0x02, 0x01, ord("G"), ord("P"), 0x02, 0x02, 0x01,
    0x0B, 0x00, 0x28, 0x53, 0x00, 0x26, 0x07,
    0x1A, 0x00, 0xFB, 0x2C, 0x01, 0x00, 0x03,
])
CNR_GOLDEN = bytes([
    0x01, 0x04, 0x03, 0x02, 0x01, 0x34, 0x12, 0x00, 0x01, 0x02, 0x00, 0x02, 0x01,
    0x00, 0x0B, 0x00, 0x26, 0x04, 0x0F, 0xFF,
    0x03, 0x1A, 0x0C, 0x23, 0x07, 0x03, 0xFF,
])


def _observation(index: int, *, lock_flags: int = 3) -> GnssCnrObservation:
    return GnssCnrObservation(
        system=index % 7,
        prn=(index % 200) + 1,
        signal_type=index % 21,
        cn0_dbhz=20 + index % 32,
        tracking_state=index % 8,
        lock_flags=lock_flags,
        glo_freq_channel=9 if index % 7 == 1 else 0xFF,
    )


def test_golden_payload_matches_firmware_codec() -> None:
    sky = decode_gnss_sky_report(SKY_GOLDEN)
    assert sky.timestamp == 0x01020304
    assert sky.talker == "GP"
    assert sky.satellites[1].elevation_deg == -5
    assert sky.satellites[1].azimuth_deg == 300

    cnr = decode_gnss_cnr_report(CNR_GOLDEN)
    assert cnr.report_id == 0x1234
    assert cnr.total_observations == 2
    assert cnr.observations[0].cn0_dbhz == 38
    assert cnr.observations[1].signal_type == 12


def test_receiver_decodes_both_gnss_commands() -> None:
    records = FrameReceiverV2().feed(
        build_frame(CmdType.GNSS_SKY_REPORT, SKY_GOLDEN)
        + build_frame(CmdType.GNSS_CNR_REPORT, CNR_GOLDEN)
    )
    assert isinstance(records[0], GnssSkyReport)
    assert isinstance(records[1], GnssCnrReport)


def test_cnr_chunks_reassemble_out_of_order_and_ignore_duplicate() -> None:
    store = GnssStore()
    observations = [_observation(i) for i in range(130)]
    second = GnssCnrReport(1, 1000, 7, 1, 2, 130, 0, observations[128:])
    first = GnssCnrReport(1, 1000, 7, 0, 2, 130, 0, observations[:128])
    assert not store.update_cnr_chunk(second)
    assert not store.update_cnr_chunk(second)  # 完全相同的重复片无害
    assert store.update_cnr_chunk(first)
    snapshot = store.snapshot().cnr
    assert snapshot is not None
    assert snapshot.report_id == 7
    assert list(snapshot.observations) == observations


def test_new_report_replaces_incomplete_old_report() -> None:
    store = GnssStore()
    old = GnssCnrReport(1, 1000, 10, 0, 2, 129, 0, [_observation(i) for i in range(128)])
    new = GnssCnrReport(1, 2000, 11, 0, 1, 1, 0, [_observation(200)])
    assert not store.update_cnr_chunk(old)
    assert store.update_cnr_chunk(new)
    assert store.snapshot().cnr.report_id == 11


def test_late_old_chunk_does_not_replace_new_pending_report() -> None:
    store = GnssStore()
    new_first = GnssCnrReport(1, 2000, 11, 0, 2, 2, 0, [_observation(0)])
    old_late = GnssCnrReport(1, 1000, 10, 1, 2, 2, 0, [_observation(9)])
    new_second = GnssCnrReport(1, 2000, 11, 1, 2, 2, 0, [_observation(1)])
    assert not store.update_cnr_chunk(new_first)
    assert not store.update_cnr_chunk(old_late)
    assert store.update_cnr_chunk(new_second)
    assert store.snapshot().cnr.report_id == 11


def test_incomplete_report_expires(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(gnss_store_module.time, "monotonic", lambda: clock[0])
    store = GnssStore()
    partial = GnssCnrReport(1, 1000, 10, 0, 2, 2, 0, [_observation(0)])
    assert not store.update_cnr_chunk(partial)
    clock[0] = 103.1
    assert store.expire_incomplete()
    assert not store.update_cnr_chunk(GnssCnrReport(1, 1000, 10, 1, 2, 2, 0, [_observation(1)]))
    assert store.snapshot().cnr is None


def test_conflicting_duplicate_discards_report() -> None:
    store = GnssStore()
    first = GnssCnrReport(1, 1000, 1, 0, 2, 2, 0, [_observation(0)])
    conflict = GnssCnrReport(1, 1000, 1, 0, 2, 2, 0, [_observation(1)])
    assert not store.update_cnr_chunk(first)
    assert not store.update_cnr_chunk(conflict)
    assert store.snapshot().cnr is None


def test_ug016_signal_band_mapping_and_labels() -> None:
    expected = {
        0: {0: "L1", 5: "L2", 9: "L2", 14: "L5", 16: "L1", 17: "L2"},
        1: {0: "G1", 1: "G2", 5: "G2"},
        2: {0: "L1", 6: "L5"},
        3: {1: "E1", 2: "E1", 6: "E6", 7: "E6", 12: "E5a", 17: "E5b", 20: "E5 AltBOC"},
        4: {0: "B1", 1: "B2", 2: "B3", 4: "B1", 5: "B2", 6: "B3", 7: "B1", 9: "B2", 10: "B2"},
        5: {0: "L1", 14: "L5", 16: "L1", 17: "L2"},
        6: {0: "L5"},
    }
    for system, mappings in expected.items():
        for signal_type, band in mappings.items():
            assert signal_band(system, signal_type) == band
    assert satellite_label(0, 11) == "G11"
    assert satellite_label(3, 26) == "E26"
    assert infer_sky_system("GP", 11) == 0
    assert infer_sky_system("GP", 40) == 2
    assert infer_sky_system("GP", 193) == 5


def test_store_stale_after_three_seconds(monkeypatch) -> None:
    clock = [200.0]
    monkeypatch.setattr(gnss_store_module.time, "monotonic", lambda: clock[0])
    store = GnssStore()
    store.update_sky(decode_gnss_sky_report(SKY_GOLDEN))
    assert not store.is_stale()
    clock[0] = 203.01
    assert store.is_stale()
    assert store.data_age_s() == pytest.approx(3.01)


def test_lock_filter() -> None:
    assert observation_locked(_observation(0, lock_flags=1))
    assert observation_locked(_observation(0, lock_flags=2))
    assert not observation_locked(_observation(0, lock_flags=4))


def test_bar_grouping_uses_locked_max_and_keeps_all_details() -> None:
    store = GnssStore()
    details = [
        GnssCnrObservation(0, 11, 0, 32, 1, 3, 0xFF),
        GnssCnrObservation(0, 11, 16, 40, 2, 1, 0xFF),  # 同为 GPS L1
        GnssCnrObservation(0, 11, 0, 51, 3, 4, 0xFF),   # 未 code/phase lock，不参与柱高
    ]
    assert store.update_cnr_chunk(GnssCnrReport(1, 10, 1, 0, 1, 3, 0, details))
    bars = build_frequency_bars(store.snapshot(), {0})
    assert len(bars) == 1
    assert bars[0].band == "L1"
    assert bars[0].cn0_dbhz == 40
    assert len(bars[0].details) == 3


def test_sky_projection_cardinal_directions() -> None:
    center = QPointF(100, 100)
    north = sky_point(center, 90, 0, 0)
    east = sky_point(center, 90, 0, 90)
    zenith = sky_point(center, 90, 90, 123)
    assert (round(north.x()), round(north.y())) == (100, 10)
    assert (round(east.x()), round(east.y())) == (190, 100)
    assert (round(zenith.x()), round(zenith.y())) == (100, 100)


def test_gnss_widget_live_and_playback(qapp) -> None:
    store = GnssStore(keep_history=True)
    widget = GnssWidget(store, playback=True)
    store.update_sky(GnssSkyReport(
        version=1,
        timestamp=100,
        talker="GP",
        total_visible=1,
        flags=0,
        satellites=[GnssSkySatellite(11, 40, 83, 38, 7)],
    ))
    store.update_cnr_chunk(GnssCnrReport(1, 101, 1, 0, 1, 1, 0, [_observation(0)]))
    widget.refresh()
    assert widget._history_slider.maximum() == 1
    assert widget._bars._bars
    widget.set_theme("light")
    widget.close()


def test_sdb_v2_preserves_all_gnss_frames(tmp_path) -> None:
    path = tmp_path / "gnss.sdb"
    recorder = DataRecorder(path)
    assert recorder.start()
    assert recorder.write_frame(build_frame(CmdType.GNSS_SKY_REPORT, SKY_GOLDEN))
    assert recorder.write_frame(build_frame(CmdType.GNSS_CNR_REPORT, CNR_GOLDEN))
    assert recorder.stop()
    records = list(DataImporter.open_sdb(path).iter_records())
    assert [type(record) for record in records] == [GnssSkyReport, GnssCnrReport]


def test_live_disconnect_clears_gnss(qapp, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    view = LiveView(settings=Settings())
    view._on_data_received(build_frame(CmdType.GNSS_SKY_REPORT, SKY_GOLDEN))
    assert view._gnss_store.has_data()
    assert view._gnss_btn.isEnabled()
    view._on_disconnected()
    assert not view._gnss_store.has_data()
    assert not view._gnss_btn.isEnabled()
    view.close()


def test_playback_enables_only_when_gnss_records_exist(qapp, tmp_path) -> None:
    view = PlaybackView()
    assert not view._gnss_btn.isEnabled()

    path = tmp_path / "playback-gnss.sdb"
    recorder = DataRecorder(path)
    assert recorder.start()
    assert recorder.write_frame(build_frame(CmdType.GNSS_SKY_REPORT, SKY_GOLDEN))
    assert recorder.write_frame(build_frame(CmdType.GNSS_CNR_REPORT, CNR_GOLDEN))
    assert recorder.stop()

    view._load_file(path)
    assert view._gnss_btn.isEnabled()
    assert len(view._gnss_store.history()) == 2
    view._on_clear_clicked()
    assert not view._gnss_btn.isEnabled()
    assert not view._gnss_store.has_data()
    view.close()
