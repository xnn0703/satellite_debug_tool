"""Security primitives used by customer-facing workflows."""

from .firmware_package import (
    FirmwareEncryptionKey,
    FirmwarePackage,
    FirmwarePackageError,
    FirmwarePackageErrorCode,
    build_encrypted_manifest_aad,
    create_firmware_package_bytes,
    device_firmware_versions_equal,
    firmware_version_is_older,
    firmware_versions_equal,
    load_bundled_encryption_keys,
    validate_firmware_manifest_fields,
    verify_firmware_package,
)

__all__ = [
    "FirmwareEncryptionKey",
    "FirmwarePackage",
    "FirmwarePackageError",
    "FirmwarePackageErrorCode",
    "build_encrypted_manifest_aad",
    "create_firmware_package_bytes",
    "device_firmware_versions_equal",
    "firmware_version_is_older",
    "firmware_versions_equal",
    "load_bundled_encryption_keys",
    "validate_firmware_manifest_fields",
    "verify_firmware_package",
]
