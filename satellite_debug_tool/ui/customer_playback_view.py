"""Read-only customer playback over full SDB v2/v3 support recordings."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.data import DataStore, GnssStore, StateStore
from satellite_debug_tool.core.product import ProductServiceStore
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    DataReport,
    EventReport,
    GnssCnrReport,
    GnssSatReport,
    GnssSignalReport,
    GnssSkyReport,
    MetaInfo,
    StateReport,
)
from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.io.data_importer import DataImporter, SdbFile
from satellite_debug_tool.ui import icons, styles as S
from satellite_debug_tool.ui.customer_overview_view import CustomerOverviewView


class _PlaybackSession(QObject):
    connection_state_changed = Signal(bool)
    profile_ready = Signal(str)
    recording_state_changed = Signal(bool, str)
    gnss_requested = Signal()
    is_playback = True

    def __init__(self) -> None:
        super().__init__()
        self._profiles = ProfileStore(cache=None)
        self._data = DataStore(max_channels=64, buffer_capacity=None)
        self._states = StateStore()
        self._gnss = GnssStore(keep_history=True, parent=self)
        self._products = ProductServiceStore(parent=self, enforce_stale=False)
        self._loaded = False

    def profile_store(self):
        return self._profiles

    def data_store(self):
        return self._data

    def state_store(self):
        return self._states

    def gnss_store(self):
        return self._gnss

    def product_store(self):
        return self._products

    def is_connected(self):
        return self._loaded

    def is_recording(self):
        return False

    def toggle_recording(self):
        return None

    def show_gnss_details(self):
        self.gnss_requested.emit()

    def clear_runtime(self) -> None:
        self._data.clear()
        self._states.clear()
        self._gnss.clear()
        self._products.clear()

    def reset_file(self) -> None:
        self.clear_runtime()
        self._profiles.clear()
        self._loaded = False
        self.connection_state_changed.emit(False)

    def load_profile(self, sdb: SdbFile) -> Optional[str]:
        self._profiles.clear()
        hw_type: Optional[str] = None
        if sdb.profile is not None:
            hw_type = self._profiles.import_dict(sdb.profile)
            if hw_type is not None:
                profile = self._profiles.get_profile(hw_type)
                if profile is not None and profile.meta is not None:
                    self._profiles.apply_meta(profile.meta)
        if hw_type is None:
            identity = sdb.metadata.get("identity", {})
            candidate = sdb.metadata.get("hardware_type") or identity.get("model")
            if candidate:
                hw_type = str(candidate).lower()
                self._profiles.apply_meta(
                    MetaInfo(
                        protocol_ver=2,
                        fw_ver=str(identity.get("main_firmware") or ""),
                        hw_type=hw_type,
                        device_sn=str(identity.get("serial_number") or ""),
                    )
                )
        self._loaded = True
        self.connection_state_changed.emit(True)
        if hw_type:
            self.profile_ready.emit(hw_type)
        return hw_type


class CustomerPlaybackView(QWidget):
    """Timeline playback that exposes only stable customer product semantics."""

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
        self._enable_3d = bool(enable_3d)
        self._session = _PlaybackSession()
        self._entries: list[tuple[float, object]] = []
        self._cursor_index = 0
        self._position_s = 0.0
        self._duration_s = 0.0
        self._playing = False
        self._last_tick = time.monotonic()
        self._current_file: Optional[Path] = None
        self._loaded_version: Optional[int] = None
        self._loaded_quality: dict = {}
        self._gnss_widget = None
        self._gnss_dock: Optional[QDockWidget] = None
        self._build_ui()
        self._session.gnss_requested.connect(self._toggle_gnss)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()
        register_translatable(self)

    @property
    def overview(self) -> CustomerOverviewView:
        return self._overview

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        toolbar = QFrame()
        toolbar.setObjectName("customerPlaybackToolbar")
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(12, 7, 12, 7)
        row.setSpacing(8)
        self._open_btn = QPushButton(tr("Open recording"))
        self._open_btn.clicked.connect(self._choose_file)
        row.addWidget(self._open_btn)
        self._play_btn = QPushButton(tr("Play"))
        self._play_btn.setEnabled(False)
        self._play_btn.clicked.connect(self._toggle_play)
        row.addWidget(self._play_btn)
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 10000)
        self._slider.setEnabled(False)
        self._slider.sliderMoved.connect(self._on_slider_moved)
        row.addWidget(self._slider, 1)
        self._time_label = QLabel("00:00.0 / 00:00.0")
        self._time_label.setObjectName("customerPlaybackTime")
        row.addWidget(self._time_label)
        self._speed = QComboBox()
        for text, value in (("0.5x", 0.5), ("1x", 1.0), ("2x", 2.0), ("4x", 4.0)):
            self._speed.addItem(text, value)
        self._speed.setCurrentIndex(1)
        row.addWidget(self._speed)
        root.addWidget(toolbar)

        info = QFrame()
        info.setObjectName("customerPlaybackInfo")
        info_row = QHBoxLayout(info)
        info_row.setContentsMargins(12, 5, 12, 5)
        self._file_label = QLabel(tr("No recording loaded"))
        info_row.addWidget(self._file_label, 1)
        self._quality_label = QLabel("—")
        self._quality_label.setObjectName("customerPlaybackQuality")
        info_row.addWidget(self._quality_label)
        root.addWidget(info)

        self._overview = CustomerOverviewView(
            self._session,
            self._settings,
            enable_3d=self._enable_3d,
            playback_mode=True,
        )
        scroll = QScrollArea()
        scroll.setObjectName("customerPlaybackScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._overview)
        root.addWidget(scroll, 1)

    def _choose_file(self) -> None:
        last_dir = self._settings.get("paths.recording_dir", "") or ""
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            tr("Open customer recording"),
            last_dir,
            tr("SDB files (*.sdb);;All files (*)"),
        )
        if filepath:
            self.load_file(Path(filepath))

    def load_file(self, path: Path) -> bool:
        try:
            sdb = DataImporter.open_sdb(path)
        except Exception as exc:
            self.status_message.emit(tr("Failed to open: {detail}", detail=exc), 5000)
            return False

        timed = list(sdb.iter_timed_records())
        self._entries = self._build_timeline(timed)
        self._duration_s = self._entries[-1][0] if self._entries else 0.0
        self._cursor_index = 0
        self._position_s = 0.0
        self._playing = False
        self._session.reset_file()
        hw_type = self._session.load_profile(sdb)
        self._process_until(0.0)
        self._overview.refresh()
        self._current_file = path
        self._loaded_version = sdb.version
        self._loaded_quality = dict(sdb.quality)
        self._file_label.setText(path.name)
        self._refresh_quality_label()
        self._play_btn.setEnabled(bool(self._entries))
        self._slider.setEnabled(bool(self._entries))
        self._update_controls()
        if hw_type:
            self._overview._on_profile_ready(hw_type)
        self.status_message.emit(
            tr(
                "Loaded customer playback: {count} record(s), {duration:.1f}s",
                count=len(self._entries),
                duration=self._duration_s,
            ),
            4000,
        )
        return True

    def _refresh_quality_label(self) -> None:
        dropped = int(self._loaded_quality.get("dropped_chunks", 0))
        if self._loaded_version == 3:
            complete = bool(self._loaded_quality.get("complete", False))
            self._quality_label.setText(
                tr("Complete · no gaps")
                if complete and dropped == 0
                else tr("Incomplete · {count} dropped chunk(s)", count=dropped)
            )
            self._quality_label.setProperty("complete", complete and dropped == 0)
        elif self._loaded_version == 2:
            self._quality_label.setText(tr("SDB v2 · capture quality unavailable"))
            self._quality_label.setProperty("complete", False)
        else:
            self._quality_label.setText("—")
            self._quality_label.setProperty("complete", False)
        self._repolish(self._quality_label)

    @staticmethod
    def _build_timeline(timed: list[tuple[int, object]]) -> list[tuple[float, object]]:
        if not timed:
            return []
        host_values = [host for host, _record in timed if host > 0]
        first_host = host_values[0] if host_values else 0
        first_device: Optional[float] = None
        last_position = 0.0
        entries: list[tuple[float, object]] = []
        for host_ns, record in timed:
            if first_host and host_ns > 0:
                position = max(0.0, (host_ns - first_host) / 1_000_000_000.0)
            else:
                timestamp = getattr(record, "timestamp", None)
                if timestamp is not None:
                    if first_device is None:
                        first_device = float(timestamp)
                    position = max(0.0, (float(timestamp) - first_device) / 1000.0)
                else:
                    position = last_position
            last_position = max(last_position, position)
            entries.append((last_position, record))
        return entries

    def _toggle_play(self) -> None:
        if not self._entries:
            return
        if self._position_s >= self._duration_s and not self._playing:
            self.seek(0.0)
        self._playing = not self._playing
        self._last_tick = time.monotonic()
        self._update_controls()

    def _on_tick(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_tick
        self._last_tick = now
        if not self._playing:
            return
        speed = float(self._speed.currentData() or 1.0)
        self._position_s = min(self._duration_s, self._position_s + elapsed * speed)
        self._process_until(self._position_s)
        if self._position_s >= self._duration_s:
            self._playing = False
        self._update_controls()

    def _on_slider_moved(self, value: int) -> None:
        position = self._duration_s * max(0, min(10000, int(value))) / 10000.0
        self.seek(position)

    def seek(self, position_s: float) -> None:
        target = max(0.0, min(self._duration_s, float(position_s)))
        if target < self._position_s:
            self._session.clear_runtime()
            self._cursor_index = 0
        self._position_s = target
        self._process_until(target)
        self._overview.refresh()
        self._update_controls()

    def _process_until(self, position_s: float) -> None:
        while self._cursor_index < len(self._entries):
            record_position, record = self._entries[self._cursor_index]
            if record_position > position_s:
                break
            self._apply_record(record)
            self._cursor_index += 1

    def _apply_record(self, record: object) -> None:
        if self._session.product_store().feed(record):
            return
        hw_type = self._session.profile_store().current_hw_type()
        if isinstance(record, DataReport):
            self._session.data_store().update(record)
        elif hw_type is not None and isinstance(record, StateReport):
            self._session.state_store().update(hw_type, record)
        elif isinstance(record, (GnssSkyReport, GnssCnrReport, GnssSatReport, GnssSignalReport)):
            self._session.gnss_store().update(record)
        elif isinstance(record, EventReport):
            return

    def _update_controls(self) -> None:
        self._play_btn.setText(tr("Pause") if self._playing else tr("Play"))
        icon_name = "pause" if self._playing else "play"
        self._play_btn.setIcon(
            icons.icon(icon_name, color=S.palette(self._theme)["text"], size=14)
        )
        if self._duration_s > 0.0:
            self._slider.setValue(int(self._position_s / self._duration_s * 10000.0))
        else:
            self._slider.setValue(0)
        self._time_label.setText(
            f"{self._format_time(self._position_s)} / {self._format_time(self._duration_s)}"
        )

    @staticmethod
    def _format_time(seconds: float) -> str:
        minutes = int(max(0.0, seconds)) // 60
        remain = max(0.0, seconds) - minutes * 60
        return f"{minutes:02d}:{remain:04.1f}"

    def _toggle_gnss(self) -> None:
        if not self._session.gnss_store().has_data():
            return
        if self._gnss_dock is None:
            from satellite_debug_tool.ui.gnss_widget import GnssWidget

            self._gnss_widget = GnssWidget(self._session.gnss_store(), playback=True)
            self._gnss_widget.set_theme(self._theme)
            self._gnss_dock = QDockWidget(tr("GNSS details — Playback"), self)
            self._gnss_dock.setAllowedAreas(Qt.DockWidgetArea.NoDockWidgetArea)
            self._gnss_dock.setFloating(True)
            self._gnss_dock.setWidget(self._gnss_widget)
            self._gnss_dock.resize(1180, 730)
        self._gnss_dock.setVisible(not self._gnss_dock.isVisible())

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
        pal = S.palette(theme)
        self.setStyleSheet(
            f"CustomerPlaybackView {{ background: {pal['bg']}; color: {pal['text']}; }}"
            f"#customerPlaybackToolbar, #customerPlaybackInfo {{ background: {pal['panel']}; "
            f"border-bottom: 1px solid {pal['border']}; }}"
            f"#customerPlaybackTime {{ color: {pal['text_2']}; font-family: '{S.monospace_family()}'; }}"
            f"#customerPlaybackQuality {{ color: {pal['warn']}; }}"
            f"#customerPlaybackQuality[complete='true'] {{ color: {pal['ok']}; }}"
        )
        self._overview.set_theme(theme, scale)
        if self._gnss_widget is not None:
            self._gnss_widget.set_theme(theme, scale)
        self._update_controls()

    def retranslate_ui(self) -> None:
        self._open_btn.setText(tr("Open recording"))
        if self._current_file is None:
            self._file_label.setText(tr("No recording loaded"))
        self._refresh_quality_label()
        self._update_controls()
        self._overview.retranslate_ui()
