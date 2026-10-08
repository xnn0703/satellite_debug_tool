"""Registered product RF controls require exact ACK and applied readback."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from satellite_debug_tool.core.product import ControlMode
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.i18n import tr
from satellite_debug_tool.core.protocol import (
    CmdType,
    MetaInfo,
    ServiceCapabilities,
    ServiceControlOp,
    ServiceControlResponse,
    ServiceFastState,
    ServiceIdentity,
    ServiceSlowState,
)
from satellite_debug_tool.core.session import DeviceSessionCore
from satellite_debug_tool.ui.customer_rf_control_view import CustomerRfControlView


class _LiveDouble(QObject):
    connection_state_changed = Signal(bool)

    def __init__(self, *, hardware_type: str = "afd01") -> None:
        super().__init__()
        self._profiles = ProfileStore()
        self._hardware_type = hardware_type
        self._profiles.apply_meta(MetaInfo(2, "0.0.130", hardware_type, "AFD01-TEST"))
        self._session = DeviceSessionCore(profile_store=self._profiles)
        self._products = self._session.product_store
        self.sent: list[bytes] = []
        self._session.attach_transport(self, self.send_product_frame)

    def profile_store(self):
        return self._profiles

    def product_store(self):
        return self._products

    def session_core(self):
        return self._session

    def customer_service_state(self):
        return self._session.customer_service_state()

    def is_connected(self):
        return True

    def send_product_frame(self, frame: bytes):
        self.sent.append(frame)
        return True


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _seed_service(
    live: _LiveDouble,
    *,
    manual: bool = False,
    service_protocol: int = 2,
    capabilities_valid_mask: int = 0xFF,
    feature_flags: int = 0x03,
) -> None:
    live._products.feed(
        ServiceIdentity(
            1,
            1,
            0x17,
            live._hardware_type.upper(),
            "AFD01-TEST",
            "0.0.130",
            "",
            service_protocol,
        )
    )
    live._products.feed(
        ServiceCapabilities(
            1,
            1,
            capabilities_valid_mask,
            17700.0,
            21200.0,
            27500.0,
            31000.0,
            0x0C,
            feature_flags,
            0x01,
        )
    )
    live._products.feed(
        ServiceFastState(
            1, 1, 0xFFF, 1 if manual else 0, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )
    live._products.feed(
        ServiceSlowState(1, 1, 0xFF, 31.8, 118.8, 10.0, 19798.0, 29798.0, 2, 3, False)
    )


def test_manual_controls_wait_for_manual_readback(app) -> None:
    live = _LiveDouble()
    _seed_service(live, manual=False)
    view = CustomerRfControlView(live)
    view.activate_view()

    assert view._auto_btn.isChecked()
    assert not view._rx_freq.isEnabled()
    assert not view._tx_enable.isEnabled()

    view.deactivate_view()
    live._products.feed(
        ServiceFastState(
            1, 2, 0xFFF, 1, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )

    assert view._auto_btn.isChecked()
    assert not view._rx_freq.isEnabled()
    view.activate_view()
    assert view._manual_btn.isChecked()
    assert view._rx_freq.isEnabled()
    assert view._tx_enable.isEnabled()
    assert not view._rx_polar.model().item(0).isEnabled()
    assert not view._rx_polar.model().item(1).isEnabled()
    assert view._rx_polar.model().item(2).isEnabled()
    assert view._rx_polar.model().item(3).isEnabled()


@pytest.mark.parametrize("hardware_type", ("afd01a", "afd01b2"))
def test_new_afd01_variant_rf_controls_follow_registered_service(
    app, hardware_type: str
) -> None:
    live = _LiveDouble(hardware_type=hardware_type)
    _seed_service(live, manual=True, service_protocol=8)
    view = CustomerRfControlView(live)
    view.activate_view()
    assert live.customer_service_state().customer_service_ready
    assert view._rx_freq.isEnabled()
    assert view._tx_enable.isEnabled()


def test_identity_and_capabilities_do_not_claim_control_service_online(app) -> None:
    live = _LiveDouble()
    live._products.feed(
        ServiceIdentity(1, 1, 0x17, "AFD01", "AFD01-TEST", "0.0.130", "", 2)
    )
    live._products.feed(
        ServiceCapabilities(
            1, 1, 0xFF, 17700.0, 21200.0, 27500.0, 31000.0, 0x0C, 0x03, 0x01
        )
    )

    view = CustomerRfControlView(live)
    view.activate_view()

    assert view._service_state.text() == "等待产品服务"
    assert not view._service_state.property("online")
    assert not view._auto_btn.isEnabled()
    assert not view._manual_btn.isEnabled()


def test_mode_request_requires_matching_response_and_readback(app) -> None:
    live = _LiveDouble()
    _seed_service(live, manual=False)
    view = CustomerRfControlView(live)
    view.activate_view()

    view._request_mode(ControlMode.MANUAL)
    assert view._pending is not None
    request_id = view._pending.request_id
    assert live.sent[-1][3] == CmdType.SERVICE_CONTROL_REQUEST
    assert live.sent[-1][11] == ServiceControlOp.SET_CONTROL_MODE

    live._products.feed(
        ServiceControlResponse(
            1, request_id + 1, ServiceControlOp.SET_CONTROL_MODE, 0, 1,
            1, 19798.0, 29798.0, 2, 3, False,
        )
    )
    assert view._pending is not None

    live._products.feed(
        ServiceControlResponse(
            1, request_id, ServiceControlOp.SET_CONTROL_MODE, 0, 1,
            1, 19798.0, 29798.0, 2, 3, False,
        )
    )
    assert view._pending is not None
    assert view._pending.response_received

    live._products.feed(
        ServiceFastState(
            1, 3, 0xFFF, 1, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )
    assert view._pending is None
    assert view._transaction_state.property("result") == "ok"


@pytest.mark.parametrize(
    ("hardware_type", "service_protocol", "supported"),
    (
        ("afd01", 2, True),
        ("afd01", 6, True),
        ("afd01c", 8, True),
        ("afd01c", 7, False),
        ("esa01", 6, True),
        ("unknown-terminal", 6, False),
    ),
)
def test_rf_controls_follow_registered_customer_product_policy(
    app,
    hardware_type: str,
    service_protocol: int,
    supported: bool,
) -> None:
    live = _LiveDouble(hardware_type=hardware_type)
    _seed_service(live, manual=True, service_protocol=service_protocol)
    view = CustomerRfControlView(live)
    view.activate_view()

    assert live.customer_service_state().customer_service_supported is supported
    assert view._service_state.property("online") is supported
    assert view._rx_freq.isEnabled() is supported
    assert view._tx_enable.isEnabled() is supported


@pytest.mark.parametrize(
    ("capabilities_valid_mask", "feature_flags", "reason"),
    (
        (0xDF, 0x03, "RF control unavailable: independent RX/TX polarization capability is unavailable"),
        (0xFF, 0x02, "RF control unavailable: device does not support independent RX/TX polarization"),
    ),
)
def test_rf_controls_require_independent_polarization_capability(
    app,
    capabilities_valid_mask: int,
    feature_flags: int,
    reason: str,
) -> None:
    live = _LiveDouble()
    _seed_service(
        live,
        manual=True,
        service_protocol=6,
        capabilities_valid_mask=capabilities_valid_mask,
        feature_flags=feature_flags,
    )
    view = CustomerRfControlView(live)
    view.activate_view()

    assert not live.customer_service_state().rf_control_ready
    assert not view._rx_freq.isEnabled()
    assert not view._apply_rf_btn.isEnabled()
    assert view._range_hint.text() == tr(reason)


def test_tx_confirmation_is_rejected_when_connection_changes_in_dialog(
    app,
    monkeypatch,
) -> None:
    live = _LiveDouble()
    _seed_service(live, manual=True)
    view = CustomerRfControlView(live)
    view.activate_view()
    sent_before_confirmation = tuple(live.sent)

    def change_connection(*_args, **_kwargs):
        live.session_core().begin_connection(
            endpoint=("192.168.1.99", 4004),
            transport=live,
            sender=live.send_product_frame,
            handshake_enabled=False,
        )
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "warning", change_connection)

    view._tx_enable.click()

    assert tuple(live.sent) == sent_before_confirmation
    assert not view._tx_enable.isChecked()
    assert view._transaction_state.text() == tr(
        "Operation cancelled because the device session changed"
    )


def test_tx_disable_is_bound_to_the_current_device_scope(app) -> None:
    live = _LiveDouble()
    _seed_service(live, manual=True)
    live._products.feed(
        ServiceFastState(
            1, 2, 0xFFF, 1, 0, False, 3, 3, True,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )
    view = CustomerRfControlView(live)
    view.activate_view()

    assert view._tx_enable.isChecked()
    sent_before = len(live.sent)
    view._request_tx(False)

    assert len(live.sent) == sent_before + 1
    assert live.sent[-1][11] == ServiceControlOp.SET_TX_ENABLE
