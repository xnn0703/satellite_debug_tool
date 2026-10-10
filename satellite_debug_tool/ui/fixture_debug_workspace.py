"""Engineering-only motion-platform and MS-6222 fixture diagnostic workspace."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, replace
from itertools import islice
import math
from pathlib import Path
import time
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
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
from satellite_debug_tool.core.motion_platform import (
    DirectMotionPlatformController,
    MotionActionResult,
    MotionPlatformSnapshot,
    MotionPlatformState,
    TrajectoryResult,
)
from satellite_debug_tool.core.production.fixture_control import ControllerFactory
from satellite_debug_tool.core.production import (
    CALIBRATION_REFERENCE_STABILITY_LIMIT_DEG,
    BoundedAttitudeOuterLoop,
    CALIBRATION_SEQUENCE,
    CalibrationStage,
    CombinedSineProfile,
    FixtureAnalysisError,
    FixtureAxisLimits,
    FixtureCalibration,
    FixtureCalibrationStore,
    FixtureControlLease,
    FixtureControlWorker,
    FixtureAttitudeSample,
    FixtureAttitudeSource,
    FixtureTelemetryBus,
    FixtureLeaseError,
    FixtureModelPreset,
    FixtureProfileError,
    FixtureProfileStore,
    FixtureQualificationConfig,
    FixtureQualificationController,
    FixtureQualificationResult,
    FixtureSessionConclusion,
    FixtureSessionCoordinator,
    FixtureSessionRecorder,
    GuidedFixtureCalibration,
    MotionPlatformError,
    OuterLoopStatus,
    Ms6222FrameEnvelope,
    Ms6222FrameType,
    Ms6222InsRecord,
    Ms6222ControlLease,
    Ms6222DebugError,
    Ms6222SerialWorker,
    Ms6222WorkerStatistics,
    PlatformPose,
    SineAxis,
    TimedAttitude,
    WorkstationFixtureProfile,
    compare_attitude_streams,
    create_fixture_session_zip,
    compute_dynamic_metrics,
    compute_static_metrics,
    fixture_model_presets,
    validate_sine_profile,
)
from satellite_debug_tool.i18n import register_translatable, tr, tr_source
from satellite_debug_tool.ui import styles as S


def _settlement_result(result: object) -> Optional[MotionActionResult]:
    """Return the authoritative settlement fact carried by a control result."""
    if isinstance(result, MotionActionResult):
        return result
    if isinstance(result, TrajectoryResult):
        return result.arrival
    return None


class FixtureProfileDialog(QDialog):
    def __init__(
        self,
        profile: WorkstationFixtureProfile,
        parent: Optional[QWidget] = None,
        model_presets: Optional[tuple[FixtureModelPreset, ...]] = None,
    ) -> None:
        super().__init__(parent)
        self._source = profile
        self._result_profile: Optional[WorkstationFixtureProfile] = None
        presets = model_presets or fixture_model_presets()
        self._model_presets = {preset.model_id: preset for preset in presets}
        for preset in presets:
            preset.validate()
        if not presets:
            raise FixtureProfileError("at least one fixture model preset is required")
        initial = profile
        self.setWindowTitle(tr("Installation configuration"))
        self.setMinimumWidth(860)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self._profile_form = form
        self._model_combo = QComboBox()
        self._model_combo.setObjectName("fixtureModelCombo")
        for preset in presets:
            self._model_combo.addItem(preset.display_name, preset.model_id)
        if initial.model_id not in self._model_presets:
            self._model_combo.addItem(tr("Custom or migrated profile"), "custom")
        model_index = self._model_combo.findData(initial.model_id)
        self._model_combo.setCurrentIndex(max(0, model_index))
        self._model_combo.setEnabled(False)
        self._profile_id = QLineEdit(initial.profile_id)
        self._profile_id.setObjectName("fixtureProfileIdEdit")
        self._profile_id.setReadOnly(True)
        self._local_ipv4 = QLineEdit(initial.local_ipv4)
        self._feedback_port = QSpinBox()
        self._feedback_port.setRange(1, 65535)
        self._feedback_port.setValue(initial.feedback_port)
        self._controller_ipv4 = QLineEdit(initial.controller_ipv4)
        self._command_port = QSpinBox()
        self._command_port.setRange(1, 65535)
        self._command_port.setValue(initial.command_port)
        self._calibration_id = QLineEdit(initial.calibration_id)
        self._calibration_id.setReadOnly(True)
        self._minimum_duration = QSpinBox()
        self._minimum_duration.setRange(1, 60000)
        self._minimum_duration.setValue(initial.minimum_duration_ms)
        form.addRow(tr("Motion platform model"), self._model_combo)
        form.addRow(tr("Profile ID"), self._profile_id)
        form.addRow(tr("Local feedback IPv4"), self._local_ipv4)
        form.addRow(tr("Feedback port"), self._feedback_port)
        form.addRow(tr("Controller IPv4"), self._controller_ipv4)
        form.addRow(tr("Command port"), self._command_port)
        form.addRow(tr("Calibration ID"), self._calibration_id)
        form.addRow(tr("Minimum command time (ms)"), self._minimum_duration)
        self._model_note = QLabel(
            tr(
                "Model parameters and FRD-to-controller signs are generated by software. "
                "The pending calibration ID is replaced after guided MS-6222 calibration."
            )
        )
        self._model_note.setWordWrap(True)
        form.addRow("", self._model_note)
        root.addLayout(form)

        limits_group = QGroupBox(tr("Axis operating envelope"))
        self._limits_group = limits_group
        limits = QGridLayout(limits_group)
        headers = (
            tr("Axis"),
            tr("Angle (deg)"),
            tr("Frequency (Hz)"),
            tr("Velocity (deg/s)"),
            tr("Acceleration (deg/s²)"),
            tr("Sign"),
        )
        self._limit_headers: list[QLabel] = []
        for column, header in enumerate(headers):
            label = QLabel(header)
            self._limit_headers.append(label)
            limits.addWidget(label, 0, column)
        self._limit_edits: dict[str, tuple[QLineEdit, ...]] = {}
        self._sign_boxes: dict[str, QComboBox] = {}
        for row, axis in enumerate(("roll", "pitch", "yaw"), start=1):
            limits.addWidget(QLabel(axis.upper()), row, 0)
            source_limits = initial.axis_limits[axis]
            values = (
                source_limits.abs_angle_deg,
                source_limits.max_frequency_hz,
                source_limits.max_velocity_deg_s,
                source_limits.max_acceleration_deg_s2,
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
            current_sign = getattr(initial, f"{axis}_sign")
            sign_box.setCurrentIndex(0 if current_sign == 1 else 1)
            limits.addWidget(sign_box, row, 5)
            self._limit_edits[axis] = tuple(edits)
            self._sign_boxes[axis] = sign_box
        root.addWidget(limits_group)

        geometry = QGroupBox(tr("Confirmed A6 geometry"))
        self._geometry_group = geometry
        geometry_form = QFormLayout(geometry)
        self._geometry_form = geometry_form
        self._center_z = QDoubleSpinBox()
        self._center_z.setRange(0, 1000)
        self._center_z.setDecimals(2)
        self._center_z.setValue(initial.center_pose.z_mm)
        self._z_min = QDoubleSpinBox()
        self._z_min.setRange(-1000, 1000)
        self._z_min.setValue(initial.z_min_mm)
        self._z_max = QDoubleSpinBox()
        self._z_max.setRange(-1000, 1000)
        self._z_max.setValue(initial.z_max_mm)
        geometry_form.addRow(tr("Center Z (mm)"), self._center_z)
        geometry_form.addRow(tr("Minimum Z (mm)"), self._z_min)
        geometry_form.addRow(tr("Maximum Z (mm)"), self._z_max)
        root.addWidget(geometry)

        self._error = QLabel("")
        self._error.setObjectName("fixtureProfileError")
        self._error.setWordWrap(True)
        root.addWidget(self._error)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self._validate_and_accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)
        self._set_model_owned_fields_read_only(initial.model_id in self._model_presets)
        register_translatable(self)

    def retranslate_ui(self) -> None:
        self.setWindowTitle(tr("Installation configuration"))
        for field, source in (
            (self._model_combo, "Motion platform model"),
            (self._profile_id, "Profile ID"),
            (self._local_ipv4, "Local feedback IPv4"),
            (self._feedback_port, "Feedback port"),
            (self._controller_ipv4, "Controller IPv4"),
            (self._command_port, "Command port"),
            (self._calibration_id, "Calibration ID"),
            (self._minimum_duration, "Minimum command time (ms)"),
        ):
            label = self._profile_form.labelForField(field)
            if label is not None:
                label.setText(tr(source))
        self._model_note.setText(
            tr(
                "Model parameters and FRD-to-controller signs are generated by software. "
                "The pending calibration ID is replaced after guided MS-6222 calibration."
            )
        )
        self._limits_group.setTitle(tr("Axis operating envelope"))
        for label, source in zip(
            self._limit_headers,
            (
                "Axis",
                "Angle (deg)",
                "Frequency (Hz)",
                "Velocity (deg/s)",
                "Acceleration (deg/s²)",
                "FRD to controller",
            ),
        ):
            label.setText(tr(source))
        for edits in self._limit_edits.values():
            for edit in edits:
                edit.setPlaceholderText(tr("Required"))
        self._geometry_group.setTitle(tr("Confirmed A6 geometry"))
        for field, source in (
            (self._center_z, "Center Z (mm)"),
            (self._z_min, "Minimum Z (mm)"),
            (self._z_max, "Maximum Z (mm)"),
        ):
            label = self._geometry_form.labelForField(field)
            if label is not None:
                label.setText(tr(source))
        self._buttons.button(QDialogButtonBox.StandardButton.Save).setText(
            tr("Save")
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            tr("Cancel")
        )

    @property
    def result_profile(self) -> Optional[WorkstationFixtureProfile]:
        return self._result_profile

    def _set_model_owned_fields_read_only(self, read_only: bool) -> None:
        for field in (
            self._local_ipv4,
            self._feedback_port,
            self._controller_ipv4,
            self._command_port,
            self._minimum_duration,
            self._center_z,
            self._z_min,
            self._z_max,
        ):
            field.setReadOnly(read_only)
        for edits in self._limit_edits.values():
            for edit in edits:
                edit.setReadOnly(read_only)
        for sign_box in self._sign_boxes.values():
            sign_box.setEnabled(not read_only)

    def _validate_and_accept(self) -> None:
        try:
            axis_limits = {}
            for axis, edits in self._limit_edits.items():
                values = [float(edit.text().strip()) for edit in edits]
                axis_limits[axis] = FixtureAxisLimits(*values)
            revision = self._source.revision + 1
            profile = WorkstationFixtureProfile(
                profile_id=self._profile_id.text().strip(),
                revision=revision,
                model_id=str(self._model_combo.currentData() or "custom"),
                local_ipv4=self._local_ipv4.text().strip(),
                feedback_port=self._feedback_port.value(),
                controller_ipv4=self._controller_ipv4.text().strip(),
                command_port=self._command_port.value(),
                center_pose=PlatformPose(0, 0, 0, 0, 0, self._center_z.value()),
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
        self._widget = pg.PlotWidget()
        self._plot = self._widget.getPlotItem()
        self._plots = {"combined": self._plot}
        self._curves = {}
        self._coordinate_valid = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._widget)
        axis_colors = {
            "roll": "#5f7cff",
            "pitch": "#00b3a4",
            "yaw": "#ffb020",
        }
        series_styles = {
            "target": (Qt.PenStyle.SolidLine, 1.7),
            "measured": (Qt.PenStyle.DashLine, 1.5),
            "error": (Qt.PenStyle.DotLine, 1.3),
        }
        self._curve_sources = {
            "target": tr_source("Target"),
            "measured": tr_source("Measured"),
            "error": tr_source("Error"),
        }
        self._legend_entries: list[tuple[object, object, str, str]] = []
        self._plot.showGrid(x=True, y=True, alpha=0.15)
        self._plot.setLabel("left", tr("Attitude and error"), units="deg")
        self._plot.setLabel("bottom", tr("Session time"), units="s")
        self._plot.getAxis("left").enableAutoSIPrefix(False)
        self._plot.setDownsampling(mode="peak", auto=True)
        self._plot.setClipToView(True)
        legend = self._plot.addLegend(offset=(8, 8), colCount=3)
        for axis in ("roll", "pitch", "yaw"):
            self._curves[axis] = {
                name: self._plot.plot(
                    [],
                    [],
                    pen=pg.mkPen(
                        axis_colors[axis],
                        width=series_styles[name][1],
                        style=series_styles[name][0],
                    ),
                    name=f"{axis.upper()} {tr(self._curve_sources[name])}",
                )
                for name in ("target", "measured", "error")
            }
        if legend is not None:
            self._legend_entries = [
                (sample, label, axis, name)
                for (sample, label), (axis, name) in zip(
                    legend.items,
                    (
                        (axis, name)
                        for axis in ("roll", "pitch", "yaw")
                        for name in ("target", "measured", "error")
                    ),
                )
            ]
        self.setMinimumHeight(300)

    def set_coordinate_valid(self, valid: bool) -> None:
        self._coordinate_valid = bool(valid)
        for axis in ("roll", "pitch", "yaw"):
            self._curves[axis]["target"].setVisible(self._coordinate_valid)
            self._curves[axis]["error"].setVisible(self._coordinate_valid)

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
                visible_samples = (
                    samples
                    if name == "measured" or self._coordinate_valid
                    else ()
                )
                x, y = _attitude_plot_series(
                    visible_samples,
                    axis=axis,
                    origin_ns=origin_ns,
                    break_time_gaps=name != "target",
                )
                self._curves[axis][name].setData(x, y, connect="finite")
        timeline = measurements or (targets if self._coordinate_valid else ())
        if timeline:
            latest_ns = max(
                [sample.monotonic_ns for sample in timeline[-1:]]
                + (
                    [sample.monotonic_ns for sample in targets[-1:]]
                    if self._coordinate_valid
                    else []
                )
            )
            x_max = max(30.0, (latest_ns - origin_ns) / 1e9)
            self._plot.setXRange(max(0.0, x_max - 300.0), x_max, padding=0.0)

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
        self._plot.setLabel("left", tr("Attitude and error"), units="deg")
        self._plot.setLabel("bottom", tr("Session time"), units="s")
        for _sample, label, axis, name in self._legend_entries:
            label.setText(f"{axis.upper()} {tr(self._curve_sources[name])}")


def _attitude_plot_series(
    samples: tuple[TimedAttitude, ...],
    *,
    axis: str,
    origin_ns: int,
    break_time_gaps: bool,
) -> tuple[np.ndarray, np.ndarray]:
    if not samples:
        return np.asarray([], dtype=float), np.asarray([], dtype=float)
    x_values: list[float] = []
    y_values: list[float] = []
    previous_ns: Optional[int] = None
    valid_intervals = [
        (sample.monotonic_ns - samples[index - 1].monotonic_ns) / 1e9
        for index, sample in enumerate(samples[1:], start=1)
        if sample.monotonic_ns > samples[index - 1].monotonic_ns
        and sample.monotonic_ns - samples[index - 1].monotonic_ns <= 1_000_000_000
    ]
    typical_interval_s = float(min(valid_intervals)) if valid_intervals else 0.01
    gap_threshold_ns = int(max(0.1, typical_interval_s * 10.0) * 1e9)
    for sample in samples:
        if (
            break_time_gaps
            and previous_ns is not None
            and sample.monotonic_ns - previous_ns > gap_threshold_ns
        ):
            x_values.append((previous_ns - origin_ns) / 1e9)
            y_values.append(math.nan)
        x_values.append((sample.monotonic_ns - origin_ns) / 1e9)
        y_values.append(
            getattr(sample, f"{axis}_deg")
            if sample.valid
            else math.nan
        )
        previous_ns = sample.monotonic_ns
    return np.asarray(x_values, dtype=float), np.asarray(y_values, dtype=float)


class _SessionFinalizeThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        recorder: FixtureSessionRecorder,
        conclusion: FixtureSessionConclusion,
        notes: str,
        calibration: Optional[FixtureCalibration],
        max_target_gap_s: float,
        control_runs: tuple[dict, ...],
        ms6222_statistics: Optional[dict],
        qualification_summary: Optional[dict] = None,
        abort_reason: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._recorder = recorder
        self._conclusion = conclusion
        self._notes = notes
        self._calibration = calibration
        self._max_target_gap_s = float(max_target_gap_s)
        self._control_runs = control_runs
        self._ms6222_statistics = ms6222_statistics
        self._qualification_summary = qualification_summary
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
                        "qualification": self._qualification_summary,
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
                self._recorder.generate_comparison_sample(
                    calibration=self._calibration,
                    max_target_gap_s=self._max_target_gap_s,
                )
                if coordinate_valid
                else ()
            )
            summary = {
                "timing_mode": "HOST_ARRIVAL_ONLY_NO_PPS",
                "phase_delay_is_engineering_estimate": True,
                "coordinate_calibration_id": (
                    self._calibration.calibration_id if self._calibration else ""
                ),
                "coordinate_valid": coordinate_valid,
                "metric_sample_count": len(comparisons),
                "control_runs": list(self._control_runs),
                "static_metrics": compute_static_metrics(comparisons),
                "dynamic_metrics": {
                    axis: compute_dynamic_metrics(comparisons, primary_axis=axis)
                    for axis in ("roll", "pitch", "yaw")
                },
                "ms6222_statistics": self._ms6222_statistics,
                "qualification": self._qualification_summary,
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
                        "qualification": self._qualification_summary,
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
    runtime_state_changed = Signal(str, str, str)

    def __init__(
        self,
        settings: Settings,
        lease: FixtureControlLease,
        *,
        profile_store: Optional[FixtureProfileStore] = None,
        calibration_store: Optional[FixtureCalibrationStore] = None,
        session_root: Optional[Path] = None,
        controller_factory: Optional[ControllerFactory] = None,
        reference_lease: Optional[Ms6222ControlLease] = None,
        telemetry: Optional[FixtureTelemetryBus] = None,
        motion_only: bool = False,
        customer_mode: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._profile_store = profile_store or FixtureProfileStore()
        self._calibration_store = calibration_store or FixtureCalibrationStore()
        self._session_coordinator = FixtureSessionCoordinator(
            lease,
            session_root=session_root,
            parent=self,
        )
        self._controller_factory = controller_factory
        self._reference_lease = reference_lease
        self._telemetry = telemetry
        self._reference_lease_handle = None
        self._motion_only = bool(motion_only)
        self._customer_mode = bool(customer_mode)
        self._theme = "dark"
        self._profile: Optional[WorkstationFixtureProfile] = None
        self._calibration: Optional[FixtureCalibration] = None
        self._control: Optional[FixtureControlWorker] = None
        self._ms_worker: Optional[Ms6222SerialWorker] = None
        self._ms_connected = False
        self._ms_last_error = ""
        self._recorder: Optional[FixtureSessionRecorder] = None
        self._finalizer: Optional[_SessionFinalizeThread] = None
        self._session_accepting_frames = False
        self._evidence_ready = True
        self._session_origin_ns = time.monotonic_ns()
        self._control_session_id = ""
        self._targets: deque[TimedAttitude] = deque(maxlen=60000)
        # Calibration needs only a recent capture window. Full measurements and
        # comparisons are persisted by FixtureSessionRecorder.
        self._measurements: deque[TimedAttitude] = deque(maxlen=2000)
        self._display_measurements: deque[TimedAttitude] = deque(maxlen=30000)
        self._errors: deque[TimedAttitude] = deque(maxlen=30000)
        self._run_summaries: list[dict] = []
        self._latest_ms_stats: Optional[Ms6222WorkerStatistics] = None
        self._latest_ins_quality: Optional[tuple[int, int, int]] = None
        self._latest_valid_ins_ns = 0
        self._qualification: Optional[FixtureQualificationController] = None
        self._qualification_request_id = ""
        self._qualification_summary: Optional[dict] = None
        self._qualification_export_pending = False
        self._qualification_start_pending = False
        self._guided_calibration: Optional[GuidedFixtureCalibration] = None
        self._outer_loop = BoundedAttitudeOuterLoop()
        self._calibration_capture_enabled = False
        self._view_active = False
        self._build_ui()
        self._on_motion_snapshot(
            MotionPlatformSnapshot(MotionPlatformState.OFFLINE, 0)
        )
        if self._motion_only:
            self._apply_motion_only_mode()
        elif self._customer_mode:
            self._apply_customer_mode()
        self._load_fixture_models()
        if not self._motion_only:
            self._refresh_serial_ports()
        self._plot_timer = QTimer(self)
        self._plot_timer.setInterval(100)
        self._plot_timer.timeout.connect(self._refresh_plot)
        register_translatable(self)

    def _apply_motion_only_mode(self) -> None:
        """Expose only the motion-platform owner in the production diagnostics tab."""
        self._profile_group.setTitle(tr("Motion platform profile"))
        for widget in (
            self._serial_label,
            self._serial_combo,
            self._serial_refresh,
            self._serial_connect,
            self._ms_status,
        ):
            widget.hide()
        self._calibration_group.hide()
        self._live_group.hide()

    def _apply_customer_mode(self) -> None:
        """Expose daily pose control while hiding maintenance and qualification actions."""
        self._profile_group.setTitle(tr("Motion platform and attitude reference"))
        self._safety_group.setTitle(tr("Motion safety confirmation"))
        self._session_start.setText(tr("Start control session"))
        self._session_end.setText(tr("Finish control session"))
        self._model_combo.setEnabled(False)
        self._installation_config.hide()
        self._trajectory_group.setTitle(tr("Combined continuous rocking"))
        self._preview_button.setText(tr("Preview and validate"))
        self._trajectory_start.setText(tr("Start combined rocking"))
        self._stop.setText(tr("Controlled stop"))
        self._qualification_group.hide()
        self._calibration_group.hide()
        self._reset.hide()
        self._conclusion_label.hide()
        self._conclusion.hide()
        self._notes.hide()

    def select_reference_port(self, port: str) -> None:
        """Select the station-owned reference port without creating another owner."""
        value = str(port).strip()
        if not value or self._ms_worker is not None:
            return
        index = self._serial_combo.findText(value)
        if index < 0:
            self._serial_combo.addItem(value)
            index = self._serial_combo.count() - 1
        self._serial_combo.setCurrentIndex(index)

    @property
    def profile(self) -> Optional[WorkstationFixtureProfile]:
        return self._profile

    @property
    def session_active(self) -> bool:
        return self._session_coordinator.active

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        top = QGroupBox(tr("Fixture profile and reference"))
        self._profile_group = top
        top_grid = QGridLayout(top)
        top_grid.setContentsMargins(8, 12, 8, 7)
        self._model_label = QLabel(tr("Motion platform model"))
        self._model_combo = QComboBox()
        self._model_combo.setObjectName("fixtureModelSelector")
        self._model_combo.currentIndexChanged.connect(self._on_model_selected)
        self._installation_config = QPushButton(tr("Installation configuration..."))
        self._installation_config.clicked.connect(self._edit_installation_configuration)
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
        self._service_label = QLabel(tr("Motion controller"))
        self._service_status = QLabel(tr("Offline"))
        self._service_status.setObjectName("fixtureServiceStatus")
        top_grid.addWidget(self._model_label, 0, 0)
        top_grid.addWidget(self._model_combo, 0, 1, 1, 2)
        top_grid.addWidget(self._installation_config, 0, 3, 1, 2)
        top_grid.addWidget(self._profile_status, 0, 5, 1, 3)
        top_grid.addWidget(self._service_label, 1, 0)
        top_grid.addWidget(self._service_status, 1, 1, 1, 7)
        top_grid.addWidget(self._serial_label, 2, 0)
        top_grid.addWidget(self._serial_combo, 2, 1, 1, 2)
        top_grid.addWidget(self._serial_refresh, 2, 3)
        top_grid.addWidget(self._serial_connect, 2, 4)
        top_grid.addWidget(self._ms_status, 2, 5, 1, 3)
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
        controls_layout.addWidget(self._build_qualification_group())
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
        self._absolute_heading = QLabel(tr("MS-6222 absolute heading: unavailable"))
        self._absolute_heading.setObjectName("fixtureAbsoluteHeading")
        live_layout.addWidget(self._absolute_heading)
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
        group = QGroupBox(tr("Absolute movement (device FRD)"))
        self._manual_group = group
        layout = QGridLayout(group)
        self._frd_contract = QLabel(
            tr("FRD positive directions: Roll right wing down / Pitch nose up / Yaw right (clockwise from above)")
        )
        self._frd_contract.setWordWrap(True)
        self._axis = QComboBox()
        self._axis.addItem("Roll", "roll")
        self._axis.addItem("Pitch", "pitch")
        self._axis.addItem("Yaw", "yaw")
        self._axis.currentIndexChanged.connect(self._update_axis_range)
        self._target = QDoubleSpinBox()
        self._target.setDecimals(2)
        self._target.setRange(-15.0, 15.0)
        self._duration = QDoubleSpinBox()
        self._duration.setRange(0.02, 600.0)
        self._duration.setValue(3.0)
        self._duration.setSuffix(" s")
        self._closed_loop = QCheckBox(tr("MS-6222 bounded attitude correction"))
        self._closed_loop.toggled.connect(
            lambda enabled: None if enabled else self._outer_loop.stop()
        )
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
        self._reset = QPushButton(tr("Home..."))
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
        layout.addWidget(self._frd_contract, 0, 0, 1, 2)
        layout.addWidget(self._manual_labels["axis"], 1, 0)
        layout.addWidget(self._axis, 1, 1)
        layout.addWidget(self._manual_labels["target"], 2, 0)
        layout.addWidget(self._target, 2, 1)
        layout.addWidget(self._manual_labels["duration"], 3, 0)
        layout.addWidget(self._duration, 3, 1)
        layout.addWidget(self._closed_loop, 4, 0, 1, 2)
        layout.addWidget(self._move, 5, 0, 1, 2)
        layout.addWidget(self._center, 6, 0)
        layout.addWidget(self._stop, 6, 1)
        layout.addWidget(self._reset, 7, 0, 1, 2)
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
        self._sample_period.setValue(50)
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

    def _build_qualification_group(self) -> QGroupBox:
        group = QGroupBox(tr("Automatic platform qualification"))
        self._qualification_group = group
        layout = QGridLayout(group)
        self._qualification_status = QLabel(
            tr("Ready to test center and Roll/Pitch/Yaw ±3 deg with MS-6222.")
        )
        self._qualification_status.setWordWrap(True)
        self._qualification_start = QPushButton(tr("Start automatic test"))
        self._qualification_start.setProperty("variant", "primary")
        self._qualification_start.clicked.connect(self._toggle_qualification)
        self._qualification_progress = QLabel(tr("Not started"))
        self._qualification_progress.setWordWrap(True)
        layout.addWidget(self._qualification_status, 0, 0, 1, 2)
        layout.addWidget(self._qualification_start, 1, 0)
        layout.addWidget(self._qualification_progress, 1, 1)
        return group

    def _load_fixture_models(self) -> None:
        selected_profile_id = str(
            self._settings.get("production.fixture_profile_id", "") or ""
        )
        profiles = self._profile_store.list_profiles()
        profiles_by_id = {profile.profile_id: profile for profile in profiles}
        selected_profile = profiles_by_id.get(selected_profile_id)

        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        for preset in fixture_model_presets():
            default_profile_id = preset.create_profile().profile_id
            candidates = [
                profile for profile in profiles if profile.model_id == preset.model_id
            ]
            profile = (
                selected_profile
                if selected_profile is not None
                and selected_profile.model_id == preset.model_id
                else profiles_by_id.get(default_profile_id)
            )
            if profile is None and candidates:
                profile = max(candidates, key=lambda item: item.revision)
            if profile is None:
                profile = preset.create_profile()
                try:
                    self._profile_store.save(profile)
                except (FixtureProfileError, OSError) as exc:
                    self._profile_status.setText(str(exc))
                    continue
                profiles_by_id[profile.profile_id] = profile
            else:
                reconciled = preset.reconcile_profile(profile)
                if reconciled is not profile:
                    try:
                        self._profile_store.save(
                            reconciled,
                            expected_revision=profile.revision,
                        )
                    except (FixtureProfileError, OSError) as exc:
                        self._profile_status.setText(str(exc))
                        continue
                    profile = reconciled
                    profiles_by_id[profile.profile_id] = profile
            self._model_combo.addItem(preset.display_name, profile.profile_id)

        if selected_profile is not None and selected_profile.model_id == "custom":
            self._model_combo.addItem(
                tr("Custom or migrated profile"), selected_profile.profile_id
            )
        index = self._model_combo.findData(selected_profile_id)
        self._model_combo.setCurrentIndex(index if index >= 0 else 0)
        self._model_combo.blockSignals(False)
        self._on_model_selected(self._model_combo.currentIndex())

    def _on_model_selected(self, _index: int) -> None:
        profile_id = str(self._model_combo.currentData() or "")
        self._profile = None
        self._calibration = None
        if profile_id:
            try:
                self._profile = self._profile_store.load(profile_id)
            except (FixtureProfileError, OSError, ValueError) as exc:
                self._profile_status.setText(str(exc))
            else:
                self._settings.set("production.fixture_profile_id", profile_id)
                self._settings.persist_preferences()
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
        self._installation_config.setEnabled(
            self._profile is not None and not self.session_active
        )
        self._update_profile_ranges()
        self._update_start_gate()
        self._update_calibration_status()
        self._update_attitude_status()

    def _edit_installation_configuration(self) -> None:
        if self.session_active or self._profile is None:
            return
        source = self._profile
        dialog = FixtureProfileDialog(source, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result_profile is None:
            return
        profile = dialog.result_profile
        try:
            self._profile_store.save(
                profile,
                expected_revision=source.revision,
            )
        except (FixtureProfileError, OSError) as exc:
            QMessageBox.critical(self, tr("Fixture profile"), str(exc))
            return
        self._settings.set("production.fixture_profile_id", profile.profile_id)
        self._settings.persist_preferences()
        self._load_fixture_models()

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
        if self._motion_only:
            return
        if self._ms_worker is not None:
            self._disconnect_ms()
            return
        port = self._serial_combo.currentText().strip()
        if not port:
            self._show_error(tr("Select an MS-6222 serial port."))
            return
        lease_handle = None
        if self._reference_lease is not None:
            try:
                lease_handle = self._reference_lease.acquire("fixture-diagnostics")
            except Ms6222DebugError as exc:
                self._show_error(str(exc))
                return
        worker = Ms6222SerialWorker(port, parent=self)
        worker.frame_received.connect(self._on_ms_frame)
        worker.invalid_frame_received.connect(self._on_ms_frame)
        worker.statistics_changed.connect(self._on_ms_statistics)
        worker.connection_changed.connect(self._on_ms_connection_changed)
        worker.error_occurred.connect(self._on_ms_error)
        worker.finished.connect(self._on_ms_worker_finished)
        self._ms_worker = worker
        self._reference_lease_handle = lease_handle
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
        self._qualification_start_pending = False
        self._ms_last_error = ""
        self._latest_ins_quality = None
        if worker is not None:
            worker.stop()
            worker.wait(2000)
        if self._reference_lease is not None:
            self._reference_lease.release(self._reference_lease_handle)
        self._reference_lease_handle = None
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
        self.runtime_state_changed.emit(
            "reference",
            "connected" if connected else "configured",
            port if connected else "",
        )
        self._update_start_gate()

    def _on_ms_error(self, details: str) -> None:
        self._ms_last_error = str(details)
        self._ms_connected = False
        self._qualification_start_pending = False
        self._ms_status.setText(tr("Reference error: {details}", details=details))
        self._append_event(self._ms_status.text())
        self.runtime_state_changed.emit("reference", "fault", str(details))
        qualification = self._qualification
        if qualification is not None and qualification.active:
            qualification.motion_failed(f"MS-6222 error: {details}")
        self._update_start_gate()

    def _on_ms_worker_finished(self) -> None:
        worker = self.sender()
        if worker is self._ms_worker:
            self._ms_worker = None
            self._ms_connected = False
            self._qualification_start_pending = False
            self._serial_connect.setText(tr("Connect reference"))
            self._serial_connect.setEnabled(True)
            if not self._ms_last_error:
                self._ms_status.setText(tr("Reference disconnected"))
            if self._reference_lease is not None:
                self._reference_lease.release(self._reference_lease_handle)
            self._reference_lease_handle = None
            qualification = self._qualification
            if qualification is not None and qualification.active:
                qualification.motion_failed("MS-6222 serial stream stopped")
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
        self.runtime_state_changed.emit(
            "reference", "ready", "valid MS-6222 INSPVAXB frames"
        )
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
        profile = self._profile
        calibration = self._calibration
        telemetry = self._telemetry
        profile_id = "" if profile is None else profile.profile_id
        profile_sha = "" if profile is None else profile.sha256
        calibration_id = "" if calibration is None else calibration.calibration_id
        quality = {
            "gnss_state": int(record.gnss_state),
            "fix_type": int(record.fix_type),
            "ekf_state": int(record.ekf_state),
            "roll_std_deg": float(record.roll_std_deg),
            "pitch_std_deg": float(record.pitch_std_deg),
            "yaw_std_deg": float(record.yaw_std_deg),
        }
        if telemetry is not None:
            telemetry.publish(
                FixtureAttitudeSample(
                    source=FixtureAttitudeSource.MS6222_TRUE_HEADING,
                    host_monotonic_ns=envelope.host_monotonic_ns,
                    host_wall_time_ns=envelope.host_wall_time_ns,
                    yaw_deg=(record.yaw_deg % 360.0) if solution_valid else None,
                    valid=solution_valid,
                    invalid_reason="" if solution_valid else "reference_quality_invalid",
                    quality=quality,
                    fixture_profile_id=profile_id,
                    fixture_profile_sha256=profile_sha,
                    calibration_id=calibration_id,
                    control_session_id=self._control_session_id,
                )
            )
        if solution_valid:
            self._absolute_heading.setText(
                tr(
                    "MS-6222 absolute heading: {heading:.3f} deg | standard deviation {std:.3f} deg",
                    heading=record.yaw_deg % 360.0,
                    std=record.yaw_std_deg,
                )
            )
            self._latest_valid_ins_ns = raw.monotonic_ns
            if self._qualification_start_pending:
                self._qualification_start_pending = False
                QTimer.singleShot(0, self._start_pending_qualification)
        else:
            self._absolute_heading.setText(
                tr("MS-6222 absolute heading: unavailable (reference quality invalid)")
            )
        qualification = self._qualification
        if qualification is not None:
            qualification.add_sample(raw)
        self._measurements.append(raw)
        if recorder is not None and self._session_accepting_frames:
            try:
                recorder.record_measurement(raw)
            except Exception as exc:
                self._fail_evidence(str(exc))
        coordinate_valid = bool(calibration is not None and calibration.coordinate_valid)
        display = calibration.apply(raw) if coordinate_valid else raw
        if telemetry is not None:
            relative_valid = bool(solution_valid and coordinate_valid and display.valid)
            telemetry.publish(
                FixtureAttitudeSample(
                    source=FixtureAttitudeSource.MS6222_RELATIVE,
                    host_monotonic_ns=envelope.host_monotonic_ns,
                    host_wall_time_ns=envelope.host_wall_time_ns,
                    roll_deg=display.roll_deg if relative_valid else None,
                    pitch_deg=display.pitch_deg if relative_valid else None,
                    yaw_deg=display.yaw_deg if relative_valid else None,
                    valid=relative_valid,
                    invalid_reason=(
                        ""
                        if relative_valid
                        else (
                            "reference_quality_invalid"
                            if not solution_valid
                            else "coordinate_calibration_unavailable"
                        )
                    ),
                    quality=quality,
                    fixture_profile_id=profile_id,
                    fixture_profile_sha256=profile_sha,
                    calibration_id=calibration_id,
                    control_session_id=self._control_session_id,
                )
            )
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
        ready = (
            self._profile is not None
            and all(check.isChecked() for check in self._safety_checks)
        )
        self._session_start.setEnabled(ready and not self.session_active and self._finalizer is None)
        if self.session_active:
            self._platform_status.setText(tr("Preflight complete | control lease held"))
        elif self._profile is None:
            self._platform_status.setText(tr("Profile not ready"))
        elif not all(check.isChecked() for check in self._safety_checks):
            self._platform_status.setText(tr("Waiting for all safety confirmations"))
        else:
            self._platform_status.setText(tr("Profile valid | ready to start engineering session"))
        controller_ready = bool(
            self._control is not None
            and self._control.controller.snapshot.state == MotionPlatformState.READY
        )
        self._set_motion_controls_enabled(
            self.session_active and self._evidence_ready and controller_ready
        )

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
        context = self._session_coordinator.begin(
            self._profile,
            operator=str(self._settings.get("production.last_operator", "") or ""),
        )
        recorder = context.recorder
        try:
            control = FixtureControlWorker(
                self._profile,
                controller_factory=self._controller_factory,
                recorder=recorder,
                parent=self,
            )
            control.confirm_preflight()
            control.send_result.connect(self._on_platform_send)
            control.request_finished.connect(self._on_control_finished)
            control.request_failed.connect(self._on_control_failed)
            control.evidence_failed.connect(self._fail_evidence)
            control.busy_changed.connect(self._on_control_busy)
            control.snapshot_changed.connect(self._on_motion_snapshot)
            control.connected.connect(self._on_motion_connected)
            control.start()
        except Exception:
            try:
                self._session_coordinator.abort(reason="session startup failed")
            except Exception:
                pass
            raise
        self._recorder = recorder
        self._control = control
        self._evidence_ready = True
        self._session_accepting_frames = True
        self._session_origin_ns = time.monotonic_ns()
        self._control_session_id = context.session_dir.name
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
        recorder.record_target(self._targets[-1])
        self._append_event(
            tr("Engineering session started: {path}", path=str(context.session_dir)),
            event_type="session_started",
            details={"ms6222_connected": self._ms_connected},
        )
        self._session_end.setEnabled(True)
        self._model_combo.setEnabled(False)
        self._installation_config.setEnabled(False)
        self.active_changed.emit(True)
        self._update_start_gate()
        return context.session_dir

    def _set_motion_controls_enabled(self, enabled: bool) -> None:
        qualification_active = bool(
            self._qualification is not None and self._qualification.active
        )
        for widget in (
            self._move,
            self._center,
            self._stop,
            self._reset,
            self._trajectory_start,
            self._calibration_start,
        ):
            widget.setEnabled(enabled and not qualification_active)
        qualification_ready = (
            not self._motion_only
            and self._profile is not None
            and all(check.isChecked() for check in self._safety_checks)
            and self._finalizer is None
            and self._evidence_ready
        )
        self._qualification_start.setEnabled(
            qualification_active
            or self._qualification_start_pending
            or qualification_ready
        )
        self._qualification_start.setText(
            tr("Stop automatic test")
            if qualification_active
            else (
                tr("Cancel automatic test start")
                if self._qualification_start_pending
                else tr("Start automatic test")
            )
        )

    def _toggle_qualification(self) -> None:
        qualification = self._qualification
        if qualification is not None and qualification.active:
            qualification.stop()
            control = self._control
            if control is not None:
                control.stop_sequence()
            return
        if self._qualification_start_pending:
            self._qualification_start_pending = False
            self._qualification_status.setText(tr("Automatic test start cancelled."))
            self._update_start_gate()
            return
        if not self._ms_connected:
            self._qualification_start_pending = True
            self._qualification_status.setText(
                tr("Opening MS-6222 and waiting for a valid attitude frame...")
            )
            if self._ms_worker is None:
                self._toggle_ms_connection()
            if self._ms_worker is None:
                self._qualification_start_pending = False
                self._qualification_status.setText(
                    tr("Automatic test could not start."),
                )
            self._update_start_gate()
            return
        if (
            self._latest_valid_ins_ns <= 0
            or time.monotonic_ns() - self._latest_valid_ins_ns > 1_000_000_000
        ):
            self._qualification_start_pending = True
            self._qualification_status.setText(
                tr("Waiting for a valid MS-6222 attitude frame...")
            )
            self._update_start_gate()
            return
        try:
            self._start_qualification()
        except (
            FixtureAnalysisError,
            FixtureLeaseError,
            FixtureProfileError,
            MotionPlatformError,
            OSError,
            RuntimeError,
        ) as exc:
            self._show_error(str(exc))

    def _start_pending_qualification(self) -> None:
        try:
            self._start_qualification()
        except (
            FixtureAnalysisError,
            FixtureLeaseError,
            FixtureProfileError,
            MotionPlatformError,
            OSError,
            RuntimeError,
        ) as exc:
            self._show_error(str(exc))
            self._update_start_gate()

    def _start_qualification(self) -> None:
        self._qualification_start_pending = False
        if self._profile is None:
            raise FixtureProfileError("no fixture profile is selected")
        if not self._ms_connected or self._ms_worker is None:
            raise MotionPlatformError("MS-6222 reference must be connected")
        if (
            self._latest_valid_ins_ns <= 0
            or time.monotonic_ns() - self._latest_valid_ins_ns > 1_000_000_000
        ):
            raise MotionPlatformError("MS-6222 has no recent valid attitude frame")
        if not self.session_active:
            self.start_engineering_session()
        if self._control is None:
            raise MotionPlatformError("fixture control is not active")
        qualification = FixtureQualificationController(
            profile_id=self._profile.profile_id,
            profile_sha256=self._profile.sha256,
            center_pose=self._profile.center_pose,
            analysis_calibration=self._calibration,
            config=FixtureQualificationConfig(),
            parent=self,
        )
        qualification.move_requested.connect(self._on_qualification_move_requested)
        qualification.stage_changed.connect(self._on_qualification_stage_changed)
        qualification.completed.connect(self._on_qualification_completed)
        qualification.failed.connect(self._on_qualification_failed)
        self._qualification = qualification
        self._qualification_request_id = ""
        self._qualification_summary = None
        self._qualification_export_pending = True
        self._append_event(
            tr("Automatic platform qualification started."),
            event_type="qualification_started",
        )
        self._set_motion_controls_enabled(True)
        qualification.start()

    def _on_qualification_move_requested(
        self,
        stage: str,
        pose: PlatformPose,
        duration_ms: int,
    ) -> None:
        try:
            self._qualification_request_id = self._require_control().submit_pose(
                pose,
                total_duration_ms=duration_ms,
            )
        except MotionPlatformError as exc:
            qualification = self._qualification
            if qualification is not None:
                qualification.motion_failed(str(exc))
        else:
            self._append_event(
                tr("Qualification stage command submitted: {stage}", stage=stage),
                event_type="qualification_stage_command",
                details={"stage": stage, "duration_ms": duration_ms},
            )

    def _on_qualification_stage_changed(
        self,
        stage: str,
        state: str,
        index: int,
        total: int,
    ) -> None:
        self._qualification_progress.setText(
            tr(
                "Stage {index}/{total}: {stage} | {state}",
                index=index,
                total=total,
                stage=stage,
                state=state,
            )
        )

    def _on_qualification_completed(
        self,
        result: FixtureQualificationResult,
    ) -> None:
        calibration = self._confirm_calibration_preview(result.proposed_calibration)
        if calibration is not None:
            result = replace(result, proposed_calibration=calibration)
        self._qualification_summary = result.to_payload()
        self._qualification_progress.setText(
            tr("Automatic test complete; finalizing evidence package...")
        )
        self._append_event(
            tr("Automatic platform qualification sequence completed."),
            event_type="qualification_completed",
            details={
                "verdict": result.verdict,
                "mapping": list(
                    result.proposed_calibration.sensor_axis_for_logical
                ),
                "signs": list(result.proposed_calibration.logical_signs),
                "calibration_confirmed": result.proposed_calibration.confirmed,
            },
        )
        self._begin_session_finish(
            conclusion=FixtureSessionConclusion.INCONCLUSIVE,
            notes=tr(
                "Automatic platform qualification completed; thresholds remain engineering-only."
            ),
        )

    def _on_qualification_failed(self, details: str) -> None:
        self._qualification_progress.setText(
            tr("Automatic test incomplete: {details}", details=details)
        )
        self._append_event(
            tr("Automatic platform qualification stopped: {details}", details=details),
            event_type="qualification_failed",
            details={"reason": details},
        )
        if self.session_active and self._finalizer is None:
            self._begin_session_finish(
                conclusion=FixtureSessionConclusion.INCONCLUSIVE,
                notes=details,
                abort_reason=details,
            )

    def _send_axis_move(self) -> None:
        control = self._require_control()
        try:
            axis = str(self._axis.currentData())
            values = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
            values[axis] = self._target.value()
            pose = PlatformPose(
                values["roll"],
                values["pitch"],
                values["yaw"],
                z_mm=self._profile.center_pose.z_mm if self._profile else 100.0,
            )
            if self._closed_loop.isChecked():
                self._require_outer_loop_ready()
                self._outer_loop.begin(pose)
                control.submit_pose(
                    pose,
                    total_duration_ms=round(self._duration.value() * 1000),
                )
            else:
                control.submit_axis_move(
                    axis,
                    self._target.value(),
                    total_duration_ms=round(self._duration.value() * 1000),
                )
        except MotionPlatformError as exc:
            self._outer_loop.stop()
            self._show_error(str(exc))

    def _send_center(self) -> None:
        try:
            if self._closed_loop.isChecked():
                self._require_outer_loop_ready()
                if self._profile is None:
                    raise MotionPlatformError("fixture profile is not ready")
                self._outer_loop.begin(self._profile.center_pose)
            self._require_control().submit_center(
                duration_ms=round(self._duration.value() * 1000)
            )
        except MotionPlatformError as exc:
            self._outer_loop.stop()
            self._show_error(str(exc))

    def _stop_sequence(self) -> None:
        self._outer_loop.stop()
        control = self._control
        if control is not None:
            control.stop_sequence()
            self._append_event(
                tr(
                    "Controlled stop requested; no further trajectory points will be sent. "
                    "Use the physical emergency stop for an unsafe motion."
                )
            )

    def _send_reset(self) -> None:
        answer = QMessageBox.warning(
            self,
            tr("Home platform"),
            tr("Homing may interrupt feedback for about one minute. Confirm the mechanism is safe before continuing."),
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
        qualification = self._qualification
        if qualification is not None and qualification.active:
            qualification.record_command_sent(result.monotonic_ns)
        pose = result.target_pose
        if pose is not None:
            self._targets.append(
                TimedAttitude(result.monotonic_ns, pose.roll_deg, pose.pitch_deg, pose.yaw_deg)
            )
            recorder = self._recorder
            if recorder is not None and self._session_accepting_frames:
                try:
                    recorder.record_target(self._targets[-1])
                except Exception as exc:
                    self._fail_evidence(str(exc))
            telemetry = self._telemetry
            profile = self._profile
            if telemetry is not None:
                telemetry.publish(
                    FixtureAttitudeSample(
                        source=FixtureAttitudeSource.PLATFORM_TARGET,
                        host_monotonic_ns=int(result.monotonic_ns),
                        host_wall_time_ns=time.time_ns(),
                        roll_deg=float(pose.roll_deg),
                        pitch_deg=float(pose.pitch_deg),
                        yaw_deg=float(pose.yaw_deg),
                        quality={
                            "action": str(action),
                            "sequence": int(sequence),
                            "fact": "command_sent",
                        },
                        fixture_profile_id="" if profile is None else profile.profile_id,
                        fixture_profile_sha256="" if profile is None else profile.sha256,
                        calibration_id=(
                            "" if self._calibration is None else self._calibration.calibration_id
                        ),
                        control_session_id=self._control_session_id,
                    )
                )
        self._platform_status.setText(
            tr(
                "Command sent | {action} | sequence {sequence}",
                action=self._control_action_text(action),
                sequence=sequence,
            )
        )

    def _on_control_finished(self, request_id: str, result) -> None:
        payload = {"type": type(result).__name__}
        if hasattr(result, "__dataclass_fields__"):
            payload.update(asdict(result))
        self._run_summaries.append(payload)
        if isinstance(result, TrajectoryResult):
            self._append_event(
                tr(
                    "Trajectory executed: sent {sent}/{intended}, skipped {skipped}; "
                    "maximum scheduler lateness {lateness:.2f} ms.",
                    sent=result.sent_points,
                    intended=result.intended_points,
                    skipped=result.skipped_points,
                    lateness=result.maximum_lateness_ms,
                ),
                event_type="trajectory_execution_completed",
                details={
                    "intended_points": result.intended_points,
                    "sent_points": result.sent_points,
                    "skipped_points": result.skipped_points,
                    "maximum_lateness_ms": result.maximum_lateness_ms,
                },
            )
        settlement = _settlement_result(result)
        motion_stopped = bool(settlement and settlement.motion_stopped)
        target_guard_passed = bool(settlement and settlement.target_guard_passed)
        if settlement is not None and motion_stopped and target_guard_passed:
            self._append_event(
                tr(
                    "Motion stopped within the actuator guard; maximum residual {error:.4f} mm.",
                    error=float(settlement.maximum_error_mm or 0.0),
                )
            )
            details = settlement.details
        elif settlement is not None:
            details = tr(
                "Motion stopped outside the actuator guard; maximum residual {error:.4f} mm.",
                error=float(settlement.maximum_error_mm or 0.0),
            )
            self._append_event(details)
            self.status_message.emit(details, 8000)
        elif isinstance(result, TrajectoryResult) and result.stopped:
            details = tr("Trajectory generation stopped; no final position conclusion.")
            self._append_event(details)
        else:
            details = tr("Control request completed without a motion settlement result.")
            self._append_event(details)
        qualification = self._qualification
        if (
            qualification is not None
            and qualification.active
            and request_id == self._qualification_request_id
        ):
            if motion_stopped and target_guard_passed:
                qualification.motion_completed()
            else:
                qualification.motion_failed(details)
        if self._guided_calibration is not None and motion_stopped and target_guard_passed:
            self._calibration_capture.setEnabled(True)
        if self._outer_loop.active and motion_stopped and target_guard_passed:
            QTimer.singleShot(1000, self._evaluate_outer_loop)

    def _require_outer_loop_ready(self) -> None:
        if self._calibration is None or not self._calibration.coordinate_valid:
            raise MotionPlatformError("confirmed coordinate calibration is required for attitude correction")
        if (
            not self._ms_connected
            or not self._latest_valid_ins_ns
            or time.monotonic_ns() - self._latest_valid_ins_ns > 1_000_000_000
        ):
            raise MotionPlatformError("MS-6222 has no recent valid attitude frame")

    def _evaluate_outer_loop(self) -> None:
        if not self._outer_loop.active:
            return
        try:
            self._require_outer_loop_ready()
            measured = next(
                sample for sample in reversed(self._display_measurements) if sample.valid
            )
            decision = self._outer_loop.evaluate(measured)
            details = {
                "status": decision.status.value,
                "iteration": decision.iteration,
                "error_deg": list(decision.error_deg),
                "error_norm_deg": decision.error_norm_deg,
            }
            if decision.status == OuterLoopStatus.CORRECTION_REQUIRED:
                if decision.command_pose is None:
                    raise MotionPlatformError("outer-loop correction has no command pose")
                details["command_pose"] = asdict(decision.command_pose)
                self._append_event(
                    tr(
                        "Attitude correction iteration {iteration}: error {error:.3f} deg.",
                        iteration=decision.iteration,
                        error=decision.error_norm_deg,
                    ),
                    event_type="attitude_outer_loop_correction",
                    details=details,
                )
                self._require_control().submit_pose(
                    decision.command_pose,
                    total_duration_ms=round(self._duration.value() * 1000),
                )
                return
            message = {
                OuterLoopStatus.TARGET_VERIFIED: tr(
                    "MS-6222 verified the target attitude; residual {error:.3f} deg.",
                    error=decision.error_norm_deg,
                ),
                OuterLoopStatus.ITERATION_LIMIT: tr(
                    "Attitude correction stopped at its iteration limit; residual {error:.3f} deg.",
                    error=decision.error_norm_deg,
                ),
                OuterLoopStatus.DIVERGING: tr(
                    "Attitude correction stopped because the error increased to {error:.3f} deg.",
                    error=decision.error_norm_deg,
                ),
            }[decision.status]
            self._append_event(
                message,
                event_type="attitude_outer_loop_finished",
                details=details,
            )
        except (MotionPlatformError, RuntimeError, StopIteration) as exc:
            self._outer_loop.stop()
            self._append_event(
                tr("Attitude correction stopped: {details}", details=str(exc)),
                event_type="attitude_outer_loop_failed",
                details={"reason": str(exc)},
            )

    def _on_control_failed(self, request_id: str, details: str) -> None:
        self._append_event(tr("Control request failed: {details}", details=details))
        self.status_message.emit(details, 8000)
        self.runtime_state_changed.emit("motion", "fault", str(details))
        qualification = self._qualification
        if (
            qualification is not None
            and qualification.active
            and request_id == self._qualification_request_id
        ):
            qualification.motion_failed(details)

    def _on_control_busy(self, busy: bool) -> None:
        ready = self.session_active and self._evidence_ready
        self._set_trajectory_parameters_enabled(not busy)
        qualification_active = bool(
            self._qualification is not None and self._qualification.active
        )
        self._move.setEnabled(ready and not busy)
        self._center.setEnabled(ready and not busy)
        self._reset.setEnabled(ready and not busy)
        self._trajectory_start.setEnabled(ready and not busy)
        self._stop.setEnabled(ready)
        if qualification_active:
            for widget in (
                self._move,
                self._center,
                self._reset,
                self._trajectory_start,
                self._calibration_start,
            ):
                widget.setEnabled(False)
        self._qualification_start.setEnabled(
            qualification_active or (ready and self._ms_connected and not busy)
        )

    def _set_trajectory_parameters_enabled(self, enabled: bool) -> None:
        for axis_widgets in self._trajectory_axes.values():
            for widget in axis_widgets:
                widget.setEnabled(enabled)
        for widget in (
            self._ramp_in,
            self._steady,
            self._ramp_out,
            self._sample_period,
            self._preview_button,
        ):
            widget.setEnabled(enabled)

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
            "home": tr("Home"),
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
        calibration = self._confirm_calibration_preview(preview)
        if calibration is None:
            return
        self._guided_calibration = None
        self._calibration_evaluate.setEnabled(False)
        self._calibration_status.setText(
            tr("Calibration saved: {calibration_id}", calibration_id=calibration.calibration_id)
        )
        self._update_attitude_status()

    def _confirm_calibration_preview(
        self,
        preview: FixtureCalibration,
    ) -> Optional[FixtureCalibration]:
        confirmed_preview = replace(preview, confirmed=True)
        if not confirmed_preview.coordinate_valid:
            QMessageBox.warning(
                self,
                tr("Coordinate calibration rejected"),
                tr(
                    "The center reference is not stable. Maximum center dispersion is "
                    "{dispersion:.3f} deg; the engineering limit is {limit:.3f} deg. "
                    "Wait for MS-6222 heading to stabilize, then run the qualification test again.",
                    dispersion=max(preview.static_noise_std_deg),
                    limit=CALIBRATION_REFERENCE_STABILITY_LIMIT_DEG,
                ),
            )
            return None
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
            return None
        calibration = confirmed_preview
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
            return None
        self._calibration = calibration
        self._profile = revised
        self._append_event(
            tr(
                "Coordinate calibration confirmed and saved: {calibration_id}",
                calibration_id=calibration.calibration_id,
            ),
            event_type="calibration_confirmed",
            details={"calibration_id": calibration.calibration_id},
        )
        self._update_attitude_status()
        return calibration

    def _update_attitude_status(self) -> None:
        if self._calibration and self._calibration.coordinate_valid:
            self._attitude_plot.set_coordinate_valid(True)
            self._live_group.setTitle(tr("Target / measured / error attitude"))
            self._attitude_status.setText(
                tr(
                    "Coordinate calibration {calibration_id} applied. Timing uses host "
                    "serial arrival; phase and delay are engineering estimates.",
                    calibration_id=self._calibration.calibration_id,
                )
            )
        else:
            self._attitude_plot.set_coordinate_valid(False)
            self._live_group.setTitle(tr("MS-6222 raw installation attitude"))
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
            effective_abort_reason = abort_reason
            hold = self._extend_target_hold(time.monotonic_ns())
            if hold is not None and not effective_abort_reason:
                try:
                    recorder.record_target(hold)
                except Exception as exc:
                    effective_abort_reason = f"target evidence failed: {exc}"
            try:
                self._session_coordinator.begin_finalization()
            except RuntimeError as exc:
                self._on_session_finalize_failed(str(exc))
                return
            finalizer = _SessionFinalizeThread(
                recorder,
                conclusion,
                notes,
                self._calibration,
                max(
                    0.5,
                    self._sample_period.value() / 1000.0 * 3.0,
                ),
                tuple(self._run_summaries),
                asdict(self._latest_ms_stats) if self._latest_ms_stats else None,
                qualification_summary=self._qualification_summary,
                abort_reason=effective_abort_reason,
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
        if abort_reason:
            self._append_event_display(tr("Finalizing incomplete fixture session..."))
        else:
            self._append_event(tr("Finishing session and calculating engineering metrics..."))

    def _on_session_finalized(self, result) -> None:
        if self._qualification_export_pending:
            try:
                archive_path = create_fixture_session_zip(result.session_dir)
            except Exception as exc:
                self._append_event_display(
                    tr("Diagnostic ZIP generation failed: {details}", details=str(exc))
                )
            else:
                self._append_event_display(
                    tr("Diagnostic ZIP saved: {path}", path=str(archive_path))
                )
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
        self._session_coordinator.complete(result)
        self._release_session_state()

    def _on_session_finalize_failed(self, details: str) -> None:
        self._append_event_display(
            tr("Session finalization failed: {details}", details=details)
        )
        self.status_message.emit(details, 10000)
        self._session_coordinator.finalization_failed()
        self._release_session_state()

    def _release_session_state(self) -> None:
        self._control = None
        self._recorder = None
        self._finalizer = None
        self._session_accepting_frames = False
        self._control_session_id = ""
        self._evidence_ready = True
        self._qualification = None
        self._qualification_request_id = ""
        self._qualification_export_pending = False
        self._qualification_start_pending = False
        for check in self._safety_checks:
            check.setChecked(False)
        self._model_combo.setEnabled(True)
        self._session_end.setEnabled(False)
        self.active_changed.emit(False)
        self._session_coordinator.reset()
        self._load_fixture_models()
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

    def _extend_target_hold(self, monotonic_ns: int) -> Optional[TimedAttitude]:
        if not self._targets:
            return None
        last = self._targets[-1]
        timestamp = int(monotonic_ns)
        if timestamp <= last.monotonic_ns:
            return None
        hold = TimedAttitude(
            timestamp,
            last.roll_deg,
            last.pitch_deg,
            last.yaw_deg,
        )
        self._targets.append(hold)
        return hold

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
            f"#fixtureProfileStatus, #fixtureMsStatus, #fixtureServiceStatus, #fixturePlatformStatus {{ color: {palette['text_2']}; }}"
            f"#fixtureProfileError {{ color: {palette['err']}; }}"
        )
        self._attitude_plot.set_theme(theme)
        self._preview.setBackground(palette["panel"])
        for axis_name in ("left", "bottom"):
            axis = self._preview.getAxis(axis_name)
            axis.setPen(pg.mkPen(palette["border_2"]))
            axis.setTextPen(pg.mkPen(palette["text_2"]))

    def retranslate_ui(self) -> None:
        if self._customer_mode:
            self._profile_group.setTitle(tr("Motion platform and attitude reference"))
            self._safety_group.setTitle(tr("Motion safety confirmation"))
            self._session_start.setText(tr("Start control session"))
            self._session_end.setText(tr("Finish control session"))
        else:
            self._profile_group.setTitle(
                tr("Motion platform profile")
                if self._motion_only
                else tr("Fixture profile and reference")
            )
            self._safety_group.setTitle(tr("Motion safety gate"))
            self._session_start.setText(tr("Start engineering session"))
            self._session_end.setText(tr("Finish session"))
        self._manual_group.setTitle(tr("Absolute movement (device FRD)"))
        self._frd_contract.setText(
            tr("FRD positive directions: Roll right wing down / Pitch nose up / Yaw right (clockwise from above)")
        )
        self._trajectory_group.setTitle(
            tr("Combined continuous rocking")
            if self._customer_mode
            else tr("Three-axis sine trajectory")
        )
        self._qualification_group.setTitle(tr("Automatic platform qualification"))
        self._calibration_group.setTitle(tr("Guided coordinate calibration"))
        self._live_group.setTitle(tr("Target / measured / error attitude"))
        self._event_group.setTitle(tr("Fixture events"))
        self._model_label.setText(tr("Motion platform model"))
        self._installation_config.setText(tr("Installation configuration..."))
        self._service_label.setText(tr("Motion controller"))
        self._serial_label.setText(tr("MS-6222 port"))
        self._serial_refresh.setText(tr("Refresh"))
        self._serial_connect.setText(
            tr("Disconnect reference") if self._ms_connected else tr("Connect reference")
        )
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
        self._stop.setText(
            tr("Controlled stop") if self._customer_mode else tr("Stop sequence")
        )
        self._reset.setText(tr("Home..."))
        self._preview_button.setText(tr("Preview and validate"))
        self._trajectory_start.setText(
            tr("Start combined rocking")
            if self._customer_mode
            else tr("Start trajectory")
        )
        qualification_active = bool(
            self._qualification is not None and self._qualification.active
        )
        self._qualification_start.setText(
            tr("Stop automatic test")
            if qualification_active
            else tr("Start automatic test")
        )
        if not qualification_active and self._qualification_summary is None:
            self._qualification_status.setText(
                tr("Ready to test center and Roll/Pitch/Yaw ±3 deg with MS-6222.")
            )
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
                "close controller feedback, and save the session as incomplete. "
                "The platform may continue an already accepted movement. Close anyway?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def activate_view(self) -> None:
        """Refresh plots only while the fixture page is visible."""
        if self._view_active:
            return
        self._view_active = True
        self._refresh_plot()
        self._plot_timer.start()

    def deactivate_view(self) -> None:
        """Stop plot rendering while control and evidence workers continue."""
        if not self._view_active:
            return
        self._view_active = False
        self._plot_timer.stop()

    def shutdown(self, *, wait: bool = True) -> None:
        self.deactivate_view()
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
                self._session_coordinator.abort(
                    reason="workspace shutdown",
                    summary=self._engineering_summary(),
                )
            except Exception:
                pass
        self._control = None
        self._recorder = None

    def _on_motion_connected(self) -> None:
        self._append_event(tr("Motion controller feedback is connected."))
        self.runtime_state_changed.emit(
            "motion", "connected", "controller feedback connected"
        )
        self._update_start_gate()

    def _on_motion_snapshot(self, snapshot: MotionPlatformSnapshot) -> None:
        runtime_state = {
            MotionPlatformState.OFFLINE: "configured",
            MotionPlatformState.STARTING: "connecting",
            MotionPlatformState.READY: "ready",
            MotionPlatformState.HOMING: "running",
            MotionPlatformState.MOVING: "running",
            MotionPlatformState.TRAJECTORY: "running",
            MotionPlatformState.RECOVERY_REQUIRED: "fault",
            MotionPlatformState.FAULT: "fault",
        }[snapshot.state]
        self.runtime_state_changed.emit("motion", runtime_state, snapshot.details)
        state_labels = {
            MotionPlatformState.OFFLINE: tr("Offline"),
            MotionPlatformState.STARTING: tr("Waiting for controller feedback"),
            MotionPlatformState.READY: tr("Ready"),
            MotionPlatformState.HOMING: tr("Homing"),
            MotionPlatformState.MOVING: tr("Moving"),
            MotionPlatformState.TRAJECTORY: tr("Trajectory running"),
            MotionPlatformState.RECOVERY_REQUIRED: tr("Recovery required"),
            MotionPlatformState.FAULT: tr("Controller fault"),
        }
        age = "-" if snapshot.feedback_age_ms is None else f"{snapshot.feedback_age_ms:.0f} ms"
        errors = ",".join(f"0x{code:04X}" for code in snapshot.servo_error_codes if code) or "0"
        target = ", ".join(str(value) for value in snapshot.target_pulses) or "-"
        actual = ", ".join(str(value) for value in snapshot.actual_pulses) or "-"
        delta = ", ".join(str(value) for value in snapshot.errors_pulses) or "-"
        self._service_status.setText(
            tr(
                "{state} | generation {generation} | feedback {age} | servo errors {errors} | "
                "target [{target}] | actual [{actual}] | delta [{delta}] | {details}",
                state=state_labels[snapshot.state],
                generation=snapshot.generation,
                age=age,
                errors=errors,
                target=target,
                actual=actual,
                delta=delta,
                details=snapshot.details,
            )
        )
        self._update_start_gate()


__all__ = ["FixtureDebugWorkspace", "FixtureProfileDialog"]
