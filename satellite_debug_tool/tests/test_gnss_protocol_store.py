"""GNSS debug payload、分片重组与 UG016 频段映射。"""

import os
import pytest

import satellite_debug_tool.core.data.gnss_store as gnss_store_module
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.data.gnss_store import (
    GNSS_SOURCE_BYNAV,
    GNSS_SOURCE_MG902,
    SIGNAL_NAMESPACE_UBX_M9,
    SIGNAL_NAMESPACE_RAW,
    GnssStore,
    infer_sky_system,
    observation_locked,
    satellite_label,
    signal_band,
    signal_name,
)
from satellite_debug_tool.core.protocol import (
    CmdType,
    FrameReceiverV2,
    GnssCnrObservation,
    GnssCnrReport,
    GnssSkyReport,
    GnssSkySatellite,
    GnssSatReport,
    GnssSignalRecord,
    GnssSignalReport,
    StateReport,
    StateSample,
    build_frame,
    decode_gnss_cnr_report,
    decode_gnss_sky_report,
    decode_gnss_sat_report,
    decode_gnss_signal_report,
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
SAT_GOLDEN = bytes([
    0x01, 0x01, 0x78, 0x56, 0x34, 0x12, 0xBC, 0x9A, 0x00, 0x01, 0x02, 0x00, 0x02, 0xA5,
    0x00, 0x03, 0x2D, 0xF6, 0x2C, 0x01, 0x44, 0x33, 0x22, 0x11,
    0x06, 0x08, 0x32, 0x14, 0x67, 0x01, 0xDD, 0xCC, 0xBB, 0xAA,
])
SIGNAL_GOLDEN = bytes([
    0x01, 0x01, 0x04, 0x03, 0x02, 0x01, 0x02, 0x10, 0x00, 0x01, 0x01, 0x00, 0x01, 0x40,
    0x06, 0x0C, 0x05, 0xF9, 0x2F, 0x04, 0x02, 0x03, 0x2E, 0xFB, 0xAA, 0x55,
])
SIGNAL_FRAME_GOLDEN = bytes([
    0xAA, 0x55, 0x0D, 0x10, 0x1A, 0x00,
    *SIGNAL_GOLDEN,
    0x0E, 0x51, 0xEE,
])


def _native_payload(
    template: bytes,
    record_size: int,
    *,
    total: int,
    chunk_index: int,
    chunk_count: int,
    record_count: int,
) -> bytes:
    """基于一条 golden record 构造指定 native 分片元数据。"""
    header = bytearray(template[:14])
    header[8] = chunk_index
    header[9] = chunk_count
    header[10:12] = total.to_bytes(2, "little")
    header[12] = record_count
    record = template[14:14 + record_size]
    return bytes(header) + record * record_count


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


def test_mg902_golden_payload_and_receiver() -> None:
    sat = decode_gnss_sat_report(SAT_GOLDEN)
    assert sat.source == GNSS_SOURCE_MG902
    assert sat.records[0].sv_id == 3
    assert sat.records[0].raw_sat_flags == 0x11223344
    assert sat.records[1].raw_sat_flags == 0xAABBCCDD
    signal = decode_gnss_signal_report(SIGNAL_GOLDEN)
    assert signal.records[0].freq_id == -7
    assert signal.records[0].pr_res_0p1m == -1234
    assert signal.records[0].raw_sig_flags == 0x55AA
    assert build_frame(CmdType.GNSS_SIGNAL_REPORT, SIGNAL_GOLDEN) == SIGNAL_FRAME_GOLDEN
    records = FrameReceiverV2().feed(
        build_frame(CmdType.GNSS_SAT_REPORT, SAT_GOLDEN)
        + build_frame(CmdType.GNSS_SIGNAL_REPORT, SIGNAL_GOLDEN)
    )
    assert isinstance(records[0], GnssSatReport)
    assert isinstance(records[1], GnssSignalReport)


@pytest.mark.parametrize("decoder,payload", [
    (decode_gnss_sat_report, SAT_GOLDEN),
    (decode_gnss_signal_report, SIGNAL_GOLDEN),
])
def test_mg902_decoder_rejects_bad_fragment_metadata(decoder, payload) -> None:
    bad = bytearray(payload)
    bad[9] = 0  # chunk_count
    with pytest.raises(ValueError):
        decoder(bytes(bad))


@pytest.mark.parametrize("decoder,template,record_size", [
    (decode_gnss_sat_report, SAT_GOLDEN, 10),
    (decode_gnss_signal_report, SIGNAL_GOLDEN, 12),
])
@pytest.mark.parametrize("total,chunk_index,chunk_count,record_count", [
    (0, 0, 1, 0),
    (64, 0, 1, 64),
    (65, 0, 2, 64),
    (65, 1, 2, 1),
    (92, 1, 2, 28),
])
def test_mg902_decoder_accepts_canonical_chunk_boundaries(
    decoder, template, record_size, total, chunk_index, chunk_count, record_count,
) -> None:
    report = decoder(_native_payload(
        template,
        record_size,
        total=total,
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        record_count=record_count,
    ))
    assert report.total_records == total
    assert report.chunk_index == chunk_index
    assert report.chunk_count == chunk_count
    assert len(report.records) == record_count


@pytest.mark.parametrize("decoder,template,record_size", [
    (decode_gnss_sat_report, SAT_GOLDEN, 10),
    (decode_gnss_signal_report, SIGNAL_GOLDEN, 12),
])
@pytest.mark.parametrize("total,chunk_index,chunk_count,record_count", [
    (2, 0, 2, 2),       # 64 条以内禁止多造空尾片
    (65, 0, 1, 64),     # 65 条起必须分为两片
    (65, 0, 2, 63),     # 非末片必须恰好 64 条
    (65, 1, 2, 2),      # 末片条数必须等于 total - 64
    (92, 1, 2, 27),     # 最大报告末片不能少记录
    (93, 0, 2, 64),     # 完整报告最多 92 条
])
def test_mg902_decoder_rejects_noncanonical_chunk_boundaries(
    decoder, template, record_size, total, chunk_index, chunk_count, record_count,
) -> None:
    payload = _native_payload(
        template,
        record_size,
        total=total,
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        record_count=record_count,
    )
    with pytest.raises(ValueError):
        decoder(payload)


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


def test_duplicate_native_chunk_requires_matching_records_and_flags() -> None:
    records = [
        GnssSignalRecord(0, (index % 32) + 1, 0, -128, 30, 4, 0, 0, 0, 0x08)
        for index in range(65)
    ]
    store = GnssStore()
    first = GnssSignalReport(1, 1, 1000, 7, 0, 2, 65, 1, records[:64])
    duplicate_with_different_flags = GnssSignalReport(
        1, 1, 1000, 7, 0, 2, 65, 2, records[:64],
    )
    last = GnssSignalReport(1, 1, 1000, 7, 1, 2, 65, 1, records[64:])

    assert not store.update(first)
    assert not store.update(duplicate_with_different_flags)
    # 冲突重复片会丢弃整轮；仅补末片不能复活已污染的报告。
    assert not store.update(last)
    assert store.snapshot().signal is None


def test_native_chunks_require_consistent_total_records() -> None:
    records = [
        GnssSignalRecord(0, (index % 32) + 1, 0, -128, 30, 4, 0, 0, 0, 0x08)
        for index in range(92)
    ]
    store = GnssStore()
    first = GnssSignalReport(1, 1, 1000, 8, 0, 2, 92, 0, records[:64])
    conflicting_last = GnssSignalReport(1, 1, 1000, 8, 1, 2, 91, 0, records[64:91])
    correct_last = GnssSignalReport(1, 1, 1000, 8, 1, 2, 92, 0, records[64:])

    assert not store.update(first)
    assert not store.update(conflicting_last)
    # total_records 冲突会丢弃整轮；随后单独正确末片仍不能完成。
    assert not store.update(correct_last)
    assert store.snapshot().signal is None


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


def test_ubx_m9_signal_mapping_keeps_bds_pilot_and_data_distinct() -> None:
    expected = {
        5: ("B1", "B1C pilot"),
        6: ("B1", "B1C data"),
        7: ("B2", "B2a pilot"),
        8: ("B2", "B2a data"),
    }
    for signal_id, (band, name) in expected.items():
        assert signal_band(4, signal_id, SIGNAL_NAMESPACE_UBX_M9) == band
        assert signal_name(4, signal_id, SIGNAL_NAMESPACE_UBX_M9) == name


def test_store_stale_after_three_seconds(monkeypatch) -> None:
    clock = [200.0]
    monkeypatch.setattr(gnss_store_module.time, "monotonic", lambda: clock[0])
    store = GnssStore()
    store.update_sky(decode_gnss_sky_report(SKY_GOLDEN))
    assert not store.is_stale()
    clock[0] = 203.01
    assert store.is_stale()
    assert store.data_age_s() == pytest.approx(3.01)


def test_mg902_store_keeps_sky_and_signal_age_separate(monkeypatch) -> None:
    clock = [300.0]
    monkeypatch.setattr(gnss_store_module.time, "monotonic", lambda: clock[0])
    store = GnssStore()
    assert store.update(decode_gnss_sat_report(SAT_GOLDEN))
    assert store.source == GNSS_SOURCE_MG902
    clock[0] = 302.0
    assert store.update(decode_gnss_signal_report(SIGNAL_GOLDEN))
    clock[0] = 303.1
    assert store.sky_is_stale(3.0)
    assert not store.signal_is_stale(3.0)
    assert store.sky_age_s() == pytest.approx(3.1)
    assert store.signal_age_s() == pytest.approx(1.1)


def test_source_switch_clears_previous_snapshots() -> None:
    store = GnssStore()
    assert store.update(decode_gnss_sat_report(SAT_GOLDEN))
    assert store.snapshot().sat is not None
    store.update_sky(decode_gnss_sky_report(SKY_GOLDEN))
    snapshot = store.snapshot()
    assert snapshot.source == GNSS_SOURCE_BYNAV
    assert snapshot.sat is None
    assert snapshot.sky_by_talker


def test_incomplete_new_source_waits_for_complete_report_and_clears_old_history() -> None:
    store = GnssStore(keep_history=True)
    assert store.update(decode_gnss_sat_report(SAT_GOLDEN))
    old_history = store.history()
    assert len(old_history) == 1
    assert old_history[0].source == GNSS_SOURCE_MG902

    # 新源第一片的时间戳故意小于旧源，两个接收机之间不得比较 timestamp。
    first = GnssCnrReport(1, 10, 7, 0, 2, 2, 0, [_observation(0)])
    second = GnssCnrReport(1, 10, 7, 1, 2, 2, 0, [_observation(1)])
    assert not store.update(first)
    pending_snapshot = store.snapshot()
    assert pending_snapshot.source == GNSS_SOURCE_MG902
    assert pending_snapshot.sat is not None
    assert pending_snapshot.cnr is None
    assert store.history() == old_history

    assert store.update(second)
    switched = store.snapshot()
    assert switched.source == GNSS_SOURCE_BYNAV
    assert switched.sat is None
    assert switched.cnr is not None
    assert len(switched.cnr.observations) == 2
    assert len(store.history()) == 1
    assert store.history()[0].source == GNSS_SOURCE_BYNAV


def test_unknown_native_source_is_not_treated_as_uninitialized() -> None:
    unknown_sat = bytearray(SAT_GOLDEN)
    unknown_sat[1] = 0
    unknown_signal = bytearray(SIGNAL_GOLDEN)
    unknown_signal[1] = 0
    store = GnssStore()
    assert store.update(decode_gnss_sat_report(bytes(unknown_sat)))
    assert store.update(decode_gnss_signal_report(bytes(unknown_signal)))
    assert store.source == 0
    assert store.snapshot().sat is not None
    assert store.snapshot().signal is not None
    assert store.snapshot().signal_namespace == SIGNAL_NAMESPACE_RAW
    assert signal_name(4, 5, SIGNAL_NAMESPACE_RAW) == "Signal 5"

    assert store.update(decode_gnss_signal_report(SIGNAL_GOLDEN))
    snapshot = store.snapshot()
    assert snapshot.source == GNSS_SOURCE_MG902
    assert snapshot.sat is None
    assert snapshot.signal is not None


def test_native_pending_is_visible_and_expires(monkeypatch) -> None:
    clock = [400.0]
    monkeypatch.setattr(gnss_store_module.time, "monotonic", lambda: clock[0])
    store = GnssStore()
    records = [GnssSignalRecord(0, 1, 0, -128, 30, 4, 0, 0, 0, 0x08)]
    partial = GnssSignalReport(1, 1, 5000, 10, 0, 2, 2, 0, records)
    assert not store.update(partial)
    assert store.signal_pending()
    assert not store.sky_pending()
    clock[0] = 403.1
    assert store.expire_incomplete()
    assert not store.signal_pending()


def test_playback_widget_follows_latest_after_store_clear(qapp) -> None:
    store = GnssStore(keep_history=True)
    widget = GnssWidget(store, playback=True)
    first = decode_gnss_sky_report(SKY_GOLDEN)
    first.timestamp = 1
    second = decode_gnss_sky_report(SKY_GOLDEN)
    second.timestamp = 2
    store.update(first)
    store.update(second)
    widget._select_history(0)
    assert not widget._follow_latest

    store.clear()
    new_first = decode_gnss_sky_report(SKY_GOLDEN)
    new_first.timestamp = 10
    new_second = decode_gnss_sky_report(SKY_GOLDEN)
    new_second.timestamp = 20
    store.update(new_first)
    store.update(new_second)
    assert widget._follow_latest
    assert widget._current_snapshot().timestamp == 20
    widget.close()


def test_nav_sat_does_not_create_signal_bars() -> None:
    store = GnssStore()
    assert store.update(decode_gnss_sat_report(SAT_GOLDEN))
    assert build_frequency_bars(store.snapshot()) == []
    assert store.update(decode_gnss_signal_report(SIGNAL_GOLDEN))
    snapshot = store.snapshot()
    assert snapshot.signal_namespace == SIGNAL_NAMESPACE_UBX_M9
    bars = build_frequency_bars(snapshot)
    assert len(bars) == 1
    assert bars[0].band == "Signal 5"


def test_mg902_store_reassembles_92_signal_records_out_of_order() -> None:
    records = [
        GnssSignalRecord(0, (index % 32) + 1, 0, -1, 30, 4, 0, 0, 0, 0x38)
        for index in range(92)
    ]
    store = GnssStore()
    second = GnssSignalReport(1, 1, 5000, 9, 1, 2, 92, 2, records[64:])
    first = GnssSignalReport(1, 1, 5000, 9, 0, 2, 92, 1, records[:64])
    assert not store.update(second)
    assert store.update(first)
    snapshot = store.snapshot().signal
    assert snapshot is not None
    assert len(snapshot.records) == 92
    assert snapshot.flags == 3


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


def test_sdb_v2_preserves_mg902_gnss_frames(tmp_path) -> None:
    path = tmp_path / "mg902-gnss.sdb"
    recorder = DataRecorder(path)
    assert recorder.start()
    assert recorder.write_frame(build_frame(CmdType.GNSS_SAT_REPORT, SAT_GOLDEN))
    assert recorder.write_frame(build_frame(CmdType.GNSS_SIGNAL_REPORT, SIGNAL_GOLDEN))
    assert recorder.stop()
    records = list(DataImporter.open_sdb(path).iter_records())
    assert [type(record) for record in records] == [GnssSatReport, GnssSignalReport]


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


def test_live_status_components_share_store_and_debug_off_clears(qapp, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    view = LiveView(settings=Settings())
    assert view._status_strip._states is view._state_store
    assert view._dashboard._states is view._state_store
    view._state_store.update("afd01", StateReport(100, [StateSample(2, 3)]))
    assert view._state_store.get_value("afd01", 2) == 3
    view._debug_enabled = True
    view._apply_debug_state(False, source="test")
    assert view._state_store.get_value("afd01", 2) is None
    view.close()


def test_playback_enables_only_when_gnss_records_exist(qapp, tmp_path) -> None:
    view = PlaybackView()
    assert not view._gnss_btn.isEnabled()
    view._state_store.update("afd01", StateReport(100, [StateSample(2, 3)]))

    path = tmp_path / "playback-gnss.sdb"
    recorder = DataRecorder(path)
    assert recorder.start()
    assert recorder.write_frame(build_frame(CmdType.GNSS_SKY_REPORT, SKY_GOLDEN))
    assert recorder.write_frame(build_frame(CmdType.GNSS_CNR_REPORT, CNR_GOLDEN))
    assert recorder.stop()

    view._load_file(path)
    assert view._state_store.get_value("afd01", 2) is None
    assert view._gnss_btn.isEnabled()
    assert len(view._gnss_store.history()) == 2
    view._on_clear_clicked()
    assert not view._gnss_btn.isEnabled()
    assert not view._gnss_store.has_data()
    view.close()
