"""Guarded AFD01 RF controls backed by product-service readback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.product import Availability, ControlMode, ProductSnapshot
from satellite_debug_tool.core.protocol import (
    ServiceControlOp,
    ServiceControlResponse,
    ServiceResultCode,
    build_service_apply_rf,
    build_service_set_control_mode,
    build_service_set_tx_enable,
)
from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.ui import styles as S


@dataclass
class _PendingControl:
    request_id: int
    operation: ServiceControlOp
    expected: Any
    response_received: bool = False


def _result_text(result: ServiceResultCode) -> str:
    if result == ServiceResultCode.INVALID_REQUEST:
        return tr("Invalid request")
    if result == ServiceResultCode.OUT_OF_RANGE:
        return tr("Value outside the device range")
    if result == ServiceResultCode.STATE_NOT_ALLOWED:
        return tr("Operation is not allowed in the current mode")
    if result == ServiceResultCode.NOT_SUPPORTED:
        return tr("Operation is not supported by this firmware")
    if result == ServiceResultCode.BUSY:
        return tr("Device is busy")
    if result == ServiceResultCode.INTERNAL_ERROR:
        return tr("Device internal error")
    return tr("Device rejected the command")


class CustomerRfControlView(QWidget):
    """Typed controls that never infer success from an uncorrelated response."""

    status_message = Signal(str, int)

    def __init__(self, live_view, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._live = live_view
        self._store = live_view.product_store()
        self._theme = "dark"
        self._pending: Optional[_PendingControl] = None
        self._rf_dirty = False
        self._last_snapshot = ProductSnapshot()
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        self._build_ui()
        self._store.updated.connect(self._on_store_updated)
        self._store.control_response.connect(self._on_control_response)
        self._live.connection_state_changed.connect(lambda _connected: self.refresh())
        self.refresh()
        register_translatable(self)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 18)
        root.setSpacing(14)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self._title = QLabel(tr("RF control"))
        self._title.setObjectName("customerPageTitle")
        self._subtitle = QLabel(tr("Manual controls become available after device readback confirms manual mode."))
        self._subtitle.setObjectName("customerPageSubtitle")
        self._subtitle.setWordWrap(True)
        title_col.addWidget(self._title)
        title_col.addWidget(self._subtitle)
        header.addLayout(title_col, 1)
        self._service_state = QLabel(tr("Waiting for AFD01 service"))
        self._service_state.setObjectName("customerServiceState")
        header.addWidget(self._service_state)
        root.addLayout(header)

        mode_band = QFrame()
        mode_band.setObjectName("customerControlBand")
        mode_layout = QHBoxLayout(mode_band)
        mode_layout.setContentsMargins(14, 12, 14, 12)
        mode_layout.setSpacing(10)
        mode_text = QVBoxLayout()
        mode_text.setSpacing(2)
        self._mode_title = QLabel(tr("Tracking mode"))
        self._mode_title.setObjectName("customerControlTitle")
        self._mode_readback = QLabel("—")
        self._mode_readback.setObjectName("customerReadback")
        mode_text.addWidget(self._mode_title)
        mode_text.addWidget(self._mode_readback)
        mode_layout.addLayout(mode_text, 1)
        self._auto_btn = QPushButton(tr("Automatic"))
        self._manual_btn = QPushButton(tr("Manual"))
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        for mode, button in ((ControlMode.AUTO, self._auto_btn), (ControlMode.MANUAL, self._manual_btn)):
            button.setObjectName("customerSegment")
            button.setCheckable(True)
            button.setMinimumSize(118, 34)
            self._mode_group.addButton(button)
            button.clicked.connect(lambda _checked, target=mode: self._request_mode(target))
            mode_layout.addWidget(button)
        root.addWidget(mode_band)

        rf_band = QFrame()
        rf_band.setObjectName("customerControlBand")
        rf_layout = QVBoxLayout(rf_band)
        rf_layout.setContentsMargins(14, 12, 14, 14)
        rf_layout.setSpacing(10)
        rf_head = QHBoxLayout()
        self._rf_title = QLabel(tr("Frequency and polarization"))
        self._rf_title.setObjectName("customerControlTitle")
        rf_head.addWidget(self._rf_title)
        rf_head.addStretch(1)
        self._rf_readback = QLabel("—")
        self._rf_readback.setObjectName("customerReadback")
        rf_head.addWidget(self._rf_readback)
        rf_layout.addLayout(rf_head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self._rx_freq_label = QLabel(tr("RX frequency"))
        self._tx_freq_label = QLabel(tr("TX frequency"))
        self._rx_pol_label = QLabel(tr("RX polarization"))
        self._tx_pol_label = QLabel(tr("TX polarization"))
        self._rx_freq = self._frequency_input()
        self._tx_freq = self._frequency_input()
        self._rx_polar = self._polarization_combo()
        self._tx_polar = self._polarization_combo()
        grid.addWidget(self._rx_freq_label, 0, 0)
        grid.addWidget(self._rx_freq, 0, 1)
        grid.addWidget(self._rx_pol_label, 0, 2)
        grid.addWidget(self._rx_polar, 0, 3)
        grid.addWidget(self._tx_freq_label, 1, 0)
        grid.addWidget(self._tx_freq, 1, 1)
        grid.addWidget(self._tx_pol_label, 1, 2)
        grid.addWidget(self._tx_polar, 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        rf_layout.addLayout(grid)
        action_row = QHBoxLayout()
        self._range_hint = QLabel("—")
        self._range_hint.setObjectName("customerRangeHint")
        self._range_hint.setWordWrap(True)
        action_row.addWidget(self._range_hint, 1)
        self._apply_rf_btn = QPushButton(tr("Apply RF settings"))
        self._apply_rf_btn.setMinimumHeight(34)
        self._apply_rf_btn.clicked.connect(self._request_rf)
        action_row.addWidget(self._apply_rf_btn)
        rf_layout.addLayout(action_row)
        root.addWidget(rf_band)

        tx_band = QFrame()
        tx_band.setObjectName("customerControlBand")
        tx_layout = QHBoxLayout(tx_band)
        tx_layout.setContentsMargins(14, 12, 14, 12)
        tx_text = QVBoxLayout()
        tx_text.setSpacing(2)
        self._tx_title = QLabel(tr("Transmit output"))
        self._tx_title.setObjectName("customerControlTitle")
        self._tx_readback = QLabel("—")
        self._tx_readback.setObjectName("customerReadback")
        tx_text.addWidget(self._tx_title)
        tx_text.addWidget(self._tx_readback)
        tx_layout.addLayout(tx_text, 1)
        self._tx_enable = QCheckBox(tr("Transmit enabled"))
        self._tx_enable.setMinimumHeight(34)
        self._tx_enable.clicked.connect(self._request_tx)
        tx_layout.addWidget(self._tx_enable)
        root.addWidget(tx_band)

        self._transaction_state = QLabel(tr("No pending operation"))
        self._transaction_state.setObjectName("customerTransactionState")
        self._transaction_state.setWordWrap(True)
        root.addWidget(self._transaction_state)
        root.addStretch(1)

    def _frequency_input(self) -> QDoubleSpinBox:
        field = QDoubleSpinBox()
        field.setDecimals(3)
        field.setSingleStep(1.0)
        field.setSuffix(" MHz")
        field.setKeyboardTracking(False)
        field.setRange(0.0, 100000.0)
        field.valueChanged.connect(self._mark_rf_dirty)
        return field

    def _polarization_combo(self) -> QComboBox:
        combo = QComboBox()
        for source, value in (
            ("Vertical", 0),
            ("Horizontal", 1),
            ("Left circular", 2),
            ("Right circular", 3),
        ):
            combo.addItem(tr(source), value)
        combo.currentIndexChanged.connect(self._mark_rf_dirty)
        return combo

    def _mark_rf_dirty(self, *_args) -> None:
        self._rf_dirty = True

    def _on_store_updated(self) -> None:
        self.refresh()
        self._check_readback()

    def _is_afd01(self) -> bool:
        hw_type = self._live.profile_store().current_hw_type()
        return isinstance(hw_type, str) and hw_type.lower() == "afd01"

    def refresh(self) -> None:
        snapshot = self._store.snapshot()
        self._last_snapshot = snapshot
        service_ready = self._live.is_connected() and self._is_afd01() and self._store.service_available
        self._service_state.setText(tr("AFD01 service online") if service_ready else tr("Waiting for AFD01 service"))
        self._service_state.setProperty("online", service_ready)
        self._repolish(self._service_state)

        op = snapshot.operation
        mode_valid = op.control_mode.availability == Availability.VALID and op.control_mode.value is not None
        actual_mode = op.control_mode.value if mode_valid else ControlMode.UNKNOWN
        with QSignalBlocker(self._auto_btn), QSignalBlocker(self._manual_btn):
            self._auto_btn.setChecked(actual_mode == ControlMode.AUTO)
            self._manual_btn.setChecked(actual_mode == ControlMode.MANUAL)
        mode_text = {
            ControlMode.AUTO: tr("Device readback: automatic"),
            ControlMode.MANUAL: tr("Device readback: manual"),
        }.get(actual_mode, tr("Device readback unavailable"))
        self._mode_readback.setText(mode_text)

        pending = self._pending is not None
        mode_controls = service_ready and mode_valid and not pending
        self._auto_btn.setEnabled(mode_controls)
        self._manual_btn.setEnabled(mode_controls)
        manual_confirmed = mode_valid and actual_mode == ControlMode.MANUAL

        caps = snapshot.rf_capabilities
        cap_values_valid = all(
            value.availability == Availability.VALID and value.value is not None
            for value in (
                caps.rx_frequency_min_mhz,
                caps.rx_frequency_max_mhz,
                caps.tx_frequency_min_mhz,
                caps.tx_frequency_max_mhz,
                caps.polarization_mask,
            )
        )
        if cap_values_valid:
            self._set_frequency_ranges(snapshot)
            self._set_polarization_mask(int(caps.polarization_mask.value))
        can_edit_rf = service_ready and manual_confirmed and cap_values_valid and not pending
        for widget in (self._rx_freq, self._tx_freq, self._rx_polar, self._tx_polar, self._apply_rf_btn):
            widget.setEnabled(can_edit_rf)

        can_tx = (
            service_ready
            and manual_confirmed
            and caps.tx_control.availability == Availability.VALID
            and bool(caps.tx_control.value)
            and not pending
        )
        self._tx_enable.setEnabled(can_tx)
        if op.tx_enabled.value is not None:
            with QSignalBlocker(self._tx_enable):
                self._tx_enable.setChecked(bool(op.tx_enabled.value))
            self._tx_readback.setText(
                tr("Device readback: enabled") if op.tx_enabled.value else tr("Device readback: disabled")
            )
        else:
            self._tx_readback.setText(tr("Device readback unavailable"))

        self._refresh_rf_readback(snapshot)
        if not self._rf_dirty and not pending:
            self._load_rf_inputs(snapshot)

    def _set_frequency_ranges(self, snapshot: ProductSnapshot) -> None:
        caps = snapshot.rf_capabilities
        rx_min = float(caps.rx_frequency_min_mhz.value)
        rx_max = float(caps.rx_frequency_max_mhz.value)
        tx_min = float(caps.tx_frequency_min_mhz.value)
        tx_max = float(caps.tx_frequency_max_mhz.value)
        with QSignalBlocker(self._rx_freq), QSignalBlocker(self._tx_freq):
            self._rx_freq.setRange(rx_min, rx_max)
            self._tx_freq.setRange(tx_min, tx_max)
        self._range_hint.setText(
            tr(
                "Allowed ranges: RX {rx_min:.0f}-{rx_max:.0f} MHz; TX {tx_min:.0f}-{tx_max:.0f} MHz",
                rx_min=rx_min,
                rx_max=rx_max,
                tx_min=tx_min,
                tx_max=tx_max,
            )
        )

    def _set_polarization_mask(self, mask: int) -> None:
        for combo in (self._rx_polar, self._tx_polar):
            first_supported = -1
            with QSignalBlocker(combo):
                for index in range(combo.count()):
                    value = int(combo.itemData(index))
                    supported = bool(mask & (1 << value))
                    item = combo.model().item(index)
                    if item is not None:
                        item.setEnabled(supported)
                    if supported and first_supported < 0:
                        first_supported = index
                current = int(combo.currentData()) if combo.currentData() is not None else -1
                if not (mask & (1 << current)) and first_supported >= 0:
                    combo.setCurrentIndex(first_supported)

    def _load_rf_inputs(self, snapshot: ProductSnapshot) -> None:
        op = snapshot.operation
        with QSignalBlocker(self._rx_freq):
            with QSignalBlocker(self._tx_freq):
                with QSignalBlocker(self._rx_polar):
                    with QSignalBlocker(self._tx_polar):
                        if op.rx_frequency_mhz.value is not None:
                            self._rx_freq.setValue(float(op.rx_frequency_mhz.value))
                        if op.tx_frequency_mhz.value is not None:
                            self._tx_freq.setValue(float(op.tx_frequency_mhz.value))
                        if op.rx_polarization.value is not None:
                            index = self._rx_polar.findData(int(op.rx_polarization.value))
                            if index >= 0:
                                self._rx_polar.setCurrentIndex(index)
                        if op.tx_polarization.value is not None:
                            index = self._tx_polar.findData(int(op.tx_polarization.value))
                            if index >= 0:
                                self._tx_polar.setCurrentIndex(index)

    def _refresh_rf_readback(self, snapshot: ProductSnapshot) -> None:
        op = snapshot.operation
        if op.rx_frequency_mhz.value is None or op.tx_frequency_mhz.value is None:
            self._rf_readback.setText(tr("Device readback unavailable"))
            return
        rx_pol = self._polarization_text(op.rx_polarization.value)
        tx_pol = self._polarization_text(op.tx_polarization.value)
        self._rf_readback.setText(
            tr(
                "Readback RX {rx:.3f} MHz {rx_pol}; TX {tx:.3f} MHz {tx_pol}",
                rx=float(op.rx_frequency_mhz.value),
                rx_pol=rx_pol,
                tx=float(op.tx_frequency_mhz.value),
                tx_pol=tx_pol,
            )
        )

    @staticmethod
    def _polarization_text(value: Any) -> str:
        return {
            0: tr("Vertical"),
            1: tr("Horizontal"),
            2: tr("Left circular"),
            3: tr("Right circular"),
        }.get(value, "—")

    def _request_mode(self, target: ControlMode) -> None:
        if self._pending is not None or target == self._last_snapshot.operation.control_mode.value:
            self.refresh()
            return
        request_id = self._live.next_product_request_id()
        frame = build_service_set_control_mode(request_id, 1 if target == ControlMode.MANUAL else 0)
        self._begin_request(request_id, ServiceControlOp.SET_CONTROL_MODE, target, frame)

    def _request_rf(self) -> None:
        if self._pending is not None:
            return
        expected = (
            self._rx_freq.value(),
            self._tx_freq.value(),
            int(self._rx_polar.currentData()),
            int(self._tx_polar.currentData()),
        )
        request_id = self._live.next_product_request_id()
        frame = build_service_apply_rf(request_id, *expected)
        self._begin_request(request_id, ServiceControlOp.APPLY_RF, expected, frame)

    def _request_tx(self, checked: bool) -> None:
        actual = bool(self._last_snapshot.operation.tx_enabled.value)
        if checked == actual:
            return
        if checked:
            answer = QMessageBox.warning(
                self,
                tr("Enable transmit output"),
                tr("Confirm that the RF path and test environment are ready before enabling transmit output."),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                with QSignalBlocker(self._tx_enable):
                    self._tx_enable.setChecked(actual)
                return
        request_id = self._live.next_product_request_id()
        frame = build_service_set_tx_enable(request_id, checked)
        self._begin_request(request_id, ServiceControlOp.SET_TX_ENABLE, bool(checked), frame)

    def _begin_request(self, request_id: int, operation: ServiceControlOp, expected: Any, frame: bytes) -> None:
        self._pending = _PendingControl(request_id, operation, expected)
        if not self._live.send_product_frame(frame):
            self._pending = None
            self._transaction_state.setText(tr("Command could not be sent"))
            self.refresh()
            return
        self._transaction_state.setText(tr("Waiting for device response (request {request_id})", request_id=request_id))
        self._timeout.start(3000)
        self.refresh()

    def _on_control_response(self, response: ServiceControlResponse) -> None:
        pending = self._pending
        if pending is None or response.request_id != pending.request_id or response.operation != int(pending.operation):
            return
        try:
            result = ServiceResultCode(response.result_code)
        except ValueError:
            result = ServiceResultCode.INTERNAL_ERROR
        if result != ServiceResultCode.SUCCESS:
            self._finish_request(False, _result_text(result))
            return
        pending.response_received = True
        self._transaction_state.setText(tr("Command accepted; waiting for applied-value readback"))
        self._timeout.start(5000)
        self._check_readback()

    def _check_readback(self) -> None:
        pending = self._pending
        if pending is None or not pending.response_received:
            return
        op = self._store.snapshot().operation
        matched = False
        if pending.operation == ServiceControlOp.SET_CONTROL_MODE:
            matched = op.control_mode.availability == Availability.VALID and op.control_mode.value == pending.expected
        elif pending.operation == ServiceControlOp.APPLY_RF:
            rx, tx, rx_pol, tx_pol = pending.expected
            matched = (
                op.rx_frequency_mhz.availability == Availability.VALID
                and op.tx_frequency_mhz.availability == Availability.VALID
                and op.rx_polarization.availability == Availability.VALID
                and op.tx_polarization.availability == Availability.VALID
                and abs(float(op.rx_frequency_mhz.value) - rx) <= 0.001
                and abs(float(op.tx_frequency_mhz.value) - tx) <= 0.001
                and int(op.rx_polarization.value) == rx_pol
                and int(op.tx_polarization.value) == tx_pol
            )
        elif pending.operation == ServiceControlOp.SET_TX_ENABLE:
            matched = op.tx_enabled.availability == Availability.VALID and bool(op.tx_enabled.value) == pending.expected
        if matched:
            self._rf_dirty = False
            self._finish_request(True, tr("Applied values confirmed by device"))

    def _on_timeout(self) -> None:
        pending = self._pending
        if pending is None:
            return
        detail = (
            tr("Applied-value readback timed out")
            if pending.response_received
            else tr("Device response timed out")
        )
        self._finish_request(False, detail)

    def _finish_request(self, ok: bool, detail: str) -> None:
        self._timeout.stop()
        self._pending = None
        self._transaction_state.setText(detail)
        self._transaction_state.setProperty("result", "ok" if ok else "error")
        self._repolish(self._transaction_state)
        self.status_message.emit(detail, 3500)
        self.refresh()

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def set_theme(self, theme: str, _scale: str = "small") -> None:
        self._theme = theme
        pal = S.palette(theme)
        self.setStyleSheet(
            f"CustomerRfControlView {{ background: {pal['bg']}; color: {pal['text']}; }}"
            f"#customerPageTitle {{ color: {pal['text']}; font-size: 20px; font-weight: 650; }}"
            f"#customerPageSubtitle, #customerRangeHint {{ color: {pal['text_2']}; }}"
            f"#customerControlBand {{ background: {pal['panel']}; border-top: 1px solid {pal['border']}; "
            f"border-bottom: 1px solid {pal['border']}; }}"
            f"#customerControlTitle {{ color: {pal['text']}; font-weight: 600; }}"
            f"#customerReadback {{ color: {pal['text_2']}; }}"
            f"#customerServiceState {{ color: {pal['text_muted']}; padding: 5px 8px; }}"
            f"#customerServiceState[online='true'] {{ color: {pal['ok']}; }}"
            f"#customerSegment:checked {{ color: {pal['accent_2']}; border-color: {pal['accent_2']}; "
            f"background: {pal['accent_dim']}; }}"
            f"#customerTransactionState {{ color: {pal['text_2']}; padding: 7px 0; }}"
            f"#customerTransactionState[result='ok'] {{ color: {pal['ok']}; }}"
            f"#customerTransactionState[result='error'] {{ color: {pal['err']}; }}"
        )

    def retranslate_ui(self) -> None:
        self._title.setText(tr("RF control"))
        self._subtitle.setText(tr("Manual controls become available after device readback confirms manual mode."))
        self._mode_title.setText(tr("Tracking mode"))
        self._auto_btn.setText(tr("Automatic"))
        self._manual_btn.setText(tr("Manual"))
        self._rf_title.setText(tr("Frequency and polarization"))
        self._rx_freq_label.setText(tr("RX frequency"))
        self._tx_freq_label.setText(tr("TX frequency"))
        self._rx_pol_label.setText(tr("RX polarization"))
        self._tx_pol_label.setText(tr("TX polarization"))
        self._apply_rf_btn.setText(tr("Apply RF settings"))
        self._tx_title.setText(tr("Transmit output"))
        self._tx_enable.setText(tr("Transmit enabled"))
        for combo in (self._rx_polar, self._tx_polar):
            current = combo.currentData()
            for index, source in enumerate(("Vertical", "Horizontal", "Left circular", "Right circular")):
                combo.setItemText(index, tr(source))
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
        self.refresh()
