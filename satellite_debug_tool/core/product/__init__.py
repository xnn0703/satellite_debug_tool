"""Stable customer-facing product semantics."""

from .legacy_v2 import LegacyV2Projector
from .recording_state import CustomerRecordingState
from .playback_projection import (
    CUSTOMER_PLAYBACK_HW_TYPE,
    CustomerPlaybackChannel,
    CustomerPlaybackProjector,
    customer_channel_entries,
)
from .service_store import ProductServiceStore
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
    "ControlMode",
    "CustomerRecordingState",
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
    "customer_channel_entries",
    "pending_product_snapshot",
    "stamp_snapshot_received",
    "stamp_snapshot_source",
]
