"""Security primitives used by customer-facing workflows."""

from .firmware_package import (
    FirmwarePackage,
    FirmwarePackageError,
    FirmwarePackageErrorCode,
    TrustedFirmwareKey,
    build_signed_manifest_message,
    device_firmware_versions_equal,
    firmware_versions_equal,
    load_bundled_trusted_keys,
    validate_firmware_manifest_fields,
    verify_firmware_package,
)

__all__ = [
    "FirmwarePackage",
    "FirmwarePackageError",
    "FirmwarePackageErrorCode",
    "TrustedFirmwareKey",
    "build_signed_manifest_message",
    "device_firmware_versions_equal",
    "firmware_versions_equal",
    "load_bundled_trusted_keys",
    "validate_firmware_manifest_fields",
    "verify_firmware_package",
]
