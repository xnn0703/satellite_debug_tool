"""AFD01 Product Service domain."""

from __future__ import annotations

from types import MappingProxyType

from ..codec_v2 import (
    build_service_apply_rf,
    build_service_set_capture_profile,
    build_service_set_control_mode,
    build_service_set_tx_enable,
    build_service_subscribe,
    decode_service_capabilities,
    decode_service_component_health,
    decode_service_control_response,
    decode_service_external_ins_diagnostics,
    decode_service_fast_state,
    decode_service_hardware_identity,
    decode_service_identity,
    decode_service_link_detail,
    decode_service_navigation_source_info,
    decode_service_rf_lock_status,
    decode_service_slow_state,
)
from ..frame_v2 import (
    CmdType,
    ServiceCapabilities,
    ServiceComponentHealth,
    ServiceControlResponse,
    ServiceExternalInsDiagnostics,
    ServiceFastState,
    ServiceHardwareIdentity,
    ServiceIdentity,
    ServiceLinkDetail,
    ServiceNavigationSourceInfo,
    ServiceRfLockStatus,
    ServiceSlowState,
)


DECODERS = MappingProxyType({
    int(CmdType.SERVICE_IDENTITY): decode_service_identity,
    int(CmdType.SERVICE_HARDWARE_IDENTITY): decode_service_hardware_identity,
    int(CmdType.SERVICE_NAV_SOURCE_INFO): decode_service_navigation_source_info,
    int(CmdType.SERVICE_EXTERNAL_INS_DIAGNOSTICS): decode_service_external_ins_diagnostics,
    int(CmdType.SERVICE_FAST_STATE): decode_service_fast_state,
    int(CmdType.SERVICE_SLOW_STATE): decode_service_slow_state,
    int(CmdType.SERVICE_LINK_DETAIL): decode_service_link_detail,
    int(CmdType.SERVICE_RF_LOCK_STATUS): decode_service_rf_lock_status,
    int(CmdType.SERVICE_COMPONENT_HEALTH): decode_service_component_health,
    int(CmdType.SERVICE_CAPABILITIES): decode_service_capabilities,
    int(CmdType.SERVICE_CONTROL_RESPONSE): decode_service_control_response,
})


__all__ = [
    "DECODERS",
    "ServiceCapabilities",
    "ServiceComponentHealth",
    "ServiceControlResponse",
    "ServiceExternalInsDiagnostics",
    "ServiceFastState",
    "ServiceHardwareIdentity",
    "ServiceIdentity",
    "ServiceLinkDetail",
    "ServiceNavigationSourceInfo",
    "ServiceRfLockStatus",
    "ServiceSlowState",
    "build_service_apply_rf",
    "build_service_set_capture_profile",
    "build_service_set_control_mode",
    "build_service_set_tx_enable",
    "build_service_subscribe",
]
