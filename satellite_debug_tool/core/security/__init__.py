"""Security primitives used by customer-facing workflows."""

from .firmware_package import (
    FirmwarePackage,
    FirmwarePackageError,
    FirmwarePackageErrorCode,
    TrustedFirmwareKey,
    build_signed_manifest_message,
    load_bundled_trusted_keys,
    verify_firmware_package,
)

__all__ = [
    "FirmwarePackage",
    "FirmwarePackageError",
    "FirmwarePackageErrorCode",
    "TrustedFirmwareKey",
    "build_signed_manifest_message",
    "load_bundled_trusted_keys",
    "verify_firmware_package",
]
