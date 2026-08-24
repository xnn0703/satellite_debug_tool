"""Disk-backed telemetry series with bounded window queries.

The SDB file remains the authoritative recording.  This provider is a disposable
query cache: the loader streams decoded ``DataReport`` objects into SQLite and
builds fixed time-bucket extrema.  A chart query therefore materializes only a
pixel-sized window instead of every sample in the recording.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Iterable, Optional

from satellite_debug_tool.core.data import DataStore
from satellite_debug_tool.core.protocol import ChannelSample, DataReport


_BUCKET_LEVELS_MS = (100, 500, 1_000, 5_000, 10_000, 30_000, 60_000)
_INSERT_BATCH = 4096


@dataclass(frozen=True)
class SeriesSummary:
    report_count: int
    sample_count: int
    first_timestamp_ms: Optional[float]
    last_timestamp_ms: Optional[float]
    channel_counts: dict[int, int]


@dataclass
class _PeakBucket:
    bucket_id: int
    first_ts: float
    first_value: float
    min_ts: float
    min_value: float
    max_ts: float
    max_value: float
    last_ts: float
    last_value: float

    @classmethod
    def create(cls, bucket_id: int, timestamp: float, value: float) -> "_PeakBucket":
        return cls(
            bucket_id=bucket_id,
            first_ts=timestamp,
            first_value=value,
            min_ts=timestamp,
            min_value=value,
            max_ts=timestamp,
            max_value=value,
            last_ts=timestamp,
            last_value=value,
        )

    def add(self, timestamp: float, value: float) -> None:
        self.last_ts = timestamp
        self.last_value = value
        if value < self.min_value:
            self.min_ts = timestamp
            self.min_value = value
        if value > self.max_value:
            self.max_ts = timestamp
            self.max_value = value


class PlaybackSeriesProvider:
    """Disposable on-disk cache for decoded channel samples."""

    def __init__(self, cache_path: Optional[Path] = None) -> None:
        if cache_path is None:
            descriptor, raw_path = tempfile.mkstemp(prefix="satellite-playback-", suffix=".sqlite3")
            os.close(descriptor)
            self._path = Path(raw_path)
            self._owns_path = True
        else:
            self._path = Path(cache_path)
            self._owns_path = False
        self._connection = sqlite3.connect(str(self._path))
        self._connection.execute("PRAGMA journal_mode=OFF")
        self._connection.execute("PRAGMA synchronous=OFF")
        self._connection.execute("PRAGMA temp_store=MEMORY")
        self._connection.executescript(
            """
            CREATE TABLE samples (
                channel_id INTEGER NOT NULL,
                timestamp_ms REAL NOT NULL,
                value REAL NOT NULL
            );
            CREATE TABLE peaks (
                level_ms INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                bucket_id INTEGER NOT NULL,
                first_ts REAL NOT NULL,
                first_value REAL NOT NULL,
                min_ts REAL NOT NULL,
                min_value REAL NOT NULL,
                max_ts REAL NOT NULL,
                max_value REAL NOT NULL,
                last_ts REAL NOT NULL,
                last_value REAL NOT NULL
            );
            """
        )
        self._sample_rows: list[tuple[int, float, float]] = []
        self._peak_rows: list[tuple] = []
        self._buckets: dict[tuple[int, int], _PeakBucket] = {}
        self._report_count = 0
        self._sample_count = 0
        self._channel_counts: dict[int, int] = {}
        self._first_timestamp_ms: Optional[float] = None
        self._last_timestamp_ms: Optional[float] = None
        self._finalized = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def summary(self) -> SeriesSummary:
        return SeriesSummary(
            report_count=self._report_count,
            sample_count=self._sample_count,
            first_timestamp_ms=self._first_timestamp_ms,
            last_timestamp_ms=self._last_timestamp_ms,
            channel_counts=dict(self._channel_counts),
        )

    def append_report(self, report: DataReport) -> None:
        if self._finalized:
            raise RuntimeError("playback series is already finalized")
        timestamp = float(report.timestamp)
        self._report_count += 1
        self._first_timestamp_ms = (
            timestamp
            if self._first_timestamp_ms is None
            else min(self._first_timestamp_ms, timestamp)
        )
        self._last_timestamp_ms = (
            timestamp
            if self._last_timestamp_ms is None
            else max(self._last_timestamp_ms, timestamp)
        )
        for sample in report.samples:
            channel_id = int(sample.channel_id)
            value = float(sample.value)
            self._sample_rows.append((channel_id, timestamp, value))
            self._sample_count += 1
            self._channel_counts[channel_id] = self._channel_counts.get(channel_id, 0) + 1
            for level_ms in _BUCKET_LEVELS_MS:
                bucket_id = math.floor(timestamp / level_ms)
                key = (level_ms, channel_id)
                bucket = self._buckets.get(key)
                if bucket is None:
                    self._buckets[key] = _PeakBucket.create(bucket_id, timestamp, value)
                elif bucket.bucket_id == bucket_id:
                    bucket.add(timestamp, value)
                else:
                    self._queue_peak(level_ms, channel_id, bucket)
                    self._buckets[key] = _PeakBucket.create(bucket_id, timestamp, value)
        self._flush_batches()

    def finalize(self) -> SeriesSummary:
        if self._finalized:
            return self.summary
        for (level_ms, channel_id), bucket in tuple(self._buckets.items()):
            self._queue_peak(level_ms, channel_id, bucket)
        self._buckets.clear()
        self._flush_batches(force=True)
        self._connection.executescript(
            """
            CREATE INDEX samples_channel_time ON samples(channel_id, timestamp_ms);
            CREATE INDEX peaks_level_channel_bucket ON peaks(level_ms, channel_id, bucket_id);
            """
        )
        self._connection.commit()
        self._connection.close()
        self._finalized = True
        return self.summary

    def load_window(
        self,
        start_timestamp_ms: Optional[float] = None,
        end_timestamp_ms: Optional[float] = None,
        *,
        max_points_per_channel: int = 6000,
    ) -> DataStore:
        """Load a bounded, extrema-preserving chart window."""

        if not self._finalized:
            raise RuntimeError("playback series must be finalized before querying")
        summary = self.summary
        if summary.first_timestamp_ms is None or summary.last_timestamp_ms is None:
            return DataStore(max_channels=64, buffer_capacity=max_points_per_channel)
        start = float(
            summary.first_timestamp_ms if start_timestamp_ms is None else start_timestamp_ms
        )
        end = float(summary.last_timestamp_ms if end_timestamp_ms is None else end_timestamp_ms)
        if end < start:
            start, end = end, start
        point_budget = max(64, int(max_points_per_channel))
        store = DataStore(max_channels=64, buffer_capacity=point_budget)
        with sqlite3.connect(str(self._path)) as connection:
            counts = connection.execute(
                """
                SELECT channel_id, COUNT(*)
                FROM samples
                WHERE timestamp_ms >= ? AND timestamp_ms <= ?
                GROUP BY channel_id
                """,
                (start, end),
            ).fetchall()
            max_count = max((int(row[1]) for row in counts), default=0)
            if max_count <= point_budget:
                rows: Iterable[tuple[int, float, float]] = connection.execute(
                    """
                    SELECT channel_id, timestamp_ms, value
                    FROM samples
                    WHERE timestamp_ms >= ? AND timestamp_ms <= ?
                    ORDER BY channel_id, timestamp_ms
                    """,
                    (start, end),
                )
                self._append_rows(store, rows)
                return store

            level_ms = self._select_level(end - start, point_budget)
            first_bucket = math.floor(start / level_ms) - 1
            last_bucket = math.floor(end / level_ms) + 1
            rows = connection.execute(
                """
                SELECT channel_id, first_ts, first_value, min_ts, min_value,
                       max_ts, max_value, last_ts, last_value
                FROM peaks
                WHERE level_ms = ? AND bucket_id >= ? AND bucket_id <= ?
                ORDER BY channel_id, bucket_id
                """,
                (level_ms, first_bucket, last_bucket),
            )
            current_channel: Optional[int] = None
            channel_points: list[tuple[float, float]] = []
            for row in rows:
                channel_id = int(row[0])
                if current_channel is not None and channel_id != current_channel:
                    self._append_channel_points(store, current_channel, channel_points, start, end)
                    channel_points = []
                current_channel = channel_id
                channel_points.extend(
                    (
                        (float(row[1]), float(row[2])),
                        (float(row[3]), float(row[4])),
                        (float(row[5]), float(row[6])),
                        (float(row[7]), float(row[8])),
                    )
                )
            if current_channel is not None:
                self._append_channel_points(store, current_channel, channel_points, start, end)
        return store

    @staticmethod
    def _select_level(duration_ms: float, point_budget: int) -> int:
        for level_ms in _BUCKET_LEVELS_MS:
            estimated = (max(1.0, duration_ms) / level_ms + 2.0) * 4.0
            if estimated <= point_budget:
                return level_ms
        return _BUCKET_LEVELS_MS[-1]

    @staticmethod
    def _append_rows(store: DataStore, rows: Iterable[tuple[int, float, float]]) -> None:
        for channel_id, timestamp, value in rows:
            store.update(
                DataReport(
                    float(timestamp),
                    [ChannelSample(int(channel_id), float(value))],
                )
            )

    @staticmethod
    def _append_channel_points(
        store: DataStore,
        channel_id: int,
        points: list[tuple[float, float]],
        start: float,
        end: float,
    ) -> None:
        previous: Optional[tuple[float, float]] = None
        for timestamp, value in sorted(points, key=lambda item: item[0]):
            current = (timestamp, value)
            if current == previous or timestamp < start or timestamp > end:
                continue
            store.update(DataReport(timestamp, [ChannelSample(channel_id, value)]))
            previous = current

    def close(self) -> None:
        if not self._finalized:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass
            self._finalized = True
        if self._owns_path:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass

    def _queue_peak(self, level_ms: int, channel_id: int, bucket: _PeakBucket) -> None:
        self._peak_rows.append(
            (
                level_ms,
                channel_id,
                bucket.bucket_id,
                bucket.first_ts,
                bucket.first_value,
                bucket.min_ts,
                bucket.min_value,
                bucket.max_ts,
                bucket.max_value,
                bucket.last_ts,
                bucket.last_value,
            )
        )

    def _flush_batches(self, *, force: bool = False) -> None:
        if force or len(self._sample_rows) >= _INSERT_BATCH:
            if self._sample_rows:
                self._connection.executemany(
                    "INSERT INTO samples(channel_id, timestamp_ms, value) VALUES (?, ?, ?)",
                    self._sample_rows,
                )
                self._sample_rows.clear()
        if force or len(self._peak_rows) >= _INSERT_BATCH:
            if self._peak_rows:
                self._connection.executemany(
                    """
                    INSERT INTO peaks(
                        level_ms, channel_id, bucket_id,
                        first_ts, first_value, min_ts, min_value,
                        max_ts, max_value, last_ts, last_value
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._peak_rows,
                )
                self._peak_rows.clear()

    def __enter__(self) -> "PlaybackSeriesProvider":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


__all__ = ["PlaybackSeriesProvider", "SeriesSummary"]
