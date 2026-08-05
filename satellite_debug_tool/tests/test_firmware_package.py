"""Signed customer firmware package verification and rejection paths."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Optional

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from satellite_debug_tool.core.security import (
    FirmwarePackageError,
    FirmwarePackageErrorCode,
    TrustedFirmwareKey,
    build_signed_manifest_message,
    verify_firmware_package,
)


@pytest.fixture
def signing_material():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private, {"release-2026": TrustedFirmwareKey("release-2026", public, "Release 2026")}


def _write_package(
    path: Path,
    private: Ed25519PrivateKey,
    *,
    product: str = "AFD01",
    hardware_types: Optional[list[str]] = None,
    version: str = "0.0.131",
    version_policy: str = "upgrade_only",
    firmware: bytes = b"AFD01-FIRMWARE" * 64,
    sign: bool = True,
    hash_override: Optional[str] = None,
    signature_override: Optional[bytes] = None,
) -> None:
    manifest = {
        "schema_version": 1,
        "product": product,
        "hardware_types": hardware_types or ["afd01"],
        "version": version,
        "version_policy": version_policy,
        "firmware": "afd01_application.bin",
        "firmware_size": len(firmware),
        "firmware_sha256": hash_override or hashlib.sha256(firmware).hexdigest(),
        "key_id": "release-2026",
    }
    signature = signature_override or private.sign(build_signed_manifest_message(manifest))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("afd01_application.bin", firmware)
        if sign:
            archive.writestr("signature.ed25519", signature)


def _verify(path: Path, keys):
    return verify_firmware_package(
        path,
        expected_product="AFD01",
        expected_hardware="afd01",
        current_version="0.0.130_beta",
        trusted_keys=keys,
    )


def test_valid_signed_package_returns_verified_inner_image(
    tmp_path: Path, signing_material
) -> None:
    private, keys = signing_material
    path = tmp_path / "afd01.sfpkg"
    _write_package(path, private)

    package = _verify(path, keys)

    assert package.product == "AFD01"
    assert package.version == "0.0.131"
    assert package.firmware.startswith(b"AFD01-FIRMWARE")
    assert package.key_label == "Release 2026"


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"sign": False}, FirmwarePackageErrorCode.UNSIGNED),
        ({"signature_override": b"X" * 65}, FirmwarePackageErrorCode.UNSIGNED),
        ({"signature_override": b"X" * 64}, FirmwarePackageErrorCode.SIGNATURE_INVALID),
        ({"hash_override": "0" * 64}, FirmwarePackageErrorCode.HASH_MISMATCH),
        ({"product": "ESA01"}, FirmwarePackageErrorCode.PRODUCT_MISMATCH),
        ({"hardware_types": ["afd01-rev-b"]}, FirmwarePackageErrorCode.HARDWARE_MISMATCH),
        ({"version": "0.0.129"}, FirmwarePackageErrorCode.VERSION_NOT_ALLOWED),
    ],
)
def test_invalid_package_is_rejected(
    tmp_path: Path, signing_material, changes: dict, expected_code
) -> None:
    private, keys = signing_material
    path = tmp_path / "invalid.sfpkg"
    _write_package(path, private, **changes)

    with pytest.raises(FirmwarePackageError) as raised:
        _verify(path, keys)

    assert raised.value.code == expected_code


def test_signed_manifest_can_authorize_same_version_or_downgrade(
    tmp_path: Path, signing_material
) -> None:
    private, keys = signing_material
    same = tmp_path / "same.sfpkg"
    downgrade = tmp_path / "downgrade.sfpkg"
    _write_package(same, private, version="0.0.130_beta", version_policy="allow_same")
    _write_package(downgrade, private, version="0.0.100", version_policy="allow_downgrade")

    assert _verify(same, keys).version == "0.0.130_beta"
    assert _verify(downgrade, keys).version == "0.0.100"
