"""XESA01 Orbit and TLE domain."""

from __future__ import annotations

from types import MappingProxyType

from ..codec_v2 import (
    build_orbit_capabilities,
    build_orbit_catalog,
    build_orbit_current,
    build_orbit_predict,
    build_orbit_prediction_page,
    build_orbit_scan,
    build_orbit_select,
    build_orbit_sky_snapshot,
    build_orbit_upload_abort,
    build_orbit_upload_begin,
    build_orbit_upload_chunk,
    build_orbit_upload_end,
    decode_orbit_report,
)
from ..frame_v2 import (
    CmdType,
    OrbitCapabilitiesReport,
    OrbitCatalogReport,
    OrbitCurrentReport,
    OrbitPassPage,
    OrbitPredictionAccepted,
    OrbitPredictionPage,
    OrbitSkyReport,
    OrbitStatusReport,
    OrbitUploadProgress,
)


DECODERS = MappingProxyType({
    int(CmdType.ORBIT_REPORT): decode_orbit_report,
})


__all__ = [
    "DECODERS",
    "OrbitCapabilitiesReport",
    "OrbitCatalogReport",
    "OrbitCurrentReport",
    "OrbitPassPage",
    "OrbitPredictionAccepted",
    "OrbitPredictionPage",
    "OrbitSkyReport",
    "OrbitStatusReport",
    "OrbitUploadProgress",
    "build_orbit_capabilities",
    "build_orbit_catalog",
    "build_orbit_current",
    "build_orbit_predict",
    "build_orbit_prediction_page",
    "build_orbit_scan",
    "build_orbit_select",
    "build_orbit_sky_snapshot",
    "build_orbit_upload_abort",
    "build_orbit_upload_begin",
    "build_orbit_upload_chunk",
    "build_orbit_upload_end",
]
