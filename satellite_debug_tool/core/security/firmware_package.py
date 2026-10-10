"""AES-GCM customer firmware package creation, authentication, and decryption."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import zipfile
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import Mapping, Optional, Union


PACKAGE_SCHEMA_VERSION = 2
PACKAGE_ALGORITHM = "AES-256-GCM"
PACKAGE_MANIFEST_NAME = "manifest.json"
PACKAGE_PAYLOAD_NAME = "firmware.aesgcm"
PACKAGE_AAD_DOMAIN = b"SOFTHERTZ-AFD-FIRMWARE-PACKAGE\x00v2\x00"
AES_KEY_BYTES = 32
AES_GCM_NONCE_BYTES = 12
AES_GCM_TAG_BYTES = 16
MAX_MANIFEST_BYTES = 64 * 1024
MAX_FIRMWARE_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_BYTES = MAX_FIRMWARE_BYTES + MAX_MANIFEST_BYTES + 256 * 1024


class FirmwarePackageErrorCode(Enum):
    FORMAT = "format"
    UNKNOWN_KEY = "unknown_key"
    AUTHENTICATION_FAILED = "authentication_failed"
    HASH_MISMATCH = "hash_mismatch"
    SIZE_MISMATCH = "size_mismatch"
    PRODUCT_MISMATCH = "product_mismatch"
    HARDWARE_MISMATCH = "hardware_mismatch"
    VERSION_INVALID = "version_invalid"
    VERSION_NOT_ALLOWED = "version_not_allowed"
    CRYPTO_UNAVAILABLE = "crypto_unavailable"
    KEY_STORE_INVALID = "key_store_invalid"


class FirmwarePackageError(ValueError):
    def __init__(self, code: FirmwarePackageErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class FirmwareEncryptionKey:
    key_id: str
    key: bytes
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
    algorithm: str = PACKAGE_ALGORITHM


def _package_error(code: FirmwarePackageErrorCode, detail: str):
    raise FirmwarePackageError(code, detail)


def _canonical_manifest(manifest: dict) -> bytes:
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_encrypted_manifest_aad(manifest: dict) -> bytes:
    """Return the exact authenticated metadata bytes for AES-GCM."""

    return PACKAGE_AAD_DOMAIN + _canonical_manifest(manifest)


_MAX_VERSION_TEXT_LENGTH = 128
_VERSION_COMPONENT = r"(?:0|[1-9][0-9]{0,8})"
_PRERELEASE_IDENTIFIER = r"[0-9A-Za-z-]{1,32}"
_PRERELEASE = rf"{_PRERELEASE_IDENTIFIER}(?:\.{_PRERELEASE_IDENTIFIER})*"
_PACKAGE_VERSION_PATTERN = re.compile(
    rf"^[vV]?(?P<major>{_VERSION_COMPONENT})\."
    rf"(?P<minor>{_VERSION_COMPONENT})\."
    rf"(?P<patch>{_VERSION_COMPONENT})"
    rf"(?:(?:-|_)(?P<prerelease>{_PRERELEASE}))?"
    rf"(?:\+(?P<build>{_PRERELEASE}))?$"
)
_DEVICE_WIRE_VERSION_PATTERN = re.compile(
    rf"^(?:(?:[vV])|(?:[A-Za-z][0-9A-Za-z]{{0,31}}-))?"
    rf"(?P<major>{_VERSION_COMPONENT})\."
    rf"(?P<minor>{_VERSION_COMPONENT})\."
    rf"(?P<patch>{_VERSION_COMPONENT})"
    r"(?:\.(?P<variant>[A-Za-z]))?"
    rf"(?:\s+(?P<stage>alpha|beta|rc)(?:[._-]?(?P<stage_number>{_VERSION_COMPONENT}))?)?"
    r"(?:\s+(?P<git_identity>[0-9A-Fa-f]{7,40})(?:\.[0-9A-Za-z])?)?$",
    re.IGNORECASE,
)
_MANIFEST_IDENTIFIER_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")
_SHA256_PATTERN = re.compile(r"^[0-9A-Fa-f]{64}$")
_WINDOWS_RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CONIN$",
    "CONOUT$",
    "CLOCK$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_WINDOWS_FORBIDDEN_FILENAME_CHARS = frozenset('<>:"/\\|?*')
_MANIFEST_FIELDS = {
    "schema_version",
    "algorithm",
    "product",
    "hardware_types",
    "version",
    "version_policy",
    "firmware",
    "payload",
    "firmware_size",
    "firmware_sha256",
    "key_id",
    "nonce_base64",
}


def _prerelease_key(suffix: str) -> tuple[tuple[int, object], ...]:
    if not suffix:
        return ()
    identifiers = suffix.split(".")
    if any(not identifier for identifier in identifiers):
        _package_error(
            FirmwarePackageErrorCode.VERSION_INVALID,
            f"invalid semantic-version prerelease: {suffix!r}",
        )
    result: list[tuple[int, object]] = []
    for identifier in identifiers:
        if identifier.isdigit():
            if len(identifier) > 1 and identifier.startswith("0"):
                _package_error(
                    FirmwarePackageErrorCode.VERSION_INVALID,
                    f"numeric prerelease identifier has a leading zero: {identifier!r}",
                )
            result.append((0, int(identifier)))
        else:
            result.append((1, identifier.lower()))
    return tuple(result)


def _normalized_version_text(value: str) -> str:
    if not isinstance(value, str):
        _package_error(
            FirmwarePackageErrorCode.VERSION_INVALID,
            f"version must be text, got {type(value).__name__}",
        )
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_VERSION_TEXT_LENGTH:
        _package_error(
            FirmwarePackageErrorCode.VERSION_INVALID,
            f"invalid version length: {len(normalized)}",
        )
    return normalized


def _matched_version_key(
    match: re.Match[str],
    prerelease: str,
) -> tuple[int, int, int, int, tuple[tuple[int, object], ...]]:
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))
    return major, minor, patch, 1 if not prerelease else 0, _prerelease_key(prerelease)


def _package_version_key(
    value: str,
) -> tuple[int, int, int, int, tuple[tuple[int, object], ...]]:
    normalized = _normalized_version_text(value)
    match = _PACKAGE_VERSION_PATTERN.fullmatch(normalized)
    if match is None:
        _package_error(
            FirmwarePackageErrorCode.VERSION_INVALID,
            f"invalid package semantic version: {value!r}",
        )
    return _matched_version_key(match, match.group("prerelease") or "")


def _version_key(
    value: str,
) -> tuple[int, int, int, int, tuple[tuple[int, object], ...]]:
    normalized = _normalized_version_text(value)
    match = _PACKAGE_VERSION_PATTERN.fullmatch(normalized)
    if match is not None:
        return _matched_version_key(match, match.group("prerelease") or "")
    match = _DEVICE_WIRE_VERSION_PATTERN.fullmatch(normalized)
    if match is None:
        _package_error(
            FirmwarePackageErrorCode.VERSION_INVALID,
            f"invalid semantic version: {value!r}",
        )
    stage = (match.group("stage") or "").lower()
    stage_number = match.group("stage_number")
    prerelease = f"{stage}.{stage_number}" if stage and stage_number else stage
    return _matched_version_key(match, prerelease)


def _check_version_policy(package_version: str, current_version: str, policy: str) -> None:
    package_key = _package_version_key(package_version)
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


def firmware_versions_equal(package_version: str, device_wire_version: str) -> bool:
    """Compare package and device versions while ignoring build identity."""

    return _package_version_key(package_version) == _version_key(device_wire_version)


def device_firmware_versions_equal(first: str, second: str) -> bool:
    """Compare two firmware versions reported through device protocols."""

    return _version_key(first) == _version_key(second)


def _required_string(manifest: dict, name: str) -> str:
    value = manifest.get(name)
    if not isinstance(value, str) or not value.strip():
        _package_error(FirmwarePackageErrorCode.FORMAT, f"manifest {name} is required")
    return value.strip()


def _validate_firmware_filename(firmware_name: str) -> None:
    try:
        encoded_name = firmware_name.encode("utf-8")
    except UnicodeEncodeError as exc:
        _package_error(
            FirmwarePackageErrorCode.FORMAT,
            f"firmware entry is not valid UTF-8 text: {exc}",
        )
    upper_stem = firmware_name.split(".", 1)[0].rstrip(" .").upper()
    if (
        firmware_name in {".", ".."}
        or firmware_name.endswith((".", " "))
        or len(encoded_name) > 255
        or any(character in _WINDOWS_FORBIDDEN_FILENAME_CHARS for character in firmware_name)
        or any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in firmware_name)
        or upper_stem in _WINDOWS_RESERVED_FILENAMES
        or firmware_name.lower() in {PACKAGE_MANIFEST_NAME.lower(), PACKAGE_PAYLOAD_NAME.lower()}
    ):
        _package_error(FirmwarePackageErrorCode.FORMAT, "firmware entry is invalid")


def _decode_nonce(value: str) -> bytes:
    try:
        nonce = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        _package_error(FirmwarePackageErrorCode.FORMAT, f"manifest nonce_base64 is invalid: {exc}")
    if len(nonce) != AES_GCM_NONCE_BYTES:
        _package_error(FirmwarePackageErrorCode.FORMAT, "manifest nonce length is invalid")
    return nonce


def validate_firmware_manifest_fields(
    manifest: dict,
) -> tuple[str, tuple[str, ...], str, str, int, str, str, str, bytes]:
    """Validate the manifest contract shared by the builder and verifier."""

    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        _package_error(FirmwarePackageErrorCode.FORMAT, "manifest fields do not match schema v2")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != PACKAGE_SCHEMA_VERSION:
        _package_error(FirmwarePackageErrorCode.FORMAT, "unsupported package schema")
    if manifest.get("algorithm") != PACKAGE_ALGORITHM:
        _package_error(FirmwarePackageErrorCode.FORMAT, "unsupported firmware encryption algorithm")
    if manifest.get("payload") != PACKAGE_PAYLOAD_NAME:
        _package_error(FirmwarePackageErrorCode.FORMAT, "manifest payload is invalid")

    product = _required_string(manifest, "product")
    version = _required_string(manifest, "version")
    version_policy = _required_string(manifest, "version_policy")
    firmware_name = _required_string(manifest, "firmware")
    firmware_sha256 = _required_string(manifest, "firmware_sha256").lower()
    key_id = _required_string(manifest, "key_id")
    hardware = manifest.get("hardware_types")
    firmware_size = manifest.get("firmware_size")

    for name, value in (("product", product), ("key_id", key_id)):
        if _MANIFEST_IDENTIFIER_PATTERN.fullmatch(value) is None:
            _package_error(FirmwarePackageErrorCode.FORMAT, f"manifest {name} is invalid")
    _package_version_key(version)
    if version_policy not in {"upgrade_only", "allow_same", "allow_downgrade"}:
        _package_error(
            FirmwarePackageErrorCode.FORMAT,
            f"unsupported version_policy: {version_policy!r}",
        )
    _validate_firmware_filename(firmware_name)
    if type(firmware_size) is not int or not (1 <= firmware_size <= MAX_FIRMWARE_BYTES):
        _package_error(FirmwarePackageErrorCode.FORMAT, "firmware_size is invalid")
    if _SHA256_PATTERN.fullmatch(firmware_sha256) is None:
        _package_error(FirmwarePackageErrorCode.FORMAT, "firmware_sha256 is invalid")
    if (
        not isinstance(hardware, list)
        or not hardware
        or not all(
            isinstance(item, str)
            and _MANIFEST_IDENTIFIER_PATTERN.fullmatch(item.strip()) is not None
            for item in hardware
        )
    ):
        _package_error(FirmwarePackageErrorCode.FORMAT, "hardware_types is invalid")
    normalized_hardware = tuple(item.strip().lower() for item in hardware)
    if len(normalized_hardware) != len(set(normalized_hardware)):
        _package_error(FirmwarePackageErrorCode.FORMAT, "hardware_types contains duplicates")
    nonce = _decode_nonce(_required_string(manifest, "nonce_base64"))
    return (
        product,
        normalized_hardware,
        version,
        version_policy,
        firmware_size,
        firmware_name,
        firmware_sha256,
        key_id,
        nonce,
    )


def _validate_encryption_key(key_id: str, key: FirmwareEncryptionKey) -> None:
    if (
        not isinstance(key, FirmwareEncryptionKey)
        or key.key_id != key_id
        or not isinstance(key.key, bytes)
        or len(key.key) != AES_KEY_BYTES
    ):
        _package_error(
            FirmwarePackageErrorCode.KEY_STORE_INVALID,
            f"invalid AES-256 key entry: {key_id}",
        )


def load_bundled_encryption_keys() -> dict[str, FirmwareEncryptionKey]:
    """Load product AES keys bundled into the application artifact."""

    try:
        data = (
            resources.files("satellite_debug_tool.resources")
            .joinpath("firmware_encryption_keys.json")
            .read_text(encoding="utf-8")
        )
        document = json.loads(data)
    except Exception as exc:
        _package_error(
            FirmwarePackageErrorCode.KEY_STORE_INVALID,
            f"failed to load bundled firmware key store: {exc}",
        )
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        _package_error(FirmwarePackageErrorCode.KEY_STORE_INVALID, "unsupported firmware key-store schema")
    entries = document.get("keys")
    if not isinstance(entries, list) or not entries:
        _package_error(FirmwarePackageErrorCode.KEY_STORE_INVALID, "firmware key store is empty or invalid")
    keys: dict[str, FirmwareEncryptionKey] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"key_id", "label", "key_base64"}:
            _package_error(FirmwarePackageErrorCode.KEY_STORE_INVALID, "firmware key entry is invalid")
        key_id = entry.get("key_id")
        encoded = entry.get("key_base64")
        if (
            not isinstance(key_id, str)
            or _MANIFEST_IDENTIFIER_PATTERN.fullmatch(key_id) is None
            or not isinstance(encoded, str)
            or key_id in keys
        ):
            _package_error(FirmwarePackageErrorCode.KEY_STORE_INVALID, "firmware key identity is invalid")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            _package_error(FirmwarePackageErrorCode.KEY_STORE_INVALID, f"invalid AES key {key_id}: {exc}")
        key = FirmwareEncryptionKey(key_id, raw, str(entry.get("label") or key_id))
        _validate_encryption_key(key_id, key)
        keys[key_id] = key
    return keys


def create_firmware_package_bytes(
    firmware: bytes,
    *,
    firmware_name: str,
    product: str,
    hardware_types: tuple[str, ...],
    version: str,
    version_policy: str,
    encryption_key: FirmwareEncryptionKey,
    nonce: Optional[bytes] = None,
) -> tuple[dict, bytes]:
    """Create an authenticated manifest and encrypted payload entirely in memory."""

    payload = bytes(firmware)
    actual_nonce = os.urandom(AES_GCM_NONCE_BYTES) if nonce is None else bytes(nonce)
    manifest = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "algorithm": PACKAGE_ALGORITHM,
        "product": str(product).strip(),
        "hardware_types": [str(value).strip() for value in hardware_types],
        "version": str(version).strip(),
        "version_policy": str(version_policy),
        "firmware": str(firmware_name),
        "payload": PACKAGE_PAYLOAD_NAME,
        "firmware_size": len(payload),
        "firmware_sha256": hashlib.sha256(payload).hexdigest(),
        "key_id": encryption_key.key_id,
        "nonce_base64": base64.b64encode(actual_nonce).decode("ascii"),
    }
    validate_firmware_manifest_fields(manifest)
    _validate_encryption_key(encryption_key.key_id, encryption_key)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        _package_error(
            FirmwarePackageErrorCode.CRYPTO_UNAVAILABLE,
            f"AES-GCM dependency is unavailable: {exc}",
        )
    encrypted = AESGCM(encryption_key.key).encrypt(
        actual_nonce,
        payload,
        build_encrypted_manifest_aad(manifest),
    )
    return manifest, encrypted


_ZIP_OPERATION_ERRORS = (zipfile.BadZipFile, KeyError, OSError, RuntimeError)


def _zip_infos(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    try:
        infos = archive.infolist()
    except _ZIP_OPERATION_ERRORS as exc:
        _package_error(FirmwarePackageErrorCode.FORMAT, f"invalid package archive: {exc}")
    if any(info.file_size < 0 or info.compress_size < 0 for info in infos):
        _package_error(FirmwarePackageErrorCode.FORMAT, "package entry size is invalid")
    if sum(info.compress_size for info in infos) > MAX_PACKAGE_BYTES:
        _package_error(FirmwarePackageErrorCode.FORMAT, "compressed package entries are too large")
    return infos


def _zip_info(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    try:
        return archive.getinfo(name)
    except _ZIP_OPERATION_ERRORS as exc:
        _package_error(FirmwarePackageErrorCode.FORMAT, f"invalid package entry {name!r}: {exc}")


def _zip_read(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    try:
        return archive.read(info)
    except _ZIP_OPERATION_ERRORS as exc:
        _package_error(FirmwarePackageErrorCode.FORMAT, f"invalid package entry {info.filename!r}: {exc}")


def verify_firmware_package(
    path: Union[str, Path],
    *,
    expected_product: str,
    expected_hardware: str,
    current_version: Optional[str],
    encryption_keys: Optional[Mapping[str, FirmwareEncryptionKey]] = None,
) -> FirmwarePackage:
    """Authenticate, decrypt, and validate a customer package without extracting it."""

    package_path = Path(path)
    keys = dict(load_bundled_encryption_keys() if encryption_keys is None else encryption_keys)
    try:
        package_size = package_path.stat().st_size
    except OSError as exc:
        _package_error(FirmwarePackageErrorCode.FORMAT, f"invalid package archive: {exc}")
    if not (1 <= package_size <= MAX_PACKAGE_BYTES):
        _package_error(FirmwarePackageErrorCode.FORMAT, "package archive size is invalid")
    try:
        archive = zipfile.ZipFile(package_path, "r")
    except _ZIP_OPERATION_ERRORS as exc:
        _package_error(FirmwarePackageErrorCode.FORMAT, f"invalid package archive: {exc}")

    with archive:
        infos = _zip_infos(archive)
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            _package_error(FirmwarePackageErrorCode.FORMAT, "duplicate archive entry")
        if set(names) != {PACKAGE_MANIFEST_NAME, PACKAGE_PAYLOAD_NAME}:
            _package_error(FirmwarePackageErrorCode.FORMAT, "package entries do not match schema v2")
        manifest_info = _zip_info(archive, PACKAGE_MANIFEST_NAME)
        payload_info = _zip_info(archive, PACKAGE_PAYLOAD_NAME)
        if manifest_info.flag_bits & 0x1 or payload_info.flag_bits & 0x1:
            _package_error(FirmwarePackageErrorCode.FORMAT, "ZIP entry encryption is unsupported")
        if manifest_info.file_size > MAX_MANIFEST_BYTES:
            _package_error(FirmwarePackageErrorCode.FORMAT, "manifest is too large")
        if not (AES_GCM_TAG_BYTES < payload_info.file_size <= MAX_FIRMWARE_BYTES + AES_GCM_TAG_BYTES):
            _package_error(FirmwarePackageErrorCode.FORMAT, "encrypted firmware size is invalid")
        try:
            manifest = json.loads(_zip_read(archive, manifest_info).decode("utf-8"))
        except Exception as exc:
            _package_error(FirmwarePackageErrorCode.FORMAT, f"invalid manifest JSON: {exc}")
        (
            product,
            normalized_hardware,
            version,
            version_policy,
            firmware_size,
            firmware_name,
            firmware_sha256,
            key_id,
            nonce,
        ) = validate_firmware_manifest_fields(manifest)
        if payload_info.file_size != firmware_size + AES_GCM_TAG_BYTES:
            _package_error(FirmwarePackageErrorCode.SIZE_MISMATCH, "encrypted firmware size mismatch")
        key = keys.get(key_id)
        if key is None:
            _package_error(FirmwarePackageErrorCode.UNKNOWN_KEY, f"unknown firmware encryption key: {key_id}")
        _validate_encryption_key(key_id, key)
        encrypted = _zip_read(archive, payload_info)
        try:
            from cryptography.exceptions import InvalidTag
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            _package_error(
                FirmwarePackageErrorCode.CRYPTO_UNAVAILABLE,
                f"AES-GCM dependency is unavailable: {exc}",
            )
        try:
            firmware = AESGCM(key.key).decrypt(
                nonce,
                encrypted,
                build_encrypted_manifest_aad(manifest),
            )
        except InvalidTag:
            _package_error(FirmwarePackageErrorCode.AUTHENTICATION_FAILED, "firmware package authentication failed")
        if len(firmware) != firmware_size:
            _package_error(FirmwarePackageErrorCode.SIZE_MISMATCH, "firmware size mismatch")
        actual_hash = hashlib.sha256(firmware).hexdigest()
        if not hmac.compare_digest(actual_hash, firmware_sha256):
            _package_error(FirmwarePackageErrorCode.HASH_MISMATCH, "firmware SHA-256 mismatch")

    if product.casefold() != expected_product.strip().casefold():
        _package_error(
            FirmwarePackageErrorCode.PRODUCT_MISMATCH,
            f"package product {product!r} does not match {expected_product!r}",
        )
    if expected_hardware.strip().lower() not in normalized_hardware:
        _package_error(
            FirmwarePackageErrorCode.HARDWARE_MISMATCH,
            f"package does not support hardware {expected_hardware!r}",
        )
    if current_version is not None:
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
        key_label=key.label,
    )
