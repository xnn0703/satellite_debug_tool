import struct

import pytest

from satellite_debug_tool.core.tracking_simulator import (
    ScenarioEngine,
    TrackingScenario,
    build_tracking_simulation_frame,
    calculate_snr,
    ka256_scan_loss_db,
)


def _scenario() -> TrackingScenario:
    return TrackingScenario.from_dict(
        {
            "schema": 1,
            "name": "turn",
            "duration_s": 10.0,
            "random_seed": 7,
            "satellite_id": 1250,
            "satellite_longitude_deg": 125.0,
            "rx_frequency_mhz": 19450.0,
            "tx_frequency_mhz": 29888.0,
            "rx_polarization": 3,
            "tx_polarization": 2,
            "hpbw_deg": 4.0,
            "hard_limit_deg": 60.0,
            "pose_keyframes": [
                {"time_s": 0.0, "latitude_deg": 31.0, "longitude_deg": 118.0, "altitude_m": 10.0, "yaw_deg": 350.0, "pitch_deg": 0.0, "roll_deg": 0.0},
                {"time_s": 10.0, "latitude_deg": 31.001, "longitude_deg": 118.002, "altitude_m": 20.0, "yaw_deg": 10.0, "pitch_deg": 2.0, "roll_deg": 4.0},
            ],
            "link_keyframes": [
                {"time_s": 0.0, "base_snr_db": 20.0, "rx_online": True},
                {"time_s": 10.0, "base_snr_db": 18.0, "rx_online": False},
            ],
        }
    )


def test_interpolation_uses_shortest_yaw_path_and_derives_rates() -> None:
    sample = ScenarioEngine(_scenario()).sample(5.0, pointing_error_deg=0.0, offaxis_deg=10.0, scan_loss_db=1.0, beam_fresh=True)
    assert sample.yaw_deg == pytest.approx(0.0)
    assert sample.body_rate_z_dps == pytest.approx(2.0)
    assert sample.altitude_m == pytest.approx(15.0)


def test_snr_and_ka256_loss_formula() -> None:
    raw, normalized = calculate_snr(base_snr_db=20.0, scan_loss_db=1.0, pointing_error_deg=2.0, hpbw_deg=4.0, obstruction_loss_db=2.0, rain_loss_db=1.0, noise_db=0.0)
    assert raw == pytest.approx(13.0)
    assert normalized == pytest.approx(14.0)
    assert ka256_scan_loss_db(19450.0, 45.0) == pytest.approx(1.642, abs=0.01)


def test_wire_payload_lengths_are_fixed() -> None:
    model = _scenario()
    sample = ScenarioEngine(model).sample(0.0, pointing_error_deg=0.0, offaxis_deg=0.0, scan_loss_db=0.0, beam_fresh=True)
    start = build_tracking_simulation_frame(0, 9, model, sample)
    stop = build_tracking_simulation_frame(2, 9, model)
    assert struct.unpack_from("<H", start, 4)[0] == 87
    assert struct.unpack_from("<H", stop, 4)[0] == 6
