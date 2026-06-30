"""LogView virtual profile semantic tests."""

from __future__ import annotations

import numpy as np

from satellite_debug_tool.core.log_parser import WindTermLogResult
from satellite_debug_tool.core.profile import CHANNEL_ROLE_GPS_LAT, ProfileStore
from satellite_debug_tool.ui.log_view import _build_virtual_profile_dict


def test_virtual_profile_generates_channel_roles():
    result = WindTermLogResult(
        columns=["latitude", "yaw"],
        data=np.array([[31.2, 10.0]], dtype=float),
        timestamps_ms=np.array([0], dtype=int),
    )

    profile_dict = _build_virtual_profile_dict(result)
    assert profile_dict["schema_version"] == 2

    store = ProfileStore(cache=None)
    hw = store.import_dict(profile_dict)
    assert hw == "windterm_log"
    assert store.find_channel_by_role("windterm_log", CHANNEL_ROLE_GPS_LAT).channel_id == 0

