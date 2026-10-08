"""Signed customer firmware package verification and rejection paths."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
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
    device_firmware_versions_equal,
    firmware_versions_equal,
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
    compression: int = zipfile.ZIP_DEFLATED,
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
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
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


def test_device_wire_version_is_compared_without_git_identity(
    tmp_path: Path, signing_material
) -> None:
    private, keys = signing_material
    path = tmp_path / "wire-version.sfpkg"
    _write_package(path, private, version="0.0.131")

    package = verify_firmware_package(
        path,
        expected_product="AFD01",
        expected_hardware="afd01",
        current_version="V0.0.130 beta e9b8b64396a3.d",
        trusted_keys=keys,
    )

    assert package.version == "0.0.131"


def test_semantic_version_prerelease_identifiers_use_numeric_order(
    tmp_path: Path, signing_material
) -> None:
    private, keys = signing_material
    path = tmp_path / "prerelease.sfpkg"
    _write_package(path, private, version="0.0.130-beta.10")

    package = verify_firmware_package(
        path,
        expected_product="AFD01",
        expected_hardware="afd01",
        current_version="0.0.130-beta.2",
        trusted_keys=keys,
    )

    assert package.version == "0.0.130-beta.10"


@pytest.mark.parametrize(
    ("current_version", "package_version", "version_policy"),
    [
        ("V0.0.130 beta e9b8b64396a3.d", "0.0.130_beta", "allow_same"),
        ("V1.0.7.c", "1.0.7+release.1", "allow_same"),
        ("afd01-1.3.2", "1.3.2+release.7", "allow_same"),
        ("V2.0.0 alpha abcdef1.d", "2.0.0-beta", "upgrade_only"),
        ("V2.0.0 beta abcdef1.d", "2.0.0-rc.1", "upgrade_only"),
        ("V2.0.0 rc1 abcdef1.d", "2.0.0", "upgrade_only"),
    ],
)
def test_supported_device_version_grammar_and_prerelease_order(
    tmp_path: Path,
    signing_material,
    current_version: str,
    package_version: str,
    version_policy: str,
) -> None:
    private, keys = signing_material
    path = tmp_path / f"version-{hash(current_version)}.sfpkg"
    _write_package(
        path,
        private,
        version=package_version,
        version_policy=version_policy,
    )

    package = verify_firmware_package(
        path,
        expected_product="AFD01",
        expected_hardware="afd01",
        current_version=current_version,
        trusted_keys=keys,
    )

    assert package.version == package_version


@pytest.mark.parametrize("invalid_version", ["9" * 5000 + ".0.0", "1.2.3-" + "9" * 100])
def test_oversized_version_numbers_are_rejected_as_version_invalid(
    tmp_path: Path,
    signing_material,
    invalid_version: str,
) -> None:
    private, keys = signing_material
    path = tmp_path / "oversized-version.sfpkg"
    _write_package(path, private, version=invalid_version)

    with pytest.raises(FirmwarePackageError) as raised:
        _verify(path, keys)

    assert raised.value.code is FirmwarePackageErrorCode.VERSION_INVALID


@pytest.mark.parametrize("invalid_version", ["1.2.3..x", "1.2.3-", "1.2.3_"])
def test_malformed_package_version_is_rejected_as_version_invalid(
    tmp_path: Path,
    signing_material,
    invalid_version: str,
) -> None:
    private, keys = signing_material
    path = tmp_path / "malformed-version.sfpkg"
    _write_package(path, private, version=invalid_version)

    with pytest.raises(FirmwarePackageError) as raised:
        _verify(path, keys)

    assert raised.value.code is FirmwarePackageErrorCode.VERSION_INVALID


def test_oversized_device_version_is_rejected_as_version_invalid(
    tmp_path: Path,
    signing_material,
) -> None:
    private, keys = signing_material
    path = tmp_path / "valid-package.sfpkg"
    _write_package(path, private, version_policy="allow_downgrade")

    with pytest.raises(FirmwarePackageError) as raised:
        verify_firmware_package(
            path,
            expected_product="AFD01",
            expected_hardware="afd01",
            current_version="V" + "9" * 5000 + ".0.0",
            trusted_keys=keys,
        )

    assert raised.value.code is FirmwarePackageErrorCode.VERSION_INVALID


def test_firmware_version_equivalence_ignores_only_build_identity() -> None:
    assert firmware_versions_equal("0.0.130_beta", "V0.0.130 beta e9b8b64396a3.d")
    assert firmware_versions_equal("1.0.7+release.1", "V1.0.7.c")
    assert firmware_versions_equal("1.3.2", "afd01-1.3.2")
    assert not firmware_versions_equal("2.0.0-rc.1", "V2.0.0 beta abcdef1.d")
    assert device_firmware_versions_equal(
        "V0.0.130 beta e9b8b64396a3.d",
        "0.0.130_beta",
    )
    assert not device_firmware_versions_equal("1.0.0", "2.0.0")


def test_caller_supplied_trust_entry_must_match_key_id_and_ed25519_size(
    tmp_path: Path,
    signing_material,
) -> None:
    private, _keys = signing_material
    path = tmp_path / "valid-package.sfpkg"
    _write_package(path, private)

    for trusted_key in (
        TrustedFirmwareKey("different-key", b"x" * 32),
        TrustedFirmwareKey("release-2026", b"x" * 31),
        TrustedFirmwareKey("release-2026", "x" * 32),  # type: ignore[arg-type]
    ):
        with pytest.raises(FirmwarePackageError) as raised:
            _verify(path, {"release-2026": trusted_key})
        assert raised.value.code is FirmwarePackageErrorCode.TRUST_STORE_INVALID


def test_stored_firmware_crc_corruption_is_reported_as_package_format(
    tmp_path: Path,
    signing_material,
) -> None:
    private, keys = signing_material
    path = tmp_path / "bad-crc.sfpkg"
    _write_package(path, private, compression=zipfile.ZIP_STORED)

    with zipfile.ZipFile(path, "r") as archive:
        info = archive.getinfo("afd01_application.bin")
        offset = info.header_offset
    archive_bytes = bytearray(path.read_bytes())
    name_length = int.from_bytes(archive_bytes[offset + 26 : offset + 28], "little")
    extra_length = int.from_bytes(archive_bytes[offset + 28 : offset + 30], "little")
    data_offset = offset + 30 + name_length + extra_length
    archive_bytes[data_offset] ^= 0x01
    path.write_bytes(archive_bytes)

    with pytest.raises(FirmwarePackageError) as raised:
        _verify(path, keys)

    assert raised.value.code is FirmwarePackageErrorCode.FORMAT


def test_oversized_package_archive_is_rejected_before_zip_read(
    tmp_path: Path,
    signing_material,
) -> None:
    private, keys = signing_material
    path = tmp_path / "oversized-archive.sfpkg"
    _write_package(path, private)
    with path.open("r+b") as stream:
        stream.truncate(5 * 1024 * 1024)

    with pytest.raises(FirmwarePackageError) as raised:
        _verify(path, keys)

    assert raised.value.code is FirmwarePackageErrorCode.FORMAT


@pytest.mark.parametrize(
    ("firmware_name", "firmware_size"),
    (
        ("../firmware.bin", 8),
        ("folder\\firmware.bin", 8),
        ("SIGNATURE.ED25519", 8),
        (".", 8),
        ("..", 8),
        ("C:firmware.bin", 8),
        ("CON.bin", 8),
        ("NUL .bin", 8),
        ("COM1.bin", 8),
        ("bad?.bin", 8),
        ("bad\x1f.bin", 8),
        ("firmware.bin.", 8),
        ("x.bin", True),
    ),
)
def test_manifest_rejects_unsafe_name_and_non_integer_size(
    tmp_path: Path,
    signing_material,
    firmware_name,
    firmware_size,
) -> None:
    private, keys = signing_material
    firmware = b"firmware"
    manifest = {
        "schema_version": 1,
        "product": "AFD01",
        "hardware_types": ["afd01"],
        "version": "0.0.131",
        "version_policy": "upgrade_only",
        "firmware": firmware_name,
        "firmware_size": firmware_size,
        "firmware_sha256": hashlib.sha256(firmware).hexdigest(),
        "key_id": "release-2026",
    }
    path = tmp_path / "invalid-manifest.sfpkg"
    signature = private.sign(build_signed_manifest_message(manifest))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(firmware_name, firmware)
        archive.writestr("signature.ed25519", signature)

    with pytest.raises(FirmwarePackageError) as raised:
        _verify(path, keys)

    assert raised.value.code is FirmwarePackageErrorCode.FORMAT


@pytest.mark.parametrize("product", ("AFD01A", "AFD01B2", "AFD01C"))
def test_release_builder_uses_registered_variant_identity(
    tmp_path: Path, signing_material, product: str
) -> None:
    private, _keys = signing_material
    firmware = tmp_path / f"{product.lower()}_application.bin"
    firmware.write_bytes(product.encode() * 64)
    private_path = tmp_path / "release-key.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    output = tmp_path / f"{product.lower()}.sfpkg"
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "tools" / "build_signed_firmware_package.py"),
        str(firmware),
        str(output),
        "--private-key",
        str(private_path),
        "--key-id",
        " release-2026 ",
        "--product",
        product,
        "--version",
        "0.0.131",
    ]

    completed = subprocess.run(command, capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    assert '"key_id": "release-2026"' in completed.stdout
    assert '"key_id": " release-2026 "' not in completed.stdout
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    package = verify_firmware_package(
        output,
        expected_product=product,
        expected_hardware=product.lower(),
        current_version="V0.0.130 beta e9b8b64396a3.d",
        trusted_keys={"release-2026": TrustedFirmwareKey("release-2026", public)},
    )
    assert package.product == product
    assert package.hardware_types == (product.lower(),)


def test_release_builder_rejects_unregistered_product(
    tmp_path: Path, signing_material
) -> None:
    private, _keys = signing_material
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware")
    private_path = tmp_path / "release-key.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    output = tmp_path / "unknown.sfpkg"
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "tools" / "build_signed_firmware_package.py"),
        str(firmware),
        str(output),
        "--private-key",
        str(private_path),
        "--key-id",
        "release-2026",
        "--product",
        "UNKNOWN",
        "--version",
        "0.0.131",
    ]

    completed = subprocess.run(command, capture_output=True, text=True, check=False)

    assert completed.returncode != 0
    assert "unregistered customer OTA product" in completed.stderr
    assert not output.exists()


def test_release_builder_rejects_unsafe_cross_platform_firmware_name(
    tmp_path: Path, signing_material
) -> None:
    private, _keys = signing_material
    firmware = tmp_path / "C:firmware.bin"
    firmware.write_bytes(b"firmware")
    private_path = tmp_path / "release-key.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    output = tmp_path / "unsafe-name.sfpkg"
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "tools" / "build_signed_firmware_package.py"),
        str(firmware),
        str(output),
        "--private-key",
        str(private_path),
        "--key-id",
        "release-2026",
        "--version",
        "0.0.131",
    ]

    completed = subprocess.run(command, capture_output=True, text=True, check=False)

    assert completed.returncode != 0
    assert "firmware entry is invalid" in completed.stderr
    assert not output.exists()
