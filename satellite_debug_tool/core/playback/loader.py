"""Background builders for engineering and customer playback caches."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QThread, Signal

from satellite_debug_tool.core.product import CustomerPlaybackProjector
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    DataReport,
    EventReport,
    FrameV2Record,
    GnssCnrReport,
    GnssSatReport,
    GnssSignalReport,
    GnssSkyReport,
    ServiceFastState,
    ServiceIdentity,
    ServiceSlowState,
    StateReport,
)
from satellite_debug_tool.io.data_importer import DataImporter, SdbFile

from .series_provider import PlaybackSeriesProvider


_GNSS_RECORDS = (GnssSkyReport, GnssCnrReport, GnssSatReport, GnssSignalReport)
_MAX_EVENT_RECORDS = 10_000
_MAX_GNSS_RECORDS = 5_000


class PlaybackLoadCancelled(RuntimeError):
    pass


@dataclass
class PlaybackLoadResult:
    path: Path
    sdb: SdbFile
    provider: PlaybackSeriesProvider
    profile: Optional[dict]
    source_hw_type: Optional[str]
    latest_state: Optional[StateReport]
    events: tuple[EventReport, ...]
    gnss_records: tuple[FrameV2Record, ...]
    latest_identity: Optional[ServiceIdentity]
    source: str

    def close(self) -> None:
        self.provider.close()


def _check_cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise PlaybackLoadCancelled("playback loading cancelled")


def _source_profile(sdb: SdbFile) -> tuple[Optional[ProfileStore], Optional[str]]:
    if sdb.profile is None:
        return None, None
    store = ProfileStore(cache=None)
    hw_type = store.import_dict(sdb.profile)
    return store, hw_type


def build_engineering_playback(
    path: Path,
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> PlaybackLoadResult:
    sdb = DataImporter.open_sdb(path)
    provider = PlaybackSeriesProvider()
    latest_state: Optional[StateReport] = None
    events: deque[EventReport] = deque(maxlen=_MAX_EVENT_RECORDS)
    gnss_records: deque[FrameV2Record] = deque(maxlen=_MAX_GNSS_RECORDS)
    try:
        for ordinal, (_host_ns, record) in enumerate(sdb.iter_timed_records()):
            if ordinal % 256 == 0:
                _check_cancelled(cancelled)
            if isinstance(record, DataReport):
                provider.append_report(record)
            elif isinstance(record, StateReport):
                latest_state = record
            elif isinstance(record, EventReport):
                events.append(record)
            elif isinstance(record, _GNSS_RECORDS):
                gnss_records.append(record)
        provider.finalize()
        _profile_store, source_hw = _source_profile(sdb)
        return PlaybackLoadResult(
            path=Path(path),
            sdb=sdb,
            provider=provider,
            profile=sdb.profile,
            source_hw_type=source_hw,
            latest_state=latest_state,
            events=tuple(events),
            gnss_records=tuple(gnss_records),
            latest_identity=None,
            source="debug_v2",
        )
    except BaseException:
        provider.close()
        raise


def build_customer_playback(
    path: Path,
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> PlaybackLoadResult:
    sdb = DataImporter.open_sdb(path)
    source_profile, source_hw = _source_profile(sdb)
    product_projector = CustomerPlaybackProjector(
        source_profile=source_profile,
        source_hw_type=source_hw,
        prefer_product_service=True,
    )
    legacy_projector = CustomerPlaybackProjector(
        source_profile=source_profile,
        source_hw_type=source_hw,
        prefer_product_service=False,
    )
    product_provider = PlaybackSeriesProvider()
    legacy_provider = PlaybackSeriesProvider()
    latest_identity: Optional[ServiceIdentity] = None
    gnss_records: deque[FrameV2Record] = deque(maxlen=_MAX_GNSS_RECORDS)
    try:
        for ordinal, (_host_ns, record) in enumerate(sdb.iter_timed_records()):
            if ordinal % 256 == 0:
                _check_cancelled(cancelled)
            if isinstance(record, (ServiceFastState, ServiceSlowState)):
                report = product_projector.project(record)
                if report is not None:
                    product_provider.append_report(report)
            elif isinstance(record, DataReport):
                report = legacy_projector.project(record)
                if report is not None:
                    legacy_provider.append_report(report)
            elif isinstance(record, ServiceIdentity):
                latest_identity = record
            elif isinstance(record, _GNSS_RECORDS):
                gnss_records.append(record)
        product_summary = product_provider.finalize()
        legacy_summary = legacy_provider.finalize()
        if product_summary.report_count:
            provider = product_provider
            legacy_provider.close()
            source = "product_service"
        else:
            provider = legacy_provider
            product_provider.close()
            source = "legacy_v2"
        return PlaybackLoadResult(
            path=Path(path),
            sdb=sdb,
            provider=provider,
            profile=sdb.profile,
            source_hw_type=source_hw,
            latest_state=None,
            events=(),
            gnss_records=tuple(gnss_records),
            latest_identity=latest_identity,
            source=source,
        )
    except BaseException:
        product_provider.close()
        legacy_provider.close()
        raise


class PlaybackLoadThread(QThread):
    """Own one cancellable SDB build without touching widgets."""

    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, path: Path, *, customer: bool, parent=None) -> None:
        super().__init__(parent)
        self._path = Path(path)
        self._customer = bool(customer)

    def run(self) -> None:
        builder = build_customer_playback if self._customer else build_engineering_playback
        try:
            result = builder(self._path, cancelled=self.isInterruptionRequested)
        except PlaybackLoadCancelled:
            return
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        if self.isInterruptionRequested():
            result.close()
            return
        self.loaded.emit(result)


__all__ = [
    "PlaybackLoadCancelled",
    "PlaybackLoadResult",
    "PlaybackLoadThread",
    "build_customer_playback",
    "build_engineering_playback",
]
