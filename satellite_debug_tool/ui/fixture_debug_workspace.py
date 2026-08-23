"""Engineering-only motion-platform and MS-6222 fixture diagnostic workspace."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, replace
from itertools import islice
import math
from pathlib import Path
import time
from typing import Callable, Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.production import (
    CALIBRATION_SEQUENCE,
    CalibrationStage,
    CombinedSineProfile,
    FixtureAnalysisError,
    FixtureAxisLimits,
    FixtureCalibration,
    FixtureCalibrationStore,
    FixtureControlLease,
    FixtureControlWorker,
    FixtureLeaseError,
    FixtureLeaseHandle,
    FixtureProfileError,
    FixtureProfileStore,
    FixtureSessionConclusion,
    FixtureSessionRecorder,
    GuidedFixtureCalibration,
    MotionPlatformError,
    Ms6222FrameEnvelope,
    Ms6222FrameType,
    Ms6222InsRecord,
    Ms6222SerialWorker,
    Ms6222WorkerStatistics,
    PlatformPose,
    SineAxis,
    TimedAttitude,
    WorkstationFixtureProfile,
    compare_attitude_streams,
    compute_dynamic_metrics,
    compute_static_metrics,
    validate_sine_profile,
)
from satellite_debug_tool.i18n import register_translatable, tr, tr_source
from satellite_debug_tool.ui import styles as S


class FixtureProfileDialog(QDialog):
    def __init__(
        self,
        profile: Optional[WorkstationFixtureProfile] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._source = profile
        self._result_profile: Optional[WorkstationFixtureProfile] = None
        self.setWindowTitle(tr("Fixture profile"))
        self.setMinimumWidth(860)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self._profile_id = QLineEdit(profile.profile_id if profile else "")
        self._host = QLineEdit(profile.host if profile else "192.168.1.50")
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(profile.port if profile else 9800)
        self._calibration_id = QLineEdit(profile.calibration_id if profile else "")
        self._minimum_duration = QSpinBox()
        self._minimum_duration.setRange(1, 60000)
        self._minimum_duration.setValue(profile.minimum_duration_ms if profile else 50)
        form.addRow(tr("Profile ID"), self._profile_id)
        form.addRow(tr("Platform IPv4"), self._host)
        form.addRow(tr("UDP port"), self._port)
        form.addRow(tr("Calibration ID"), self._calibration_id)
        form.addRow(tr("Minimum command time (ms)"), self._minimum_duration)
        root.addLayout(form)

        limits_group = QGroupBox(tr("Per-axis hard limits"))
        limits = QGridLayout(limits_group)
        headers = (
            tr("Axis"),
            tr("Angle (deg)"),
            tr("Step (deg)"),
            tr("Frequency (Hz)"),
            tr("Velocity (deg/s)"),
            tr("Acceleration (deg/s²)"),
            tr("Sign"),
        )
        for column, header in enumerate(headers):
            limits.addWidget(QLabel(header), 0, column)
        self._limit_edits: dict[str, tuple[QLineEdit, ...]] = {}
        self._sign_boxes: dict[str, QComboBox] = {}
        for row, axis in enumerate(("roll", "pitch", "yaw"), start=1):
            limits.addWidget(QLabel(axis.upper()), row, 0)
            source_limits = profile.axis_limits[axis] if profile else None
            values = (
                None if source_limits is None else source_limits.abs_angle_deg,
                None if source_limits is None else source_limits.max_step_deg,
                None if source_limits is None else source_limits.max_frequency_hz,
                None if source_limits is None else source_limits.max_velocity_deg_s,
                None if source_limits is None else source_limits.max_acceleration_deg_s2,
            )
            edits = []
            for column, value in enumerate(values, start=1):
                edit = QLineEdit("" if value is None else f"{value:g}")
                edit.setPlaceholderText(tr("Required"))
                limits.addWidget(edit, row, column)
                edits.append(edit)
            sign_box = QComboBox()
            sign_box.addItem("+1", 1)
            sign_box.addItem("-1", -1)
            current_sign = getattr(profile, f"{axis}_sign") if profile else 1
            sign_box.setCurrentIndex(0 if current_sign == 1 else 1)
            limits.addWidget(sign_box, row, 6)
            self._limit_edits[axis] = tuple(edits)
            self._sign_boxes[axis] = sign_box
        root.addWidget(limits_group)

        geometry = QGroupBox(tr("Confirmed A6 geometry"))
        geometry_form = QFormLayout(geometry)
        self._center_z = QDoubleSpinBox()
        self._center_z.setRange(0, 1000)
        self._center_z.setDecimals(2)
        self._center_z.setValue(profile.center_pose.z_mm if profile else 100.0)
        self._reset_z = QDoubleSpinBox()
        self._reset_z.setRange(0, 1000)
        self._reset_z.setDecimals(2)
        self._reset_z.setValue(profile.reset_pose.z_mm if profile else 0.0)
        self._z_min = QDoubleSpinBox()
        self._z_min.setRange(-1000, 1000)
        self._z_min.setValue(profile.z_min_mm if profile else 0.0)
        self._z_max = QDoubleSpinBox()
        self._z_max.setRange(-1000, 1000)
        self._z_max.setValue(profile.z_max_mm if profile else 100.0)
        geometry_form.addRow(tr("Center Z (mm)"), self._center_z)
        geometry_form.addRow(tr("Reset Z (mm)"), self._reset_z)
        geometry_form.addRow(tr("Minimum Z (mm)"), self._z_min)
        geometry_form.addRow(tr("Maximum Z (mm)"), self._z_max)
        root.addWidget(geometry)

        self._error = QLabel("")
        self._error.setObjectName("fixtureProfileError")
        self._error.setWordWrap(True)
        root.addWidget(self._error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        register_translatable(self)

    @property
    def result_profile(self) -> Optional[WorkstationFixtureProfile]:
        return self._result_profile

    def _validate_and_accept(self) -> None:
        try:
            axis_limits = {}
            for axis, edits in self._limit_edits.items():
                values = [float(edit.text().strip()) for edit in edits]
                axis_limits[axis] = FixtureAxisLimits(*values)
            revision = 1 if self._source is None else self._source.revision + 1
            profile = WorkstationFixtureProfile(
                profile_id=self._profile_id.text().strip(),
                revision=revision,
                host=self._host.text().strip(),
                port=self._port.value(),
                center_pose=PlatformPose(0, 0, 0, 0, 0, self._center_z.value()),
                reset_pose=PlatformPose(0, 0, 0, 0, 0, self._reset_z.value()),
                roll_limits=axis_limits["roll"],
                pitch_limits=axis_limits["pitch"],
                yaw_limits=axis_limits["yaw"],
                calibration_id=self._calibration_id.text().strip(),
                roll_sign=int(self._sign_boxes["roll"].currentData()),
                pitch_sign=int(self._sign_boxes["pitch"].currentData()),
                yaw_sign=int(self._sign_boxes["yaw"].currentData()),
                z_min_mm=self._z_min.value(),
                z_max_mm=self._z_max.value(),
                minimum_duration_ms=self._minimum_duration.value(),
            )
            profile.validate()
        except (FixtureProfileError, TypeError, ValueError) as exc:
            self._error.setText(str(exc))
            return
        self._result_profile = profile
        self.accept()


class FixtureAttitudePlot(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._widget = pg.GraphicsLayoutWidget()
        self._plots = {}
        self._curves = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._widget)
        colors = {"target": "#5f7cff", "measured": "#00b3a4", "error": "#ff6b70"}
        self._curve_sources = {
            "target": tr_source("Target"),
            "measured": tr_source("Measured"),
            "error": tr_source("Error"),
        }
        for row, axis in enumerate(("roll", "pitch", "yaw")):
            plot = self._widget.addPlot(row=row, col=0)
            plot.showGrid(x=True, y=True, alpha=0.15)
            plot.setLabel("left", axis.upper(), units="deg")
            plot.setDownsampling(mode="peak", auto=True)
            plot.setClipToView(True)
            if row < 2:
                plot.hideAxis("bottom")
            else:
                plot.setLabel("bottom", tr("Session time"), units="s")
            if row == 0:
                plot.addLegend(offset=(8, 8))
            self._plots[axis] = plot
            self._curves[axis] = {
                name: plot.plot(
                    [],
                    [],
                    pen=pg.mkPen(color, width=1.5),
                    name=tr(self._curve_sources[name]),
                )
                for name, color in colors.items()
            }
        self.setMinimumHeight(270)

    def set_data(
        self,
        targets: tuple[TimedAttitude, ...],
        measurements: tuple[TimedAttitude, ...],
        errors: tuple[TimedAttitude, ...],
        *,
        origin_ns: int,
    ) -> None:
        for axis in ("roll", "pitch", "yaw"):
            for name, samples in (
                ("target", targets),
                ("measured", measurements),
                ("error", errors),
            ):
                x = np.asarray(
                    [(sample.monotonic_ns - origin_ns) / 1e9 for sample in samples],
                    dtype=float,
                )
                y = np.asarray([getattr(sample, f"{axis}_deg") for sample in samples], dtype=float)
                self._curves[axis][name].setData(x, y, connect="finite")
        if targets or measurements:
            latest_ns = max(
                [sample.monotonic_ns for sample in targets[-1:]]
                + [sample.monotonic_ns for sample in measurements[-1:]]
            )
            x_max = max(30.0, (latest_ns - origin_ns) / 1e9)
            for plot in self._plots.values():
                plot.setXRange(max(0.0, x_max - 300.0), x_max, padding=0.0)

    def clear(self) -> None:
        for curves in self._curves.values():
            for curve in curves.values():
                curve.setData([], [])

    def set_theme(self, theme: str) -> None:
        palette = S.palette(theme)
        self._widget.setBackground(palette["panel"])
        for plot in self._plots.values():
            for axis_name in ("left", "bottom"):
                axis = plot.getAxis(axis_name)
                axis.setPen(pg.mkPen(palette["border_2"]))
                axis.setTextPen(pg.mkPen(palette["text_2"]))

    def retranslate_ui(self) -> None:
        self._plots["yaw"].setLabel("bottom", tr("Session time"), units="s")
        legend = self._plots["roll"].legend
        if legend is not None:
            for (_sample, label), source in zip(
                legend.items,
                self._curve_sources.values(),
            ):
                label.setText(tr(source))


class _SessionFinalizeThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        recorder: FixtureSessionRecorder,
        conclusion: FixtureSessionConclusion,
        notes: str,
        targets: tuple[TimedAttitude, ...],
        measurements: tuple[TimedAttitude, ...],
        calibration: Optional[FixtureCalibration],
        max_target_gap_s: float,
        control_runs: tuple[dict, ...],
        ms6222_statistics: Optional[dict],
        abort_reason: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._recorder = recorder
        self._conclusion = conclusion
        self._notes = notes
        self._targets = targets
        self._measurements = measurements
        self._calibration = calibration
        self._max_target_gap_s = float(max_target_gap_s)
        self._control_runs = control_runs
        self._ms6222_statistics = ms6222_statistics
        self._abort_reason = str(abort_reason)

    def run(self) -> None:
        if self._abort_reason:
            try:
                result = self._recorder.abort(
                    reason=self._abort_reason,
                    summary={
                        "timing_mode": "HOST_ARRIVAL_ONLY_NO_PPS",
                        "metrics_incomplete": True,
                        "control_runs": list(self._control_runs),
                        "ms6222_statistics": self._ms6222_statistics,
                    },
                )
            except Exception as exc:
                self.failed.emit(str(exc))
            else:
                self.completed.emit(result)
            return

        try:
            coordinate_valid = bool(
                self._calibration and self._calibration.coordinate_valid
            )
            comparisons = (
                compare_attitude_streams(
                    self._targets,
                    self._measurements,
                    calibration=self._calibration,
                    max_target_gap_s=self._max_target_gap_s,
                )
                if coordinate_valid
                else ()
            )
            self._recorder.record_comparisons(comparisons)
            summary = {
                "timing_mode": "HOST_ARRIVAL_ONLY_NO_PPS",
                "phase_delay_is_engineering_estimate": True,
                "coordinate_calibration_id": (
                    self._calibration.calibration_id if self._calibration else ""
                ),
                "coordinate_valid": coordinate_valid,
                "control_runs": list(self._control_runs),
                "static_metrics": compute_static_metrics(comparisons),
                "dynamic_metrics": {
                    axis: compute_dynamic_metrics(comparisons, primary_axis=axis)
                    for axis in ("roll", "pitch", "yaw")
                },
                "ms6222_statistics": self._ms6222_statistics,
            }
            result = self._recorder.complete(
                conclusion=self._conclusion,
                notes=self._notes,
                summary=summary,
            )
        except Exception as exc:
            reason = f"session finalization failed: {exc}"
            try:
                result = self._recorder.abort(
                    reason=reason,
                    summary={
                        "timing_mode": "HOST_ARRIVAL_ONLY_NO_PPS",
                        "metrics_incomplete": True,
                        "control_runs": list(self._control_runs),
                        "ms6222_statistics": self._ms6222_statistics,
                    },
                )
            except Exception as abort_exc:
                self.failed.emit(f"{reason}; incomplete manifest update failed: {abort_exc}")
            else:
                self.completed.emit(result)
        else:
            self.completed.emit(result)


class FixtureDebugWorkspace(QWidget):
    status_message = Signal(str, int)
    active_changed = Signal(bool)

    def __init__(
        self,
        settings: Settings,
        lease: FixtureControlLease,
        *,
        profile_store: Optional[FixtureProfileStore] = None,
        calibration_store: Optional[FixtureCalibrationStore] = None,
        session_root: Optional[Path] = None,
        control_sender: Optional[Callable[[bytes, tuple[str, int]], bool]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._lease = lease
        self._profile_store = profile_store or FixtureProfileStore()
        self._calibration_store = calibration_store or FixtureCalibrationStore()
        self._session_root = session_root
        self._control_sender = control_sender
        self._theme = "dark"
        self._profile: Optional[WorkstationFixtureProfile] = None
        self._calibration: Optional[FixtureCalibration] = None
        self._lease_handle: Optional[FixtureLeaseHandle] = None
        self._control: Optional[FixtureControlWorker] = None
        self._ms_worker: Optional[Ms6222SerialWorker] = None
        self._ms_connected = False
        self._ms_last_error = ""
        self._recorder: Optional[FixtureSessionRecorder] = None
        self._finalizer: Optional[_SessionFinalizeThread] = None
        self._session_accepting_frames = False
        self._evidence_ready = True
        self._session_origin_ns = time.monotonic_ns()
        self._targets: deque[TimedAttitude] = deque(maxlen=100000)
        # INSPVAXB is nominally 100 Hz. Keep more than 60 minutes for final
        # metrics, while limiting plot-only buffers to the visible five minutes.
        self._measurements: deque[TimedAttitude] = deque(maxlen=400000)
        self._display_measurements: deque[TimedAttitude] = deque(maxlen=60000)
        self._errors: deque[TimedAttitude] = deque(maxlen=60000)
        self._run_summaries: list[dict] = []
        self._latest_ms_stats: Optional[Ms6222WorkerStatistics] = None
        self._latest_ins_quality: Optional[tuple[int, int, int]] = None
        self._guided_calibration: Optional[GuidedFixtureCalibration] = None
        self._calibration_capture_enabled = False
        self._build_ui()
        self._reload_profiles()
        self._refresh_serial_ports()
        self._plot_timer = QTimer(self)
        self._plot_timer.setInterval(100)
        self._plot_timer.timeout.connect(self._refresh_plot)
        self._plot_timer.start()
        register_translatable(self)

    @property
    def profile(self) -> Optional[WorkstationFixtureProfile]:
        return self._profile

    @property
    def session_active(self) -> bool:
        return self._recorder is not None and self._lease.held_by(self._lease_handle)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        top = QGroupBox(tr("Fixture profile and reference"))
        self._profile_group = top
        top_grid = QGridLayout(top)
        top_grid.setContentsMargins(8, 12, 8, 7)
        self._profile_label = QLabel(tr("Profile"))
        self._profile_combo = QComboBox()
        self._profile_combo.setObjectName("fixtureProfileCombo")
        self._profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        self._profile_new = QPushButton(tr("New..."))
        self._profile_new.clicked.connect(lambda: self._edit_profile(new=True))
        self._profile_edit = QPushButton(tr("Edit..."))
        self._profile_edit.clicked.connect(lambda: self._edit_profile(new=False))
        self._profile_reload = QPushButton(tr("Reload"))
        self._profile_reload.clicked.connect(self._reload_profiles)
        self._profile_status = QLabel(tr("No fixture profile"))
        self._profile_status.setObjectName("fixtureProfileStatus")
        self._serial_label = QLabel(tr("MS-6222 port"))
        self._serial_combo = QComboBox()
        self._serial_combo.setEditable(True)
        self._serial_refresh = QPushButton(tr("Refresh"))
        self._serial_refresh.clicked.connect(self._refresh_serial_ports)
        self._serial_connect = QPushButton(tr("Connect reference"))
        self._serial_connect.clicked.connect(self._toggle_ms_connection)
        self._ms_status = QLabel(tr("Reference disconnected"))
        self._ms_status.setObjectName("fixtureMsStatus")
        top_grid.addWidget(self._profile_label, 0, 0)
        top_grid.addWidget(self._profile_combo, 0, 1)
        top_grid.addWidget(self._profile_new, 0, 2)
        top_grid.addWidget(self._profile_edit, 0, 3)
        top_grid.addWidget(self._profile_reload, 0, 4)
        top_grid.addWidget(self._profile_status, 0, 5, 1, 3)
        top_grid.addWidget(self._serial_label, 1, 0)
        top_grid.addWidget(self._serial_combo, 1, 1, 1, 2)
        top_grid.addWidget(self._serial_refresh, 1, 3)
        top_grid.addWidget(self._serial_connect, 1, 4)
        top_grid.addWidget(self._ms_status, 1, 5, 1, 3)
        top_grid.setColumnStretch(1, 2)
        top_grid.setColumnStretch(5, 3)
        root.addWidget(top)

        safety = QGroupBox(tr("Motion safety gate"))
        self._safety_group = safety
        safety_layout = QHBoxLayout(safety)
        safety_layout.setContentsMargins(8, 12, 8, 7)
        self._safety_checks = []
        self._safety_sources = (
            tr_source("Platform is centered"),
            tr_source("Area is clear"),
            tr_source("Emergency stop is available"),
            tr_source("Fixture is secured"),
        )
        for source in self._safety_sources:
            check = QCheckBox(tr(source))
            check.stateChanged.connect(self._update_start_gate)
            self._safety_checks.append(check)
            safety_layout.addWidget(check)
        self._session_start = QPushButton(tr("Start engineering session"))
        self._session_start.setProperty("variant", "primary")
        self._session_start.clicked.connect(self._start_session_clicked)
        self._session_end = QPushButton(tr("Finish session"))
        self._session_end.setEnabled(False)
        self._session_end.clicked.connect(self._finish_session_clicked)
        safety_layout.addStretch(1)
        safety_layout.addWidget(self._session_start)
        safety_layout.addWidget(self._session_end)
        root.addWidget(safety)

        main = QSplitter(Qt.Orientation.Horizontal)
        main.setChildrenCollapsible(False)
        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        controls_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 4, 0)
        controls_layout.setSpacing(6)
        controls_layout.addWidget(self._build_manual_group())
        controls_layout.addWidget(self._build_trajectory_group())
        controls_layout.addWidget(self._build_calibration_group())
        controls_layout.addStretch(1)
        controls_scroll.setWidget(controls)
        controls_scroll.setMinimumWidth(420)
        main.addWidget(controls_scroll)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        live_group = QGroupBox(tr("Target / measured / error attitude"))
        self._live_group = live_group
        live_layout = QVBoxLayout(live_group)
        live_layout.setContentsMargins(6, 12, 6, 6)
        self._attitude_plot = FixtureAttitudePlot()
        live_layout.addWidget(self._attitude_plot)
        self._attitude_status = QLabel(tr("Raw attitude only; no confirmed coordinate calibration."))
        live_layout.addWidget(self._attitude_status)
        right_layout.addWidget(live_group, 3)

        event_group = QGroupBox(tr("Fixture events"))
        self._event_group = event_group
        event_layout = QVBoxLayout(event_group)
        event_layout.setContentsMargins(6, 12, 6, 6)
        self._event_log = QPlainTextEdit()
        self._event_log.setReadOnly(True)
        self._event_log.setMaximumBlockCount(3000)
        self._event_log.setMinimumHeight(75)
        event_layout.addWidget(self._event_log)
        right_layout.addWidget(event_group, 1)
        main.addWidget(right)
        main.setStretchFactor(0, 3)
        main.setStretchFactor(1, 5)
        main.setSizes([450, 830])
        root.addWidget(main, 1)

        footer = QHBoxLayout()
        self._platform_status = QLabel(tr("Profile not ready"))
        self._platform_status.setObjectName("fixturePlatformStatus")
        self._conclusion = QComboBox()
        self._conclusion.setMinimumWidth(112)
        self._conclusion.addItem(tr("Inconclusive"), FixtureSessionConclusion.INCONCLUSIVE.value)
        self._conclusion.addItem(tr("Pass"), FixtureSessionConclusion.PASS.value)
        self._conclusion.addItem(tr("Fail"), FixtureSessionConclusion.FAIL.value)
        self._notes = QLineEdit()
        self._notes.setPlaceholderText(tr("Operator notes"))
        footer.addWidget(self._platform_status, 2)
        self._conclusion_label = QLabel(tr("Engineering conclusion"))
        footer.addWidget(self._conclusion_label)
        footer.addWidget(self._conclusion)
        footer.addWidget(self._notes, 3)
        root.addLayout(footer)

    def _build_manual_group(self) -> QGroupBox:
        group = QGroupBox(tr("Absolute movement"))
        self._manual_group = group
        layout = QGridLayout(group)
        self._axis = QComboBox()
        self._axis.addItem("Roll", "roll")
        self._axis.addItem("Pitch", "pitch")
        self._axis.addItem("Yaw", "yaw")
        self._axis.currentIndexChanged.connect(self._update_axis_range)
        self._target = QDoubleSpinBox()
        self._target.setDecimals(2)
        self._target.setRange(-15.0, 15.0)
        self._duration = QDoubleSpinBox()
        self._duration.setRange(0.05, 600.0)
        self._duration.setValue(3.0)
        self._duration.setSuffix(" s")
        for spin in (self._target, self._duration):
            spin.setMinimumWidth(0)
            spin.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Fixed,
            )
        self._move = QPushButton(tr("Send absolute target"))
        self._move.clicked.connect(self._send_axis_move)
        self._center = QPushButton(tr("Center"))
        self._center.clicked.connect(self._send_center)
        self._stop = QPushButton(tr("Stop sequence"))
        self._stop.setProperty("variant", "danger")
        self._stop.clicked.connect(self._stop_sequence)
        self._reset = QPushButton(tr("Reset Z=0..."))
        self._reset.clicked.connect(self._send_reset)
        for button in (self._move, self._center, self._stop, self._reset):
            button.setMinimumWidth(0)
            button.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Fixed,
            )
        self._manual_labels = {
            "axis": QLabel(tr("Axis")),
            "target": QLabel(tr("Target angle")),
            "duration": QLabel(tr("Total completion time")),
        }
        layout.addWidget(self._manual_labels["axis"], 0, 0)
        layout.addWidget(self._axis, 0, 1)
        layout.addWidget(self._manual_labels["target"], 1, 0)
        layout.addWidget(self._target, 1, 1)
        layout.addWidget(self._manual_labels["duration"], 2, 0)
        layout.addWidget(self._duration, 2, 1)
        layout.addWidget(self._move, 3, 0, 1, 2)
        layout.addWidget(self._center, 4, 0)
        layout.addWidget(self._stop, 4, 1)
        layout.addWidget(self._reset, 5, 0, 1, 2)
        return group

    def _build_trajectory_group(self) -> QGroupBox:
        group = QGroupBox(tr("Three-axis sine trajectory"))
        self._trajectory_group = group
        layout = QGridLayout(group)
        self._trajectory_headers = [
            QLabel(tr("Axis")),
            QLabel(tr("Amplitude")),
            QLabel(tr("Frequency")),
            QLabel(tr("Phase")),
        ]
        for column, label in enumerate(self._trajectory_headers):
            layout.addWidget(label, 0, column)
        self._trajectory_axes = {}
        for row, axis in enumerate(("roll", "pitch", "yaw"), start=1):
            enabled = QCheckBox(axis.upper())
            amplitude = QDoubleSpinBox()
            amplitude.setDecimals(2)
            amplitude.setRange(0.0, 180.0)
            amplitude.setSuffix(" °")
            frequency = QDoubleSpinBox()
            frequency.setDecimals(3)
            frequency.setRange(0.0, 10.0)
            frequency.setSuffix(" Hz")
            phase = QDoubleSpinBox()
            phase.setRange(-360.0, 360.0)
            phase.setSuffix(" °")
            for spin in (amplitude, frequency, phase):
                spin.setMinimumWidth(0)
                spin.setSizePolicy(
                    QSizePolicy.Policy.Ignored,
                    QSizePolicy.Policy.Fixed,
                )
            layout.addWidget(enabled, row, 0)
            layout.addWidget(amplitude, row, 1)
            layout.addWidget(frequency, row, 2)
            layout.addWidget(phase, row, 3)
            self._trajectory_axes[axis] = (enabled, amplitude, frequency, phase)
        self._ramp_in = QDoubleSpinBox()
        self._ramp_in.setRange(0, 600)
        self._ramp_in.setValue(10)
        self._ramp_in.setSuffix(" s")
        self._steady = QDoubleSpinBox()
        self._steady.setRange(0.1, 21600)
        self._steady.setValue(60)
        self._steady.setSuffix(" s")
        self._ramp_out = QDoubleSpinBox()
        self._ramp_out.setRange(0, 600)
        self._ramp_out.setValue(10)
        self._ramp_out.setSuffix(" s")
        self._sample_period = QSpinBox()
        self._sample_period.setRange(10, 10000)
        self._sample_period.setValue(100)
        self._sample_period.setSuffix(" ms")
        self._trajectory_labels = {
            "ramp_in": QLabel(tr("Ramp in")),
            "steady": QLabel(tr("Steady")),
            "ramp_out": QLabel(tr("Ramp out")),
            "sample": QLabel(tr("Sample period")),
        }
        layout.addWidget(self._trajectory_labels["ramp_in"], 4, 0)
        layout.addWidget(self._ramp_in, 4, 1)
        layout.addWidget(self._trajectory_labels["steady"], 5, 0)
        layout.addWidget(self._steady, 5, 1)
        layout.addWidget(self._trajectory_labels["ramp_out"], 6, 0)
        layout.addWidget(self._ramp_out, 6, 1)
        layout.addWidget(self._trajectory_labels["sample"], 7, 0)
        layout.addWidget(self._sample_period, 7, 1)
        self._preview = pg.PlotWidget()
        self._preview.setMinimumHeight(100)
        self._preview.setMouseEnabled(x=False, y=False)
        self._preview.setMenuEnabled(False)
        self._preview.showGrid(x=True, y=True, alpha=0.15)
        self._preview_curves = {
            "roll": self._preview.plot([], [], pen=pg.mkPen("#5f7cff")),
            "pitch": self._preview.plot([], [], pen=pg.mkPen("#00b3a4")),
            "yaw": self._preview.plot([], [], pen=pg.mkPen("#ffb020")),
        }
        layout.addWidget(self._preview, 8, 0, 1, 4)
        self._preview_button = QPushButton(tr("Preview and validate"))
        self._preview_button.clicked.connect(self._preview_trajectory)
        self._trajectory_start = QPushButton(tr("Start trajectory"))
        self._trajectory_start.setProperty("variant", "primary")
        self._trajectory_start.clicked.connect(self._start_trajectory)
        layout.addWidget(self._preview_button, 9, 0, 1, 2)
        layout.addWidget(self._trajectory_start, 9, 2, 1, 2)
        return group

    def _build_calibration_group(self) -> QGroupBox:
        group = QGroupBox(tr("Guided coordinate calibration"))
        self._calibration_group = group
        layout = QGridLayout(group)
        self._calibration_status = QLabel(tr("Not started"))
        self._calibration_status.setWordWrap(True)
        self._calibration_start = QPushButton(tr("Start guide"))
        self._calibration_start.clicked.connect(self._start_calibration)
        self._calibration_send = QPushButton(tr("Send guide pose"))
        self._calibration_send.setEnabled(False)
        self._calibration_send.clicked.connect(self._send_calibration_pose)
        self._calibration_capture = QPushButton(tr("Capture last 2 s"))
        self._calibration_capture.setEnabled(False)
        self._calibration_capture.clicked.connect(self._capture_calibration_stage)
        self._calibration_evaluate = QPushButton(tr("Evaluate and confirm..."))
        self._calibration_evaluate.setEnabled(False)
        self._calibration_evaluate.clicked.connect(self._evaluate_calibration)
        layout.addWidget(self._calibration_status, 0, 0, 1, 2)
        layout.addWidget(self._calibration_start, 1, 0)
        layout.addWidget(self._calibration_send, 1, 1)
        layout.addWidget(self._calibration_capture, 2, 0)
        layout.addWidget(self._calibration_evaluate, 2, 1)
        return group

    def _reload_profiles(self) -> None:
        selected = str(
            self._settings.get("production.fixture_profile_id", "") or ""
        )
        profiles = self._profile_store.list_profiles()
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        self._profile_combo.addItem(tr("Select a profile"), "")
        for profile in profiles:
            self._profile_combo.addItem(
                f"{profile.profile_id} r{profile.revision}", profile.profile_id
            )
        index = self._profile_combo.findData(selected)
        self._profile_combo.setCurrentIndex(max(0, index))
        self._profile_combo.blockSignals(False)
        self._on_profile_selected(self._profile_combo.currentIndex())

    def _on_profile_selected(self, _index: int) -> None:
        profile_id = str(self._profile_combo.currentData() or "")
        self._profile = None
        self._calibration = None
        if profile_id:
            try:
                self._profile = self._profile_store.load(profile_id)
            except (FixtureProfileError, OSError, ValueError) as exc:
                self._profile_status.setText(str(exc))
            else:
                self._settings.set("production.fixture_profile_id", profile_id)
                self._settings.save()
                candidates = self._calibration_store.list_for_profile(profile_id)
                self._calibration = next(
                    (
                        item
                        for item in reversed(candidates)
                        if item.calibration_id == self._profile.calibration_id
                        and item.confirmed
                        and item.profile_sha256 == self._profile.sha256
                    ),
                    None,
                )
                self._profile_status.setText(
                    tr(
                        "Profile valid | r{revision} | SHA {sha}",
                        revision=self._profile.revision,
                        sha=self._profile.sha256[:12],
                    )
                )
        if self._profile is None:
            self._profile_status.setText(tr("No fixture profile"))
        self._profile_edit.setEnabled(self._profile is not None and not self.session_active)
        self._update_profile_ranges()
        self._update_start_gate()
        self._update_calibration_status()
        self._update_attitude_status()

    def _edit_profile(self, *, new: bool) -> None:
        if self.session_active:
            return
        source = None if new else self._profile
        dialog = FixtureProfileDialog(source, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result_profile is None:
            return
        profile = dialog.result_profile
        try:
            self._profile_store.save(
                profile,
                expected_revision=None if source is None else source.revision,
            )
        except (FixtureProfileError, OSError) as exc:
            QMessageBox.critical(self, tr("Fixture profile"), str(exc))
            return
        self._settings.set("production.fixture_profile_id", profile.profile_id)
        self._settings.save()
        self._reload_profiles()

    def _update_profile_ranges(self) -> None:
        if self._profile is None:
            return
        self._update_axis_range()
        for axis, (_enabled, amplitude, frequency, _phase) in self._trajectory_axes.items():
            limits = self._profile.axis_limits[axis]
            amplitude.setMaximum(limits.abs_angle_deg)
            frequency.setMaximum(limits.max_frequency_hz or 0.0)

    def _update_axis_range(self) -> None:
        if self._profile is None:
            return
        axis = str(self._axis.currentData())
        limit = self._profile.axis_limits[axis].abs_angle_deg
        self._target.setRange(-limit, limit)

    def _refresh_serial_ports(self) -> None:
        current = self._serial_combo.currentText().strip()
        ports = [item.device for item in list_ports.comports()]
        self._serial_combo.clear()
        self._serial_combo.addItems(ports)
        if current:
            self._serial_combo.setCurrentText(current)

    def _toggle_ms_connection(self) -> None:
        if self._ms_worker is not None:
            self._disconnect_ms()
            return
        port = self._serial_combo.currentText().strip()
        if not port:
            self._show_error(tr("Select an MS-6222 serial port."))
            return
        worker = Ms6222SerialWorker(port, parent=self)
        worker.frame_received.connect(self._on_ms_frame)
        worker.invalid_frame_received.connect(self._on_ms_frame)
        worker.statistics_changed.connect(self._on_ms_statistics)
        worker.connection_changed.connect(self._on_ms_connection_changed)
        worker.error_occurred.connect(self._on_ms_error)
        worker.finished.connect(self._on_ms_worker_finished)
        self._ms_worker = worker
        self._ms_connected = False
        self._ms_last_error = ""
        self._latest_ins_quality = None
        self._serial_connect.setEnabled(False)
        self._ms_status.setText(tr("Opening reference serial port..."))
        worker.start()

    def _disconnect_ms(self) -> None:
        worker = self._ms_worker
        self._ms_worker = None
        self._ms_connected = False
        self._ms_last_error = ""
        self._latest_ins_quality = None
        if worker is not None:
            worker.stop()
            worker.wait(2000)
        self._serial_connect.setText(tr("Connect reference"))
        self._serial_connect.setEnabled(True)
        self._ms_status.setText(tr("Reference disconnected"))

    def _on_ms_connection_changed(self, connected: bool, port: str) -> None:
        self._ms_connected = bool(connected)
        self._serial_connect.setEnabled(True)
        self._serial_connect.setText(
            tr("Disconnect reference") if connected else tr("Connect reference")
        )
        self._ms_status.setText(
            tr("Reference serial open: {port}", port=port)
            if connected
            else tr("Reference disconnected")
        )
        self._append_event(
            tr("MS-6222 serial connected") if connected else tr("MS-6222 serial disconnected")
        )

    def _on_ms_error(self, details: str) -> None:
        self._ms_last_error = str(details)
        self._ms_connected = False
        self._ms_status.setText(tr("Reference error: {details}", details=details))
        self._append_event(self._ms_status.text())

    def _on_ms_worker_finished(self) -> None:
        worker = self.sender()
        if worker is self._ms_worker:
            self._ms_worker = None
            self._ms_connected = False
            self._serial_connect.setText(tr("Connect reference"))
            self._serial_connect.setEnabled(True)
            if not self._ms_last_error:
                self._ms_status.setText(tr("Reference disconnected"))
        if isinstance(worker, QThread):
            worker.deleteLater()

    def _on_ms_statistics(self, statistics: Ms6222WorkerStatistics) -> None:
        self._latest_ms_stats = statistics
        if statistics.connected:
            quality = "-/-/-"
            if self._latest_ins_quality is not None:
                quality = "/".join(str(value) for value in self._latest_ins_quality)
            self._ms_status.setText(
                tr(
                    "Reference open | INS {ins:.1f} Hz | GNSS {gnss:.1f} Hz | "
                    "RAWIMU {raw:.1f} Hz | valid {ratio:.1f}% | "
                    "GNSS/Fix/EKF {quality}",
                    ins=statistics.ins_rate_hz,
                    gnss=statistics.gnss_rate_hz,
                    raw=statistics.rawimu_rate_hz,
                    ratio=statistics.valid_ratio * 100.0,
                    quality=quality,
                )
            )

    def _on_ms_frame(self, envelope: Ms6222FrameEnvelope) -> None:
        recorder = self._recorder
        if recorder is not None and self._session_accepting_frames:
            try:
                recorder.record_ms_frame(envelope)
            except Exception as exc:
                self._fail_evidence(str(exc))
        if not envelope.valid or envelope.frame_type != Ms6222FrameType.INSPVAXB:
            return
        record = envelope.record
        if not isinstance(record, Ms6222InsRecord):
            return
        self._latest_ins_quality = (
            int(record.gnss_state),
            int(record.fix_type),
            int(record.ekf_state),
        )
        solution_valid = (
            record.gnss_state in {1, 3}
            and all(
                math.isfinite(value)
                for value in (record.roll_deg, record.pitch_deg, record.yaw_deg)
            )
        )
        raw = TimedAttitude(
            envelope.host_monotonic_ns,
            record.roll_deg,
            record.pitch_deg,
            record.yaw_deg,
            valid=solution_valid,
        )
        self._measurements.append(raw)
        display = self._calibration.apply(raw) if self._calibration and self._calibration.coordinate_valid else raw
        self._display_measurements.append(display)
        if self._targets and self._calibration and self._calibration.coordinate_valid:
            recent_targets = tuple(
                reversed(tuple(islice(reversed(self._targets), 4)))
            )
            comparison = compare_attitude_streams(
                recent_targets,
                (raw,),
                calibration=self._calibration,
                max_target_gap_s=max(
                    0.5,
                    self._sample_period.value() / 1000.0 * 3.0,
                ),
            )
            if comparison:
                latest = comparison[-1]
                self._errors.append(
                    TimedAttitude(
                        raw.monotonic_ns,
                        latest.error_roll_deg,
                        latest.error_pitch_deg,
                        latest.error_yaw_deg,
                    )
                )

    def _update_start_gate(self) -> None:
        ready = self._profile is not None and all(check.isChecked() for check in self._safety_checks)
        self._session_start.setEnabled(ready and not self.session_active and self._finalizer is None)
        if self.session_active:
            self._platform_status.setText(tr("Preflight complete | control lease held"))
        elif self._profile is None:
            self._platform_status.setText(tr("Profile not ready"))
        elif not all(check.isChecked() for check in self._safety_checks):
            self._platform_status.setText(tr("Waiting for all safety confirmations"))
        else:
            self._platform_status.setText(tr("Profile valid | ready to start engineering session"))
        self._set_motion_controls_enabled(self.session_active and self._evidence_ready)

    def _start_session_clicked(self) -> None:
        try:
            self.start_engineering_session()
        except (FixtureLeaseError, FixtureProfileError, MotionPlatformError, OSError, RuntimeError) as exc:
            self._show_error(str(exc))

    def start_engineering_session(self) -> Path:
        if self._profile is None:
            raise FixtureProfileError("no fixture profile is selected")
        if not all(check.isChecked() for check in self._safety_checks):
            raise MotionPlatformError("all motion safety confirmations are required")
        recorder = FixtureSessionRecorder(
            self._profile,
            operator=str(self._settings.get("production.last_operator", "") or ""),
            root=self._session_root,
        )
        handle = self._lease.acquire(f"fixture-debug:{recorder.session_id}")
        try:
            session_dir = recorder.start()
            control = FixtureControlWorker(
                self._profile,
                sender=self._control_sender,
                recorder=recorder,
                parent=self,
            )
            control.confirm_preflight()
            control.send_result.connect(self._on_platform_send)
            control.request_finished.connect(self._on_control_finished)
            control.request_failed.connect(self._on_control_failed)
            control.evidence_failed.connect(self._fail_evidence)
            control.busy_changed.connect(self._on_control_busy)
            control.start()
        except Exception:
            try:
                recorder.abort(reason="session startup failed")
            except Exception:
                pass
            self._lease.release(handle)
            raise
        self._recorder = recorder
        self._control = control
        self._lease_handle = handle
        self._evidence_ready = True
        self._session_accepting_frames = True
        self._session_origin_ns = time.monotonic_ns()
        self._targets.clear()
        self._measurements.clear()
        self._display_measurements.clear()
        self._errors.clear()
        self._run_summaries.clear()
        center = self._profile.center_pose
        self._targets.append(
            TimedAttitude(
                self._session_origin_ns,
                center.roll_deg,
                center.pitch_deg,
                center.yaw_deg,
            )
        )
        self._append_event(
            tr("Engineering session started: {path}", path=str(session_dir)),
            event_type="session_started",
            details={"ms6222_connected": self._ms_connected},
        )
        self._session_end.setEnabled(True)
        self._profile_combo.setEnabled(False)
        self._profile_new.setEnabled(False)
        self._profile_edit.setEnabled(False)
        self._profile_reload.setEnabled(False)
        self.active_changed.emit(True)
        self._update_start_gate()
        return session_dir

    def _set_motion_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self._move,
            self._center,
            self._stop,
            self._reset,
            self._trajectory_start,
            self._calibration_start,
        ):
            widget.setEnabled(enabled)

    def _send_axis_move(self) -> None:
        control = self._require_control()
        try:
            control.submit_axis_move(
                str(self._axis.currentData()),
                self._target.value(),
                total_duration_ms=round(self._duration.value() * 1000),
            )
        except MotionPlatformError as exc:
            self._show_error(str(exc))

    def _send_center(self) -> None:
        try:
            self._require_control().submit_center(
                duration_ms=round(self._duration.value() * 1000)
            )
        except MotionPlatformError as exc:
            self._show_error(str(exc))

    def _stop_sequence(self) -> None:
        control = self._control
        if control is not None:
            control.stop_sequence()
            self._append_event(
                tr("Stop requested; no further sequence points will be sent. The current A6T command is not cancelled.")
            )

    def _send_reset(self) -> None:
        answer = QMessageBox.warning(
            self,
            tr("Reset platform"),
            tr("Reset sends Z=0. Confirm the mechanism is safe before continuing."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._require_control().submit_reset(
                duration_ms=round(self._duration.value() * 1000),
                explicit_confirmation=True,
            )
        except MotionPlatformError as exc:
            self._show_error(str(exc))

    def _trajectory_profile(self) -> CombinedSineProfile:
        axes = {}
        for axis, (enabled, amplitude, frequency, phase) in self._trajectory_axes.items():
            axes[axis] = SineAxis(
                amplitude.value(),
                frequency.value(),
                phase.value(),
                enabled.isChecked(),
            )
        return CombinedSineProfile(
            profile_id=f"fixture-ui-{int(time.time())}",
            sample_period_ms=self._sample_period.value(),
            ramp_in_s=self._ramp_in.value(),
            steady_duration_s=self._steady.value(),
            ramp_out_s=self._ramp_out.value(),
            roll=axes["roll"],
            pitch=axes["pitch"],
            yaw=axes["yaw"],
            work_z_mm=100.0 if self._profile is None else self._profile.center_pose.z_mm,
        )

    def _preview_trajectory(self) -> Optional[CombinedSineProfile]:
        if self._profile is None:
            self._show_error(tr("Select a valid fixture profile first."))
            return None
        profile = self._trajectory_profile()
        try:
            validate_sine_profile(self._profile.to_motion_config(), profile)
        except MotionPlatformError as exc:
            self._show_error(str(exc))
            return None
        count = min(1200, profile.sample_count)
        times = np.linspace(0.0, profile.total_duration_s, count)
        poses = [profile.pose_at(float(value)) for value in times]
        for axis, curve in self._preview_curves.items():
            curve.setData(times, [getattr(pose, f"{axis}_deg") for pose in poses])
        self._append_event(
            tr(
                "Trajectory validated: {points} command points, {duration:.1f} s.",
                points=profile.sample_count,
                duration=profile.total_duration_s,
            )
        )
        return profile

    def _start_trajectory(self) -> None:
        profile = self._preview_trajectory()
        if profile is None:
            return
        try:
            self._require_control().submit_trajectory(profile)
        except MotionPlatformError as exc:
            self._show_error(str(exc))

    def _on_platform_send(self, result, action: str, sequence: int) -> None:
        if result.sent:
            pose = result.logical_pose
            completion_ns = result.monotonic_ns + int(result.duration_ms or 0) * 1_000_000
            self._targets.append(
                TimedAttitude(completion_ns, pose.roll_deg, pose.pitch_deg, pose.yaw_deg)
            )
            self._platform_status.setText(
                tr(
                    "Command sent | {action} | sequence {sequence}",
                    action=self._control_action_text(action),
                    sequence=sequence,
                )
            )
        else:
            self._platform_status.setText(tr("Command send failed: {details}", details=result.error))

    def _on_control_finished(self, _request_id: str, result) -> None:
        payload = {"type": type(result).__name__}
        if hasattr(result, "__dataclass_fields__"):
            payload.update(asdict(result))
        self._run_summaries.append(payload)
        self._append_event(tr("Control request completed; this confirms command dispatch only."))
        if self._guided_calibration is not None:
            self._calibration_capture.setEnabled(True)

    def _on_control_failed(self, _request_id: str, details: str) -> None:
        self._append_event(tr("Control request failed: {details}", details=details))
        self.status_message.emit(details, 8000)

    def _on_control_busy(self, busy: bool) -> None:
        ready = self.session_active and self._evidence_ready
        self._move.setEnabled(ready and not busy)
        self._center.setEnabled(ready and not busy)
        self._reset.setEnabled(ready and not busy)
        self._trajectory_start.setEnabled(ready and not busy)
        self._stop.setEnabled(ready)

    def _start_calibration(self) -> None:
        if self._profile is None or not self.session_active:
            return
        self._guided_calibration = GuidedFixtureCalibration(
            self._profile.profile_id,
            self._profile.sha256,
        )
        self._calibration_send.setEnabled(True)
        self._calibration_capture.setEnabled(False)
        self._calibration_evaluate.setEnabled(False)
        self._update_calibration_status()

    def _send_calibration_pose(self) -> None:
        guided = self._guided_calibration
        if guided is None or guided.next_stage is None or self._profile is None:
            return
        stage = guided.next_stage
        values = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
        if stage.value.endswith("_plus"):
            values[stage.value.partition("_")[0]] = guided.command_angle_deg
        elif stage.value.endswith("_minus"):
            values[stage.value.partition("_")[0]] = -guided.command_angle_deg
        target = PlatformPose(
            values["roll"],
            values["pitch"],
            values["yaw"],
            0,
            0,
            self._profile.center_pose.z_mm,
        )
        try:
            self._require_control().submit_pose(target, total_duration_ms=3000)
        except MotionPlatformError as exc:
            self._show_error(str(exc))
            return
        self._calibration_send.setEnabled(False)
        self._calibration_capture.setEnabled(False)

    def _capture_calibration_stage(self) -> None:
        guided = self._guided_calibration
        if guided is None or guided.next_stage is None:
            return
        now_ns = time.monotonic_ns()
        samples = [sample for sample in self._measurements if sample.monotonic_ns >= now_ns - 2_000_000_000]
        try:
            guided.record_stage(guided.next_stage, samples)
        except FixtureAnalysisError as exc:
            self._show_error(str(exc))
            return
        self._calibration_capture.setEnabled(False)
        if guided.next_stage is None:
            self._calibration_send.setEnabled(False)
            self._calibration_evaluate.setEnabled(True)
        else:
            self._calibration_send.setEnabled(True)
        self._update_calibration_status()

    def _update_calibration_status(self) -> None:
        guided = self._guided_calibration
        if guided is None:
            if self._calibration is not None:
                self._calibration_status.setText(
                    tr(
                        "Calibration saved: {calibration_id}",
                        calibration_id=self._calibration.calibration_id,
                    )
                )
            else:
                self._calibration_status.setText(tr("Not started"))
            return
        if guided.next_stage is None:
            self._calibration_status.setText(tr("All guide stages captured; evaluate the result."))
        else:
            self._calibration_status.setText(
                tr(
                    "Next stage: {stage} ({completed}/{total})",
                    stage=self._calibration_stage_text(guided.next_stage),
                    completed=len(guided.completed_stages),
                    total=len(CALIBRATION_SEQUENCE),
                )
            )

    @staticmethod
    def _calibration_stage_text(stage: CalibrationStage) -> str:
        labels = {
            CalibrationStage.CENTER_START: tr("Center static sample (start)"),
            CalibrationStage.ROLL_PLUS: tr("Roll +3 deg"),
            CalibrationStage.ROLL_MINUS: tr("Roll -3 deg"),
            CalibrationStage.PITCH_PLUS: tr("Pitch +3 deg"),
            CalibrationStage.PITCH_MINUS: tr("Pitch -3 deg"),
            CalibrationStage.YAW_PLUS: tr("Yaw +3 deg"),
            CalibrationStage.YAW_MINUS: tr("Yaw -3 deg"),
            CalibrationStage.CENTER_END: tr("Center static sample (end)"),
        }
        return labels[stage]

    @staticmethod
    def _control_action_text(action: str) -> str:
        return {
            "absolute_axis": tr("Absolute movement"),
            "absolute_pose": tr("Absolute pose"),
            "trajectory": tr("Trajectory"),
            "center": tr("Center"),
            "reset": tr("Reset"),
        }.get(action, action)

    def _evaluate_calibration(self) -> None:
        guided = self._guided_calibration
        if guided is None:
            return
        try:
            preview = guided.evaluate(confirmed=False)
        except FixtureAnalysisError as exc:
            self._show_error(str(exc))
            return
        mapping = ", ".join(
            f"{logical.upper()}<-{sensor.upper()} x{sign:+d}"
            for logical, sensor, sign in zip(
                ("roll", "pitch", "yaw"),
                preview.sensor_axis_for_logical,
                preview.logical_signs,
            )
        )
        answer = QMessageBox.question(
            self,
            tr("Confirm coordinate calibration"),
            tr(
                "Detected mapping: {mapping}\nMaximum cross coupling: {coupling:.3f}\n"
                "This remains ENGINEERING_ONLY. Confirm and save?",
                mapping=mapping,
                coupling=max(preview.cross_coupling_ratio),
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        calibration = guided.evaluate(confirmed=True)
        try:
            if self._profile is None:
                raise FixtureProfileError("fixture profile disappeared")
            revised = replace(
                self._profile,
                revision=self._profile.revision + 1,
                calibration_id=calibration.calibration_id,
            )
            calibration = replace(calibration, profile_sha256=revised.sha256)
            self._calibration_store.save(calibration)
            self._profile_store.save(revised, expected_revision=self._profile.revision)
        except (FixtureAnalysisError, FixtureProfileError, OSError) as exc:
            self._show_error(str(exc))
            return
        self._calibration = calibration
        self._profile = revised
        self._guided_calibration = None
        self._calibration_evaluate.setEnabled(False)
        self._calibration_status.setText(
            tr("Calibration saved: {calibration_id}", calibration_id=calibration.calibration_id)
        )
        self._update_attitude_status()

    def _update_attitude_status(self) -> None:
        if self._calibration and self._calibration.coordinate_valid:
            self._attitude_status.setText(
                tr(
                    "Coordinate calibration {calibration_id} applied. Timing uses host "
                    "serial arrival; phase and delay are engineering estimates.",
                    calibration_id=self._calibration.calibration_id,
                )
            )
        else:
            self._attitude_status.setText(
                tr("Raw attitude only; no confirmed coordinate calibration.")
            )

    def _update_profile_status_text(self) -> None:
        if self._profile is None:
            self._profile_status.setText(tr("No fixture profile"))
            return
        self._profile_status.setText(
            tr(
                "Profile valid | r{revision} | SHA {sha}",
                revision=self._profile.revision,
                sha=self._profile.sha256[:12],
            )
        )

    def _refresh_plot(self) -> None:
        cutoff_ns = time.monotonic_ns() - 300_000_000_000
        targets = tuple(sample for sample in self._targets if sample.monotonic_ns >= cutoff_ns)
        measurements = tuple(
            sample for sample in self._display_measurements if sample.monotonic_ns >= cutoff_ns
        )
        errors = tuple(sample for sample in self._errors if sample.monotonic_ns >= cutoff_ns)
        self._attitude_plot.set_data(
            targets,
            measurements,
            errors,
            origin_ns=self._session_origin_ns,
        )

    def _finish_session_clicked(self) -> None:
        conclusion = FixtureSessionConclusion(str(self._conclusion.currentData()))
        self._begin_session_finish(conclusion=conclusion, notes=self._notes.text().strip())

    def _begin_session_finish(
        self,
        *,
        conclusion: FixtureSessionConclusion,
        notes: str,
        abort_reason: str = "",
    ) -> None:
        control = self._control
        recorder = self._recorder
        if control is None or recorder is None or self._finalizer is not None:
            return
        self._session_end.setEnabled(False)
        self._set_motion_controls_enabled(False)

        def finalize_after_control() -> None:
            if self._finalizer is not None or self._recorder is not recorder:
                return
            if self._targets and not abort_reason:
                remaining_ns = self._targets[-1].monotonic_ns - time.monotonic_ns()
                if remaining_ns > 0:
                    QTimer.singleShot(
                        max(1, math.ceil(remaining_ns / 1_000_000)),
                        finalize_after_control,
                    )
                    return
            self._session_accepting_frames = False
            self._extend_target_hold(time.monotonic_ns())
            finalizer = _SessionFinalizeThread(
                recorder,
                conclusion,
                notes,
                tuple(self._targets),
                tuple(self._measurements),
                self._calibration,
                max(
                    0.5,
                    self._sample_period.value() / 1000.0 * 3.0,
                ),
                tuple(self._run_summaries),
                asdict(self._latest_ms_stats) if self._latest_ms_stats else None,
                abort_reason,
                parent=self,
            )
            finalizer.completed.connect(self._on_session_finalized)
            finalizer.failed.connect(self._on_session_finalize_failed)
            finalizer.finished.connect(finalizer.deleteLater)
            self._finalizer = finalizer
            finalizer.start()

        if control.isRunning():
            control.finished.connect(
                finalize_after_control,
                Qt.ConnectionType.SingleShotConnection,
            )
            control.shutdown()
        else:
            finalize_after_control()
        if self._targets and not abort_reason:
            remaining_s = max(
                0.0,
                (self._targets[-1].monotonic_ns - time.monotonic_ns()) / 1e9,
            )
            if remaining_s > 0:
                self._append_event(
                    tr(
                        "Waiting {seconds:.1f} s for the current A6T completion interval before finalizing.",
                        seconds=remaining_s,
                    )
                )
        if abort_reason:
            self._append_event_display(tr("Finalizing incomplete fixture session..."))
        else:
            self._append_event(tr("Finishing session and calculating engineering metrics..."))

    def _on_session_finalized(self, result) -> None:
        if result.status == "complete":
            self._append_event_display(tr("Session saved: {path}", path=str(result.session_dir)))
            self.status_message.emit(tr("Fixture engineering session saved."), 5000)
        else:
            self._append_event_display(
                tr("Session incomplete: {path}", path=str(result.session_dir))
            )
            self.status_message.emit(
                tr("Fixture evidence failed; session saved as incomplete."),
                10000,
            )
        self._release_session_state()

    def _on_session_finalize_failed(self, details: str) -> None:
        self._append_event_display(
            tr("Session finalization failed: {details}", details=details)
        )
        self.status_message.emit(details, 10000)
        self._release_session_state()

    def _release_session_state(self) -> None:
        handle = self._lease_handle
        self._lease_handle = None
        if handle is not None:
            try:
                self._lease.release(handle)
            except FixtureLeaseError:
                pass
        self._control = None
        self._recorder = None
        self._finalizer = None
        self._session_accepting_frames = False
        self._evidence_ready = True
        for check in self._safety_checks:
            check.setChecked(False)
        self._profile_combo.setEnabled(True)
        self._profile_new.setEnabled(True)
        self._profile_reload.setEnabled(True)
        self._session_end.setEnabled(False)
        self.active_changed.emit(False)
        self._reload_profiles()
        self._update_start_gate()

    def _engineering_summary(self) -> dict:
        return {
            "timing_mode": "HOST_ARRIVAL_ONLY_NO_PPS",
            "metrics_incomplete": True,
            "coordinate_calibration_id": (
                self._calibration.calibration_id if self._calibration else ""
            ),
            "control_runs": list(self._run_summaries),
            "ms6222_statistics": (
                asdict(self._latest_ms_stats) if self._latest_ms_stats else None
            ),
        }

    def _extend_target_hold(self, monotonic_ns: int) -> None:
        if not self._targets:
            return
        last = self._targets[-1]
        timestamp = int(monotonic_ns)
        if timestamp <= last.monotonic_ns:
            return
        self._targets.append(
            TimedAttitude(
                timestamp,
                last.roll_deg,
                last.pitch_deg,
                last.yaw_deg,
            )
        )

    def _require_control(self) -> FixtureControlWorker:
        if self._control is None or not self.session_active or not self._evidence_ready:
            raise MotionPlatformError("no active fixture engineering session")
        return self._control

    def _fail_evidence(self, details: str) -> None:
        if not self._evidence_ready:
            return
        self._evidence_ready = False
        self._session_accepting_frames = False
        message = tr("Evidence recording failed: {details}", details=details)
        self._append_event_display(message)
        self._platform_status.setText(message)
        self.status_message.emit(message, 10000)
        self._set_motion_controls_enabled(False)
        control = self._control
        if control is not None:
            control.stop_sequence()
        if self.session_active and self._finalizer is None:
            self._begin_session_finish(
                conclusion=FixtureSessionConclusion.INCONCLUSIVE,
                notes=message,
                abort_reason=message,
            )

    def _append_event(
        self,
        message: str,
        *,
        event_type: str = "workspace_event",
        details: Optional[dict] = None,
    ) -> None:
        self._append_event_display(message)
        recorder = self._recorder
        if recorder is not None and self._evidence_ready:
            try:
                recorder.record_event(event_type, message, details=details)
            except Exception as exc:
                self._fail_evidence(str(exc))

    def _append_event_display(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._event_log.appendPlainText(f"[{stamp}] {message}")

    def _show_error(self, message: str) -> None:
        self._append_event(message)
        self.status_message.emit(message, 8000)

    def clear_safety_confirmation(self) -> None:
        if self.session_active:
            return
        for check in self._safety_checks:
            check.setChecked(False)
        self._update_start_gate()

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = theme
        palette = S.palette(theme)
        self.setStyleSheet(
            f"#fixtureProfileStatus, #fixtureMsStatus, #fixturePlatformStatus {{ color: {palette['text_2']}; }}"
            f"#fixtureProfileError {{ color: {palette['err']}; }}"
        )
        self._attitude_plot.set_theme(theme)
        self._preview.setBackground(palette["panel"])
        for axis_name in ("left", "bottom"):
            axis = self._preview.getAxis(axis_name)
            axis.setPen(pg.mkPen(palette["border_2"]))
            axis.setTextPen(pg.mkPen(palette["text_2"]))

    def retranslate_ui(self) -> None:
        self._profile_group.setTitle(tr("Fixture profile and reference"))
        self._safety_group.setTitle(tr("Motion safety gate"))
        self._manual_group.setTitle(tr("Absolute movement"))
        self._trajectory_group.setTitle(tr("Three-axis sine trajectory"))
        self._calibration_group.setTitle(tr("Guided coordinate calibration"))
        self._live_group.setTitle(tr("Target / measured / error attitude"))
        self._event_group.setTitle(tr("Fixture events"))
        self._profile_label.setText(tr("Profile"))
        self._profile_new.setText(tr("New..."))
        self._profile_edit.setText(tr("Edit..."))
        self._profile_reload.setText(tr("Reload"))
        self._serial_label.setText(tr("MS-6222 port"))
        self._serial_refresh.setText(tr("Refresh"))
        self._serial_connect.setText(
            tr("Disconnect reference") if self._ms_connected else tr("Connect reference")
        )
        self._session_start.setText(tr("Start engineering session"))
        self._session_end.setText(tr("Finish session"))
        for check, source in zip(self._safety_checks, self._safety_sources):
            check.setText(tr(source))
        self._manual_labels["axis"].setText(tr("Axis"))
        self._manual_labels["target"].setText(tr("Target angle"))
        self._manual_labels["duration"].setText(tr("Total completion time"))
        for label, source in zip(
            self._trajectory_headers,
            ("Axis", "Amplitude", "Frequency", "Phase"),
        ):
            label.setText(tr(source))
        self._trajectory_labels["ramp_in"].setText(tr("Ramp in"))
        self._trajectory_labels["steady"].setText(tr("Steady"))
        self._trajectory_labels["ramp_out"].setText(tr("Ramp out"))
        self._trajectory_labels["sample"].setText(tr("Sample period"))
        self._move.setText(tr("Send absolute target"))
        self._center.setText(tr("Center"))
        self._stop.setText(tr("Stop sequence"))
        self._reset.setText(tr("Reset Z=0..."))
        self._preview_button.setText(tr("Preview and validate"))
        self._trajectory_start.setText(tr("Start trajectory"))
        self._calibration_start.setText(tr("Start guide"))
        self._calibration_send.setText(tr("Send guide pose"))
        self._calibration_capture.setText(tr("Capture last 2 s"))
        self._calibration_evaluate.setText(tr("Evaluate and confirm..."))
        self._notes.setPlaceholderText(tr("Operator notes"))
        self._conclusion_label.setText(tr("Engineering conclusion"))
        current_conclusion = str(self._conclusion.currentData())
        conclusion_labels = {
            FixtureSessionConclusion.INCONCLUSIVE.value: tr("Inconclusive"),
            FixtureSessionConclusion.PASS.value: tr("Pass"),
            FixtureSessionConclusion.FAIL.value: tr("Fail"),
        }
        for index in range(self._conclusion.count()):
            value = str(self._conclusion.itemData(index))
            self._conclusion.setItemText(index, conclusion_labels[value])
        self._conclusion.setCurrentIndex(
            max(0, self._conclusion.findData(current_conclusion))
        )
        self._attitude_plot.retranslate_ui()
        self._update_profile_status_text()
        self._update_calibration_status()
        if self._ms_connected and self._latest_ms_stats is not None:
            self._on_ms_statistics(self._latest_ms_stats)
        elif self._ms_last_error:
            self._ms_status.setText(
                tr("Reference error: {details}", details=self._ms_last_error)
            )
        else:
            self._ms_status.setText(tr("Reference disconnected"))
        self._update_attitude_status()
        self._update_start_gate()

    def confirm_shutdown(self) -> bool:
        if not self.session_active and self._finalizer is None:
            return True
        answer = QMessageBox.warning(
            self,
            tr("Close fixture diagnostics"),
            tr(
                "A fixture engineering session is active. Closing will stop future commands, "
                "leave the current A6T command to complete on the platform, and save the "
                "session as incomplete. Close anyway?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def shutdown(self, *, wait: bool = True) -> None:
        self._plot_timer.stop()
        self._disconnect_ms()
        finalizer = self._finalizer
        if finalizer is not None and wait:
            # The user has confirmed closing an active evidence session. Do not
            # destroy its QThread while hashes or large comparison files are
            # still being finalized.
            finalizer.wait()
        control = self._control
        recorder = self._recorder
        self._session_accepting_frames = False
        if control is not None:
            control.shutdown()
            if wait:
                control.wait(10000)
        if recorder is not None and finalizer is None:
            try:
                recorder.abort(reason="workspace shutdown", summary=self._engineering_summary())
            except Exception:
                pass
        handle = self._lease_handle
        self._lease_handle = None
        if handle is not None:
            try:
                self._lease.release(handle)
            except FixtureLeaseError:
                pass
        self._control = None
        self._recorder = None


__all__ = ["FixtureDebugWorkspace", "FixtureProfileDialog"]
