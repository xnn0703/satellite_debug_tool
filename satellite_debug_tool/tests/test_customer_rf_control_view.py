"""AFD01 customer RF controls require exact ACK and applied readback."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from satellite_debug_tool.core.product import ControlMode, ProductServiceStore
from satellite_debug_tool.core.profile import ProfileStore
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
from satellite_debug_tool.ui.customer_rf_control_view import CustomerRfControlView


class _LiveDouble(QObject):
    connection_state_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._profiles = ProfileStore()
        self._profiles.apply_meta(MetaInfo(2, "0.0.130", "afd01", "AFD01-TEST"))
        self._products = ProductServiceStore()
        self.sent: list[bytes] = []
        self.request_id = 0

    def profile_store(self):
        return self._profiles

    def product_store(self):
        return self._products

    def is_connected(self):
        return True

    def next_product_request_id(self):
        self.request_id += 1
        return self.request_id

    def send_product_frame(self, frame: bytes):
        self.sent.append(frame)
        return True


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _seed_service(live: _LiveDouble, *, manual: bool = False) -> None:
    live._products.feed(
        ServiceIdentity(1, 1, 0x17, "AFD01", "AFD01-TEST", "0.0.130", "", 2)
    )
    live._products.feed(
        ServiceCapabilities(
            1, 1, 0xFF, 17700.0, 21200.0, 27500.0, 31000.0, 0x0C, 0x03, 0x01
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

    assert view._auto_btn.isChecked()
    assert not view._rx_freq.isEnabled()
    assert not view._tx_enable.isEnabled()

    live._products.feed(
        ServiceFastState(
            1, 2, 0xFFF, 1, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )

    assert view._manual_btn.isChecked()
    assert view._rx_freq.isEnabled()
    assert view._tx_enable.isEnabled()
    assert not view._rx_polar.model().item(0).isEnabled()
    assert not view._rx_polar.model().item(1).isEnabled()
    assert view._rx_polar.model().item(2).isEnabled()
    assert view._rx_polar.model().item(3).isEnabled()


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

    assert view._service_state.text() == "等待 AFD01 服务"
    assert not view._service_state.property("online")
    assert not view._auto_btn.isEnabled()
    assert not view._manual_btn.isEnabled()


def test_mode_request_requires_matching_response_and_readback(app) -> None:
    live = _LiveDouble()
    _seed_service(live, manual=False)
    view = CustomerRfControlView(live)

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
