"""Shared device-session authority for all application workspaces."""

from .device_session import DeviceEndpoint, DeviceSessionCore
from .controllers import DebugController, DebugRequestResult, parse_debug_ack_target
from .ota_controller import (
    OtaCapabilityState,
    OtaController,
    OtaState,
    OtaStatus,
)
from .parameter_controller import (
    ParameterCapabilityState,
    ParameterController,
    ParameterOperation,
    ParameterStatus,
)
from .product_controller import (
    CaptureProfileController,
    CaptureProfileResult,
    DISCOVERY_FAST_ATTEMPTS,
    DISCOVERY_FAST_INTERVAL_MS,
    DISCOVERY_SLOW_INTERVAL_MS,
    PendingProductControl,
    ProductControlController,
    ProductControlStatus,
    ProductSubscriptionController,
)
from .registry import SessionAuthorityError, SessionRegistry

__all__ = [
    "CaptureProfileController",
    "CaptureProfileResult",
    "DISCOVERY_FAST_ATTEMPTS",
    "DISCOVERY_FAST_INTERVAL_MS",
    "DISCOVERY_SLOW_INTERVAL_MS",
    "DeviceEndpoint",
    "DeviceSessionCore",
    "DebugController",
    "DebugRequestResult",
    "OtaCapabilityState",
    "OtaController",
    "OtaState",
    "OtaStatus",
    "ParameterCapabilityState",
    "ParameterController",
    "ParameterOperation",
    "ParameterStatus",
    "PendingProductControl",
    "ProductControlController",
    "ProductControlStatus",
    "ProductSubscriptionController",
    "SessionAuthorityError",
    "SessionRegistry",
    "parse_debug_ack_target",
]
