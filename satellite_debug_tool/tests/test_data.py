import pytest
import numpy as np
from satellite_debug_tool.core.data.channel_buffer import ChannelBuffer
from satellite_debug_tool.core.data.data_store import DataStore
from satellite_debug_tool.core.protocol import DataFrame, ChannelData


class TestChannelBuffer:
    def test_init(self):
        buf = ChannelBuffer("TEST", 100)
        assert buf.name == "TEST"
        assert buf.get_latest() is None

    def test_append_single(self):
        buf = ChannelBuffer("TEST", 100)
        buf.append(1000.0, 1.5)
        latest = buf.get_latest()
        assert latest is not None
        assert latest[0] == 1000.0
        assert latest[1] == 1.5

    def test_append_multiple(self):
        buf = ChannelBuffer("TEST", 10)
        for i in range(20):
            buf.append(float(i), float(i * 2))
        times = buf.get_times()
        assert len(times) == 10
        assert times[-1] == 19.0

    def test_circular_wrap(self):
        buf = ChannelBuffer("TEST", 5)
        for i in range(10):
            buf.append(float(i), float(i))
        times = buf.get_times()
        assert len(times) == 5
        assert times[0] == 5.0
        assert times[-1] == 9.0

    def test_get_values(self):
        buf = ChannelBuffer("TEST", 100)
        buf.append(1.0, 10.0)
        buf.append(2.0, 20.0)
        values = buf.get_values()
        assert len(values) == 2
        assert values[0] == 10.0
        assert values[1] == 20.0

    def test_empty_buffer(self):
        buf = ChannelBuffer("TEST", 100)
        assert buf.get_times().size == 0
        assert buf.get_values().size == 0
        assert buf.get_latest() is None


class TestDataStore:
    def test_init(self):
        store = DataStore()
        assert len(store.get_all_channels()) == 0
        assert store.frame_count == 0

    def test_update_single_channel(self):
        store = DataStore()
        frame = DataFrame(
            cmd_type=1, timestamp=1000, channels=[ChannelData("GPS_LAT", 31.23)]
        )
        store.update(frame)
        assert "GPS_LAT" in store.get_all_channels()
        assert store.frame_count == 1

    def test_update_multiple_channels(self):
        store = DataStore()
        frame = DataFrame(
            cmd_type=1,
            timestamp=1000,
            channels=[
                ChannelData("GPS_LAT", 31.23),
                ChannelData("GPS_LON", 121.47),
            ],
        )
        store.update(frame)
        assert len(store.get_all_channels()) == 2
        assert store.frame_count == 1

    def test_update_multiple_frames(self):
        store = DataStore()
        for i in range(10):
            frame = DataFrame(
                cmd_type=1,
                timestamp=float(i * 100),
                channels=[ChannelData("CH1", float(i))],
            )
            store.update(frame)
        assert store.frame_count == 10
        ch = store.get_channel("CH1")
        assert ch is not None
        assert len(ch.get_values()) == 10

    def test_get_channel_not_exists(self):
        store = DataStore()
        assert store.get_channel("NOT_EXIST") is None

    def test_clear(self):
        store = DataStore()
        frame = DataFrame(
            cmd_type=1, timestamp=1000, channels=[ChannelData("GPS_LAT", 31.23)]
        )
        store.update(frame)
        store.clear()
        assert len(store.get_all_channels()) == 0
        assert store.frame_count == 0
