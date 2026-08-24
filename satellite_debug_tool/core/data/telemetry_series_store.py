"""Canonical telemetry history used by live and playback presentations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .data_store import DataStore


@dataclass(frozen=True)
class TelemetryWindow:
    """A bounded time window with explicit discontinuity positions."""

    timestamps: np.ndarray
    values: np.ndarray
    gap_indices: tuple[int, ...]


class TelemetrySeriesStore(DataStore):
    """DataStore with one shared time-window and gap contract.

    Device timestamps remain authoritative.  Gap indices identify the first
    sample after a discontinuity and let every chart render the same breaks.
    """

    def query_window(
        self,
        channel_id: int,
        *,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        max_points: int = 6000,
        gap_threshold: Optional[float] = None,
    ) -> TelemetryWindow:
        buffer = self.get_channel_by_id(channel_id)
        if buffer is None:
            return TelemetryWindow(
                np.array([], dtype=np.float64),
                np.array([], dtype=np.float32),
                (),
            )

        timestamps = buffer.get_times()
        values = buffer.get_values()
        if start_timestamp is not None:
            keep = timestamps >= float(start_timestamp)
            timestamps = timestamps[keep]
            values = values[keep]
        if end_timestamp is not None:
            keep = timestamps <= float(end_timestamp)
            timestamps = timestamps[keep]
            values = values[keep]

        point_budget = max(2, int(max_points))
        if timestamps.size > point_budget:
            indices = np.linspace(0, timestamps.size - 1, point_budget, dtype=np.int64)
            timestamps = timestamps[indices]
            values = values[indices]

        gaps: tuple[int, ...] = ()
        if gap_threshold is not None and timestamps.size > 1:
            positions = np.flatnonzero(np.diff(timestamps) > float(gap_threshold)) + 1
            gaps = tuple(int(position) for position in positions)
        return TelemetryWindow(timestamps, values, gaps)
