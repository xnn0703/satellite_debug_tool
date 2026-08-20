"""Focused Orbit/TLE wire codec, paging store, and popup lifecycle tests."""

from __future__ import annotations

import struct
import time

import pytest
from PySide6.QtWidgets import QApplication

from satellite_debug_tool.core.data import OrbitStore
from satellite_debug_tool.core.protocol import (
    CmdType,
    FrameReceiverV2,
    OrbitCapabilitiesReport,
    OrbitCatalogReport,
    OrbitCurrentReport,
    OrbitSkyReport,
    OrbitOperation,
    OrbitPassPage,
    OrbitPredictionAccepted,
    OrbitPredictionPage,
    OrbitStatus,
    OrbitStatusReport,
    OrbitUploadProgress,
    build_frame,
    build_orbit_capabilities,
    build_orbit_catalog,
    build_orbit_current,
    build_orbit_sky_snapshot,
    build_orbit_predict,
    build_orbit_prediction_page,
    build_orbit_select,
    build_orbit_upload_begin,
    build_orbit_upload_chunk,
    build_orbit_upload_end,
    decode_orbit_report,
)
from satellite_debug_tool.ui.orbit_widget import OrbitWidget
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.ui.live_view import LiveView


def _header(operation: OrbitOperation, request_id: int = 7, status: OrbitStatus = OrbitStatus.OK) -> bytes:
    return struct.pack("<BBBI", 1, int(operation), int(status), request_id)


def test_orbit_request_wire_payloads_are_little_endian_and_bounded():
    frames = (
        build_orbit_capabilities(0x12345678),
        build_orbit_catalog(1, 2),
        build_orbit_current(2, 25544, 3),
        build_orbit_sky_snapshot(2, 42, 1),
        build_orbit_predict(3, 25544, 3600, 30, 5.0),
        build_orbit_prediction_page(4, 9, 2),
        build_orbit_select(5, 25544),
        build_orbit_upload_begin(6, 123, 0x89ABCDEF),
        build_orbit_upload_chunk(6, 0, b"abc"),
        build_orbit_upload_end(6),
    )
    assert all(frame[3] == CmdType.ORBIT_REQUEST for frame in frames)
    assert frames[0][6:12] == bytes.fromhex("010178563412")
    assert frames[1][6:14] == bytes.fromhex("0103010000000200")
    assert frames[3][6:18] == bytes.fromhex("010c020000002a0000000100")
    assert len(frames[-2]) <= 1024 + 12


def test_decode_capability_catalog_and_current_reports():
    capabilities = decode_orbit_report(
        _header(OrbitOperation.CAPABILITIES)
        + struct.pack("<IHIIHHf", 7, 128, 65536, 86400, 5, 4096, 7.0)
    )
    assert isinstance(capabilities, OrbitCapabilitiesReport)
    assert capabilities.max_catalog_entries == 128

    name = "ISS".encode()
    source = "/tle/tle.txt".encode()
    catalog = decode_orbit_report(
        _header(OrbitOperation.CATALOG)
        + struct.pack("<IHHBBBBIII", 4, 1, 0, 1, 0, 0, 1, 2, 3, 4)
        + struct.pack("<IIBB", 25544, 1723939200, len(name), len(source))
        + name
        + source
    )
    assert isinstance(catalog, OrbitCatalogReport)
    assert catalog.entries[0].norad_id == 25544
    assert catalog.invalid_records == 2

    current = decode_orbit_report(
        _header(OrbitOperation.CURRENT)
        + struct.pack("<IHHB", 4, 1, 0, 1)
        + struct.pack("<IQ7fB", 25544, 1723939200123, 1.0, 2.0, 400000.0, 90.0, 45.0, 700000.0, 2.5, 2)
    )
    assert isinstance(current, OrbitCurrentReport)
    assert current.samples[0].visible is True
    assert current.samples[0].stale is False


def _sky_report(
    *, snapshot_id: int = 42, page: int = 0, total: int = 1, norad_id: int = 25544,
    norad_ids: tuple[int, ...] | None = None,
    request_id: int = 7,
) -> OrbitSkyReport:
    ids = (norad_id,) if norad_ids is None else norad_ids
    fixed = struct.pack(
        "<IIQHHB6fbBIB",
        snapshot_id,
        4,
        1723939200123,
        total,
        page,
        len(ids),
        70.0,
        60.0,
        1.0,
        2.0,
        3.0,
        4.0,
        -1,
        0,
        25544,
        0,
    )
    items = b"".join(
        struct.pack(
            "<I9fB",
            item_id,
            1.0,
            2.0,
            400000.0,
            90.0,
            45.0,
            700000.0,
            120.0,
            30.0,
            2.5,
            30 if item_id == 25544 else 14,
        )
        for item_id in ids
    )
    report = decode_orbit_report(
        _header(OrbitOperation.SKY_SNAPSHOT, request_id=request_id) + fixed + items
    )
    assert isinstance(report, OrbitSkyReport)
    return report


def test_decode_array_sky_snapshot_contract():
    report = _sky_report()

    assert report.snapshot_id == 42
    assert report.hard_offaxis_limit_deg == pytest.approx(70.0)
    assert report.azimuth_direction == -1
    assert report.samples[0].array_azimuth_deg == pytest.approx(120.0)
    assert report.samples[0].in_hard_envelope
    assert report.samples[0].active_target


def test_store_replaces_array_sky_only_after_complete_snapshot():
    store = OrbitStore()
    first = _sky_report(snapshot_id=50, page=0, total=17, norad_ids=tuple(range(1, 17)))
    second = _sky_report(snapshot_id=50, page=1, total=17, norad_id=17)

    store.feed(first)
    assert store.sky_snapshot() is None
    store.feed(second)
    assert store.sky_snapshot() is not None
    assert [item.norad_id for item in store.sky_samples()] == list(range(1, 18))
    assert len(store.sky_trail(1)) == 1

    store.feed(_sky_report(snapshot_id=49, page=0, total=1, norad_id=3))
    assert [item.norad_id for item in store.sky_samples()] == list(range(1, 18))


def test_live_view_serializes_shared_sky_pages_for_all_consumers(monkeypatch):
    app = QApplication.instance() or QApplication([])
    view = LiveView(Settings())
    sent = []
    monkeypatch.setattr(view, "_send_control_frame", lambda frame: sent.append(frame) or True)
    view._is_connected = True
    view._orbit_store.feed(
        OrbitCapabilitiesReport(
            1,
            OrbitOperation.CAPABILITIES,
            OrbitStatus.OK,
            1,
            15,
            128,
            65536,
            86400,
            5,
            4096,
            7.0,
        )
    )
    view._orbit_sky_consumers.add("customer_overview")
    view._orbit_sky_consumers.add("orbit_popup")

    assert view.request_orbit_sky_refresh()
    assert not view.request_orbit_sky_refresh()
    assert len(sent) == 1
    first_request_id = struct.unpack_from("<I", sent[0], 8)[0]
    view._orbit_store.feed(
        _sky_report(
            snapshot_id=60,
            page=0,
            total=17,
            norad_ids=tuple(range(1, 17)),
            request_id=first_request_id,
        )
    )
    assert len(sent) == 2
    assert sent[-1][6:18] == bytes.fromhex("010c020000003c0000000100")
    second_request_id = struct.unpack_from("<I", sent[1], 8)[0]
    view._orbit_store.feed(
        _sky_report(snapshot_id=60, page=1, total=17, norad_id=17, request_id=second_request_id)
    )
    assert not view._orbit_sky_request_active
    assert [item.norad_id for item in view._orbit_store.sky_samples()] == list(range(1, 18))
    view.close()
    app.processEvents()


def test_live_view_recovers_a_timed_out_sky_request(monkeypatch):
    app = QApplication.instance() or QApplication([])
    view = LiveView(Settings())
    sent = []
    now = [100.0]
    monkeypatch.setattr(view, "_send_control_frame", lambda frame: sent.append(frame) or True)
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    view._is_connected = True
    view._orbit_store.feed(
        OrbitCapabilitiesReport(
            1,
            OrbitOperation.CAPABILITIES,
            OrbitStatus.OK,
            1,
            15,
            128,
            65536,
            86400,
            5,
            4096,
            7.0,
        )
    )
    view._orbit_sky_consumers.add("customer_overview")

    assert view.request_orbit_sky_refresh()
    now[0] += 3.1
    assert view.request_orbit_sky_refresh()
    assert len(sent) == 2
    assert view._orbit_sky_request_active
    view.close()
    app.processEvents()


def test_store_keeps_only_twenty_seconds_of_array_sky_trails():
    store = OrbitStore()
    base = _sky_report(snapshot_id=70)
    for snapshot_id, utc_ms in ((70, 100_000), (71, 110_000), (72, 131_000)):
        store.feed(
            OrbitSkyReport(
                base.version,
                base.operation,
                base.status,
                base.request_id,
                snapshot_id,
                base.generation,
                utc_ms,
                base.total_entries,
                base.page,
                base.hard_offaxis_limit_deg,
                base.recommended_offaxis_limit_deg,
                base.mount_yaw_deg,
                base.mount_pitch_deg,
                base.mount_roll_deg,
                base.azimuth_zero_offset_deg,
                base.azimuth_direction,
                base.second_angle_type,
                base.active_target_id,
                base.profile_characterized,
                base.samples,
            )
        )
    assert [point.utc_unix_ms for point in store.sky_trail(25544)] == [131_000]


def test_decode_prediction_sample_pass_and_upload_reports():
    accepted = decode_orbit_report(
        _header(OrbitOperation.PREDICT_SUBMIT)
        + struct.pack("<IIIQIH4fB", 11, 4, 25544, 1723939200123, 3600, 30, 5.0, 31.2, 121.5, 8.0, 1)
    )
    assert isinstance(accepted, OrbitPredictionAccepted)
    assert accepted.assumption_flags == 1
    assert accepted.station_latitude_deg == pytest.approx(31.2)

    samples = decode_orbit_report(
        _header(OrbitOperation.PREDICT_PAGE)
        + struct.pack("<BIHIHB", 0, 11, 0, 4, 1, 1)
        + struct.pack("<Q7fB", 1723939200123, 1.0, 2.0, 400000.0, 90.0, 45.0, 700000.0, 2.5, 3)
    )
    assert isinstance(samples, OrbitPredictionPage)
    assert samples.samples[0].stale and samples.samples[0].visible

    passes = decode_orbit_report(
        _header(OrbitOperation.PREDICT_PAGE)
        + struct.pack("<BIHIHB", 1, 12, 0, 4, 1, 1)
        + struct.pack("<IQQQfB", 25544, 1000, 2000, 1500, 55.0, 1)
    )
    assert isinstance(passes, OrbitPassPage)
    assert passes.passes[0].has_pass

    progress = decode_orbit_report(
        _header(OrbitOperation.UPLOAD_CHUNK, request_id=8) + struct.pack("<I", 768)
    )
    assert isinstance(progress, OrbitUploadProgress)
    assert progress.acknowledged_size == 768
    failure = decode_orbit_report(
        _header(OrbitOperation.UPLOAD_END, request_id=8, status=OrbitStatus.CRC_ERROR)
    )
    assert isinstance(failure, OrbitStatusReport)


def test_receiver_rejects_truncated_orbit_payload_without_losing_next_frame():
    bad = build_frame(CmdType.ORBIT_REPORT, _header(OrbitOperation.CURRENT) + b"\x00")
    good_payload = _header(OrbitOperation.SCAN)
    records = FrameReceiverV2().feed(bad + build_frame(CmdType.ORBIT_REPORT, good_payload))
    assert len(records) == 1
    assert isinstance(records[0], OrbitStatusReport)


def test_store_merges_pages_and_invalidates_prediction_on_generation_change():
    store = OrbitStore()
    accepted = decode_orbit_report(
        _header(OrbitOperation.PREDICT_SUBMIT)
        + struct.pack("<IIIQIH4fB", 11, 4, 25544, 1000, 60, 5, 0.0, 0.0, 0.0, 0.0, 1)
    )
    store.feed(accepted)
    assert store.prediction_job is not None
    for page, norad in ((1, 2), (0, 1)):
        name = f"SAT-{norad}".encode()
        payload = (
            _header(OrbitOperation.CATALOG)
            + struct.pack("<IHHBBBBIII", 4, 2, page, 1, 0, 0, 1, 0, 0, 0)
            + struct.pack("<IIBB", norad, 100, len(name), 1)
            + name
            + b"x"
        )
        store.feed(decode_orbit_report(payload))
    assert [item.norad_id for item in store.catalog_snapshot().entries] == [1, 2]
    store.feed(
        decode_orbit_report(
            _header(OrbitOperation.CATALOG)
            + struct.pack("<IHHBBBBIII", 5, 0, 0, 0, 0, 0, 1, 0, 0, 0)
        )
    )
    assert store.prediction_job is None
    assert store.catalog_snapshot().generation == 5


def test_store_drops_late_catalog_current_and_prediction_generations():
    store = OrbitStore()

    def catalog(generation: int, request_id: int) -> OrbitCatalogReport:
        return decode_orbit_report(
            _header(OrbitOperation.CATALOG, request_id=request_id)
            + struct.pack("<IHHBBBBIII", generation, 0, 0, 0, 0, 0, 1, 0, 0, 0)
        )

    store.feed(catalog(8, 1))
    store.feed(catalog(9, 2))
    store.feed(catalog(8, 1))
    assert store.catalog_snapshot().generation == 9

    stale_current = decode_orbit_report(
        _header(OrbitOperation.CURRENT, request_id=3)
        + struct.pack("<IHHB", 8, 0, 0, 0)
    )
    store.feed(stale_current)
    assert store.current_total == 0

    accepted = decode_orbit_report(
        _header(OrbitOperation.PREDICT_SUBMIT, request_id=4)
        + struct.pack("<IIIQIH4fB", 21, 9, 25544, 1000, 60, 5, 0.0, 0.0, 0.0, 0.0, 1)
    )
    store.feed(accepted)
    wrong_generation_page = decode_orbit_report(
        _header(OrbitOperation.PREDICT_PAGE, request_id=5)
        + struct.pack("<BIHIHB", 0, 21, 0, 8, 1, 0)
    )
    store.feed(wrong_generation_page)
    assert store.prediction_total == 0


def test_popup_stops_refresh_timer_when_hidden(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    sent = []
    counter = iter(range(1, 100))
    widget = OrbitWidget(OrbitStore(), lambda frame: sent.append(frame) or True, counter.__next__, lambda: True)
    widget.show()
    app.processEvents()
    assert widget._refresh_timer.isActive()
    assert sent and sent[0][3] == CmdType.ORBIT_REQUEST
    widget.hide()
    app.processEvents()
    assert not widget._refresh_timer.isActive()
