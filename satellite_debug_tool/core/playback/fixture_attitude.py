"""Align fixture metadata to the device uptime axis used by SDB playback."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math
from typing import Callable, Iterable, Mapping, Optional

from satellite_debug_tool.core.production.fixture_telemetry import (
    FIXTURE_ATTITUDE_SCHEMA,
    FixtureAttitudeSource,
)
from satellite_debug_tool.core.protocol import (
    CHANNEL_FLAG_DEFAULT_VISIBLE,
    ChannelDefEntry,
    ChannelSample,
    DataReport,
    DataType,
)


PLATFORM_TARGET_CHANNELS = {"roll": 1000, "pitch": 1001, "yaw": 1002}
MS6222_RELATIVE_CHANNELS = {"roll": 1003, "pitch": 1004, "yaw": 1005}
FIXTURE_PLAYBACK_CHANNEL_IDS = frozenset(
    (*PLATFORM_TARGET_CHANNELS.values(), *MS6222_RELATIVE_CHANNELS.values())
)
_MAX_ANCHOR_GAP_NS = 5_000_000_000
_RESET_BACKSTEP_MS = 1000.0
_MIN_CLOCK_RATIO = 0.5
_MAX_CLOCK_RATIO = 1.5


@dataclass(frozen=True)
class FixtureAlignmentSummary:
    recorded_samples: int = 0
    aligned_samples: int = 0
    unaligned_samples: int = 0
    invalid_samples: int = 0
    maximum_anchor_span_ms: float = 0.0
    gap_markers: tuple["FixtureGapMarker", ...] = ()

    @property
    def has_attitude(self) -> bool:
        return self.aligned_samples > 0


@dataclass(frozen=True)
class FixtureGapMarker:
    timestamp_ms: float
    source: str
    reason: str


@dataclass(frozen=True)
class _Anchor:
    host_ns: int
    uptime_ms: float
    generation: int


class HostUptimeAligner:
    """Piecewise host-time correlation that never crosses a reset or long gap."""

    def __init__(self) -> None:
        self._anchors: list[_Anchor] = []

    def add_anchor(self, host_ns: int, uptime_ms: float) -> None:
        host_ns = int(host_ns)
        uptime_ms = float(uptime_ms)
        if host_ns <= 0 or not math.isfinite(uptime_ms):
            return
        previous = self._anchors[-1] if self._anchors else None
        if previous is not None and host_ns <= previous.host_ns:
            return
        generation = 0 if previous is None else previous.generation
        if previous is not None and (
            host_ns - previous.host_ns > _MAX_ANCHOR_GAP_NS
            or uptime_ms < previous.uptime_ms - _RESET_BACKSTEP_MS
        ):
            generation += 1
        self._anchors.append(_Anchor(host_ns, uptime_ms, generation))

    def align(self, host_ns: int) -> Optional[tuple[float, float]]:
        if len(self._anchors) < 2:
            return None
        hosts = [anchor.host_ns for anchor in self._anchors]
        index = bisect_right(hosts, int(host_ns))
        if index == 0 or index >= len(self._anchors):
            return None
        left = self._anchors[index - 1]
        right = self._anchors[index]
        span_ns = right.host_ns - left.host_ns
        if (
            left.generation != right.generation
            or span_ns <= 0
            or span_ns > _MAX_ANCHOR_GAP_NS
        ):
            return None
        uptime_span = right.uptime_ms - left.uptime_ms
        clock_ratio = uptime_span / (span_ns / 1_000_000.0)
        if not (_MIN_CLOCK_RATIO <= clock_ratio <= _MAX_CLOCK_RATIO):
            return None
        fraction = (int(host_ns) - left.host_ns) / span_ns
        return left.uptime_ms + fraction * uptime_span, span_ns / 1_000_000.0


def fixture_attitude_channel_entries(
    group_id: int,
    translate: Callable[[str], str] = lambda text: text,
) -> list[ChannelDefEntry]:
    definitions = (
        (1000, "Platform target Roll"),
        (1001, "Platform target Pitch"),
        (1002, "Platform target Yaw"),
        (1003, "MS reference Roll"),
        (1004, "MS reference Pitch"),
        (1005, "MS reference Yaw"),
    )
    return [
        ChannelDefEntry(
            channel_id=channel_id,
            data_type=int(DataType.FLOAT32),
            group_id=int(group_id),
            flags=CHANNEL_FLAG_DEFAULT_VISIBLE,
            name=translate(name),
            unit="deg",
            display_min=-180.0,
            display_max=180.0,
        )
        for channel_id, name in definitions
    ]


def aligned_fixture_reports(
    events: Iterable[Mapping[str, object]],
    aligner: HostUptimeAligner,
) -> tuple[tuple[DataReport, ...], FixtureAlignmentSummary]:
    reports: list[DataReport] = []
    recorded = aligned = unaligned = invalid = 0
    maximum_span_ms = 0.0
    gap_markers: list[FixtureGapMarker] = []
    last_valid_host_ns: dict[str, int] = {}
    last_validity: dict[str, bool] = {}
    for event in events:
        if event.get("schema") != FIXTURE_ATTITUDE_SCHEMA:
            continue
        source = str(event.get("source", ""))
        if source not in {
            FixtureAttitudeSource.PLATFORM_TARGET.value,
            FixtureAttitudeSource.MS6222_RELATIVE.value,
        }:
            continue
        recorded += 1
        try:
            host_ns = int(event["host_wall_time_ns"])
        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue
        mapping = aligner.align(host_ns)
        if mapping is None:
            unaligned += 1
            continue
        timestamp_ms, span_ms = mapping
        maximum_span_ms = max(maximum_span_ms, span_ms)
        channels = (
            PLATFORM_TARGET_CHANNELS
            if source == FixtureAttitudeSource.PLATFORM_TARGET.value
            else MS6222_RELATIVE_CHANNELS
        )
        if not bool(event.get("valid", False)):
            invalid += 1
            if last_validity.get(source) is not False:
                gap_markers.append(
                    FixtureGapMarker(
                        timestamp_ms=timestamp_ms,
                        source=source,
                        reason=str(event.get("invalid_reason") or "reference_invalid"),
                    )
                )
            last_validity[source] = False
            reports.append(
                DataReport(
                    timestamp_ms,
                    [
                        ChannelSample(channel_id, float("nan"))
                        for channel_id in channels.values()
                    ],
                )
            )
            continue
        try:
            values = {
                axis: float(event[f"{axis}_deg"])
                for axis in ("roll", "pitch", "yaw")
            }
        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue
        if not all(math.isfinite(value) for value in values.values()):
            invalid += 1
            continue
        previous_host_ns = last_valid_host_ns.get(source)
        if (
            source == FixtureAttitudeSource.MS6222_RELATIVE.value
            and previous_host_ns is not None
            and host_ns - previous_host_ns > 200_000_000
        ):
            gap_markers.append(
                FixtureGapMarker(
                    timestamp_ms=timestamp_ms,
                    source=source,
                    reason="sample_gap",
                )
            )
            reports.append(
                DataReport(
                    timestamp_ms - 0.001,
                    [
                        ChannelSample(channel_id, float("nan"))
                        for channel_id in channels.values()
                    ],
                )
            )
        reports.append(
            DataReport(
                timestamp_ms,
                [
                    ChannelSample(channels[axis], values[axis])
                    for axis in ("roll", "pitch", "yaw")
                ],
            )
        )
        last_valid_host_ns[source] = host_ns
        last_validity[source] = True
        aligned += 1
    return tuple(reports), FixtureAlignmentSummary(
        recorded_samples=recorded,
        aligned_samples=aligned,
        unaligned_samples=unaligned,
        invalid_samples=invalid,
        maximum_anchor_span_ms=maximum_span_ms,
        gap_markers=tuple(gap_markers),
    )


__all__ = [
    "FIXTURE_PLAYBACK_CHANNEL_IDS",
    "MS6222_RELATIVE_CHANNELS",
    "PLATFORM_TARGET_CHANNELS",
    "FixtureAlignmentSummary",
    "FixtureGapMarker",
    "HostUptimeAligner",
    "aligned_fixture_reports",
    "fixture_attitude_channel_entries",
]
