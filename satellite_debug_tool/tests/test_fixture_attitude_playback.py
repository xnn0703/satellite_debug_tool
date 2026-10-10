"""A15 fixture attitude recording, alignment, and playback regressions."""

from __future__ import annotations

import struct
import math

import pytest
from PySide6.QtCore import Qt

from satellite_debug_tool.core.playback import (
    HostUptimeAligner,
    aligned_fixture_reports,
    build_customer_playback,
)
from satellite_debug_tool.core.playback.series_provider import PlaybackSeriesProvider
from satellite_debug_tool.core.playback.fixture_attitude import (
    MS6222_RELATIVE_CHANNELS,
    PLATFORM_TARGET_CHANNELS,
)
from satellite_debug_tool.core.production import (
    DeviceSession,
    FixtureAttitudeSample,
    FixtureAttitudeSource,
    FixtureTelemetryBus,
)
from satellite_debug_tool.core.protocol import ChannelSample, CmdType, DataReport, build_frame
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.profile.cache import profile_to_dict
from satellite_debug_tool.core.protocol import ChannelDefEntry
from satellite_debug_tool.io import DataRecorder
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.io.data_recorder import SDB_VERSION_V3
from satellite_debug_tool.ui.customer_playback_view import CustomerPlaybackView
from satellite_debug_tool.ui.live_view import LiveView
from satellite_debug_tool.ui.playback_view import PlaybackView


def _fast(timestamp: int, yaw: float = 0.0) -> bytes:
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
        15.0,
    )
    return build_frame(CmdType.SERVICE_FAST_STATE, payload)


def _data(timestamp: int, samples: list[tuple[int, float]]) -> bytes:
    payload = struct.pack("<I", timestamp) + bytes([len(samples)])
    for channel_id, value in samples:
        payload += bytes([channel_id]) + struct.pack("<f", value)
    return build_frame(CmdType.DATA_REPORT, payload)


def _sample(source: FixtureAttitudeSource, wall_ns: int, value: float = 1.0):
    return FixtureAttitudeSample(
        source=source,
        host_monotonic_ns=wall_ns + 7,
        host_wall_time_ns=wall_ns,
        roll_deg=value,
        pitch_deg=value + 1.0,
        yaw_deg=value + 2.0,
        fixture_profile_id="fixture-a6",
        fixture_profile_sha256="a" * 64,
        calibration_id="MS6222-CAL-TEST",
        control_session_id="fixture-session-test",
    )


def test_fixture_sample_rejects_false_validity_and_non_finite_values() -> None:
    with pytest.raises(ValueError, match="requires roll"):
        FixtureAttitudeSample(
            source=FixtureAttitudeSource.PLATFORM_TARGET,
            host_monotonic_ns=1,
            host_wall_time_ns=1,
        )
    with pytest.raises(ValueError, match="finite"):
        FixtureAttitudeSample(
            source=FixtureAttitudeSource.PLATFORM_TARGET,
            host_monotonic_ns=1,
            host_wall_time_ns=1,
            roll_deg=float("nan"),
            pitch_deg=0.0,
            yaw_deg=0.0,
        )
    with pytest.raises(ValueError, match="requires a reason"):
        FixtureAttitudeSample(
            source=FixtureAttitudeSource.MS6222_RELATIVE,
            host_monotonic_ns=1,
            host_wall_time_ns=1,
            valid=False,
        )


def test_ms_telemetry_is_rate_limited_but_validity_transition_is_kept() -> None:
    bus = FixtureTelemetryBus(ms_rate_hz=50.0)
    published = []
    bus.sample_published.connect(published.append)
    first = _sample(FixtureAttitudeSource.MS6222_RELATIVE, 1_000_000_000)
    second = FixtureAttitudeSample(
        **{
            **first.__dict__,
            "sample_id": "second",
            "host_monotonic_ns": first.host_monotonic_ns + 1_000_000,
            "host_wall_time_ns": first.host_wall_time_ns + 1_000_000,
        }
    )
    invalid = FixtureAttitudeSample(
        source=FixtureAttitudeSource.MS6222_RELATIVE,
        host_monotonic_ns=first.host_monotonic_ns + 2_000_000,
        host_wall_time_ns=first.host_wall_time_ns + 2_000_000,
        valid=False,
        invalid_reason="reference_quality_invalid",
    )

    assert bus.publish(first)
    assert not bus.publish(second)
    assert bus.publish(invalid)
    assert published == [first, invalid]


def test_host_alignment_never_crosses_reset_or_long_gap() -> None:
    aligner = HostUptimeAligner()
    aligner.add_anchor(1_000_000_000, 100.0)
    aligner.add_anchor(2_000_000_000, 1100.0)
    assert aligner.align(1_500_000_000)[0] == pytest.approx(600.0)
    aligner.add_anchor(3_000_000_000, 50.0)
    aligner.add_anchor(4_000_000_000, 1050.0)
    assert aligner.align(2_500_000_000) is None
    assert aligner.align(3_500_000_000)[0] == pytest.approx(550.0)

    gap = HostUptimeAligner()
    gap.add_anchor(1_000_000_000, 100.0)
    gap.add_anchor(8_000_000_000, 7100.0)
    assert gap.align(4_000_000_000) is None

    drift = HostUptimeAligner()
    drift.add_anchor(1_000_000_000, 100.0)
    drift.add_anchor(2_000_000_000, 5000.0)
    assert drift.align(1_500_000_000) is None


def test_fixture_metadata_round_trip_and_customer_playback_overlay(
    qapplication_session,
    tmp_path,
) -> None:
    path = tmp_path / "fixture-overlay.sdb"
    recorder = DataRecorder(
        path,
        format_version=SDB_VERSION_V3,
        metadata={"hardware_type": "afd01", "capture_profile": "support_full"},
    )
    assert recorder.start()
    assert recorder.write_frame(_fast(100, 3.0), host_timestamp_ns=1_000_000_000)
    target = _sample(FixtureAttitudeSource.PLATFORM_TARGET, 2_000_000_000, 4.0)
    reference = _sample(FixtureAttitudeSource.MS6222_RELATIVE, 2_100_000_000, 4.2)
    invalid_reference = FixtureAttitudeSample(
        source=FixtureAttitudeSource.MS6222_RELATIVE,
        host_monotonic_ns=2_200_000_007,
        host_wall_time_ns=2_200_000_000,
        valid=False,
        invalid_reason="reference_quality_invalid",
        fixture_profile_id="fixture-a6",
        fixture_profile_sha256="a" * 64,
        calibration_id="MS6222-CAL-TEST",
        control_session_id="fixture-session-test",
    )
    assert recorder.write_metadata_event(
        target.metadata_event(), host_timestamp_ns=target.host_wall_time_ns
    )
    assert recorder.write_metadata_event(
        reference.metadata_event(), host_timestamp_ns=reference.host_wall_time_ns
    )
    assert recorder.write_metadata_event(
        invalid_reference.metadata_event(),
        host_timestamp_ns=invalid_reference.host_wall_time_ns,
    )
    assert recorder.write_frame(_fast(2100, 5.0), host_timestamp_ns=3_000_000_000)
    assert recorder.stop()

    sdb = DataImporter.open_sdb(path)
    assert [event["sample_id"] for event in sdb.metadata_events] == [
        target.sample_id,
        reference.sample_id,
        invalid_reference.sample_id,
    ]
    result = build_customer_playback(path)
    assert result.fixture_alignment.recorded_samples == 3
    assert result.fixture_alignment.aligned_samples == 2
    assert result.fixture_alignment.invalid_samples == 1
    assert result.fixture_alignment.gap_markers[0].reason == "reference_quality_invalid"
    store = result.provider.load_window(max_points_per_channel=6000)
    assert store.get_channel_by_id(PLATFORM_TARGET_CHANNELS["roll"]).get_latest()[1] == pytest.approx(4.0)
    reference_values = store.get_channel_by_id(
        MS6222_RELATIVE_CHANNELS["yaw"]
    ).get_values()
    assert any(float(value) == pytest.approx(6.2) for value in reference_values)
    assert math.isnan(float(reference_values[-1]))
    result.close()

    view = CustomerPlaybackView(type("Settings", (), {"get": lambda _s, _k, d=None: d, "set": lambda *_a: None})())
    assert view.load_file(path)
    assert set(PLATFORM_TARGET_CHANNELS.values()) <= set(view.chart._curves)
    assert set(MS6222_RELATIVE_CHANNELS.values()) <= set(view.chart._curves)
    target_pen = view.chart._curves[PLATFORM_TARGET_CHANNELS["roll"]][1].opts["pen"]
    reference_pen = view.chart._curves[MS6222_RELATIVE_CHANNELS["roll"]][1].opts["pen"]
    assert target_pen.style() == Qt.PenStyle.DashLine
    assert reference_pen.style() == Qt.PenStyle.DotLine
    assert "2/3" in view._quality_label.text()
    assert len(view.chart._event_lines) == 1
    view.shutdown()
    view.close()


def test_invalid_fixture_sample_becomes_gap_and_unaligned_sample_is_excluded() -> None:
    aligner = HostUptimeAligner()
    aligner.add_anchor(1_000_000_000, 0.0)
    aligner.add_anchor(2_000_000_000, 1000.0)
    invalid = _sample(FixtureAttitudeSource.MS6222_RELATIVE, 1_500_000_000).metadata_event()
    invalid.update({"valid": False, "invalid_reason": "quality"})
    outside = _sample(FixtureAttitudeSource.PLATFORM_TARGET, 3_000_000_000).metadata_event()

    reports, summary = aligned_fixture_reports((invalid, outside), aligner)

    assert len(reports) == 1
    assert all(
        sample.value != sample.value
        for sample in reports[0].samples
    )
    assert summary.recorded_samples == 2
    assert summary.invalid_samples == 1
    assert summary.unaligned_samples == 1
    assert summary.gap_markers[0].timestamp_ms == pytest.approx(500.0)
    assert summary.gap_markers[0].reason == "quality"


def test_playback_provider_preserves_nan_gap_during_peak_downsampling() -> None:
    provider = PlaybackSeriesProvider()
    for timestamp in range(100):
        provider.append_report(
            DataReport(
                float(timestamp),
                [ChannelSample(1003, float("nan") if timestamp == 50 else float(timestamp))],
            )
        )
    provider.finalize()

    store = provider.load_window(max_points_per_channel=64)
    values = store.get_channel_by_id(1003).get_values()
    times = store.get_channel_by_id(1003).get_times()
    assert any(math.isnan(float(value)) for value in values)
    assert times.tolist() == sorted(times.tolist())
    provider.close()


def test_live_recorder_subscribes_to_fixture_bus(
    qapplication_session,
    tmp_path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.config import Settings

    monkeypatch.setenv("HOME", str(tmp_path))
    bus = FixtureTelemetryBus()
    live = LiveView(Settings(), fixture_telemetry=bus, defer_presentation=True)
    recorder = DataRecorder(tmp_path / "live-fixture.sdb", format_version=SDB_VERSION_V3)
    assert recorder.start()
    live._recorder = recorder
    live._is_recording = True
    sample = _sample(FixtureAttitudeSource.PLATFORM_TARGET, 2_000_000_000)

    assert bus.publish(sample)
    live._is_recording = False
    live._recorder = None
    assert recorder.stop()
    events = DataImporter.open_sdb(recorder.filepath).metadata_events
    assert len(events) == 1
    assert events[0]["sample_id"] == sample.sample_id
    live.close()


def test_engineering_playback_adds_fixture_curves_to_device_attitude_group(
    qapplication_session,
    tmp_path,
) -> None:
    profile_store = ProfileStore(cache=None)
    profile_store.apply_channel_define(
        "afd01",
        1,
        [
            ChannelDefEntry(1, 1, 7, 2, "roll", "deg", -180.0, 180.0),
            ChannelDefEntry(2, 1, 7, 2, "pitch", "deg", -90.0, 90.0),
            ChannelDefEntry(3, 1, 7, 2, "yaw", "deg", 0.0, 360.0),
        ],
    )
    path = tmp_path / "engineering-fixture.sdb"
    recorder = DataRecorder(
        path,
        profile_dict=profile_to_dict(profile_store.get_profile("afd01")),
        format_version=SDB_VERSION_V3,
    )
    assert recorder.start()
    assert recorder.write_frame(
        _data(100, [(1, 0.0), (2, 0.0), (3, 10.0)]),
        host_timestamp_ns=1_000_000_000,
    )
    sample = _sample(FixtureAttitudeSource.PLATFORM_TARGET, 2_000_000_000, 2.0)
    assert recorder.write_metadata_event(
        sample.metadata_event(), host_timestamp_ns=sample.host_wall_time_ns
    )
    assert recorder.write_frame(
        _data(2100, [(1, 1.0), (2, 1.0), (3, 11.0)]),
        host_timestamp_ns=3_000_000_000,
    )
    assert recorder.stop()

    view = PlaybackView()
    view._load_file(path)
    view._chart.set_mode("stacked")

    assert set(PLATFORM_TARGET_CHANNELS.values()) <= set(view._chart._curves)
    assert all(
        view._chart._curves[channel_id][0] == 7
        for channel_id in PLATFORM_TARGET_CHANNELS.values()
    )
    assert "1/1" in view._fixture_label.text()
    view.shutdown()
    view.close()


def test_same_fixture_sample_is_written_to_each_device_owned_sdb(tmp_path) -> None:
    first = DeviceSession(("192.168.1.11", 4004), 1)
    second = DeviceSession(("192.168.1.12", 4004), 2)
    root = tmp_path / "batch"
    assert first.arm_recording("batch-a", root)
    assert second.arm_recording("batch-a", root)
    sample = _sample(FixtureAttitudeSource.MS6222_RELATIVE, 2_000_000_000)

    assert first.record_fixture_attitude_sample(sample)
    assert second.record_fixture_attitude_sample(sample)
    first_path = first.finalize_recording()
    second_path = second.finalize_recording()

    assert first_path is not None and second_path is not None
    first_event = DataImporter.open_sdb(first_path).metadata_events[0]
    second_event = DataImporter.open_sdb(second_path).metadata_events[0]
    assert first_event["sample_id"] == sample.sample_id
    assert second_event["sample_id"] == sample.sample_id


def test_live_recorder_surfaces_fixture_queue_failure(
    qapplication_session,
    tmp_path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.config import Settings

    class FullRecorder:
        format_version = SDB_VERSION_V3

        def write_metadata_event(self, *_args, **_kwargs):
            return False

    monkeypatch.setenv("HOME", str(tmp_path))
    live = LiveView(Settings(), defer_presentation=True)
    live._is_recording = True
    live._recorder = FullRecorder()
    messages = []
    live.status_message.connect(lambda message, _timeout: messages.append(message))

    assert not live.record_fixture_attitude_sample(
        _sample(FixtureAttitudeSource.PLATFORM_TARGET, 2_000_000_000)
    )
    assert messages
    live._is_recording = False
    live._recorder = None
    live.close()
