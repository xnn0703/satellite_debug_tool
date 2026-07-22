"""GNSS 天空图与逐信号 C/N₀ 共享 store。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.protocol import GnssCnrObservation, GnssCnrReport, GnssSkyReport


SYSTEM_NAMES = {0: "GPS", 1: "GLONASS", 2: "SBAS", 3: "Galileo", 4: "BDS", 5: "QZSS", 6: "NavIC", 7: "Other"}
SYSTEM_PREFIXES = {0: "G", 1: "R", 2: "S", 3: "E", 4: "C", 5: "J", 6: "I", 7: "U"}
TALKER_SYSTEMS = {"GP": 0, "GL": 1, "GA": 3, "GB": 4, "BD": 4, "GQ": 5, "QZ": 5, "GI": 6}

LOCK_PHASE = 1 << 0
LOCK_CODE = 1 << 1
LOCK_PRN = 1 << 2
LOCK_PRIMARY = 1 << 3

_SIGNAL_BANDS = {
    0: {0: "L1", 5: "L2", 9: "L2", 14: "L5", 16: "L1", 17: "L2"},
    1: {0: "G1", 1: "G2", 5: "G2"},
    2: {0: "L1", 6: "L5"},
    3: {1: "E1", 2: "E1", 6: "E6", 7: "E6", 12: "E5a", 17: "E5b", 20: "E5 AltBOC"},
    4: {0: "B1", 1: "B2", 2: "B3", 4: "B1", 5: "B2", 6: "B3", 7: "B1", 9: "B2", 10: "B2"},
    5: {0: "L1", 14: "L5", 16: "L1", 17: "L2"},
    6: {0: "L5"},
}

_SIGNAL_NAMES = {
    0: {0: "L1 C/A", 5: "L2P", 9: "L2P encrypted", 14: "L5Q", 16: "L1C", 17: "L2C"},
    1: {0: "L1 C/A", 1: "L2 C/A", 5: "L2P"},
    2: {0: "L1 C/A", 6: "L5I"},
    3: {1: "E1B", 2: "E1C", 6: "E6B", 7: "E6C", 12: "E5aQ", 17: "E5bQ", 20: "E5 AltBOC Q"},
    4: {0: "B1 D1", 1: "B2 D1", 2: "B3 D1", 4: "B1 D2", 5: "B2 D2", 6: "B3 D2", 7: "B1C", 9: "B2a", 10: "B2b"},
    5: {0: "L1 C/A", 14: "L5Q", 16: "L1C", 17: "L2C"},
    6: {0: "L5"},
    7: {19: "L-band"},
}


def signal_band(system: int, signal_type: int) -> str:
    """UG016 signal type → 真实物理频段。"""
    return _SIGNAL_BANDS.get(system, {}).get(signal_type, f"Signal {signal_type}")


def signal_name(system: int, signal_type: int) -> str:
    return _SIGNAL_NAMES.get(system, {}).get(signal_type, f"Signal {signal_type}")


def satellite_label(system: int, prn: int) -> str:
    return f"{SYSTEM_PREFIXES.get(system, 'U')}{prn}"


def observation_locked(observation: GnssCnrObservation) -> bool:
    return bool(observation.lock_flags & (LOCK_PHASE | LOCK_CODE))


def infer_sky_system(talker: str, prn: int) -> int:
    normalized = talker.upper()
    # 传统 $GPGSV 可能夹带 SBAS/QZSS PRN，需先看编号范围。
    if normalized == "GP":
        if 33 <= prn <= 64:
            return 2
        if 193 <= prn <= 202:
            return 5
        return 0
    direct = TALKER_SYSTEMS.get(normalized)
    if direct is not None:
        return direct
    if 1 <= prn <= 32:
        return 0
    if 33 <= prn <= 64:
        return 2
    if 65 <= prn <= 96:
        return 1
    if 193 <= prn <= 200:
        return 5
    if 201 <= prn <= 237:
        return 4
    if 301 <= prn <= 336:
        return 3
    return 7


def _u32_timestamp_is_older(candidate: int, reference: int) -> bool:
    """按无符号 32 位 tick 顺序判断，兼容约 49 天回绕。"""
    difference = (candidate - reference) & 0xFFFFFFFF
    return difference != 0 and difference >= 0x80000000


@dataclass(frozen=True)
class GnssCnrSnapshot:
    timestamp: int
    report_id: int
    flags: int
    observations: Tuple[GnssCnrObservation, ...]


@dataclass(frozen=True)
class GnssSnapshot:
    timestamp: int
    sky_by_talker: Dict[str, GnssSkyReport] = field(default_factory=dict)
    cnr: Optional[GnssCnrSnapshot] = None


@dataclass
class _PendingCnr:
    key: Tuple[int, int]
    chunk_count: int
    total_observations: int
    flags: int
    chunks: Dict[int, Tuple[GnssCnrObservation, ...]] = field(default_factory=dict)
    started_at: float = 0.0


class GnssStore(QObject):
    """Live/Playback 共用 GNSS 状态与 CNR 分片重组。"""

    changed = Signal()
    cleared = Signal()

    def __init__(self, *, keep_history: bool = False, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._keep_history = keep_history
        self._sky: Dict[str, GnssSkyReport] = {}
        self._cnr: Optional[GnssCnrSnapshot] = None
        self._pending: Optional[_PendingCnr] = None
        self._history: List[GnssSnapshot] = []
        self._last_update_wallclock: Optional[float] = None

    def update_sky(self, report: GnssSkyReport) -> None:
        self._sky[report.talker] = report
        self._last_update_wallclock = time.monotonic()
        self._append_history(report.timestamp)
        self.changed.emit()

    def update_cnr_chunk(self, report: GnssCnrReport) -> bool:
        """写入一片；完整重组时返回 True。新 report 会丢弃未完成旧 report。"""
        self.expire_incomplete()
        key = (report.report_id, report.timestamp)
        reference_timestamp: Optional[int] = None
        if self._pending is not None:
            reference_timestamp = self._pending.key[1]
        elif self._cnr is not None:
            reference_timestamp = self._cnr.timestamp
        if reference_timestamp is not None and _u32_timestamp_is_older(report.timestamp, reference_timestamp):
            return False
        if self._pending is None or self._pending.key != key:
            self._pending = _PendingCnr(
                key=key,
                chunk_count=report.chunk_count,
                total_observations=report.total_observations,
                flags=report.flags,
                started_at=time.monotonic(),
            )
        pending = self._pending
        if pending.chunk_count != report.chunk_count or pending.total_observations != report.total_observations:
            self._pending = None
            return False

        chunk = tuple(report.observations)
        previous = pending.chunks.get(report.chunk_index)
        if previous is not None and previous != chunk:
            self._pending = None
            return False
        pending.chunks[report.chunk_index] = chunk
        pending.flags |= report.flags
        if len(pending.chunks) != pending.chunk_count:
            return False

        observations: List[GnssCnrObservation] = []
        for index in range(pending.chunk_count):
            if index not in pending.chunks:
                return False
            observations.extend(pending.chunks[index])
        if len(observations) != pending.total_observations:
            self._pending = None
            return False

        self._cnr = GnssCnrSnapshot(
            timestamp=report.timestamp,
            report_id=report.report_id,
            flags=pending.flags,
            observations=tuple(observations),
        )
        self._pending = None
        self._last_update_wallclock = time.monotonic()
        self._append_history(report.timestamp)
        self.changed.emit()
        return True

    def update(self, report: object) -> bool:
        if isinstance(report, GnssSkyReport):
            self.update_sky(report)
            return True
        if isinstance(report, GnssCnrReport):
            return self.update_cnr_chunk(report)
        return False

    def expire_incomplete(self, timeout_s: float = 3.0) -> bool:
        if self._pending is None or time.monotonic() - self._pending.started_at <= timeout_s:
            return False
        self._pending = None
        return True

    def snapshot(self) -> GnssSnapshot:
        timestamps = [report.timestamp for report in self._sky.values()]
        if self._cnr is not None:
            timestamps.append(self._cnr.timestamp)
        return GnssSnapshot(
            timestamp=max(timestamps, default=0),
            sky_by_talker=dict(self._sky),
            cnr=self._cnr,
        )

    def _append_history(self, timestamp: int) -> None:
        if self._keep_history:
            self._history.append(GnssSnapshot(timestamp=timestamp, sky_by_talker=dict(self._sky), cnr=self._cnr))

    def history(self) -> Tuple[GnssSnapshot, ...]:
        return tuple(self._history)

    def has_data(self) -> bool:
        return bool(self._sky) or self._cnr is not None

    def is_stale(self, threshold_s: float = 3.0) -> bool:
        return self._last_update_wallclock is not None and time.monotonic() - self._last_update_wallclock > threshold_s

    def data_age_s(self) -> Optional[float]:
        if self._last_update_wallclock is None:
            return None
        return max(0.0, time.monotonic() - self._last_update_wallclock)

    def clear(self) -> None:
        self._sky.clear()
        self._cnr = None
        self._pending = None
        self._history.clear()
        self._last_update_wallclock = None
        self.cleared.emit()
        self.changed.emit()
