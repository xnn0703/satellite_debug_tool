"""Customer multi-device configuration and presentation facts."""

from .device_directory import (
    CustomerDeviceBusyError,
    CustomerDeviceCapacityError,
    CustomerDeviceConfigurationBlocked,
    CustomerDeviceDirectory,
    CustomerDeviceDirectoryError,
    CustomerDeviceLeaseError,
    CustomerDeviceMutationResult,
    CustomerDeviceNotFoundError,
    CustomerDevicePersistenceError,
    CustomerDeviceSnapshot,
    CustomerDeviceSupplementalFacts,
    CustomerDeviceValidationError,
    Endpoint,
    MAX_CUSTOMER_DEVICES,
    SupplementalFactsProvider,
    normalize_customer_endpoint,
)

__all__ = [
    "CustomerDeviceBusyError",
    "CustomerDeviceCapacityError",
    "CustomerDeviceConfigurationBlocked",
    "CustomerDeviceDirectory",
    "CustomerDeviceDirectoryError",
    "CustomerDeviceLeaseError",
    "CustomerDeviceMutationResult",
    "CustomerDeviceNotFoundError",
    "CustomerDevicePersistenceError",
    "CustomerDeviceSnapshot",
    "CustomerDeviceSupplementalFacts",
    "CustomerDeviceValidationError",
    "Endpoint",
    "MAX_CUSTOMER_DEVICES",
    "SupplementalFactsProvider",
    "normalize_customer_endpoint",
]
