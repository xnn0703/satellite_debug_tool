"""Debug-only Tracking scenario editor and live device driver."""

from __future__ import annotations

import json
import math
import secrets
import time

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.product import Availability
from satellite_debug_tool.core.protocol.codec_v2 import build_service_apply_rf
from satellite_debug_tool.core.protocol.frame_v2 import ServiceControlOp, ServiceResultCode
from satellite_debug_tool.core.session.device_session import DeviceSessionCore
from satellite_debug_tool.core.tracking_simulator import (
    ScenarioEngine,
    TrackingScenario,
    build_tracking_simulation_frame,
    ka256_scan_loss_db,
)
from satellite_debug_tool.i18n import tr


_DEFAULT_SCENARIO = {
    "schema": 1,
    "name": "AFD01C GEO tracking",
    "duration_s": 120.0,
    "random_seed": 1,
    "satellite_id": 1250,
    "satellite_longitude_deg": 125.0,
    "rx_frequency_mhz": 19970.0,
    "tx_frequency_mhz": 29888.0,
    "rx_polarization": 3,
    "tx_polarization": 2,
    "mount_yaw_deg": 0.0,
    "mount_pitch_deg": 0.0,
    "mount_roll_deg": 0.0,
    "hpbw_deg": 4.0,
    "hard_limit_deg": 60.0,
    "pose_keyframes": [
        {"time_s": 0.0, "latitude_deg": 31.864, "longitude_deg": 118.820, "altitude_m": 22.0, "yaw_deg": 0.0, "pitch_deg": 0.0, "roll_deg": 0.0},
        {"time_s": 120.0, "latitude_deg": 31.864, "longitude_deg": 118.820, "altitude_m": 22.0, "yaw_deg": 30.0, "pitch_deg": 3.0, "roll_deg": -2.0},
    ],
    "link_keyframes": [
        {"time_s": 0.0, "base_snr_db": 16.0, "obstruction_loss_db": 0.0, "rain_loss_db": 0.0, "noise_std_db": 0.05, "rx_online": True},
        {"time_s": 120.0, "base_snr_db": 16.0, "obstruction_loss_db": 0.0, "rain_loss_db": 0.0, "noise_std_db": 0.05, "rx_online": True},
    ],
}


class TrackingSimulatorView(QWidget):
    status_message = Signal(str, int)

    def __init__(self, session: DeviceSessionCore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._lease_token = object()
        self._scenario: TrackingScenario | None = None
        self._engine: ScenarioEngine | None = None
        self._session_id = 0
        self._phase = "stopped"
        self._position_s = 0.0
        self._last_tick = time.monotonic()
        self._deadline = 0.0
        self._rf_request_id: int | None = None
        self._rf_ack = False

        self._editor = QTextEdit(json.dumps(_DEFAULT_SCENARIO, ensure_ascii=False, indent=2))
        self._status = QLabel(tr("Stopped"))
        self._timeline = QSlider(Qt.Orientation.Horizontal)
        self._timeline.setRange(0, 10000)
        self._timeline.valueChanged.connect(self._seek_stopped)
        self._open = QPushButton(tr("Open scenario"))
        self._save = QPushButton(tr("Save scenario"))
        self._start = QPushButton(tr("Start"))
        self._pause = QPushButton(tr("Pause"))
        self._stop = QPushButton(tr("Stop"))
        self._open.clicked.connect(self._open_scenario)
        self._save.clicked.connect(self._save_scenario)
        self._start.clicked.connect(self.start)
        self._pause.clicked.connect(self.pause)
        self._stop.clicked.connect(self.stop)

        buttons = QHBoxLayout()
        for button in (self._open, self._save, self._start, self._pause, self._stop):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr("Tracking Simulator — JSON schema 1")))
        layout.addLayout(buttons)
        layout.addWidget(self._timeline)
        layout.addWidget(self._status)
        layout.addWidget(self._editor, 1)

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        session.product_store.control_response.connect(self._on_control_response)
        session.connection_changed.connect(self._on_connection_changed)
        self.destroyed.connect(
            lambda _obj=None: session.release_device_transaction(self._lease_token)
        )
        self._refresh_controls()

    @Slot()
    def start(self) -> None:
        if self._phase == "paused":
            self._phase = "running"
            self._last_tick = time.monotonic()
            self._refresh_controls()
            return
        if self._phase != "stopped":
            return
        try:
            scenario = TrackingScenario.from_dict(json.loads(self._editor.toPlainText()))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            QMessageBox.warning(self, tr("Invalid scenario"), str(error))
            return
        if not self._session.connected or not self._session.try_acquire_device_transaction(self._lease_token):
            self._set_status(tr("Device link or transaction is unavailable"))
            return
        self._scenario = scenario
        self._engine = ScenarioEngine(scenario)
        self._session_id = secrets.randbelow(0xFFFFFFFF) + 1
        self._position_s = 0.0
        self._last_tick = time.monotonic()
        initial = self._engine.sample(0.0, pointing_error_deg=0.0, offaxis_deg=0.0, scan_loss_db=0.0, beam_fresh=False)
        if not self._session.send(build_tracking_simulation_frame(0, self._session_id, scenario, initial)):
            self._fail(tr("Failed to send simulator START"))
            return
        self._phase = "wait_sim"
        self._deadline = time.monotonic() + 3.0
        self._set_status(tr("Waiting for device simulation state and TX gate off"))
        self._refresh_controls()

    @Slot()
    def pause(self) -> None:
        if self._phase == "running":
            self._phase = "paused"
            self._set_status(tr("Paused; current sample is still being sent"))
            self._refresh_controls()

    @Slot()
    def stop(self) -> None:
        if self._phase == "stopped":
            return
        if self._session_id != 0 and self._scenario is not None:
            self._session.send(build_tracking_simulation_frame(2, self._session_id, self._scenario))
        self._finish_local(tr("Stopped; normal TX policy may resume"))

    @Slot()
    def _tick(self) -> None:
        if self._phase == "stopped" or self._scenario is None or self._engine is None:
            return
        now = time.monotonic()
        if self._phase in {"wait_sim", "wait_rf"} and now >= self._deadline:
            self._fail(tr("Simulator start timed out"))
            return
        if self._phase == "wait_sim" and self._simulation_active() and self._tx_gate_is_off():
            self._rf_request_id = self._session.next_request_id()
            frame = build_service_apply_rf(
                self._rf_request_id,
                self._scenario.rx_frequency_mhz,
                self._scenario.tx_frequency_mhz,
                self._scenario.rx_polarization,
                self._scenario.tx_polarization,
            )
            if not self._session.send(frame):
                self._fail(tr("Failed to send RF configuration"))
                return
            self._rf_ack = False
            self._phase = "wait_rf"
            self._deadline = now + 3.0
            self._set_status(tr("Waiting for RF response and telemetry readback"))
        elif self._phase == "wait_rf" and self._rf_ack and self._rf_readback_matches():
            self._phase = "running"
            self._last_tick = now
            self._set_status(tr("Running; TX is inhibited by device firmware"))

        if self._phase == "running":
            self._position_s += max(0.0, now - self._last_tick)
        self._last_tick = now
        if self._position_s >= self._scenario.duration_s:
            self.stop()
            return
        self._send_sample()
        self._timeline.blockSignals(True)
        self._timeline.setValue(round(10000 * self._position_s / self._scenario.duration_s))
        self._timeline.blockSignals(False)
        self._refresh_controls()

    def _send_sample(self) -> None:
        assert self._scenario is not None and self._engine is not None
        pointing_error, offaxis, fresh = self._beam_facts()
        scan_loss = ka256_scan_loss_db(self._scenario.rx_frequency_mhz, offaxis)
        sample = self._engine.sample(
            self._position_s,
            pointing_error_deg=pointing_error,
            offaxis_deg=offaxis,
            scan_loss_db=scan_loss,
            beam_fresh=fresh,
        )
        if not self._session.send(build_tracking_simulation_frame(1, self._session_id, self._scenario, sample)):
            self._fail(tr("Failed to send simulator SAMPLE"))

    def _beam_facts(self) -> tuple[float, float, bool]:
        hw_type = self._session.profile_store.current_hw_type()
        if not hw_type:
            return 0.0, 0.0, False
        error_channel = self._session.profile_store.find_channel_by_role(hw_type, "pointing_error")
        if error_channel is None:
            return 0.0, 0.0, False
        buffer = self._session.data_store.get_channel_by_id(error_channel.channel_id)
        received = self._session.data_store.last_received_monotonic(error_channel.channel_id)
        latest = None if buffer is None else buffer.get_latest()
        if latest is None or received is None or time.monotonic() - received > 0.250:
            return 0.0, 0.0, False
        operation = self._session.product_snapshot().operation
        az = operation.beam_az_deg.value
        el = operation.beam_el_deg.value
        if az is None or el is None:
            return float(latest[1]), 0.0, False
        return abs(float(latest[1])), math.hypot(float(az), float(el)), True

    def _simulation_active(self) -> bool:
        hw_type = self._session.profile_store.current_hw_type()
        if not hw_type:
            return False
        state = self._session.profile_store.find_state_by_role(hw_type, "tracking_sim_active")
        return state is not None and self._session.state_store.get_value(hw_type, state.state_id) == 1

    def _tx_gate_is_off(self) -> bool:
        value = self._session.product_snapshot().operation.tx_enabled
        return value.availability not in {Availability.UNSUPPORTED, Availability.PENDING} and value.value is False

    def _rf_readback_matches(self) -> bool:
        assert self._scenario is not None
        operation = self._session.product_snapshot().operation
        values = (operation.rx_frequency_mhz.value, operation.tx_frequency_mhz.value, operation.rx_polarization.value, operation.tx_polarization.value)
        if any(value is None for value in values):
            return False
        return math.isclose(float(values[0]), self._scenario.rx_frequency_mhz, abs_tol=0.01) and math.isclose(float(values[1]), self._scenario.tx_frequency_mhz, abs_tol=0.01) and int(values[2]) == self._scenario.rx_polarization and int(values[3]) == self._scenario.tx_polarization

    @Slot(object)
    def _on_control_response(self, response: object) -> None:
        if self._phase != "wait_rf" or getattr(response, "request_id", None) != self._rf_request_id or getattr(response, "operation", None) != int(ServiceControlOp.APPLY_RF):
            return
        self._rf_ack = getattr(response, "result_code", None) == int(ServiceResultCode.ACCEPTED)
        if not self._rf_ack:
            self._fail(tr("Device rejected RF configuration"))

    @Slot(bool)
    def _on_connection_changed(self, connected: bool) -> None:
        if not connected and self._phase != "stopped":
            self._finish_local(tr("Connection lost; device timeout will stop simulation"))

    @Slot(int)
    def _seek_stopped(self, value: int) -> None:
        if self._phase == "stopped" and self._scenario is not None:
            self._position_s = self._scenario.duration_s * int(value) / 10000.0

    @Slot()
    def _open_scenario(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("Open scenario"), "", "JSON (*.json)")
        if path:
            with open(path, "r", encoding="utf-8") as stream:
                self._editor.setPlainText(json.dumps(json.load(stream), ensure_ascii=False, indent=2))

    @Slot()
    def _save_scenario(self) -> None:
        try:
            scenario = TrackingScenario.from_dict(json.loads(self._editor.toPlainText()))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            QMessageBox.warning(self, tr("Invalid scenario"), str(error))
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Save scenario"), f"{scenario.name}.json", "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(scenario.to_dict(), stream, ensure_ascii=False, indent=2)

    def _fail(self, message: str) -> None:
        if self._session_id and self._scenario is not None:
            self._session.send(build_tracking_simulation_frame(2, self._session_id, self._scenario))
        self._finish_local(message)

    def _finish_local(self, message: str) -> None:
        self._phase = "stopped"
        self._session.release_device_transaction(self._lease_token)
        self._session_id = 0
        self._rf_request_id = None
        self._set_status(message)
        self._refresh_controls()

    def _set_status(self, message: str) -> None:
        self._status.setText(message)
        self.status_message.emit(message, 5000)

    def _refresh_controls(self) -> None:
        stopped = self._phase == "stopped"
        self._editor.setReadOnly(not stopped)
        self._open.setEnabled(stopped)
        self._save.setEnabled(stopped)
        self._start.setEnabled(stopped or self._phase == "paused")
        self._pause.setEnabled(self._phase == "running")
        self._stop.setEnabled(not stopped)
        self._timeline.setEnabled(stopped)

    def activate(self) -> None:
        pass

    def deactivate(self) -> None:
        pass

    def set_theme(self, _theme: str, _scale: str = "small") -> None:
        pass
