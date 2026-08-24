"""Read-only customer curve analysis over full SDB v2/v3 recordings."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.data import DataStore, GnssStore
from satellite_debug_tool.core.playback import (
    PlaybackLoadResult,
    PlaybackLoadThread,
    build_customer_playback,
)
from satellite_debug_tool.core.product import (
    CUSTOMER_PLAYBACK_HW_TYPE,
    CustomerPlaybackProjector,
    customer_channel_entries,
)
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    GnssCnrReport,
    GnssSatReport,
    GnssSignalReport,
    GnssSkyReport,
    MetaInfo,
    ServiceFastState,
    ServiceIdentity,
    ServiceSlowState,
)
from satellite_debug_tool.i18n import (
    register_translatable,
    set_raw_text,
    set_translatable_text,
    tr,
)
from satellite_debug_tool.io.data_importer import DataImporter, SdbFile
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget
from satellite_debug_tool.ui.semantic_style import set_semantic_property
from satellite_debug_tool.ui.time_range_control import TimeRangeControl


_GNSS_RECORDS = (GnssSkyReport, GnssCnrReport, GnssSatReport, GnssSignalReport)


def _customer_channel_name(source: str) -> str:
    """Keep synthetic profile labels visible to Qt's static translation scan."""

    return {
        "Roll": tr("Roll"),
        "Pitch": tr("Pitch"),
        "Yaw": tr("Yaw"),
        "Beam azimuth": tr("Beam azimuth"),
        "Beam elevation": tr("Beam elevation"),
        "SNR": tr("SNR"),
        "Longitude": tr("Longitude"),
        "Latitude": tr("Latitude"),
        "Altitude": tr("Altitude"),
    }[source]


class CustomerPlaybackView(QWidget):
    """Expose customer-approved curves while retaining full recordings on disk."""

    status_message = Signal(str, int)

    def __init__(
        self,
        settings,
        parent: Optional[QWidget] = None,
        *,
        enable_3d: bool = True,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._theme = "dark"
        self._data_store = DataStore(max_channels=16, buffer_capacity=6000)
        self._profile_store = ProfileStore(cache=None)
        self._gnss_store = GnssStore(keep_history=True, parent=self)
        self._current_file: Optional[Path] = None
        self._loaded_version: Optional[int] = None
        self._loaded_quality: dict = {}
        self._metadata_events: tuple[dict, ...] = ()
        self._first_ts_ms: Optional[float] = None
        self._last_ts_ms: Optional[float] = None
        self._total_sec = 0.0
        self._loaded_count = 0
        self._identity_parts: tuple[str, str, str] = ("", "", "")
        self._series_provider = None
        self._load_thread: Optional[PlaybackLoadThread] = None
        self._gnss_widget = None
        self._gnss_dock: Optional[QDockWidget] = None
        self._build_ui()
        self._refresh_customer_profile()
        self._chart.set_mode("stacked")
        self._apply_theme()
        register_translatable(self)

    @property
    def chart(self) -> GroupedChartWidget:
        return self._chart

    @property
    def data_store(self) -> DataStore:
        return self._data_store

    @property
    def gnss_store(self) -> GnssStore:
        return self._gnss_store

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(5, 5, 5, 5)
        root.setSpacing(5)

        toolbar = QFrame()
        toolbar.setObjectName("customerPlaybackToolbar")
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(8, 5, 8, 5)
        row.setSpacing(7)
        self._open_btn = QPushButton(tr("Open recording"))
        self._open_btn.clicked.connect(self._choose_file)
        row.addWidget(self._open_btn)
        self._clear_btn = QPushButton(tr("Clear"))
        self._clear_btn.setEnabled(False)
        self._clear_btn.clicked.connect(self.clear)
        row.addWidget(self._clear_btn)
        self._file_label = QLabel(tr("No recording loaded"))
        self._file_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        row.addWidget(self._file_label, 1)
        self._duration_label = QLabel(tr("Duration: —"))
        row.addWidget(self._duration_label)
        self._range_ctl = TimeRangeControl()
        self._range_ctl.range_changed.connect(self._on_range_changed)
        row.addWidget(self._range_ctl)
        self._gnss_btn = QPushButton("GNSS")
        self._gnss_btn.setEnabled(False)
        self._gnss_btn.clicked.connect(self._toggle_gnss)
        row.addWidget(self._gnss_btn)
        root.addWidget(toolbar)

        info = QFrame()
        info.setObjectName("customerPlaybackInfo")
        info_row = QHBoxLayout(info)
        info_row.setContentsMargins(9, 4, 9, 4)
        info_row.setSpacing(12)
        self._identity_label = QLabel(tr("Device: —"))
        self._identity_label.setObjectName("customerPlaybackIdentity")
        info_row.addWidget(self._identity_label, 1)
        self._quality_label = QLabel("—")
        self._quality_label.setObjectName("customerPlaybackQuality")
        info_row.addWidget(self._quality_label)
        root.addWidget(info)

        self._chart = GroupedChartWidget()
        self._chart.set_profile_store(self._profile_store)
        self._chart.set_settings(self._settings)
        self._chart.set_device_uptime_axis(True)
        self._chart.mode_changed.connect(self._on_chart_mode_changed)
        root.addWidget(self._chart, 1)

    def _refresh_customer_profile(self) -> None:
        self._profile_store.apply_meta(
            MetaInfo(2, "", CUSTOMER_PLAYBACK_HW_TYPE, "")
        )
        self._profile_store.apply_channel_define(
            CUSTOMER_PLAYBACK_HW_TYPE,
            1,
            customer_channel_entries(_customer_channel_name),
        )
        self._chart.set_hw_type(CUSTOMER_PLAYBACK_HW_TYPE)

    def _choose_file(self) -> None:
        last_dir = self._settings.get("paths.recording_dir", "") or ""
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            tr("Open customer recording"),
            last_dir,
            tr("SDB files (*.sdb);;All files (*)"),
        )
        if filepath:
            self._start_file_load(Path(filepath))

    def load_file(self, path: Path) -> bool:
        """Synchronous compatibility entry used by focused tests and tools."""

        self.status_message.emit(tr("Loading {file}...", file=path.name), 0)
        try:
            result = build_customer_playback(path)
        except Exception as exc:
            self.status_message.emit(tr("Failed to open: {detail}", detail=exc), 5000)
            return False
        self._apply_load_result(result)
        return True

    def _start_file_load(self, path: Path) -> None:
        if self._load_thread is not None and self._load_thread.isRunning():
            return
        self.status_message.emit(tr("Loading {file}...", file=path.name), 0)
        self._open_btn.setEnabled(False)
        self._clear_btn.setEnabled(False)
        worker = PlaybackLoadThread(path, customer=True, parent=self)
        self._load_thread = worker
        worker.loaded.connect(self._on_load_ready)
        worker.failed.connect(self._on_load_failed)
        worker.finished.connect(self._on_load_thread_finished)
        worker.start()

    def _on_load_ready(self, result: PlaybackLoadResult) -> None:
        self._apply_load_result(result)

    def _on_load_failed(self, detail: str) -> None:
        self.status_message.emit(tr("Failed to open: {detail}", detail=detail), 5000)
        self._open_btn.setEnabled(True)
        self._clear_btn.setEnabled(self._current_file is not None)

    def _on_load_thread_finished(self) -> None:
        worker = self.sender()
        if worker is self._load_thread:
            self._load_thread = None
        if worker is not None:
            worker.deleteLater()

    def _apply_load_result(self, result: PlaybackLoadResult) -> None:
        previous_provider = self._series_provider
        self._series_provider = result.provider
        if previous_provider is not None:
            previous_provider.close()

        sdb = result.sdb
        source_profile, source_hw = self._source_profile(sdb)

        self._data_store = result.provider.load_window(max_points_per_channel=6000)
        self._gnss_store.clear()
        for record in result.gnss_records:
            self._gnss_store.update(record)

        summary = result.provider.summary
        self._first_ts_ms = summary.first_timestamp_ms
        self._last_ts_ms = summary.last_timestamp_ms
        self._total_sec = (
            max(0.0, (self._last_ts_ms - self._first_ts_ms) / 1000.0)
            if self._first_ts_ms is not None and self._last_ts_ms is not None
            else 0.0
        )
        self._loaded_count = summary.report_count
        self._current_file = result.path
        self._loaded_version = sdb.version
        self._loaded_quality = dict(sdb.quality)
        self._metadata_events = tuple(sdb.metadata_events)
        self._identity_parts = self._resolve_identity(
            sdb,
            source_profile,
            source_hw,
            result.latest_identity,
        )

        self._range_ctl.set_total(self._total_sec)
        self._reset_chart()
        self._gnss_btn.setEnabled(self._gnss_store.has_data())
        self._clear_btn.setEnabled(True)
        set_raw_text(result.path.name, self._file_label)
        self._refresh_labels()
        self.status_message.emit(
            tr(
                "Loaded customer curves: {count} sample frame(s), {duration:.1f}s",
                count=self._loaded_count,
                duration=self._total_sec,
            ),
            4000,
        )
        self._open_btn.setEnabled(True)

    @staticmethod
    def _source_profile(sdb: SdbFile) -> tuple[Optional[ProfileStore], Optional[str]]:
        if sdb.profile is None:
            return None, None
        store = ProfileStore(cache=None)
        hw_type = store.import_dict(sdb.profile)
        return store, hw_type

    @staticmethod
    def _resolve_identity(
        sdb: SdbFile,
        source_profile: Optional[ProfileStore],
        source_hw: Optional[str],
        identity: Optional[ServiceIdentity],
    ) -> tuple[str, str, str]:
        metadata = sdb.metadata.get("identity", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        model = str(metadata.get("model") or sdb.metadata.get("hardware_type") or "")
        serial = str(metadata.get("serial_number") or "")
        firmware = str(metadata.get("main_firmware") or "")
        if identity is not None:
            if identity.valid_mask & (1 << 0):
                model = identity.model
            if identity.valid_mask & (1 << 1):
                serial = identity.serial_number
            if identity.valid_mask & (1 << 2):
                firmware = identity.main_firmware
        if source_profile is not None and source_hw:
            profile = source_profile.get_profile(source_hw)
            meta = None if profile is None else profile.meta
            model = model or source_hw
            if meta is not None:
                serial = serial or meta.device_sn
                firmware = firmware or meta.fw_ver
        return model, serial, firmware

    def _reset_chart(self) -> None:
        self._chart.set_hw_type(None)
        self._chart.set_hw_type(CUSTOMER_PLAYBACK_HW_TYPE)
        self._chart.set_auto_range(True)
        self._chart.refresh(self._data_store)
        if self._first_ts_ms is not None and self._last_ts_ms is not None:
            start = self._first_ts_ms / 1000.0
            end = self._last_ts_ms / 1000.0
            if end <= start:
                start = max(0.0, start - 0.5)
                end += 0.5
            self._chart.set_x_range_sec(start, end)
            self._chart.enable_y_autorange(True)

    def _on_range_changed(self, start_sec: float, end_sec: float) -> None:
        if self._first_ts_ms is None:
            return
        if self._series_provider is not None:
            start_ms = self._first_ts_ms + float(start_sec) * 1000.0
            end_ms = self._first_ts_ms + float(end_sec) * 1000.0
            self._data_store = self._series_provider.load_window(
                start_ms,
                end_ms,
                max_points_per_channel=6000,
            )
            self._chart.clear()
            self._chart.set_time_origin_ms(0.0)
            self._chart.set_auto_range(False)
            self._chart.refresh(self._data_store)
        uptime_origin = self._first_ts_ms / 1000.0
        self._chart.set_x_range_sec(
            uptime_origin + start_sec,
            uptime_origin + end_sec,
        )

    def _on_chart_mode_changed(self, _mode: str) -> None:
        if self._loaded_count:
            self._reset_chart()

    def clear(self) -> None:
        self._data_store.clear()
        if self._series_provider is not None:
            self._series_provider.close()
            self._series_provider = None
        self._gnss_store.clear()
        self._current_file = None
        self._loaded_version = None
        self._loaded_quality = {}
        self._metadata_events = ()
        self._first_ts_ms = None
        self._last_ts_ms = None
        self._total_sec = 0.0
        self._loaded_count = 0
        self._identity_parts = ("", "", "")
        self._range_ctl.set_total(0.0)
        self._reset_chart()
        self._gnss_btn.setEnabled(False)
        self._clear_btn.setEnabled(False)
        self._refresh_labels()

    def shutdown(self) -> None:
        worker = self._load_thread
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            worker.wait(5000)
        self._load_thread = None
        if self._series_provider is not None:
            self._series_provider.close()
            self._series_provider = None

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)

    def _refresh_labels(self) -> None:
        if self._current_file is None:
            self._file_label.setText(tr("No recording loaded"))
            self._duration_label.setText(tr("Duration: —"))
        else:
            set_translatable_text(
                "Duration: {duration:.1f}s · {count} sample frame(s)",
                self._duration_label,
                duration=self._total_sec,
                count=self._loaded_count,
            )
        model, serial, firmware = self._identity_parts
        identity_values = [value for value in (model, serial, firmware) if value]
        if identity_values:
            set_translatable_text(
                "Device: {identity}",
                self._identity_label,
                identity=" · ".join(identity_values),
            )
        else:
            self._identity_label.setText(tr("Device: —"))
        self._refresh_quality_label()

    def _refresh_quality_label(self) -> None:
        dropped = int(self._loaded_quality.get("dropped_chunks", 0))
        interruptions = sum(
            1 for event in self._metadata_events if event.get("event") == "device_offline"
        )
        complete = False
        if self._loaded_version == 3:
            complete = bool(self._loaded_quality.get("complete", False))
            if interruptions:
                self._quality_label.setText(
                    tr("{count} device interruption(s)", count=interruptions)
                )
            elif complete and dropped == 0:
                self._quality_label.setText(tr("Complete · no gaps"))
            else:
                self._quality_label.setText(
                    tr("Incomplete · {count} dropped chunk(s)", count=dropped)
                )
        elif self._loaded_version == 2:
            self._quality_label.setText(tr("SDB v2 · capture quality unavailable"))
        else:
            self._quality_label.setText("—")
        set_semantic_property(
            self._quality_label,
            "complete", complete and dropped == 0 and interruptions == 0
        )

    def _toggle_gnss(self) -> None:
        if not self._gnss_store.has_data():
            return
        if self._gnss_dock is None:
            from satellite_debug_tool.ui.gnss_widget import GnssWidget

            self._gnss_widget = GnssWidget(self._gnss_store, playback=True)
            self._gnss_widget.set_theme(self._theme)
            self._gnss_dock = QDockWidget(tr("GNSS details — Playback"), self)
            self._gnss_dock.setAllowedAreas(Qt.DockWidgetArea.NoDockWidgetArea)
            self._gnss_dock.setFloating(True)
            self._gnss_dock.setWidget(self._gnss_widget)
            self._gnss_dock.resize(1180, 730)
        self._gnss_dock.setVisible(not self._gnss_dock.isVisible())

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
        self._apply_theme()
        self._chart.set_theme(theme, scale)
        self._range_ctl.set_theme(theme, scale)
        if self._loaded_count:
            self._reset_chart()
        if self._gnss_widget is not None:
            self._gnss_widget.set_theme(theme, scale)

    def _apply_theme(self) -> None:
        pal = S.palette(self._theme)
        self.setStyleSheet(
            f"CustomerPlaybackView {{ background: {pal['bg']}; color: {pal['text']}; }}"
            f"#customerPlaybackToolbar, #customerPlaybackInfo {{ background: {pal['panel']}; "
            f"border: 1px solid {pal['border']}; border-radius: 5px; }}"
            f"#customerPlaybackIdentity {{ color: {pal['text_2']}; }}"
            f"#customerPlaybackQuality {{ color: {pal['warn']}; }}"
            f"#customerPlaybackQuality[complete='true'] {{ color: {pal['ok']}; }}"
        )

    def retranslate_ui(self) -> None:
        self._open_btn.setText(tr("Open recording"))
        self._clear_btn.setText(tr("Clear"))
        self._refresh_customer_profile()
        self._chart.retranslate_ui()
        if self._loaded_count:
            self._reset_chart()
        self._refresh_labels()
        if self._gnss_dock is not None:
            self._gnss_dock.setWindowTitle(tr("GNSS details — Playback"))
