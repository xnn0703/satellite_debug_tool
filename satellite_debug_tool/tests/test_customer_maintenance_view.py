"""Customer maintenance only forwards authenticated AFD01 firmware images."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from satellite_debug_tool.core.product import ProductServiceStore
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import MetaInfo, ServiceIdentity
from satellite_debug_tool.core.security import TrustedFirmwareKey, build_signed_manifest_message
from satellite_debug_tool.ui.customer_maintenance_view import CustomerMaintenanceView


class _SettingsDouble:
    def get(self, _key: str, default=None):
        return default


class _LiveDouble(QObject):
    connection_state_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._products = ProductServiceStore()
        self._profiles = ProfileStore()
        self._profiles.apply_meta(MetaInfo(2, "0.0.130", "afd01", "AFD01-CUSTOMER"))
        self._products.feed(
            ServiceIdentity(
                1, 10, 0x17, "AFD01", "AFD01-CUSTOMER", "0.0.130", "", 1
            )
        )
        self.connected = True

    def product_store(self):
        return self._products

    def profile_store(self):
        return self._profiles

    def is_connected(self):
        return self.connected


class _DeviceDouble(QObject):
    ota_status_changed = Signal(str, object, int, bool)

    def __init__(self) -> None:
        super().__init__()
        self.available = True
        self.loaded = None
        self.started = False
        self.aborted = False

    def customer_ota_available(self):
        return self.available

    def load_customer_ota_image(self, data: bytes, filename: str):
        if not self.available:
            return False
        self.loaded = (data, filename)
        return True

    def start_customer_ota(self):
        self.started = True
        self.available = False
        self.ota_status_changed.emit("Sending OTA_BEGIN...", {}, 0, True)
        return True

    def abort_customer_ota(self):
        self.aborted = True


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def signing_material():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private, {"test-release": TrustedFirmwareKey("test-release", public, "Test release")}


def _package(
    path: Path,
    private: Ed25519PrivateKey,
    *,
    hardware: str = "afd01",
) -> bytes:
    firmware = b"AFD01-CUSTOMER-IMAGE" * 64
    manifest = {
        "schema_version": 1,
        "product": "AFD01",
        "hardware_types": [hardware],
        "version": "0.0.131",
        "version_policy": "upgrade_only",
        "firmware": "afd01.bin",
        "firmware_size": len(firmware),
        "firmware_sha256": hashlib.sha256(firmware).hexdigest(),
        "key_id": "test-release",
    }
    signature = private.sign(build_signed_manifest_message(manifest))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("afd01.bin", firmware)
        archive.writestr("signature.ed25519", signature)
    return firmware


def test_verified_package_is_forwarded_and_install_requires_confirmation(
    app, tmp_path: Path, signing_material, monkeypatch
) -> None:
    private, keys = signing_material
    path = tmp_path / "release.sfpkg"
    firmware = _package(path, private)
    live = _LiveDouble()
    device = _DeviceDouble()
    view = CustomerMaintenanceView(live, device, _SettingsDouble(), trusted_keys=keys)

    assert view.load_package(path)
    assert device.loaded == (firmware, "afd01.bin")
    assert view._upload_btn.isEnabled()
    assert "0.0.131" in view._package_detail.text()

    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    view._start_ota()
    assert device.started
    assert view._progress.value() == 0


def test_wrong_hardware_and_raw_binary_are_not_forwarded(
    app, tmp_path: Path, signing_material
) -> None:
    private, keys = signing_material
    wrong = tmp_path / "wrong.sfpkg"
    _package(wrong, private, hardware="esa01")
    raw = tmp_path / "afd01.bin"
    raw.write_bytes(b"not a package")
    live = _LiveDouble()
    device = _DeviceDouble()
    view = CustomerMaintenanceView(live, device, _SettingsDouble(), trusted_keys=keys)

    assert not view.load_package(wrong)
    assert device.loaded is None
    assert not view.load_package(raw)
    assert device.loaded is None


def test_disconnect_clears_previously_verified_package(
    app, tmp_path: Path, signing_material
) -> None:
    private, keys = signing_material
    path = tmp_path / "release.sfpkg"
    _package(path, private)
    live = _LiveDouble()
    device = _DeviceDouble()
    view = CustomerMaintenanceView(live, device, _SettingsDouble(), trusted_keys=keys)
    assert view.load_package(path)

    live.connected = False
    live.connection_state_changed.emit(False)

    assert view._package is None
    assert not view._upload_btn.isEnabled()
