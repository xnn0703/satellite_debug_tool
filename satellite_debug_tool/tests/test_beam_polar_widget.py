"""Customer beam polar geometry and widget-state tests."""

from __future__ import annotations

import math

import pytest
from PySide6.QtWidgets import QApplication

from satellite_debug_tool.ui.beam_polar_widget import (
    BeamPolarWidget,
    BeamSatelliteMarker,
    beam_endpoint,
)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize(
    ("azimuth", "expected"),
    (
        (0.0, (0.0, -1.0)),
        (90.0, (1.0, 0.0)),
        (180.0, (0.0, 1.0)),
        (270.0, (-1.0, 0.0)),
        (450.0, (1.0, 0.0)),
    ),
)
def test_beam_endpoint_uses_clockwise_azimuth_from_top(azimuth, expected) -> None:
    assert beam_endpoint(azimuth, 90.0) == pytest.approx(expected, abs=1e-8)


def test_beam_endpoint_scales_and_clamps_off_axis_radius() -> None:
    assert beam_endpoint(0.0, 30.0) == pytest.approx((0.0, -1.0 / 3.0))
    assert beam_endpoint(90.0, 120.0) == pytest.approx((1.0, 0.0))
    assert beam_endpoint(0.0, -1.0) is None
    assert beam_endpoint(math.nan, 10.0) is None
    assert beam_endpoint(0.0, math.inf) is None


def test_beam_widget_never_treats_missing_or_nonfinite_data_as_zero(app) -> None:
    widget = BeamPolarWidget()
    widget.set_beam(None, None)
    assert not widget.has_beam()

    widget.set_beam(0.0, math.nan)
    assert not widget.has_beam()

    widget.set_beam(0.0, 0.0, stale=True)
    assert widget.has_beam()


def test_beam_widget_uses_hard_envelope_and_filters_invalid_satellites(app) -> None:
    widget = BeamPolarWidget()
    valid = BeamSatelliteMarker(25544, "ISS", 120.0, 30.0, active_target=True)
    outside = BeamSatelliteMarker(2, "OUT", 10.0, 71.0)

    widget.set_satellites((valid, outside), max_off_axis_deg=70.0)

    assert widget.satellites() == (valid,)
    assert beam_endpoint(90.0, 35.0, max_off_axis_deg=70.0) == pytest.approx((0.5, 0.0))
