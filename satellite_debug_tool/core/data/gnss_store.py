"""GNSS 天空图与逐信号 C/N₀ 共享 store。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.protocol import (
    GnssCnrObservation,
    GnssCnrReport,
    GnssSatRecord,
    GnssSatReport,
    GnssSignalRecord,
    GnssSignalReport,
    GnssSkyReport,
)


GNSS_SOURCE_UNKNOWN = 0
GNSS_SOURCE_MG902 = 1
GNSS_SOURCE_BYNAV = 2
SOURCE_NAMES = {0: "UNKNOWN", 1: "MG902", 2: "BYNAV", 3: "MANUAL", 4: "MS6222"}
SIGNAL_NAMESPACE_UG016 = "UG016"
SIGNAL_NAMESPACE_UBX_M9 = "UBX_M9"
SIGNAL_NAMESPACE_RAW = "RAW"

SYSTEM_NAMES = {0: "GPS", 1: "GLONASS", 2: "SBAS", 3: "Galileo", 4: "BDS", 5: "QZSS", 6: "NavIC", 7: "Other"}
SYSTEM_PREFIXES = {0: "G", 1: "R", 2: "S", 3: "E", 4: "C", 5: "J", 6: "I", 7: "U"}
TALKER_SYSTEMS = {"GP": 0, "GL": 1, "GA": 3, "GB": 4, "BD": 4, "GQ": 5, "QZ": 5, "GI": 6}

LOCK_PHASE = 1 << 0
LOCK_CODE = 1 << 1
LOCK_PRN = 1 << 2
LOCK_PRIMARY = 1 << 3

_UG016_BANDS = {
    0: {0: "L1", 5: "L2", 9: "L2", 14: "L5", 16: "L1", 17: "L2"},
    1: {0: "G1", 1: "G2", 5: "G2"},
    2: {0: "L1", 6: "L5"},
    3: {1: "E1", 2: "E1", 6: "E6", 7: "E6", 12: "E5a", 17: "E5b", 20: "E5 AltBOC"},
    4: {0: "B1", 1: "B2", 2: "B3", 4: "B1", 5: "B2", 6: "B3", 7: "B1", 9: "B2", 10: "B2"},
    5: {0: "L1", 14: "L5", 16: "L1", 17: "L2"},
    6: {0: "L5"},
}
_UG016_NAMES = {
    0: {0: "L1 C/A", 5: "L2P", 9: "L2P encrypted", 14: "L5Q", 16: "L1C", 17: "L2C"},
    1: {0: "L1 C/A", 1: "L2 C/A", 5: "L2P"},
    2: {0: "L1 C/A", 6: "L5I"},
    3: {1: "E1B", 2: "E1C", 6: "E6B", 7: "E6C", 12: "E5aQ", 17: "E5bQ", 20: "E5 AltBOC Q"},
    4: {0: "B1 D1", 1: "B2 D1", 2: "B3 D1", 4: "B1 D2", 5: "B2 D2", 6: "B3 D2", 7: "B1C", 9: "B2a", 10: "B2b"},
    5: {0: "L1 C/A", 14: "L5Q", 16: "L1C", 17: "L2C"},
    6: {0: "L5"},
    7: {19: "L-band"},
}

# u-blox M9 NAV-SIG sigId。system 已由固件归一为本模块的 system 编号。
_UBX_M9_BANDS = {
    0: {0: "L1", 3: "L2", 4: "L2", 6: "L5", 7: "L5"},
    1: {0: "G1", 2: "G2"},
    2: {0: "L1"},
    3: {0: "E1", 1: "E1", 3: "E5a", 4: "E5a", 5: "E5b", 6: "E5b"},
    4: {0: "B1", 1: "B1", 2: "B2", 3: "B2", 5: "B1", 6: "B1", 7: "B2", 8: "B2"},
    5: {0: "L1", 1: "L1", 4: "L2", 5: "L2", 8: "L5", 9: "L5"},
    6: {0: "L5"},
}
_UBX_M9_NAMES = {
    0: {0: "L1 C/A", 3: "L2 CL", 4: "L2 CM", 6: "L5 I", 7: "L5 Q"},
    1: {0: "L1 OF", 2: "L2 OF"},
    2: {0: "L1 C/A"},
    3: {0: "E1 C", 1: "E1 B", 3: "E5a I", 4: "E5a Q", 5: "E5b I", 6: "E5b Q"},
    4: {
        0: "B1I D1", 1: "B1I D2", 2: "B2I D1", 3: "B2I D2",
        5: "B1C pilot", 6: "B1C data", 7: "B2a pilot", 8: "B2a data",
    },
    5: {0: "L1 C/A", 1: "L1S", 4: "L2 CM", 5: "L2 CL", 8: "L5 I", 9: "L5 Q"},
    6: {0: "L5 A"},
}


def signal_band(system: int, signal_type: int, namespace: str = SIGNAL_NAMESPACE_UG016) -> str:
    table = (
        _UBX_M9_BANDS if namespace == SIGNAL_NAMESPACE_UBX_M9
        else _UG016_BANDS if namespace == SIGNAL_NAMESPACE_UG016
        else {}
    )
    return table.get(system, {}).get(signal_type, f"Signal {signal_type}")


def signal_name(system: int, signal_type: int, namespace: str = SIGNAL_NAMESPACE_UG016) -> str:
    table = (
        _UBX_M9_NAMES if namespace == SIGNAL_NAMESPACE_UBX_M9
        else _UG016_NAMES if namespace == SIGNAL_NAMESPACE_UG016
        else {}
    )
    return table.get(system, {}).get(signal_type, f"Signal {signal_type}")


def satellite_label(system: int, prn: int) -> str:
    return f"{SYSTEM_PREFIXES.get(system, 'U')}{prn}"


def observation_locked(observation: GnssCnrObservation) -> bool:
    return bool(observation.lock_flags & (LOCK_PHASE | LOCK_CODE))


def signal_record_used(record: GnssSignalRecord) -> bool:
    """UBX NAV-SIG sigFlags 的 prUsed/crUsed/doUsed 任一置位即参与当前解。"""
    return bool(record.raw_sig_flags & ((1 << 3) | (1 << 4) | (1 << 5)))


def signal_record_locked(record: GnssSignalRecord) -> bool:
    """MG902 qualityInd=4..7 表示信号已锁定，不等价于参与导航解算。"""
    return 4 <= record.quality_ind <= 7


def signal_record_cnr_valid(record: GnssSignalRecord) -> bool:
    """MG902 信号已锁定且 C/N₀ 为正时，载噪比可用于统计和绘图。"""
    return signal_record_locked(record) and record.cn0_dbhz > 0


def infer_sky_system(talker: str, prn: int) -> int:
    normalized = talker.upper()
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
    difference = (candidate - reference) & 0xFFFFFFFF
    return difference != 0 and difference >= 0x80000000


@dataclass(frozen=True)
class GnssCnrSnapshot:
    timestamp: int
    report_id: int
    flags: int
    observations: Tuple[GnssCnrObservation, ...]


@dataclass(frozen=True)
class GnssSatSnapshot:
    timestamp: int
    report_id: int
    flags: int
    records: Tuple[GnssSatRecord, ...]


@dataclass(frozen=True)
class GnssSignalSnapshot:
    timestamp: int
    report_id: int
    flags: int
    records: Tuple[GnssSignalRecord, ...]


@dataclass(frozen=True)
class GnssSnapshot:
    timestamp: int
    source: int = GNSS_SOURCE_UNKNOWN
    signal_namespace: str = SIGNAL_NAMESPACE_UG016
    sky_by_talker: Dict[str, GnssSkyReport] = field(default_factory=dict)
    cnr: Optional[GnssCnrSnapshot] = None
    sat: Optional[GnssSatSnapshot] = None
    signal: Optional[GnssSignalSnapshot] = None


@dataclass
class _PendingRecords:
    key: Tuple[int, int]
    source: int
    chunk_count: int
    total_records: int
    flags: int
    chunks: Dict[int, Tuple[object, ...]] = field(default_factory=dict)
    chunk_flags: Dict[int, int] = field(default_factory=dict)
    started_at: float = 0.0


class GnssStore(QObject):
    """Live/Playback 共用、按接收机来源隔离的 GNSS 状态。"""

    changed = Signal()
    cleared = Signal()

    def __init__(self, *, keep_history: bool = False, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._keep_history = keep_history
        self._source = GNSS_SOURCE_UNKNOWN
        self._source_initialized = False
        self._sky: Dict[str, GnssSkyReport] = {}
        self._cnr: Optional[GnssCnrSnapshot] = None
        self._sat: Optional[GnssSatSnapshot] = None
        self._signal: Optional[GnssSignalSnapshot] = None
        self._pending_cnr: Optional[_PendingRecords] = None
        self._pending_sat: Optional[_PendingRecords] = None
        self._pending_signal: Optional[_PendingRecords] = None
        self._history: List[GnssSnapshot] = []
        self._sky_last_update_wallclock: Optional[float] = None
        self._signal_last_update_wallclock: Optional[float] = None

    @property
    def source(self) -> int:
        return self._source

    def source_name(self) -> str:
        return SOURCE_NAMES.get(self._source, f"SOURCE_{self._source}")

    def _activate_source(self, source: int) -> None:
        if self._source_initialized and self._source == source:
            return

        # source 只在一份完整报告提交时切换。切换后当前快照与 Playback
        # history 都必须属于新源；同一新源已经开始接收的其它分片可以保留。
        self._reset_current(keep_history=False, preserve_pending_source=source)
        self._source = source
        self._source_initialized = True

    def _reset_current(self, *, keep_history: bool, preserve_pending_source: Optional[int] = None) -> None:
        self._sky.clear()
        self._cnr = None
        self._sat = None
        self._signal = None
        for name in ("_pending_cnr", "_pending_sat", "_pending_signal"):
            pending: Optional[_PendingRecords] = getattr(self, name)
            if preserve_pending_source is None or pending is None or pending.source != preserve_pending_source:
                setattr(self, name, None)
        self._sky_last_update_wallclock = None
        self._signal_last_update_wallclock = None
        if not keep_history:
            self._history.clear()

    def update_sky(self, report: GnssSkyReport) -> None:
        self._activate_source(GNSS_SOURCE_BYNAV)
        self._sky[report.talker] = report
        self._sky_last_update_wallclock = time.monotonic()
        self._append_history(report.timestamp)
        self.changed.emit()

    def _update_fragment(
        self,
        report: object,
        records: Tuple[object, ...],
        pending_name: str,
        previous_timestamp: Optional[int],
    ) -> Optional[Tuple[Tuple[object, ...], int]]:
        self.expire_incomplete()
        source = int(getattr(report, "source", GNSS_SOURCE_BYNAV))
        pending: Optional[_PendingRecords] = getattr(self, pending_name)
        key = (int(getattr(report, "report_id")), int(getattr(report, "timestamp")))
        if pending is not None and pending.source == source:
            reference = pending.key[1]
        elif self._source_initialized and self._source == source:
            reference = previous_timestamp
        else:
            # 不同接收机的 timestamp 不参与新源分片的新旧判断。
            reference = None
        if reference is not None and _u32_timestamp_is_older(key[1], reference):
            return None
        if pending is None or pending.key != key or pending.source != source:
            pending = _PendingRecords(
                key=key,
                source=source,
                chunk_count=int(getattr(report, "chunk_count")),
                total_records=int(getattr(report, "total_records", getattr(report, "total_observations", 0))),
                flags=int(getattr(report, "flags")),
                started_at=time.monotonic(),
            )
            setattr(self, pending_name, pending)
        total = int(getattr(report, "total_records", getattr(report, "total_observations", 0)))
        if pending.chunk_count != int(getattr(report, "chunk_count")) or pending.total_records != total:
            setattr(self, pending_name, None)
            return None
        index = int(getattr(report, "chunk_index"))
        flags = int(getattr(report, "flags"))
        previous = pending.chunks.get(index)
        previous_flags = pending.chunk_flags.get(index)
        if previous is not None and (previous != records or previous_flags != flags):
            setattr(self, pending_name, None)
            return None
        pending.chunks[index] = records
        pending.chunk_flags[index] = flags
        pending.flags |= flags
        if len(pending.chunks) != pending.chunk_count:
            return None
        combined: List[object] = []
        for chunk_index in range(pending.chunk_count):
            if chunk_index not in pending.chunks:
                return None
            combined.extend(pending.chunks[chunk_index])
        if len(combined) != pending.total_records:
            setattr(self, pending_name, None)
            return None
        completed_flags = pending.flags
        setattr(self, pending_name, None)
        return tuple(combined), completed_flags

    def update_cnr_chunk(self, report: GnssCnrReport) -> bool:
        # legacy 帧没有 source 字段，固定属于 Bynav/UG016。
        previous = self._cnr.timestamp if self._cnr is not None else None
        completed = self._update_fragment(report, tuple(report.observations), "_pending_cnr", previous)
        if completed is None:
            return False
        combined, flags = completed
        self._activate_source(GNSS_SOURCE_BYNAV)
        self._cnr = GnssCnrSnapshot(report.timestamp, report.report_id, flags, combined)  # type: ignore[arg-type]
        self._signal_last_update_wallclock = time.monotonic()
        self._append_history(report.timestamp)
        self.changed.emit()
        return True

    def update_sat_chunk(self, report: GnssSatReport) -> bool:
        previous = self._sat.timestamp if self._sat is not None else None
        completed = self._update_fragment(report, tuple(report.records), "_pending_sat", previous)
        if completed is None:
            return False
        combined, flags = completed
        self._activate_source(report.source)
        self._sat = GnssSatSnapshot(report.timestamp, report.report_id, flags, combined)  # type: ignore[arg-type]
        self._sky_last_update_wallclock = time.monotonic()
        self._append_history(report.timestamp)
        self.changed.emit()
        return True

    def update_signal_chunk(self, report: GnssSignalReport) -> bool:
        previous = self._signal.timestamp if self._signal is not None else None
        completed = self._update_fragment(report, tuple(report.records), "_pending_signal", previous)
        if completed is None:
            return False
        combined, flags = completed
        self._activate_source(report.source)
        self._signal = GnssSignalSnapshot(report.timestamp, report.report_id, flags, combined)  # type: ignore[arg-type]
        self._signal_last_update_wallclock = time.monotonic()
        self._append_history(report.timestamp)
        self.changed.emit()
        return True

    def update(self, report: object) -> bool:
        if isinstance(report, GnssSkyReport):
            self.update_sky(report)
            return True
        if isinstance(report, GnssCnrReport):
            return self.update_cnr_chunk(report)
        if isinstance(report, GnssSatReport):
            return self.update_sat_chunk(report)
        if isinstance(report, GnssSignalReport):
            return self.update_signal_chunk(report)
        return False

    def expire_incomplete(self, timeout_s: float = 3.0) -> bool:
        now = time.monotonic()
        expired = False
        for name in ("_pending_cnr", "_pending_sat", "_pending_signal"):
            pending: Optional[_PendingRecords] = getattr(self, name)
            if pending is not None and now - pending.started_at > timeout_s:
                setattr(self, name, None)
                expired = True
        return expired

    def snapshot(self) -> GnssSnapshot:
        timestamps = [report.timestamp for report in self._sky.values()]
        for item in (self._cnr, self._sat, self._signal):
            if item is not None:
                timestamps.append(item.timestamp)
        namespace = (
            SIGNAL_NAMESPACE_UBX_M9 if self._source == GNSS_SOURCE_MG902
            else SIGNAL_NAMESPACE_UG016 if self._source == GNSS_SOURCE_BYNAV
            else SIGNAL_NAMESPACE_RAW
        )
        return GnssSnapshot(
            timestamp=max(timestamps, default=0),
            source=self._source,
            signal_namespace=namespace,
            sky_by_talker=dict(self._sky),
            cnr=self._cnr,
            sat=self._sat,
            signal=self._signal,
        )

    def _append_history(self, timestamp: int) -> None:
        if self._keep_history:
            snapshot = self.snapshot()
            self._history.append(GnssSnapshot(
                timestamp=timestamp,
                source=snapshot.source,
                signal_namespace=snapshot.signal_namespace,
                sky_by_talker=snapshot.sky_by_talker,
                cnr=snapshot.cnr,
                sat=snapshot.sat,
                signal=snapshot.signal,
            ))

    def history(self) -> Tuple[GnssSnapshot, ...]:
        return tuple(self._history)

    def has_data(self) -> bool:
        return bool(self._sky) or any(item is not None for item in (self._cnr, self._sat, self._signal))

    @staticmethod
    def _age(last_update: Optional[float]) -> Optional[float]:
        return None if last_update is None else max(0.0, time.monotonic() - last_update)

    def sky_age_s(self) -> Optional[float]:
        return self._age(self._sky_last_update_wallclock)

    def signal_age_s(self) -> Optional[float]:
        return self._age(self._signal_last_update_wallclock)

    def sky_is_stale(self, threshold_s: float = 3.0) -> bool:
        age = self.sky_age_s()
        return age is not None and age > threshold_s

    def signal_is_stale(self, threshold_s: float = 3.0) -> bool:
        age = self.signal_age_s()
        return age is not None and age > threshold_s

    def sky_pending(self) -> bool:
        return self._pending_sat is not None

    def signal_pending(self) -> bool:
        return self._pending_cnr is not None or self._pending_signal is not None

    def is_stale(self, threshold_s: float = 3.0) -> bool:
        ages = [age for age in (self.sky_age_s(), self.signal_age_s()) if age is not None]
        return bool(ages) and min(ages) > threshold_s

    def data_age_s(self) -> Optional[float]:
        ages = [age for age in (self.sky_age_s(), self.signal_age_s()) if age is not None]
        return min(ages) if ages else None

    def clear(self) -> None:
        self._reset_current(keep_history=False)
        self._source = GNSS_SOURCE_UNKNOWN
        self._source_initialized = False
        self.cleared.emit()
        self.changed.emit()
