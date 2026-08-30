"""Shared device-session authority for all application workspaces."""

from .device_session import DeviceEndpoint, DeviceSessionCore, DeviceSessionScope
from .controllers import DebugController, DebugRequestResult, parse_debug_ack_target
from .ota_controller import (
    OTA_MAX_IMAGE_BYTES,
    OtaArtifactSource,
    OtaArtifactToken,
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
    SUBSCRIPTION_KEEPALIVE_INTERVAL_MS,
    PendingProductControl,
    PendingMountConfiguration,
    MountConfigurationController,
    MountConfigurationStatus,
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
    "SUBSCRIPTION_KEEPALIVE_INTERVAL_MS",
    "DeviceEndpoint",
    "DeviceSessionCore",
    "DeviceSessionScope",
    "DebugController",
    "DebugRequestResult",
    "OtaCapabilityState",
    "OTA_MAX_IMAGE_BYTES",
    "OtaArtifactSource",
    "OtaArtifactToken",
    "OtaController",
    "OtaState",
    "OtaStatus",
    "ParameterCapabilityState",
    "ParameterController",
    "ParameterOperation",
    "ParameterStatus",
    "PendingProductControl",
    "PendingMountConfiguration",
    "MountConfigurationController",
    "MountConfigurationStatus",
    "ProductControlController",
    "ProductControlStatus",
    "ProductSubscriptionController",
    "SessionAuthorityError",
    "SessionRegistry",
    "parse_debug_ack_target",
]
