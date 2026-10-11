"""AES-GCM customer firmware package contracts and rejection paths."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from satellite_debug_tool.core.security import (
    FirmwareEncryptionKey,
    FirmwarePackageError,
    FirmwarePackageErrorCode,
    build_encrypted_manifest_aad,
    create_firmware_package_bytes,
    device_firmware_versions_equal,
    firmware_version_is_older,
    firmware_versions_equal,
    load_bundled_encryption_keys,
    verify_firmware_package,
)


@pytest.fixture
def encryption_material():
    key = FirmwareEncryptionKey("test-package-key", bytes(range(32)), "Test package key")
    return key, {key.key_id: key}


def _write_package(
    path: Path,
    key: FirmwareEncryptionKey,
    *,
    product: str = "AFD01",
    hardware_types: tuple[str, ...] = ("afd01",),
    version: str = "0.0.131",
    version_policy: str = "upgrade_only",
    firmware: bytes = b"AFD01-FIRMWARE" * 64,
    firmware_name: str = "afd01_application.bin",
    nonce: bytes | None = None,
) -> tuple[dict, bytes]:
    manifest, encrypted = create_firmware_package_bytes(
        firmware,
        firmware_name=firmware_name,
        product=product,
        hardware_types=hardware_types,
        version=version,
        version_policy=version_policy,
        encryption_key=key,
        nonce=nonce,
    )
    _write_archive(path, manifest, encrypted)
    return manifest, encrypted


def _write_archive(path: Path, manifest: dict, encrypted: bytes, *, extra: bool = False) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("firmware.aesgcm", encrypted)
        if extra:
            archive.writestr("unexpected.txt", b"unexpected")


def _verify(path: Path, keys):
    return verify_firmware_package(
        path,
        expected_product="AFD01",
        expected_hardware="afd01",
        current_version="0.0.130_beta",
        encryption_keys=keys,
    )


def test_valid_package_returns_authenticated_decrypted_image(tmp_path: Path, encryption_material) -> None:
    key, keys = encryption_material
    path = tmp_path / "afd01.sfpkg"
    firmware = b"AFD01-FIRMWARE" * 64
    _write_package(path, key, firmware=firmware)

    package = _verify(path, keys)

    assert package.product == "AFD01"
    assert package.version == "0.0.131"
    assert package.firmware == firmware
    assert package.key_label == "Test package key"
    assert package.algorithm == "AES-256-GCM"


def test_random_nonce_changes_ciphertext_for_identical_input(tmp_path: Path, encryption_material) -> None:
    key, _keys = encryption_material
    first = tmp_path / "first.sfpkg"
    second = tmp_path / "second.sfpkg"
    first_manifest, first_ciphertext = _write_package(first, key)
    second_manifest, second_ciphertext = _write_package(second, key)

    assert first_manifest["nonce_base64"] != second_manifest["nonce_base64"]
    assert first_ciphertext != second_ciphertext


@pytest.mark.parametrize("target", ("manifest", "ciphertext", "tag"))
def test_authenticated_fields_and_payload_tampering_are_rejected(
    tmp_path: Path,
    encryption_material,
    target: str,
) -> None:
    key, keys = encryption_material
    path = tmp_path / f"tampered-{target}.sfpkg"
    manifest, encrypted = _write_package(path, key)
    if target == "manifest":
        manifest["version_policy"] = "allow_same"
    else:
        changed = bytearray(encrypted)
        changed[0 if target == "ciphertext" else -1] ^= 0x01
        encrypted = bytes(changed)
    _write_archive(path, manifest, encrypted)

    with pytest.raises(FirmwarePackageError) as raised:
        _verify(path, keys)

    assert raised.value.code is FirmwarePackageErrorCode.AUTHENTICATION_FAILED


def test_unknown_and_invalid_key_entries_are_rejected(tmp_path: Path, encryption_material) -> None:
    key, _keys = encryption_material
    path = tmp_path / "unknown-key.sfpkg"
    _write_package(path, key)

    with pytest.raises(FirmwarePackageError) as missing:
        _verify(path, {})
    assert missing.value.code is FirmwarePackageErrorCode.UNKNOWN_KEY

    for invalid in (
        FirmwareEncryptionKey("different", b"x" * 32),
        FirmwareEncryptionKey(key.key_id, b"x" * 31),
        FirmwareEncryptionKey(key.key_id, "x" * 32),  # type: ignore[arg-type]
    ):
        with pytest.raises(FirmwarePackageError) as raised:
            _verify(path, {key.key_id: invalid})
        assert raised.value.code is FirmwarePackageErrorCode.KEY_STORE_INVALID


@pytest.mark.parametrize(
    ("product", "hardware", "expected_code"),
    (
        ("ESA01", ("afd01",), FirmwarePackageErrorCode.PRODUCT_MISMATCH),
        ("AFD01", ("afd01-rev-b",), FirmwarePackageErrorCode.HARDWARE_MISMATCH),
    ),
)
def test_authenticated_wrong_product_or_hardware_is_rejected(
    tmp_path: Path,
    encryption_material,
    product: str,
    hardware: tuple[str, ...],
    expected_code: FirmwarePackageErrorCode,
) -> None:
    key, keys = encryption_material
    path = tmp_path / "wrong-identity.sfpkg"
    _write_package(path, key, product=product, hardware_types=hardware)

    with pytest.raises(FirmwarePackageError) as raised:
        _verify(path, keys)

    assert raised.value.code is expected_code


def test_version_policy_supports_same_and_downgrade(tmp_path: Path, encryption_material) -> None:
    key, keys = encryption_material
    same = tmp_path / "same.sfpkg"
    downgrade = tmp_path / "downgrade.sfpkg"
    blocked = tmp_path / "blocked.sfpkg"
    _write_package(same, key, version="0.0.130_beta", version_policy="allow_same")
    _write_package(downgrade, key, version="0.0.100", version_policy="allow_downgrade")
    _write_package(blocked, key, version="0.0.100", version_policy="upgrade_only")

    assert _verify(same, keys).version == "0.0.130_beta"
    assert _verify(downgrade, keys).version == "0.0.100"
    with pytest.raises(FirmwarePackageError) as raised:
        _verify(blocked, keys)
    assert raised.value.code is FirmwarePackageErrorCode.VERSION_NOT_ALLOWED


@pytest.mark.parametrize(
    ("current_version", "package_version", "version_policy"),
    (
        ("V0.0.130 beta e9b8b64396a3.d", "0.0.130_beta", "allow_same"),
        ("V1.0.7.c", "1.0.7+release.1", "allow_same"),
        ("afd01-1.3.2", "1.3.2+release.7", "allow_same"),
        ("V2.0.0 alpha abcdef1.d", "2.0.0-beta", "upgrade_only"),
        ("V2.0.0 beta abcdef1.d", "2.0.0-rc.1", "upgrade_only"),
        ("V2.0.0 rc1 abcdef1.d", "2.0.0", "upgrade_only"),
    ),
)
def test_supported_device_version_grammar_and_prerelease_order(
    tmp_path: Path,
    encryption_material,
    current_version: str,
    package_version: str,
    version_policy: str,
) -> None:
    key, keys = encryption_material
    path = tmp_path / f"version-{abs(hash(current_version))}.sfpkg"
    _write_package(path, key, version=package_version, version_policy=version_policy)

    package = verify_firmware_package(
        path,
        expected_product="AFD01",
        expected_hardware="afd01",
        current_version=current_version,
        encryption_keys=keys,
    )

    assert package.version == package_version


def test_firmware_version_equivalence_ignores_only_build_identity() -> None:
    assert firmware_versions_equal("0.0.130_beta", "V0.0.130 beta e9b8b64396a3.d")
    assert firmware_versions_equal("1.0.7+release.1", "V1.0.7.c")
    assert firmware_versions_equal("1.3.2", "afd01-1.3.2")
    assert not firmware_versions_equal("2.0.0-rc.1", "V2.0.0 beta abcdef1.d")
    assert device_firmware_versions_equal("V0.0.130 beta e9b8b64396a3.d", "0.0.130_beta")
    assert not device_firmware_versions_equal("1.0.0", "2.0.0")


def test_firmware_version_cutoff_accepts_device_wire_identity() -> None:
    assert firmware_version_is_older(
        "V0.0.130 beta e9b8b64396a3.d",
        "0.0.131_beta",
    )
    assert not firmware_version_is_older("V0.0.131 beta", "0.0.131_beta")


def test_schema_v1_signed_package_is_rejected(tmp_path: Path, encryption_material) -> None:
    _key, keys = encryption_material
    path = tmp_path / "legacy.sfpkg"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"schema_version": 1}))
        archive.writestr("firmware.bin", b"legacy")
        archive.writestr("signature.ed25519", b"x" * 64)

    with pytest.raises(FirmwarePackageError) as raised:
        _verify(path, keys)

    assert raised.value.code is FirmwarePackageErrorCode.FORMAT


@pytest.mark.parametrize(
    "firmware_name",
    (
        "../firmware.bin",
        "folder\\firmware.bin",
        "firmware.aesgcm",
        ".",
        "..",
        "C:firmware.bin",
        "CON.bin",
        "COM1.bin",
        "bad?.bin",
        "bad\x1f.bin",
        "firmware.bin.",
    ),
)
def test_builder_rejects_unsafe_cross_platform_firmware_name(
    encryption_material,
    firmware_name: str,
) -> None:
    key, _keys = encryption_material
    with pytest.raises(FirmwarePackageError) as raised:
        create_firmware_package_bytes(
            b"firmware",
            firmware_name=firmware_name,
            product="AFD01",
            hardware_types=("afd01",),
            version="0.0.131",
            version_policy="upgrade_only",
            encryption_key=key,
        )
    assert raised.value.code is FirmwarePackageErrorCode.FORMAT


def test_extra_archive_entry_and_oversized_archive_are_rejected(tmp_path: Path, encryption_material) -> None:
    key, keys = encryption_material
    extra = tmp_path / "extra.sfpkg"
    manifest, encrypted = _write_package(extra, key)
    _write_archive(extra, manifest, encrypted, extra=True)
    with pytest.raises(FirmwarePackageError) as raised:
        _verify(extra, keys)
    assert raised.value.code is FirmwarePackageErrorCode.FORMAT


def test_duplicate_entries_and_high_expansion_payload_are_rejected(
    tmp_path: Path,
    encryption_material,
) -> None:
    key, keys = encryption_material
    duplicate = tmp_path / "duplicate.sfpkg"
    manifest, encrypted = _write_package(duplicate, key)
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(duplicate, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("firmware.aesgcm", encrypted)
    with pytest.raises(FirmwarePackageError) as raised:
        _verify(duplicate, keys)
    assert raised.value.code is FirmwarePackageErrorCode.FORMAT

    expansion = tmp_path / "high-expansion.sfpkg"
    with zipfile.ZipFile(expansion, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("firmware.aesgcm", b"\x00" * (4 * 1024 * 1024 + 17))
    with pytest.raises(FirmwarePackageError) as raised:
        _verify(expansion, keys)
    assert raised.value.code is FirmwarePackageErrorCode.FORMAT


def test_authenticated_plaintext_size_and_hash_mismatch_are_rejected(
    tmp_path: Path,
    encryption_material,
) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key, keys = encryption_material
    firmware = b"firmware-integrity"
    size_path = tmp_path / "size-mismatch.sfpkg"
    manifest, encrypted = _write_package(size_path, key, firmware=firmware)
    manifest["firmware_size"] += 1
    _write_archive(size_path, manifest, encrypted)
    with pytest.raises(FirmwarePackageError) as raised:
        _verify(size_path, keys)
    assert raised.value.code is FirmwarePackageErrorCode.SIZE_MISMATCH

    hash_path = tmp_path / "hash-mismatch.sfpkg"
    manifest, _encrypted = _write_package(hash_path, key, firmware=firmware)
    manifest["firmware_sha256"] = "0" * 64
    nonce = base64.b64decode(manifest["nonce_base64"])
    encrypted = AESGCM(key.key).encrypt(nonce, firmware, build_encrypted_manifest_aad(manifest))
    _write_archive(hash_path, manifest, encrypted)
    with pytest.raises(FirmwarePackageError) as raised:
        _verify(hash_path, keys)
    assert raised.value.code is FirmwarePackageErrorCode.HASH_MISMATCH

    oversized = tmp_path / "oversized.sfpkg"
    _write_package(oversized, key)
    with oversized.open("r+b") as stream:
        stream.truncate(5 * 1024 * 1024)
    with pytest.raises(FirmwarePackageError) as raised:
        _verify(oversized, keys)
    assert raised.value.code is FirmwarePackageErrorCode.FORMAT


@pytest.mark.parametrize("product", ("AFD01A", "AFD01B2", "AFD01C", "AFD01D"))
def test_release_builder_uses_registered_variant_identity(tmp_path: Path, product: str) -> None:
    firmware = tmp_path / f"{product.lower()}_application.bin"
    firmware.write_bytes(product.encode() * 64)
    output = tmp_path / f"{product.lower()}.sfpkg"
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "tools" / "build_encrypted_firmware_package.py"),
        str(firmware),
        str(output),
        "--product",
        product,
        "--version",
        "0.0.131",
    ]

    completed = subprocess.run(command, capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    package = verify_firmware_package(
        output,
        expected_product=product,
        expected_hardware=product.lower(),
        current_version="V9.9.9",
    )
    assert package.product == product
    assert package.hardware_types == (product.lower(),)
    assert package.version_policy == "allow_downgrade"
    assert package.firmware == firmware.read_bytes()


def test_release_builder_rejects_unregistered_product(tmp_path: Path) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware")
    output = tmp_path / "unknown.sfpkg"
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "tools" / "build_encrypted_firmware_package.py"),
        str(firmware),
        str(output),
        "--product",
        "UNKNOWN",
        "--version",
        "0.0.131",
    ]

    completed = subprocess.run(command, capture_output=True, text=True, check=False)

    assert completed.returncode != 0
    assert "unregistered customer OTA product" in completed.stderr
    assert not output.exists()


def test_bundled_key_store_contains_one_valid_active_key() -> None:
    keys = load_bundled_encryption_keys()
    assert tuple(keys) == ("afd-host-aes-2026",)
    assert len(keys["afd-host-aes-2026"].key) == 32
