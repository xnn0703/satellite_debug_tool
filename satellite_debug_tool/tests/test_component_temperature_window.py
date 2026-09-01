"""Focused Store and UI tests for component temperature history windows."""

from __future__ import annotations

import math

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from satellite_debug_tool.core.product import (
    COMPONENT_TEMPERATURE_HISTORY_SECONDS,
    ProductServiceStore,
)
from satellite_debug_tool.core.protocol import (
    ServiceComponentHealth,
    ServiceComponentValue,
)
from satellite_debug_tool.tests.test_customer_overview_view import (
    _LiveDouble,
    _SettingsDouble,
)
from satellite_debug_tool.ui.customer_overview_view import CustomerOverviewView


def _component(temperature_c: float, *, temperature_valid: bool = True):
    return ServiceComponentValue(
        (1 << 0) | ((1 << 1) if temperature_valid else 0),
        True,
        temperature_c,
        0.0,
        0,
    )


def _health(
    timestamp_ms: int,
    converter: ServiceComponentValue,
    tx_array: ServiceComponentValue,
    rx_array: ServiceComponentValue,
) -> ServiceComponentHealth:
    return ServiceComponentHealth(
        1,
        timestamp_ms,
        converter,
        tx_array,
        rx_array,
    )


def test_component_temperature_history_keeps_gaps_rollover_and_clear(
    qapplication_session,
) -> None:
    store = ProductServiceStore()
    store.feed(
        _health(
            0xFFFFFF00,
            _component(40.0),
            _component(41.0),
            _component(42.0),
        )
    )
    store.feed(
        _health(
            0x00000100,
            _component(0.0, temperature_valid=False),
            _component(float("nan")),
            _component(43.0),
        )
    )

    times, converter = store.component_temperature_history("converter")
    _, tx_array = store.component_temperature_history("tx_array")
    _, rx_array = store.component_temperature_history("rx_array")

    assert times.tolist() == pytest.approx(
        [0xFFFFFF00 / 1000.0, 0x100000100 / 1000.0]
    )
    assert converter[0] == pytest.approx(40.0)
    assert math.isnan(float(converter[1]))
    assert tx_array[0] == pytest.approx(41.0)
    assert math.isnan(float(tx_array[1]))
    assert rx_array.tolist() == pytest.approx([42.0, 43.0])

    store.feed(
        _health(
            0x00000080,
            _component(99.0),
            _component(99.0),
            _component(99.0),
        )
    )
    assert store.component_temperature_history("converter")[0].size == 2

    store.clear()
    for component in ("converter", "tx_array", "rx_array"):
        empty_times, empty_values = store.component_temperature_history(component)
        assert not empty_times.size
        assert not empty_values.size


def test_component_temperature_history_is_time_bounded(qapplication_session) -> None:
    store = ProductServiceStore()
    first = _health(
        0,
        _component(20.0),
        _component(21.0),
        _component(22.0),
    )
    beyond_window = _health(
        int(COMPONENT_TEMPERATURE_HISTORY_SECONDS * 1000.0) + 1,
        _component(30.0),
        _component(31.0),
        _component(32.0),
    )
    store.feed(first)
    store.feed(beyond_window)

    times, values = store.component_temperature_history("converter")

    assert times.size == 1
    assert values.tolist() == pytest.approx([30.0])
    with pytest.raises(ValueError, match="unknown Product component"):
        store.component_temperature_history("unknown")


def test_customer_overview_opens_three_reusable_native_windows_from_store_only(
    qapplication_session,
) -> None:
    live = _LiveDouble()
    live.products.feed(
        _health(
            1000,
            _component(45.0),
            _component(46.0),
            _component(47.0),
        )
    )
    live.products.feed(
        _health(
            2000,
            _component(45.5),
            _component(46.5),
            _component(47.5),
        )
    )
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)

    def unexpected_device_action(*_args, **_kwargs):
        raise AssertionError("temperature history presentation must remain read-only")

    for action in (
        "connect_udp",
        "disconnect_device",
        "toggle_recording",
        "toggle_customer_recording",
        "show_gnss_details",
        "set_orbit_sky_consumer",
        "select_orbit_tracking_target",
    ):
        setattr(live, action, unexpected_device_action)
    view.resize(1100, 760)
    view.show()
    qapplication_session.processEvents()

    try:
        for component in ("converter", "tx_array", "rx_array"):
            QTest.mouseClick(
                view._component_widgets[component],
                Qt.MouseButton.LeftButton,
            )
        qapplication_session.processEvents()

        assert set(view._temperature_windows) == {
            "converter",
            "tx_array",
            "rx_array",
        }
        for component, window in view._temperature_windows.items():
            assert window.isVisible()
            assert window.isWindow()
            assert window.component_key == component
            assert window.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
            assert window.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint
            assert window.windowFlags() & Qt.WindowType.WindowCloseButtonHint
            available = window.screen().availableGeometry()
            assert available.contains(window.geometry().topLeft())
            assert available.contains(window.geometry().bottomRight())
            times, values = window._curve.getData()
            assert times.tolist() == pytest.approx([1.0, 2.0])
            expected = {
                "converter": (45.0, 45.5),
                "tx_array": (46.0, 46.5),
                "rx_array": (47.0, 47.5),
            }[component]
            assert values.tolist() == pytest.approx(expected)

        converter_window = view._temperature_windows["converter"]
        QTest.mouseClick(
            view._component_widgets["converter"],
            Qt.MouseButton.LeftButton,
        )
        assert view._temperature_windows["converter"] is converter_window
        assert len(view._temperature_windows) == 3

        converter_window.close()
        qapplication_session.processEvents()
        assert not converter_window.isVisible()
        QTest.mouseClick(
            view._component_widgets["converter"],
            Qt.MouseButton.LeftButton,
        )
        qapplication_session.processEvents()
        assert view._temperature_windows["converter"] is converter_window
        assert converter_window.isVisible()

        live.products.clear()
        qapplication_session.processEvents()
        for window in view._temperature_windows.values():
            times, values = window._curve.getData()
            assert times is None or not times.size
            assert values is None or not values.size

        live.products.feed(
            _health(
                100,
                _component(35.0),
                _component(36.0),
                _component(37.0),
            )
        )
        qapplication_session.processEvents()
        times, values = converter_window._curve.getData()
        assert times.tolist() == pytest.approx([0.1])
        assert values.tolist() == pytest.approx([35.0])

        view.set_theme("light")
        assert all(window._theme == "light" for window in view._temperature_windows.values())
    finally:
        for window in view._temperature_windows.values():
            window.close()
        view.close()
