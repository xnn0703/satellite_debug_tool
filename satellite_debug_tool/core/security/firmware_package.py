"""Verification for customer OTA firmware packages.

The archive is intentionally small and rigid. It contains exactly one manifest,
one firmware image named by that manifest, and one detached Ed25519 signature.
The signature authenticates canonical manifest bytes; the manifest SHA-256 and
size fields bind the firmware image.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import zipfile
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import Mapping, Optional, Union


PACKAGE_SCHEMA_VERSION = 1
PACKAGE_MANIFEST_NAME = "manifest.json"
PACKAGE_SIGNATURE_NAME = "signature.ed25519"
PACKAGE_SIGNATURE_DOMAIN = b"SOFTHERTZ-AFD-FIRMWARE-PACKAGE\x00v1\x00"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_FIRMWARE_BYTES = 4 * 1024 * 1024


class FirmwarePackageErrorCode(Enum):
    FORMAT = "format"
    UNSIGNED = "unsigned"
    UNKNOWN_KEY = "unknown_key"
    SIGNATURE_INVALID = "signature_invalid"
    HASH_MISMATCH = "hash_mismatch"
    SIZE_MISMATCH = "size_mismatch"
    PRODUCT_MISMATCH = "product_mismatch"
    HARDWARE_MISMATCH = "hardware_mismatch"
    VERSION_INVALID = "version_invalid"
    VERSION_NOT_ALLOWED = "version_not_allowed"
    CRYPTO_UNAVAILABLE = "crypto_unavailable"
    TRUST_STORE_INVALID = "trust_store_invalid"


class FirmwarePackageError(ValueError):
    def __init__(self, code: FirmwarePackageErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class TrustedFirmwareKey:
    key_id: str
    public_key: bytes
    label: str = ""


@dataclass(frozen=True)
class FirmwarePackage:
    path: Path
    product: str
    hardware_types: tuple[str, ...]
    version: str
    version_policy: str
    firmware_name: str
    firmware: bytes
    firmware_sha256: str
    key_id: str
    key_label: str


def _canonical_manifest(manifest: dict) -> bytes:
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_signed_manifest_message(manifest: dict) -> bytes:
    """Return the exact bytes signed by release tooling."""
    return PACKAGE_SIGNATURE_DOMAIN + _canonical_manifest(manifest)


def _package_error(code: FirmwarePackageErrorCode, detail: str):
    raise FirmwarePackageError(code, detail)


def load_bundled_trusted_keys() -> dict[str, TrustedFirmwareKey]:
    """Load immutable public keys bundled into the application artifact."""
    try:
        data = (
            resources.files("satellite_debug_tool.resources")
            .joinpath("firmware_signing_keys.json")
            .read_text(encoding="utf-8")
        )
        document = json.loads(data)
    except Exception as exc:
        _package_error(
            FirmwarePackageErrorCode.TRUST_STORE_INVALID,
            f"failed to load bundled trust store: {exc}",
        )
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        _package_error(
            FirmwarePackageErrorCode.TRUST_STORE_INVALID,
            "unsupported trust-store schema",
        )
    entries = document.get("keys")
    if not isinstance(entries, list):
        _package_error(
            FirmwarePackageErrorCode.TRUST_STORE_INVALID,
            "trust-store keys must be a list",
        )
    trusted: dict[str, TrustedFirmwareKey] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            _package_error(
                FirmwarePackageErrorCode.TRUST_STORE_INVALID,
                "trust-store key entry must be an object",
            )
        key_id = entry.get("key_id")
        encoded = entry.get("public_key_base64")
        if not isinstance(key_id, str) or not key_id or not isinstance(encoded, str):
            _package_error(
                FirmwarePackageErrorCode.TRUST_STORE_INVALID,
                "trust-store key_id/public_key_base64 is invalid",
            )
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            _package_error(
                FirmwarePackageErrorCode.TRUST_STORE_INVALID,
                f"invalid base64 public key for {key_id}: {exc}",
            )
        if len(raw) != 32 or key_id in trusted:
            _package_error(
                FirmwarePackageErrorCode.TRUST_STORE_INVALID,
                f"invalid or duplicate Ed25519 key {key_id}",
            )
        trusted[key_id] = TrustedFirmwareKey(
            key_id=key_id,
            public_key=raw,
            label=str(entry.get("label") or key_id),
        )
    return trusted


_VERSION_PATTERN = re.compile(
    r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:[-_]?([0-9A-Za-z.-]+))?$"
)


def _version_key(value: str) -> tuple[int, int, int, int, str]:
    match = _VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        _package_error(
            FirmwarePackageErrorCode.VERSION_INVALID,
            f"invalid semantic version: {value!r}",
        )
    major, minor, patch = (int(match.group(index)) for index in (1, 2, 3))
    suffix = match.group(4) or ""
    return major, minor, patch, 1 if not suffix else 0, suffix.lower()


def _check_version_policy(package_version: str, current_version: str, policy: str) -> None:
    package_key = _version_key(package_version)
    current_key = _version_key(current_version)
    allowed = {
        "upgrade_only": package_key > current_key,
        "allow_same": package_key >= current_key,
        "allow_downgrade": True,
    }.get(policy)
    if allowed is None:
        _package_error(
            FirmwarePackageErrorCode.FORMAT,
            f"unsupported version_policy: {policy!r}",
        )
    if not allowed:
        _package_error(
            FirmwarePackageErrorCode.VERSION_NOT_ALLOWED,
            f"package {package_version} is not allowed over current {current_version} ({policy})",
        )


def _required_string(manifest: dict, name: str) -> str:
    value = manifest.get(name)
    if not isinstance(value, str) or not value.strip():
        _package_error(FirmwarePackageErrorCode.FORMAT, f"manifest {name} is required")
    return value.strip()


def verify_firmware_package(
    path: Union[str, Path],
    *,
    expected_product: str,
    expected_hardware: str,
    current_version: str,
    trusted_keys: Optional[Mapping[str, TrustedFirmwareKey]] = None,
) -> FirmwarePackage:
    """Authenticate and validate a customer firmware package without extracting it."""
    package_path = Path(path)
    keys = dict(load_bundled_trusted_keys() if trusted_keys is None else trusted_keys)
    try:
        archive = zipfile.ZipFile(package_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        _package_error(FirmwarePackageErrorCode.FORMAT, f"invalid package archive: {exc}")

    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            _package_error(FirmwarePackageErrorCode.FORMAT, "duplicate archive entry")
        if PACKAGE_MANIFEST_NAME not in names:
            _package_error(FirmwarePackageErrorCode.FORMAT, "manifest.json is missing")
        if PACKAGE_SIGNATURE_NAME not in names:
            _package_error(FirmwarePackageErrorCode.UNSIGNED, "signature.ed25519 is missing")
        manifest_info = archive.getinfo(PACKAGE_MANIFEST_NAME)
        if manifest_info.file_size > MAX_MANIFEST_BYTES:
            _package_error(FirmwarePackageErrorCode.FORMAT, "manifest is too large")
        try:
            manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
        except Exception as exc:
            _package_error(FirmwarePackageErrorCode.FORMAT, f"invalid manifest JSON: {exc}")
        if not isinstance(manifest, dict) or manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION:
            _package_error(FirmwarePackageErrorCode.FORMAT, "unsupported package schema")

        product = _required_string(manifest, "product")
        version = _required_string(manifest, "version")
        version_policy = _required_string(manifest, "version_policy")
        firmware_name = _required_string(manifest, "firmware")
        firmware_sha256 = _required_string(manifest, "firmware_sha256").lower()
        key_id = _required_string(manifest, "key_id")
        hardware = manifest.get("hardware_types")
        firmware_size = manifest.get("firmware_size")
        if (
            not isinstance(hardware, list)
            or not hardware
            or not all(isinstance(item, str) and item.strip() for item in hardware)
        ):
            _package_error(FirmwarePackageErrorCode.FORMAT, "hardware_types is invalid")
        if not isinstance(firmware_size, int) or firmware_size <= 0:
            _package_error(FirmwarePackageErrorCode.FORMAT, "firmware_size is invalid")
        if (
            Path(firmware_name).name != firmware_name
            or firmware_name in {PACKAGE_MANIFEST_NAME, PACKAGE_SIGNATURE_NAME}
            or firmware_name not in names
        ):
            _package_error(FirmwarePackageErrorCode.FORMAT, "firmware entry is invalid")
        expected_entries = {PACKAGE_MANIFEST_NAME, PACKAGE_SIGNATURE_NAME, firmware_name}
        if set(names) != expected_entries:
            _package_error(FirmwarePackageErrorCode.FORMAT, "package contains unexpected entries")

        firmware_info = archive.getinfo(firmware_name)
        if firmware_info.file_size > MAX_FIRMWARE_BYTES:
            _package_error(FirmwarePackageErrorCode.FORMAT, "firmware image is too large")
        if firmware_info.flag_bits & 0x1:
            _package_error(FirmwarePackageErrorCode.FORMAT, "encrypted ZIP entries are unsupported")
        firmware = archive.read(firmware_info)
        if len(firmware) != firmware_size:
            _package_error(FirmwarePackageErrorCode.SIZE_MISMATCH, "firmware size mismatch")
        actual_hash = hashlib.sha256(firmware).hexdigest()
        if not hmac.compare_digest(actual_hash, firmware_sha256):
            _package_error(FirmwarePackageErrorCode.HASH_MISMATCH, "firmware SHA-256 mismatch")

        signature_info = archive.getinfo(PACKAGE_SIGNATURE_NAME)
        if signature_info.flag_bits & 0x1 or signature_info.file_size != 64:
            _package_error(FirmwarePackageErrorCode.UNSIGNED, "invalid Ed25519 signature length")
        signature = archive.read(signature_info)
        trusted_key = keys.get(key_id)
        if trusted_key is None:
            _package_error(FirmwarePackageErrorCode.UNKNOWN_KEY, f"untrusted signing key: {key_id}")
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError as exc:
            _package_error(
                FirmwarePackageErrorCode.CRYPTO_UNAVAILABLE,
                f"Ed25519 verification dependency is unavailable: {exc}",
            )
        try:
            Ed25519PublicKey.from_public_bytes(trusted_key.public_key).verify(
                signature,
                build_signed_manifest_message(manifest),
            )
        except InvalidSignature:
            _package_error(
                FirmwarePackageErrorCode.SIGNATURE_INVALID,
                "firmware package signature is invalid",
            )

    normalized_hardware = tuple(item.strip().lower() for item in hardware)
    if product.lower() != expected_product.strip().lower():
        _package_error(
            FirmwarePackageErrorCode.PRODUCT_MISMATCH,
            f"package product {product!r} does not match {expected_product!r}",
        )
    if expected_hardware.strip().lower() not in normalized_hardware:
        _package_error(
            FirmwarePackageErrorCode.HARDWARE_MISMATCH,
            f"package does not support hardware {expected_hardware!r}",
        )
    _check_version_policy(version, current_version, version_policy)
    return FirmwarePackage(
        path=package_path,
        product=product,
        hardware_types=normalized_hardware,
        version=version,
        version_policy=version_policy,
        firmware_name=firmware_name,
        firmware=firmware,
        firmware_sha256=firmware_sha256,
        key_id=key_id,
        key_label=trusted_key.label,
    )
