"""Stable customer-facing product semantics."""

from .legacy_v2 import LegacyV2Projector
from .identity import verified_device_uid, verified_identity_text
from .recording_state import CustomerRecordingState
from .playback_projection import (
    CUSTOMER_PLAYBACK_HW_TYPE,
    CustomerPlaybackChannel,
    CustomerPlaybackProjector,
    customer_channel_entries,
)
from .service_store import (
    COMPONENT_TEMPERATURE_HISTORY_SECONDS,
    ProductServiceStore,
)
from .support_policy import (
    CustomerProductPolicy,
    CustomerServiceState,
    customer_product_policy,
    customer_ota_product_policy,
    customer_service_state,
    product_identity_matches,
    production_product_policy,
    production_recipe_product_policy,
)
from .source_resolver import (
    ProductSnapshotResolver,
    ProductSourceState,
)
from .timestamps import U32UptimeUnwrapper, unwrap_u32_series
from .models import (
    Availability,
    ComponentHealth,
    ControlMode,
    DeviceIdentity,
    ExternalInsDiagnostics,
    ExternalInsState,
    NavigationState,
    NavigationSource,
    NavigationSourceInfo,
    OperationalSnapshot,
    ProductSnapshot,
    ProductSource,
    ProductValue,
    RfCapabilities,
    SatelliteMode,
    TrackingPhase,
    ValueQuality,
    pending_product_snapshot,
    stamp_snapshot_received,
    stamp_snapshot_source,
)

__all__ = [
    "Availability",
    "ComponentHealth",
    "COMPONENT_TEMPERATURE_HISTORY_SECONDS",
    "ControlMode",
    "CustomerProductPolicy",
    "CustomerRecordingState",
    "CustomerServiceState",
    "CUSTOMER_PLAYBACK_HW_TYPE",
    "CustomerPlaybackChannel",
    "CustomerPlaybackProjector",
    "DeviceIdentity",
    "ExternalInsDiagnostics",
    "ExternalInsState",
    "LegacyV2Projector",
    "NavigationState",
    "NavigationSource",
    "NavigationSourceInfo",
    "OperationalSnapshot",
    "ProductSnapshot",
    "ProductSnapshotResolver",
    "ProductSource",
    "ProductSourceState",
    "ProductServiceStore",
    "ProductValue",
    "RfCapabilities",
    "SatelliteMode",
    "TrackingPhase",
    "ValueQuality",
    "U32UptimeUnwrapper",
    "unwrap_u32_series",
    "verified_device_uid",
    "verified_identity_text",
    "customer_channel_entries",
    "customer_product_policy",
    "customer_ota_product_policy",
    "customer_service_state",
    "product_identity_matches",
    "production_product_policy",
    "production_recipe_product_policy",
    "pending_product_snapshot",
    "stamp_snapshot_received",
    "stamp_snapshot_source",
]
