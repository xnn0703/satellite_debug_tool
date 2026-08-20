"""XESA01 Orbit/TLE protocol reports shared by the live popup and tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.protocol import (
    OrbitCapabilitiesReport,
    OrbitCatalogEntry,
    OrbitCatalogReport,
    OrbitCurrentReport,
    OrbitCurrentSample,
    OrbitSkyReport,
    OrbitSkySample,
    OrbitPassPage,
    OrbitPassSummary,
    OrbitPredictionAccepted,
    OrbitPredictionPage,
    OrbitPredictionSample,
    OrbitReportHeader,
    OrbitStatus,
    OrbitUploadProgress,
)


@dataclass(frozen=True)
class OrbitCatalogSnapshot:
    generation: int
    total_entries: int
    scan_pending: bool
    scan_running: bool
    last_scan_success: bool
    invalid_records: int
    duplicate_records: int
    capacity_rejections: int
    entries: Tuple[OrbitCatalogEntry, ...]


@dataclass(frozen=True)
class OrbitSkyTrailPoint:
    utc_unix_ms: int
    array_azimuth_deg: float
    array_offaxis_deg: float


@dataclass(frozen=True)
class OrbitSkySnapshot:
    snapshot_id: int
    generation: int
    utc_unix_ms: int
    hard_offaxis_limit_deg: float
    recommended_offaxis_limit_deg: float
    mount_yaw_deg: float
    mount_pitch_deg: float
    mount_roll_deg: float
    azimuth_zero_offset_deg: float
    azimuth_direction: int
    second_angle_type: int
    active_target_id: int
    profile_characterized: bool
    samples: Tuple[OrbitSkySample, ...]


class OrbitStore(QObject):
    """Merge bounded protocol pages without mixing catalog generations."""

    changed = Signal(object)
    capability_changed = Signal(bool)
    sky_changed = Signal(object)
    cleared = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.capabilities: Optional[OrbitCapabilitiesReport] = None
        self.last_report: Optional[OrbitReportHeader] = None
        self._catalog_generation = 0
        self._catalog_total = 0
        self._catalog_pages: Dict[int, Tuple[OrbitCatalogEntry, ...]] = {}
        self._catalog_status: Optional[OrbitCatalogReport] = None
        self._current_generation = 0
        self._current_total = 0
        self._current_pages: Dict[int, Tuple[OrbitCurrentSample, ...]] = {}
        self._sky_snapshot: Optional[OrbitSkySnapshot] = None
        self._sky_staging_id = 0
        self._sky_staging_report: Optional[OrbitSkyReport] = None
        self._sky_pages: Dict[int, Tuple[OrbitSkySample, ...]] = {}
        self._sky_trails: Dict[int, Tuple[OrbitSkyTrailPoint, ...]] = {}
        self.prediction_job: Optional[OrbitPredictionAccepted] = None
        self._prediction_pages: Dict[int, Tuple[OrbitPredictionSample, ...]] = {}
        self._prediction_total = 0
        self._pass_pages: Dict[int, Tuple[OrbitPassSummary, ...]] = {}
        self._pass_total = 0

    @property
    def available(self) -> bool:
        return self.capabilities is not None and self.capabilities.status == OrbitStatus.OK

    @staticmethod
    def _generation_is_newer(candidate: int, current: int) -> bool:
        """Compare wrapping uint32 generations using the half-range rule."""
        delta = (candidate - current) & 0xFFFFFFFF
        return delta != 0 and delta < 0x80000000

    def feed(self, report: object) -> bool:
        if not isinstance(report, OrbitReportHeader):
            return False
        self.last_report = report
        if isinstance(report, OrbitCapabilitiesReport):
            was_available = self.available
            self.capabilities = report
            if self.available != was_available:
                self.capability_changed.emit(self.available)
        elif isinstance(report, OrbitCatalogReport):
            if report.generation != self._catalog_generation:
                if (
                    self._catalog_status is not None
                    and not self._generation_is_newer(report.generation, self._catalog_generation)
                ):
                    return True
                self._catalog_generation = report.generation
                self._catalog_pages.clear()
                self._invalidate_prediction()
                self._current_pages.clear()
                self._invalidate_sky()
            self._catalog_total = report.total_entries
            self._catalog_status = report
            self._catalog_pages[report.page] = report.entries
        elif isinstance(report, OrbitCurrentReport):
            if self._catalog_status is not None and report.generation != self._catalog_generation:
                return True
            if report.generation != self._current_generation:
                self._current_generation = report.generation
                self._current_pages.clear()
            self._current_total = report.total_entries
            self._current_pages[report.page] = report.samples
        elif isinstance(report, OrbitSkyReport):
            if self._catalog_status is not None and report.generation != self._catalog_generation:
                return True
            self._feed_sky_page(report)
        elif isinstance(report, OrbitPredictionAccepted):
            if self._catalog_status is not None and report.generation != self._catalog_generation:
                return True
            if (
                self.prediction_job is not None
                and report.job_id != self.prediction_job.job_id
                and not self._generation_is_newer(report.job_id, self.prediction_job.job_id)
            ):
                return True
            self.prediction_job = report
            self._prediction_pages.clear()
            self._prediction_total = 0
            self._pass_pages.clear()
            self._pass_total = 0
        elif isinstance(report, OrbitPredictionPage):
            if (
                self.prediction_job is not None
                and report.job_id == self.prediction_job.job_id
                and report.generation == self.prediction_job.generation
                and (self._catalog_status is None or report.generation == self._catalog_generation)
            ):
                self._prediction_total = report.total_samples
                self._prediction_pages[report.page] = report.samples
        elif isinstance(report, OrbitPassPage):
            if (
                self.prediction_job is not None
                and report.job_id == self.prediction_job.job_id
                and report.generation == self.prediction_job.generation
                and (self._catalog_status is None or report.generation == self._catalog_generation)
            ):
                self._pass_total = report.total_satellites
                self._pass_pages[report.page] = report.passes
        elif isinstance(report, OrbitUploadProgress):
            pass
        self.changed.emit(report)
        return True

    def catalog_snapshot(self) -> OrbitCatalogSnapshot:
        status = self._catalog_status
        entries = tuple(
            entry
            for page in sorted(self._catalog_pages)
            for entry in self._catalog_pages[page]
        )
        return OrbitCatalogSnapshot(
            generation=self._catalog_generation,
            total_entries=self._catalog_total,
            scan_pending=bool(status and status.scan_pending),
            scan_running=bool(status and status.scan_running),
            last_scan_success=bool(status and status.last_scan_success),
            invalid_records=0 if status is None else status.invalid_records,
            duplicate_records=0 if status is None else status.duplicate_records,
            capacity_rejections=0 if status is None else status.capacity_rejections,
            entries=entries,
        )

    def current_samples(self) -> Tuple[OrbitCurrentSample, ...]:
        return tuple(
            sample
            for page in sorted(self._current_pages)
            for sample in self._current_pages[page]
        )

    @property
    def current_total(self) -> int:
        return self._current_total

    def sky_snapshot(self) -> Optional[OrbitSkySnapshot]:
        return self._sky_snapshot

    def sky_samples(self) -> Tuple[OrbitSkySample, ...]:
        return () if self._sky_snapshot is None else self._sky_snapshot.samples

    def sky_trail(self, norad_id: int) -> Tuple[OrbitSkyTrailPoint, ...]:
        return self._sky_trails.get(int(norad_id), ())

    def prediction_samples(self) -> Tuple[OrbitPredictionSample, ...]:
        return tuple(
            sample
            for page in sorted(self._prediction_pages)
            for sample in self._prediction_pages[page]
        )

    @property
    def prediction_total(self) -> int:
        return self._prediction_total

    def pass_summaries(self) -> Tuple[OrbitPassSummary, ...]:
        return tuple(
            item
            for page in sorted(self._pass_pages)
            for item in self._pass_pages[page]
        )

    @property
    def pass_total(self) -> int:
        return self._pass_total

    def _invalidate_prediction(self) -> None:
        self.prediction_job = None
        self._prediction_pages.clear()
        self._prediction_total = 0
        self._pass_pages.clear()
        self._pass_total = 0

    @staticmethod
    def _sky_metadata(report: OrbitSkyReport) -> tuple:
        return (
            report.snapshot_id,
            report.generation,
            report.utc_unix_ms,
            report.total_entries,
            report.hard_offaxis_limit_deg,
            report.recommended_offaxis_limit_deg,
            report.mount_yaw_deg,
            report.mount_pitch_deg,
            report.mount_roll_deg,
            report.azimuth_zero_offset_deg,
            report.azimuth_direction,
            report.second_angle_type,
            report.active_target_id,
            report.profile_characterized,
        )

    def _feed_sky_page(self, report: OrbitSkyReport) -> None:
        if report.snapshot_id != self._sky_staging_id:
            current_id = 0 if self._sky_snapshot is None else self._sky_snapshot.snapshot_id
            if current_id and not self._generation_is_newer(report.snapshot_id, current_id):
                return
            if self._sky_staging_id and not self._generation_is_newer(
                report.snapshot_id, self._sky_staging_id
            ):
                return
            self._sky_staging_id = report.snapshot_id
            self._sky_staging_report = report
            self._sky_pages.clear()
        elif self._sky_staging_report is not None and self._sky_metadata(
            report
        ) != self._sky_metadata(self._sky_staging_report):
            self._sky_staging_id = 0
            self._sky_staging_report = None
            self._sky_pages.clear()
            return

        self._sky_pages[report.page] = report.samples
        pages = sorted(self._sky_pages)
        if pages != list(range(len(pages))):
            return
        samples = tuple(sample for page in pages for sample in self._sky_pages[page])
        if len(samples) != report.total_entries:
            return
        if len({sample.norad_id for sample in samples}) != len(samples):
            self._sky_staging_id = 0
            self._sky_staging_report = None
            self._sky_pages.clear()
            return

        source = self._sky_staging_report or report
        self._sky_snapshot = OrbitSkySnapshot(
            source.snapshot_id,
            source.generation,
            source.utc_unix_ms,
            source.hard_offaxis_limit_deg,
            source.recommended_offaxis_limit_deg,
            source.mount_yaw_deg,
            source.mount_pitch_deg,
            source.mount_roll_deg,
            source.azimuth_zero_offset_deg,
            source.azimuth_direction,
            source.second_angle_type,
            source.active_target_id,
            source.profile_characterized,
            samples,
        )
        self._append_sky_trails(self._sky_snapshot)
        self._sky_staging_id = 0
        self._sky_staging_report = None
        self._sky_pages.clear()
        self.sky_changed.emit(self._sky_snapshot)

    def _append_sky_trails(self, snapshot: OrbitSkySnapshot) -> None:
        cutoff = snapshot.utc_unix_ms - 20_000
        present = set()
        for sample in snapshot.samples:
            present.add(sample.norad_id)
            previous = self._sky_trails.get(sample.norad_id, ())
            if previous and snapshot.utc_unix_ms <= previous[-1].utc_unix_ms:
                previous = ()
            point = OrbitSkyTrailPoint(
                snapshot.utc_unix_ms,
                sample.array_azimuth_deg,
                sample.array_offaxis_deg,
            )
            self._sky_trails[sample.norad_id] = tuple(
                item for item in (*previous, point) if item.utc_unix_ms >= cutoff
            )
        for norad_id in tuple(self._sky_trails):
            kept = tuple(
                item for item in self._sky_trails[norad_id] if item.utc_unix_ms >= cutoff
            )
            if kept and norad_id in present:
                self._sky_trails[norad_id] = kept
            else:
                self._sky_trails.pop(norad_id, None)

    def _invalidate_sky(self) -> None:
        self._sky_snapshot = None
        self._sky_staging_id = 0
        self._sky_staging_report = None
        self._sky_pages.clear()
        self._sky_trails.clear()

    def clear(self) -> None:
        was_available = self.available
        self.capabilities = None
        self.last_report = None
        self._catalog_generation = 0
        self._catalog_total = 0
        self._catalog_pages.clear()
        self._catalog_status = None
        self._current_generation = 0
        self._current_total = 0
        self._current_pages.clear()
        self._invalidate_sky()
        self._invalidate_prediction()
        if was_available:
            self.capability_changed.emit(False)
        self.cleared.emit()


__all__ = [
    "OrbitStore",
    "OrbitCatalogSnapshot",
    "OrbitSkySnapshot",
    "OrbitSkyTrailPoint",
]
