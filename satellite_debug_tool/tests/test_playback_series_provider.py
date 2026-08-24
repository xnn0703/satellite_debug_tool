"""M21 bounded playback cache and background loading contracts."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from satellite_debug_tool.core.playback import PlaybackSeriesProvider
from satellite_debug_tool.core.protocol import ChannelSample, DataReport


def _report(timestamp_ms: int, value: float) -> DataReport:
    return DataReport(timestamp_ms, [ChannelSample(3, value)])


def test_small_window_preserves_every_sample_and_removes_cache() -> None:
    provider = PlaybackSeriesProvider()
    cache_path = provider.path
    for index in range(20):
        provider.append_report(_report(index * 10, float(index)))
    provider.finalize()

    store = provider.load_window(50, 100, max_points_per_channel=64)
    channel = store.get_channel_by_id(3)
    assert channel is not None
    assert channel.get_times().tolist() == pytest.approx([50, 60, 70, 80, 90, 100])
    assert channel.get_values().tolist() == pytest.approx([5, 6, 7, 8, 9, 10])

    provider.close()
    assert not cache_path.exists()


def test_large_window_is_bounded_and_keeps_extrema() -> None:
    provider = PlaybackSeriesProvider()
    for index in range(20_000):
        value = math.sin(index / 50.0)
        if index == 10_321:
            value = 50.0
        provider.append_report(_report(index * 10, value))
    provider.finalize()

    store = provider.load_window(max_points_per_channel=400)
    channel = store.get_channel_by_id(3)
    assert channel is not None
    assert len(channel) <= 400
    assert float(channel.get_values().max()) == pytest.approx(50.0)
    provider.close()


def test_query_replaces_memory_by_window_not_recording_size() -> None:
    provider = PlaybackSeriesProvider()
    for index in range(50_000):
        provider.append_report(_report(index * 20, float(index % 100)))
    provider.finalize()

    first = provider.load_window(0, 10_000, max_points_per_channel=256)
    last = provider.load_window(980_000, 999_980, max_points_per_channel=256)
    assert len(first.get_channel_by_id(3)) <= 256
    assert len(last.get_channel_by_id(3)) <= 256
    provider.close()
