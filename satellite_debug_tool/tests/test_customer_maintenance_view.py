"""Customer maintenance mount transactions and authenticated AFD01 OTA."""

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

from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    CmdType,
    MetaInfo,
    ServiceCapabilities,
    ServiceControlOp,
    ServiceControlResponse,
    ServiceFastState,
    ServiceIdentity,
    ServiceMountStatus,
    ServiceNavigationSourceInfo,
    build_device_reboot,
)
from satellite_debug_tool.core.session import DeviceSessionCore
from satellite_debug_tool.core.security import TrustedFirmwareKey, build_signed_manifest_message
from satellite_debug_tool.i18n import tr
from satellite_debug_tool.ui.customer_maintenance_view import CustomerMaintenanceView


class _SettingsDouble:
    def get(self, _key: str, default=None):
        return default


class _LiveDouble(QObject):
    connection_state_changed = Signal(bool)

    def __init__(
        self,
        *,
        hardware_type: str = "afd01",
        product_identity: str = "AFD01",
        service_protocol: int = 2,
    ) -> None:
        super().__init__()
        self._profiles = ProfileStore()
        self._profiles.apply_meta(
            MetaInfo(2, "0.0.130", hardware_type, f"{product_identity}-CUSTOMER")
        )
        self._session = DeviceSessionCore(profile_store=self._profiles)
        self._products = self._session.product_store
        self.sent: list[bytes] = []
        self._session.attach_transport(self, self.send_product_frame)
        self._products.feed(
            ServiceIdentity(
                1,
                10,
                0x17,
                product_identity,
                f"{product_identity}-CUSTOMER",
                "0.0.130",
                "",
                service_protocol,
            )
        )
        self.connected = True

    def product_store(self):
        return self._products

    def profile_store(self):
        return self._profiles

    def session_core(self):
        return self._session

    def customer_service_state(self):
        return self._session.customer_service_state()

    def send_product_frame(self, frame: bytes):
        self.sent.append(frame)
        return True

    def is_connected(self):
        return self.connected


class _DeviceDouble(QObject):
    ota_status_changed = Signal(str, object, int, bool)
    ota_artifact_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.available = True
        self.loaded = None
        self.started = False
        self.aborted = False
        self.artifact = None

    def customer_ota_available(self, artifact=None):
        return self.available and (artifact is None or artifact is self.artifact)

    def load_customer_ota_package(self, package):
        if not self.available:
            return None
        self.loaded = (package.firmware, package.firmware_name)
        self.artifact = object()
        self.ota_artifact_changed.emit(self.artifact)
        return self.artifact

    def start_customer_ota(self, artifact):
        if artifact is not self.artifact:
            return False
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
    product: str = "AFD01",
) -> bytes:
    firmware = b"AFD01-CUSTOMER-IMAGE" * 64
    manifest = {
        "schema_version": 1,
        "product": product,
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


def test_replacing_ota_artifact_invalidates_verified_package(
    app, tmp_path: Path, signing_material
) -> None:
    private, keys = signing_material
    path = tmp_path / "release.sfpkg"
    _package(path, private)
    live = _LiveDouble()
    device = _DeviceDouble()
    view = CustomerMaintenanceView(live, device, _SettingsDouble(), trusted_keys=keys)

    assert view.load_package(path)
    device.artifact = object()
    device.ota_artifact_changed.emit(device.artifact)

    assert view._package is None
    assert view._package_artifact is None
    assert not view._upload_btn.isEnabled()


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


def test_afd01c_accepts_only_matching_signed_product(
    app, tmp_path: Path, signing_material
) -> None:
    private, keys = signing_material
    afd01c_path = tmp_path / "afd01c.sfpkg"
    _package(
        afd01c_path,
        private,
        hardware="afd01c",
        product="AFD01C",
    )
    afd01_path = tmp_path / "afd01.sfpkg"
    _package(afd01_path, private, hardware="afd01", product="AFD01")
    live = _LiveDouble(
        hardware_type="afd01c",
        product_identity="AFD01C",
        service_protocol=8,
    )
    device = _DeviceDouble()
    view = CustomerMaintenanceView(
        live,
        device,
        _SettingsDouble(),
        trusted_keys=keys,
    )

    assert view.load_package(afd01c_path)
    device.loaded = None
    assert not view.load_package(afd01_path)
    assert device.loaded is None

    afd01_live = _LiveDouble()
    afd01_device = _DeviceDouble()
    afd01_view = CustomerMaintenanceView(
        afd01_live,
        afd01_device,
        _SettingsDouble(),
        trusted_keys=keys,
    )
    assert not afd01_view.load_package(afd01c_path)
    assert afd01_device.loaded is None


def test_ota_requires_matching_debug_and_product_identity(
    app, tmp_path: Path, signing_material
) -> None:
    private, keys = signing_material
    path = tmp_path / "afd01c.sfpkg"
    _package(path, private, hardware="afd01c", product="AFD01C")
    live = _LiveDouble(
        hardware_type="afd01c",
        product_identity="AFD01",
        service_protocol=8,
    )
    device = _DeviceDouble()
    view = CustomerMaintenanceView(
        live,
        device,
        _SettingsDouble(),
        trusted_keys=keys,
    )

    assert not view.load_package(path)
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


def test_identity_change_clears_previously_verified_package(
    app, tmp_path: Path, signing_material
) -> None:
    private, keys = signing_material
    path = tmp_path / "release.sfpkg"
    _package(path, private)
    live = _LiveDouble()
    device = _DeviceDouble()
    view = CustomerMaintenanceView(
        live,
        device,
        _SettingsDouble(),
        trusted_keys=keys,
    )
    view.activate_view()
    assert view.load_package(path)

    live._products.feed(
        ServiceIdentity(
            1,
            11,
            0x17,
            "AFD01C",
            "AFD01C-OTHER",
            "0.0.130",
            "",
            8,
        )
    )

    assert view._package is None
    assert not view._upload_btn.isEnabled()


def test_ota_confirmation_is_rejected_when_identity_changes_in_dialog(
    app,
    tmp_path: Path,
    signing_material,
    monkeypatch,
) -> None:
    private, keys = signing_material
    path = tmp_path / "release.sfpkg"
    _package(path, private)
    live = _LiveDouble()
    device = _DeviceDouble()
    view = CustomerMaintenanceView(
        live,
        device,
        _SettingsDouble(),
        trusted_keys=keys,
    )
    assert view.load_package(path)

    def change_identity(*_args, **_kwargs):
        live._products.feed(
            ServiceIdentity(
                1,
                11,
                0x17,
                "AFD01",
                "AFD01-OTHER",
                "0.0.130",
                "",
                1,
            )
        )
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "warning", change_identity)

    view._start_ota()

    assert not device.started
    assert view._ota_status.text() == tr(
        "Device session changed; select the firmware again"
    )


def _mount_status(
    *,
    timestamp: int,
    yaw: float,
    pitch: float,
    roll: float,
    restart_required: bool,
    imu_mount_rotation: int = 0,
    valid_mask: int = 0x7F,
    rbv_verified: bool = True,
) -> ServiceMountStatus:
    return ServiceMountStatus(
        1,
        timestamp,
        valid_mask,
        0x31445246,
        yaw,
        pitch,
        roll,
        0.0,
        180.0,
        -90.0,
        0.0,
        180.0,
        -90.0,
        imu_mount_rotation,
        rbv_verified,
        restart_required,
    )


def _seed_mount_service(live: _LiveDouble) -> None:
    live._products.feed(
        ServiceIdentity(
            1, 20, 0x17, "AFD01", "AFD01-CUSTOMER", "0.0.140", "", 8
        )
    )
    live._products.feed(
        ServiceCapabilities(
            1, 20, 0xFF, 17700.0, 21200.0, 27500.0, 31000.0, 0x0C, 0x07, 0x03
        )
    )
    live._products.feed(
        ServiceFastState(
            1, 20, 0xFFF, 0, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )
    live._products.feed(
        ServiceNavigationSourceInfo(1, 20, 0x7F, 2, 1, 1, 3, 0x00, 0x01, 0)
    )
    live._products.feed(
        _mount_status(
            timestamp=20,
            yaw=0.0,
            pitch=0.0,
            roll=0.0,
            restart_required=False,
        )
    )


def test_mount_save_waits_for_product_serial_or_uid(
    app,
    monkeypatch,
) -> None:
    live = _LiveDouble()
    _seed_mount_service(live)
    live._products.feed(
        ServiceIdentity(1, 21, 0x15, "AFD01", "", "0.0.140", "", 8)
    )
    view = CustomerMaintenanceView(
        live,
        _DeviceDouble(),
        _SettingsDouble(),
        trusted_keys={},
    )
    view.activate_view()

    assert not view._mount_yaw.isEnabled()
    assert not view._save_mount_btn.isEnabled()
    assert "序列号" in view._mount_readback.text() or "serial number" in view._mount_readback.text()

    view._mount_dirty = True
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    sent_before = len(live.sent)
    view._request_mount()

    assert len(live.sent) == sent_before
    assert view._mount_status_source == "Waiting for device serial number or unique identifier"


def test_mount_save_is_atomic_reboots_and_waits_for_final_readback(
    app,
    monkeypatch,
) -> None:
    live = _LiveDouble()
    _seed_mount_service(live)
    device = _DeviceDouble()
    view = CustomerMaintenanceView(
        live,
        device,
        _SettingsDouble(),
        trusted_keys={},
    )
    view.activate_view()
    assert view._mount_authoritative == (0.0, 0.0, 0.0)
    assert not view._save_mount_btn.isEnabled()

    view._mount_yaw.setValue(12.5)
    view._mount_pitch.setValue(-3.0)
    view._mount_roll.setValue(1.5)
    assert view._save_mount_btn.isEnabled()
    assert view._mount_preview._yaw_value == pytest.approx(12.5)
    assert view._mount_preview._pitch_value == pytest.approx(-3.0)
    assert view._mount_preview._roll_value == pytest.approx(1.5)
    assert "+X" in view._mount_contract.text()
    assert any(
        marker in view._mount_yaw_label.text()
        for marker in ("+ right", "向右")
    )
    assert any(
        marker in view._mount_pitch_label.text()
        for marker in ("+ nose up", "抬头")
    )
    assert any(
        marker in view._mount_roll_label.text()
        for marker in ("+ left side up", "左侧抬起")
    )

    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    view._request_mount()
    pending = view._mount_controller.pending
    assert pending is not None
    assert live.sent[-1][3] == CmdType.SERVICE_CONTROL_REQUEST
    assert live.sent[-1][11] == ServiceControlOp.SET_DEVICE_MOUNT

    live._products.feed(
        ServiceControlResponse(
            1,
            pending.request_id,
            ServiceControlOp.SET_DEVICE_MOUNT,
            0,
            1 << 7,
            0,
            0.0,
            0.0,
            0,
            0,
            False,
        )
    )
    live._products.feed(
        _mount_status(
            timestamp=21,
            yaw=12.5,
            pitch=-3.0,
            roll=1.5,
            restart_required=True,
        )
    )
    assert live.sent[-1] == build_device_reboot()
    assert live.session_core().device_transaction_active

    # 重启后必须重新收到同一设备的身份与导航配置，
    # 不允许旧连接缓存事实完成事务。
    live._products.feed(
        ServiceIdentity(
            1, 22, 0x17, "AFD01", "AFD01-CUSTOMER", "0.0.140", "", 8
        )
    )
    live._products.feed(
        ServiceNavigationSourceInfo(1, 22, 0x7F, 2, 1, 1, 3, 0x00, 0x01, 0)
    )
    live._products.feed(
        _mount_status(
            timestamp=22,
            yaw=12.5,
            pitch=-3.0,
            roll=1.5,
            restart_required=False,
        )
    )
    assert view._mount_controller.pending is None
    assert not live.session_core().device_transaction_active
    assert not view._mount_dirty
    assert view._mount_status_source == "Installation attitude applied and verified"


def test_mount_preview_and_inputs_ignore_internal_raw_imu_rotation(app) -> None:
    """Only device-to-carrier mount angles drive the maintenance preview."""

    live = _LiveDouble()
    live._products.feed(
        ServiceIdentity(
            1, 20, 0x17, "AFD01", "AFD01-CUSTOMER", "0.0.140", "", 8
        )
    )
    live._products.feed(
        ServiceCapabilities(
            1, 20, 0xFF, 17700.0, 21200.0, 27500.0, 31000.0, 0x0C, 0x07, 0x03
        )
    )
    live._products.feed(
        _mount_status(
            timestamp=20,
            yaw=12.5,
            pitch=-3.0,
            roll=1.5,
            imu_mount_rotation=2,
            restart_required=False,
        )
    )
    view = CustomerMaintenanceView(
        live,
        _DeviceDouble(),
        _SettingsDouble(),
        trusted_keys={},
    )
    view.activate_view()

    assert view._mount_target() == pytest.approx((12.5, -3.0, 1.5))
    assert view._mount_preview._yaw_value == pytest.approx(12.5)
    assert view._mount_preview._pitch_value == pytest.approx(-3.0)
    assert view._mount_preview._roll_value == pytest.approx(1.5)


def test_mount_readback_keeps_missing_valid_bits_unavailable(app) -> None:
    live = _LiveDouble()
    _seed_mount_service(live)
    live._products.feed(
        _mount_status(
            timestamp=21,
            yaw=1.0,
            pitch=2.0,
            roll=3.0,
            restart_required=False,
            valid_mask=0x3F,
        )
    )
    view = CustomerMaintenanceView(
        live,
        _DeviceDouble(),
        _SettingsDouble(),
        trusted_keys={},
    )
    view.activate_view()

    assert tr("restart state unavailable") in view._mount_readback.text()
    assert tr("active") not in view._mount_readback.text()
    assert not view._mount_yaw.isEnabled()

    missing_expected = _mount_status(
        timestamp=22,
        yaw=1.0,
        pitch=2.0,
        roll=3.0,
        restart_required=False,
        valid_mask=0x7F & ~(1 << 2),
    )
    assert CustomerMaintenanceView._mount_rbv_text(missing_expected) == tr(
        "RBV expectation unavailable"
    )

    missing_verification = _mount_status(
        timestamp=23,
        yaw=1.0,
        pitch=2.0,
        roll=3.0,
        restart_required=False,
        valid_mask=0x7F & ~(1 << 5),
    )
    assert tr("verification unavailable") in (
        CustomerMaintenanceView._mount_rbv_text(missing_verification)
    )
    assert tr("not verified") not in (
        CustomerMaintenanceView._mount_rbv_text(missing_verification)
    )


def test_hidden_mount_preview_defers_render_until_reactivated(app) -> None:
    live = _LiveDouble()
    _seed_mount_service(live)
    view = CustomerMaintenanceView(
        live,
        _DeviceDouble(),
        _SettingsDouble(),
        trusted_keys={},
    )
    assert not view._mount_preview.updatesEnabled()
    view.activate_view()
    view._mount_yaw.setValue(20.0)
    assert view._mount_preview._yaw_value == pytest.approx(20.0)

    view.deactivate_view()
    view._mount_yaw.setValue(35.0)
    assert not view._mount_preview.updatesEnabled()
    assert view._mount_preview._yaw_value == pytest.approx(20.0)

    view.activate_view()
    assert view._mount_preview.updatesEnabled()
    assert view._mount_preview._yaw_value == pytest.approx(35.0)


def test_hidden_maintenance_defers_store_render_until_reactivated(app) -> None:
    live = _LiveDouble(hardware_type="")
    view = CustomerMaintenanceView(
        live,
        _DeviceDouble(),
        _SettingsDouble(),
        trusted_keys={},
    )
    view.activate_view()
    assert view._identity_labels["main_fw"].text() == "0.0.130"

    view.deactivate_view()
    live._products.feed(
        ServiceIdentity(
            1, 11, 0x17, "AFD01", "AFD01-CUSTOMER", "0.0.141", "", 8
        )
    )
    assert view._identity_labels["main_fw"].text() == "0.0.130"

    view.activate_view()
    assert view._identity_labels["main_fw"].text() == "0.0.141"


def test_mount_unavailable_text_distinguishes_offline_waiting_and_unsupported(
    app,
) -> None:
    offline_live = _LiveDouble(service_protocol=8)
    offline_live.connected = False
    offline_view = CustomerMaintenanceView(
        offline_live,
        _DeviceDouble(),
        _SettingsDouble(),
        trusted_keys={},
    )
    assert offline_view._mount_readback.text() == tr("Offline")

    waiting_live = _LiveDouble(service_protocol=8)
    waiting_view = CustomerMaintenanceView(
        waiting_live,
        _DeviceDouble(),
        _SettingsDouble(),
        trusted_keys={},
    )
    assert waiting_view._mount_readback.text() == tr("Waiting for product service")

    protocol_live = _LiveDouble(service_protocol=1)
    protocol_view = CustomerMaintenanceView(
        protocol_live,
        _DeviceDouble(),
        _SettingsDouble(),
        trusted_keys={},
    )
    unsupported = tr("Installation attitude is not supported by this firmware")
    assert protocol_view._mount_readback.text() == unsupported

    capability_live = _LiveDouble(service_protocol=8)
    capability_live._products.feed(
        ServiceCapabilities(
            1, 1, 0xFF, 17700.0, 21200.0, 27500.0, 31000.0, 0x0C, 0x03, 0x03
        )
    )
    capability_view = CustomerMaintenanceView(
        capability_live,
        _DeviceDouble(),
        _SettingsDouble(),
        trusted_keys={},
    )
    assert capability_view._mount_readback.text() == unsupported


def test_mount_capability_absence_keeps_editor_read_only(app) -> None:
    live = _LiveDouble()
    device = _DeviceDouble()
    view = CustomerMaintenanceView(
        live,
        device,
        _SettingsDouble(),
        trusted_keys={},
    )

    view._mount_yaw.setValue(10.0)

    assert not view._save_mount_btn.isEnabled()
    assert not live.customer_service_state().mount_configuration_ready


def test_mount_disconnect_discards_old_authority_but_preserves_same_device_draft(app) -> None:
    live = _LiveDouble()
    _seed_mount_service(live)
    view = CustomerMaintenanceView(
        live,
        _DeviceDouble(),
        _SettingsDouble(),
        trusted_keys={},
    )
    view.activate_view()
    view._mount_yaw.setValue(7.0)

    live.connected = False
    live.connection_state_changed.emit(False)
    live._products.clear()
    assert view._mount_authoritative is None
    assert view._mount_dirty
    assert view._mount_yaw.value() == pytest.approx(7.0)

    live.connected = True
    live.connection_state_changed.emit(True)
    live._products.feed(
        ServiceIdentity(
            1, 21, 0x17, "AFD01", "AFD01-CUSTOMER", "0.0.140", "", 8
        )
    )
    live._products.feed(
        ServiceCapabilities(
            1, 21, 0xFF, 17700.0, 21200.0, 27500.0, 31000.0, 0x0C, 0x07, 0x03
        )
    )
    live._products.feed(
        _mount_status(
            timestamp=21,
            yaw=7.0,
            pitch=0.0,
            roll=0.0,
            restart_required=False,
        )
    )
    assert view._mount_authoritative == (7.0, 0.0, 0.0)
    assert not view._mount_dirty


def test_clean_disconnect_does_not_create_mount_draft_for_next_device(app) -> None:
    live = _LiveDouble()
    _seed_mount_service(live)
    view = CustomerMaintenanceView(
        live,
        _DeviceDouble(),
        _SettingsDouble(),
        trusted_keys={},
    )
    view.activate_view()
    assert not view._mount_dirty

    live.connected = False
    live.connection_state_changed.emit(False)
    live._products.clear()

    assert not view._mount_dirty
    assert view._mount_authoritative is None

    live.connected = True
    live.connection_state_changed.emit(True)
    live._products.feed(
        ServiceIdentity(1, 30, 0x17, "AFD01", "AFD01-OTHER", "0.0.140", "", 8)
    )
    live._products.feed(
        ServiceCapabilities(
            1, 30, 0xFF, 17700.0, 21200.0, 27500.0, 31000.0, 0x0C, 0x07, 0x03
        )
    )
    live._products.feed(
        _mount_status(
            timestamp=30,
            yaw=90.0,
            pitch=0.0,
            roll=0.0,
            restart_required=False,
        )
    )

    assert not view._mount_dirty
    assert view._mount_target() == pytest.approx((90.0, 0.0, 0.0))


def test_mount_draft_is_discarded_when_same_endpoint_identity_changes(app) -> None:
    live = _LiveDouble()
    _seed_mount_service(live)
    view = CustomerMaintenanceView(
        live,
        _DeviceDouble(),
        _SettingsDouble(),
        trusted_keys={},
    )
    view.activate_view()
    view._mount_yaw.setValue(7.0)
    assert view._mount_dirty

    live.connected = False
    live.connection_state_changed.emit(False)
    live._products.clear()
    live.connected = True
    live.connection_state_changed.emit(True)
    live._products.feed(
        ServiceIdentity(1, 31, 0x17, "AFD01", "AFD01-OTHER", "0.0.140", "", 8)
    )
    live._products.feed(
        ServiceCapabilities(
            1, 31, 0xFF, 17700.0, 21200.0, 27500.0, 31000.0, 0x0C, 0x07, 0x03
        )
    )
    live._products.feed(
        _mount_status(
            timestamp=31,
            yaw=90.0,
            pitch=0.0,
            roll=0.0,
            restart_required=False,
        )
    )

    assert not view._mount_dirty
    assert view._mount_target() == pytest.approx((90.0, 0.0, 0.0))
