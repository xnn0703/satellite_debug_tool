"""Asynchronous debug-OTA state machine for one device session."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time
from typing import Any, Optional
import zlib

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from satellite_debug_tool.core.product import verified_device_uid, verified_identity_text
from satellite_debug_tool.core.profile import CapabilitySupport
from satellite_debug_tool.core.security import (
    FirmwarePackage,
    device_firmware_versions_equal,
    firmware_versions_equal,
)
from satellite_debug_tool.core.protocol import (
    CodecError,
    CommandResponse,
    MetaInfo,
    OTA_DATA_SEQUENCE_MAX,
    RespCode,
    ServiceHardwareIdentity,
    ServiceIdentity,
    build_ota_abort,
    build_ota_begin,
    build_ota_data,
    build_ota_end,
    build_request_meta_info,
)

from .device_session import DeviceSessionCore, DeviceSessionScope
from .command_sender import (
    SessionCommandSender,
    SessionOperationClass,
    SessionOperationGateway,
    operation_gateway_or_legacy,
)


OTA_CHUNK_SIZE = 512
OTA_MAX_IMAGE_BYTES = (OTA_DATA_SEQUENCE_MAX + 1) * OTA_CHUNK_SIZE
OTA_CHUNK_TIMEOUT_MS = 2000
OTA_CHUNK_MAX_RETRY = 3
OTA_BEGIN_TIMEOUT_MS = 15000
OTA_END_TIMEOUT_MS = 10000
OTA_REBOOT_TIMEOUT_S = 120.0
OTA_REBOOT_PROBE_MS = 3000


class OtaCapabilityState(str, Enum):
    DISCONNECTED = "disconnected"
    WAITING_PROFILE = "waiting_profile"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class OtaState(str, Enum):
    IDLE = "idle"
    QUIESCE = "quiesce"
    BEGIN = "begin"
    DATA = "data"
    END = "end"
    WAIT_REBOOT = "wait_reboot"
    TERMINATING = "terminating"


class OtaArtifactSource(str, Enum):
    """Provenance of the immutable bytes selected for an OTA operation."""

    ENGINEERING_RAW = "engineering_raw"
    VERIFIED_PACKAGE = "verified_package"


@dataclass(frozen=True)
class OtaArtifactToken:
    """Opaque selection identity used to bind customer confirmation to bytes."""

    value: int
    source: OtaArtifactSource


@dataclass(frozen=True)
class _OtaArtifact:
    token: OtaArtifactToken
    source: OtaArtifactSource
    data: bytes
    filename: str
    crc32: int
    scope: DeviceSessionScope
    target_version: str = ""
    product: str = ""
    firmware_sha256: str = ""


class OtaStatus(str, Enum):
    IDLE = "idle"
    WAITING_PROFILE = "waiting_profile"
    WAITING_CAPABILITY = "waiting_capability"
    UNSUPPORTED = "unsupported"
    TRANSACTION_ACTIVE = "transaction_active"
    NO_FILE = "no_file"
    ARTIFACT_TOO_LARGE = "artifact_too_large"
    ARTIFACT_MISMATCH = "artifact_mismatch"
    IDENTITY_UNAVAILABLE = "identity_unavailable"
    IDENTITY_FACTS_CONFLICT = "identity_facts_conflict"
    FIRMWARE_FACTS_CONFLICT = "firmware_facts_conflict"
    STOPPING_LIVE_DATA = "stopping_live_data"
    STOP_LIVE_DATA_FAILED = "stop_live_data_failed"
    SENDING_BEGIN = "sending_begin"
    BEGIN_SEND_FAILED = "begin_send_failed"
    BEGIN_REJECTED = "begin_rejected"
    BEGIN_TIMEOUT = "begin_timeout"
    TRANSFERRING = "transferring"
    CHUNK_SEND_FAILED = "chunk_send_failed"
    CHUNK_RETRY = "chunk_retry"
    CHUNK_REJECTED = "chunk_rejected"
    CHUNK_TIMEOUT = "chunk_timeout"
    VERIFYING = "verifying"
    END_SEND_FAILED = "end_send_failed"
    END_REJECTED = "end_rejected"
    END_TIMEOUT = "end_timeout"
    REBOOTING = "rebooting"
    REBOOT_TIMEOUT = "reboot_timeout"
    TARGET_VERSION_CONFIRMED = "target_version_confirmed"
    TARGET_VERSION_NOT_CONFIRMED = "target_version_not_confirmed"
    RETURN_IDENTITY_CHANGED = "return_identity_changed"
    RETURN_IDENTITY_UNCONFIRMED = "return_identity_unconfirmed"
    RETURN_FIRMWARE_UNCONFIRMED = "return_firmware_unconfirmed"
    FIRMWARE_CHANGED = "firmware_changed"
    APPLICATION_UNCONFIRMED = "application_unconfirmed"
    CONNECTION_LOST = "connection_lost"
    SESSION_CHANGED = "session_changed"
    ABORTED = "aborted"
    ABORT_PENDING = "abort_pending"


class OtaController(QObject):
    """Own the OTA command sequence, exact ACK matching, retries, and reboot wait."""

    capability_changed = Signal(object)
    state_changed = Signal(object)
    status_changed = Signal(object, object)
    progress_changed = Signal(int)
    file_changed = Signal(str, int)
    artifact_changed = Signal(object)
    debug_mode_requested = Signal(bool)

    def __init__(
        self,
        session: DeviceSessionCore,
        parent: Optional[QObject] = None,
        *,
        command_sender: SessionCommandSender | None = None,
        operation_gateway: SessionOperationGateway | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._operation_gateway = operation_gateway_or_legacy(
            session,
            operation_gateway,
            command_sender,
        )
        self._command_sender = self._operation_gateway
        self._lease_token = object()
        self._connected = session.connected
        self._hardware = session.profile_store.current_hw_type()
        self._capability = OtaCapabilityState.DISCONNECTED
        self._state = OtaState.IDLE
        self._active = False
        self._mutation_sent = False
        self._operation_request_id = ""
        self._artifact: _OtaArtifact | None = None
        self._active_artifact: _OtaArtifact | None = None
        self._next_artifact_id = 0
        self._sequence = 0
        self._total_chunks = 0
        self._retry = 0
        self._start_time = 0.0
        self._known_debug_enabled = False
        self._paused_debug = False
        self._restore_debug = False
        self._firmware_before = ""
        self._firmware_current = (
            session.meta_info.fw_ver if session.meta_info is not None else ""
        )
        self._reboot_started = 0.0
        self._reboot_deadline = 0.0
        self._returned_firmware_facts: dict[str, str] = {}
        self._returned_identity_fact_names: set[str] = set()

        self._response_timer = QTimer(self)
        self._response_timer.setSingleShot(True)
        self._response_timer.timeout.connect(self._on_response_timeout)
        self._reboot_timer = QTimer(self)
        self._reboot_timer.setInterval(OTA_REBOOT_PROBE_MS)
        self._reboot_timer.timeout.connect(self._on_reboot_tick)

        session.connection_changed.connect(self.set_connected)
        session.generation_changed.connect(self._on_generation_changed)
        session.record_received.connect(self.feed_record)
        session.profile_store.profile_changed.connect(self._on_profile_changed)
        self.destroyed.connect(self._on_destroyed)
        self._refresh_capability()

    @property
    def capability_state(self) -> OtaCapabilityState:
        return self._capability

    @property
    def state(self) -> OtaState:
        return self._state

    @property
    def active(self) -> bool:
        return self._active

    @property
    def supported(self) -> bool:
        return self._capability is OtaCapabilityState.SUPPORTED

    @property
    def has_file(self) -> bool:
        return self._artifact is not None

    @property
    def filename(self) -> str:
        return self._artifact.filename if self._artifact is not None else ""

    @property
    def file_size(self) -> int:
        return len(self._artifact.data) if self._artifact is not None else 0

    @property
    def crc32(self) -> int:
        return self._artifact.crc32 if self._artifact is not None else 0

    @property
    def artifact_token(self) -> OtaArtifactToken | None:
        return self._artifact.token if self._artifact is not None else None

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def total_chunks(self) -> int:
        return self._total_chunks

    def configure_engineering_file(
        self,
        data: bytes,
        filename: str,
    ) -> OtaArtifactToken | None:
        """Select immutable raw bytes for the explicitly engineering-only flow."""

        return self._configure_artifact(
            source=OtaArtifactSource.ENGINEERING_RAW,
            data=data,
            filename=filename,
        )

    def configure_verified_package(
        self,
        package: FirmwarePackage,
    ) -> OtaArtifactToken | None:
        """Bind one verified customer package and all of its provenance facts."""

        if not isinstance(package, FirmwarePackage):
            return None
        scope = self._session.device_scope()
        product_matches = (
            bool(scope.product_identity)
            and scope.product_identity.casefold() == package.product.strip().casefold()
        )
        hardware_matches = scope.hardware_type in package.hardware_types
        if not scope.has_immutable_identity:
            self.status_changed.emit(OtaStatus.IDENTITY_UNAVAILABLE, {})
            return None
        if not scope.identity_facts_consistent:
            self.status_changed.emit(OtaStatus.IDENTITY_FACTS_CONFLICT, {})
            return None
        if not scope.firmware_facts_consistent:
            self.status_changed.emit(OtaStatus.FIRMWARE_FACTS_CONFLICT, {})
            return None
        if not product_matches or not hardware_matches:
            self.status_changed.emit(OtaStatus.ARTIFACT_MISMATCH, {})
            return None
        return self._configure_artifact(
            source=OtaArtifactSource.VERIFIED_PACKAGE,
            data=package.firmware,
            filename=package.firmware_name,
            scope=scope,
            target_version=package.version,
            product=package.product,
            firmware_sha256=package.firmware_sha256,
        )

    def _configure_artifact(
        self,
        *,
        source: OtaArtifactSource,
        data: bytes,
        filename: str,
        scope: DeviceSessionScope | None = None,
        target_version: str = "",
        product: str = "",
        firmware_sha256: str = "",
    ) -> OtaArtifactToken | None:
        if self.active:
            self.status_changed.emit(OtaStatus.TRANSACTION_ACTIVE, {})
            return None
        payload = bytes(data)
        name = str(filename)
        if not self._session.connected or not payload or not name:
            return None
        if len(payload) > OTA_MAX_IMAGE_BYTES:
            self.status_changed.emit(
                OtaStatus.ARTIFACT_TOO_LARGE,
                {"maximum": OTA_MAX_IMAGE_BYTES},
            )
            return None
        try:
            build_ota_begin(len(payload), name)
        except CodecError:
            self.status_changed.emit(OtaStatus.BEGIN_SEND_FAILED, {})
            return None
        captured_scope = scope or self._session.device_scope()
        self._next_artifact_id += 1
        token = OtaArtifactToken(self._next_artifact_id, source)
        self._artifact = _OtaArtifact(
            token=token,
            source=source,
            data=payload,
            filename=name,
            crc32=zlib.crc32(payload) & 0xFFFFFFFF,
            scope=captured_scope,
            target_version=str(target_version),
            product=str(product),
            firmware_sha256=str(firmware_sha256),
        )
        self.file_changed.emit(name, len(payload))
        self.artifact_changed.emit(token)
        return token

    def verified_artifact_matches(self, token: OtaArtifactToken | None) -> bool:
        artifact = self._artifact
        return bool(
            artifact is not None
            and artifact.source is OtaArtifactSource.VERIFIED_PACKAGE
            and token == artifact.token
            and self._session.operation_scope_matches(artifact.scope)
        )

    def clear_file(self) -> bool:
        if self.active:
            return False
        had_file = self._artifact is not None
        self._artifact = None
        if had_file:
            self.file_changed.emit("", 0)
            self.artifact_changed.emit(None)
        return True

    def set_debug_state(self, enabled: bool) -> None:
        self._known_debug_enabled = bool(enabled)

    @Slot(bool)
    def set_connected(self, connected: bool) -> None:
        connected = bool(connected)
        if self._connected == connected:
            return
        self._connected = connected
        if not connected:
            self._hardware = None
            if self._active and self._state is not OtaState.WAIT_REBOOT:
                self._finish_unknown(
                    OtaStatus.CONNECTION_LOST,
                    "OTA connection was lost before a matching terminal result",
                    restore_debug=False,
                )
            elif not self.active:
                self.clear_file()
        self._refresh_capability()

    @Slot(str)
    def _on_profile_changed(self, hardware: str) -> None:
        if hardware:
            self._hardware = hardware
        self._refresh_capability()

    @Slot(int, object)
    def _on_generation_changed(self, _generation: int, endpoint: object) -> None:
        artifact = self._active_artifact
        if (
            self._active
            and self._state is OtaState.WAIT_REBOOT
            and artifact is not None
            and endpoint == artifact.scope.endpoint
        ):
            if not self._operation_gateway.try_acquire_operation(
                self._lease_token,
                purpose="ota-return-verification",
            ):
                self._finish_unknown(
                    OtaStatus.TRANSACTION_ACTIVE,
                    "OTA return verification could not retain its operation owner",
                    restore_debug=False,
                )
            return
        if self._active:
            self._finish_unknown(
                OtaStatus.SESSION_CHANGED,
                "OTA session changed before a matching terminal result",
                restore_debug=False,
            )
        if not self.active:
            self.clear_file()

    @Slot(object)
    def feed_record(self, record: object) -> None:
        if isinstance(record, MetaInfo):
            self._hardware = record.hw_type or None
            self._firmware_current = record.fw_ver or ""
            self._refresh_capability()
            if self._waiting_for_reboot_evidence() and record.fw_ver:
                self._returned_firmware_facts["debug_firmware"] = str(
                    record.fw_ver
                ).strip()
                if not self._accept_meta_identity(record):
                    return
                self._evaluate_returned_firmware()
        elif isinstance(record, ServiceIdentity):
            if self._waiting_for_reboot_evidence():
                if record.valid_mask & (1 << 2) and record.main_firmware:
                    self._returned_firmware_facts["product_firmware"] = str(
                        record.main_firmware
                    ).strip()
                if not self._accept_product_identity(record):
                    return
                self._evaluate_returned_firmware()
        elif isinstance(record, ServiceHardwareIdentity):
            if self._waiting_for_reboot_evidence():
                if not self._accept_hardware_identity(record):
                    return
                self._evaluate_returned_firmware()
        elif isinstance(record, CommandResponse):
            self._apply_response(record)

    def _waiting_for_reboot_evidence(self) -> bool:
        return bool(
            self._active
            and self._state is OtaState.WAIT_REBOOT
            and self._active_artifact is not None
        )

    def _accept_meta_identity(self, record: MetaInfo) -> bool:
        artifact = self._active_artifact
        if artifact is None:
            return False
        scope = artifact.scope
        hardware = str(record.hw_type or "").strip().casefold()
        if scope.hardware_type and hardware and hardware != scope.hardware_type.casefold():
            self._finish_identity_changed("hardware_type")
            return False
        expected = dict(scope.immutable_identity_facts).get("debug_serial_number", "")
        actual = verified_identity_text(record.device_sn)
        if expected and actual:
            if actual.casefold() != expected.casefold():
                self._finish_identity_changed("debug_serial_number")
                return False
            self._returned_identity_fact_names.add("debug_serial_number")
        return True

    def _accept_product_identity(self, record: ServiceIdentity) -> bool:
        artifact = self._active_artifact
        if artifact is None:
            return False
        scope = artifact.scope
        if record.valid_mask & (1 << 0) and record.model and scope.product_identity:
            if record.model.strip().casefold() != scope.product_identity.casefold():
                self._finish_identity_changed("product_identity")
                return False
        expected = dict(scope.immutable_identity_facts).get("product_serial_number", "")
        actual = (
            verified_identity_text(record.serial_number)
            if record.valid_mask & (1 << 1)
            else ""
        )
        if expected and actual:
            if actual.casefold() != expected.casefold():
                self._finish_identity_changed("product_serial_number")
                return False
            self._returned_identity_fact_names.add("product_serial_number")
        return True

    def _accept_hardware_identity(self, record: ServiceHardwareIdentity) -> bool:
        artifact = self._active_artifact
        if artifact is None:
            return False
        expected = dict(artifact.scope.immutable_identity_facts).get(
            "product_device_uid",
            "",
        )
        actual = verified_device_uid(record.device_uid) if record.valid_mask & 1 else ""
        if expected and actual:
            if actual != expected:
                self._finish_identity_changed("product_device_uid")
                return False
            self._returned_identity_fact_names.add("product_device_uid")
        return True

    def _returned_identity_complete(self, scope: DeviceSessionScope) -> bool:
        required = {name for name, _value in scope.immutable_identity_facts}
        return required.issubset(self._returned_identity_fact_names)

    def _returned_firmware_values(
        self,
        scope: DeviceSessionScope,
    ) -> tuple[str, ...]:
        required = tuple(name for name, _value in scope.firmware_facts)
        if required:
            return tuple(
                self._returned_firmware_facts[name]
                for name in required
                if name in self._returned_firmware_facts
            )
        return tuple(self._returned_firmware_facts.values())

    def _returned_firmware_complete(self, scope: DeviceSessionScope) -> bool:
        required = {name for name, _value in scope.firmware_facts}
        if required:
            return required.issubset(self._returned_firmware_facts)
        return bool(self._returned_firmware_facts)

    def _returned_firmware_consistent(self, scope: DeviceSessionScope) -> bool:
        values = self._returned_firmware_values(scope)
        if len(values) < 2:
            return bool(values)
        try:
            return all(
                device_firmware_versions_equal(values[0], value)
                for value in values[1:]
            )
        except ValueError:
            return False

    def _returned_firmware_value(self, scope: DeviceSessionScope) -> str:
        values = dict(self._returned_firmware_facts)
        required = {name for name, _value in scope.firmware_facts}
        for name in ("product_firmware", "debug_firmware"):
            if (not required or name in required) and values.get(name):
                return values[name]
        returned = self._returned_firmware_values(scope)
        return returned[0] if returned else ""

    def _finish_identity_changed(self, field: str) -> None:
        self._finish_unknown(
            OtaStatus.RETURN_IDENTITY_CHANGED,
            "A different device identity returned after OTA transfer",
            restore_debug=False,
            field=field,
        )

    def _evaluate_returned_firmware(self) -> None:
        artifact = self._active_artifact
        if artifact is None or not self._waiting_for_reboot_evidence():
            return
        if (
            artifact.scope.has_immutable_identity
            and not self._returned_identity_complete(artifact.scope)
        ):
            return
        if not self._returned_firmware_complete(artifact.scope):
            return
        if not self._returned_firmware_consistent(artifact.scope):
            return
        returned = self._returned_firmware_value(artifact.scope)
        if not returned:
            return
        before = self._firmware_before or "?"
        self._known_debug_enabled = False
        if artifact.source is OtaArtifactSource.VERIFIED_PACKAGE:
            returned_versions = self._returned_firmware_values(artifact.scope)
            if all(
                self._versions_equal(artifact.target_version, value)
                for value in returned_versions
            ):
                if self._versions_equal(artifact.target_version, before):
                    self._finish_unknown(
                        OtaStatus.APPLICATION_UNCONFIRMED,
                        "OTA package application was not independently confirmed",
                        restore_debug=False,
                        version=returned,
                    )
                else:
                    self._finish(
                        OtaStatus.TARGET_VERSION_CONFIRMED,
                        restore_debug=False,
                        before=before,
                        after=returned,
                        version=artifact.target_version,
                    )
            return
        if self._firmware_before and not self._device_versions_equal(
            self._firmware_before,
            returned,
        ):
            self._finish(
                OtaStatus.FIRMWARE_CHANGED,
                restore_debug=False,
                before=before,
                after=returned,
            )
        else:
            self._finish_unknown(
                OtaStatus.APPLICATION_UNCONFIRMED,
                "OTA firmware application was not independently confirmed",
                restore_debug=False,
                version=returned,
            )

    @staticmethod
    def _versions_equal(package_version: str, device_version: str) -> bool:
        try:
            return firmware_versions_equal(package_version, device_version)
        except ValueError:
            return False

    @staticmethod
    def _device_versions_equal(first: str, second: str) -> bool:
        try:
            return device_firmware_versions_equal(first, second)
        except ValueError:
            return False

    def start(
        self,
        *,
        pause_debug: bool,
        required_artifact: OtaArtifactToken | None = None,
    ) -> bool:
        if self.active or not self._connected or not self.supported:
            self._emit_capability_status()
            return False
        artifact = self._artifact
        if artifact is None:
            self.status_changed.emit(OtaStatus.NO_FILE, {})
            return False
        if artifact.source is OtaArtifactSource.VERIFIED_PACKAGE:
            if required_artifact != artifact.token:
                self.status_changed.emit(OtaStatus.ARTIFACT_MISMATCH, {})
                return False
        elif required_artifact is not None:
            self.status_changed.emit(OtaStatus.ARTIFACT_MISMATCH, {})
            return False
        if not self._session.operation_scope_matches(artifact.scope):
            self.clear_file()
            self.status_changed.emit(OtaStatus.SESSION_CHANGED, {})
            return False
        try:
            build_ota_begin(len(artifact.data), artifact.filename)
        except CodecError:
            self.status_changed.emit(OtaStatus.BEGIN_SEND_FAILED, {})
            return False
        if not self._operation_gateway.try_acquire_operation(
            self._lease_token,
            purpose="ota",
        ):
            self.status_changed.emit(OtaStatus.TRANSACTION_ACTIVE, {})
            return False
        self._active = True
        self._mutation_sent = False
        self._active_artifact = artifact
        self._sequence = 0
        self._retry = 0
        self._total_chunks = (
            len(artifact.data) + OTA_CHUNK_SIZE - 1
        ) // OTA_CHUNK_SIZE
        self._start_time = time.monotonic()
        self._paused_debug = bool(pause_debug)
        self._restore_debug = self._paused_debug and self._known_debug_enabled
        if not self._update_operation_context("prepare"):
            self._finish(OtaStatus.BEGIN_SEND_FAILED)
            return False
        self.progress_changed.emit(0)
        self._session.set_handshake_retries_paused(True)
        if self._paused_debug:
            self._set_state(OtaState.QUIESCE)
            self.status_changed.emit(OtaStatus.STOPPING_LIVE_DATA, {})
            self.debug_mode_requested.emit(False)
        else:
            self._send_begin()
        return True

    def on_debug_request_finished(self, target: bool, ok: bool, detail: str) -> None:
        if not self._active or self._state is not OtaState.QUIESCE or target:
            return
        if not ok:
            self._finish(
                OtaStatus.STOP_LIVE_DATA_FAILED,
                detail=detail,
            )
            return
        self._send_begin()

    def abort(self) -> None:
        if not self._active:
            return
        if not self._mutation_sent:
            self._finish(OtaStatus.ABORTED)
            return
        if self._state is OtaState.WAIT_REBOOT:
            self._finish_unknown(
                OtaStatus.ABORT_PENDING,
                "OTA reboot verification was abandoned without terminal device evidence",
                restore_debug=False,
            )
            return
        self._begin_terminal_abort("OTA was aborted by the operator")

    def _begin_terminal_abort(self, reason: str) -> None:
        """Send OTA_ABORT while retaining ownership until device evidence exists."""

        if not self._update_operation_context("abort"):
            self._finish_unknown(
                OtaStatus.ABORT_PENDING,
                f"{reason}; OTA abort context could not be frozen",
                restore_debug=False,
            )
            return
        if not self._operation_gateway.begin_terminating(self._lease_token):
            self._finish_unknown(
                OtaStatus.ABORT_PENDING,
                f"{reason}; OTA abort could not enter the terminating state",
                restore_debug=False,
            )
            return
        self._set_state(OtaState.TERMINATING)
        self._response_timer.stop()
        self._reboot_timer.stop()
        self._session.set_handshake_retries_paused(False)
        sent = self._command_sender.send(
            build_ota_abort(),
            operation=SessionOperationClass.TERMINAL,
        )
        self.status_changed.emit(OtaStatus.ABORT_PENDING, {"sent": bool(sent)})
        if not sent:
            self._finish_unknown(
                OtaStatus.ABORT_PENDING,
                f"{reason}; OTA abort datagram could not be written",
                restore_debug=False,
                sent=False,
            )
            return
        # DEBUG v2 has no OTA_ABORT acknowledgement. Keep the local owner in
        # TERMINATING until the bounded abort timeout closes this transaction.
        self._response_timer.start(OTA_END_TIMEOUT_MS)

    def _refresh_capability(self) -> None:
        previous = self._capability
        if not self._connected:
            capability = OtaCapabilityState.DISCONNECTED
        elif not self._hardware:
            capability = OtaCapabilityState.WAITING_PROFILE
        else:
            profile = self._session.profile_store
            ota = profile.capability_status(self._hardware, "ota")
            context = profile.capability_status(
                self._hardware,
                "command_response_context",
            )
            if all(status is CapabilitySupport.SUPPORTED for status in (ota, context)):
                capability = OtaCapabilityState.SUPPORTED
            elif CapabilitySupport.UNKNOWN in {ota, context}:
                capability = OtaCapabilityState.WAITING_PROFILE
            else:
                capability = OtaCapabilityState.UNSUPPORTED
        self._capability = capability
        if previous is not capability:
            self.capability_changed.emit(capability)
        if not self.active:
            self._emit_capability_status()

    def _emit_capability_status(self) -> None:
        status = {
            OtaCapabilityState.DISCONNECTED: OtaStatus.IDLE,
            OtaCapabilityState.WAITING_PROFILE: (
                OtaStatus.WAITING_PROFILE
                if not self._hardware
                else OtaStatus.WAITING_CAPABILITY
            ),
            OtaCapabilityState.SUPPORTED: OtaStatus.IDLE,
            OtaCapabilityState.UNSUPPORTED: OtaStatus.UNSUPPORTED,
        }[self._capability]
        self.status_changed.emit(status, {})

    def _send_begin(self) -> None:
        artifact = self._active_artifact
        if not self._active or artifact is None:
            return
        self._set_state(OtaState.BEGIN)
        self.status_changed.emit(OtaStatus.SENDING_BEGIN, {})
        if not self._update_operation_context("begin"):
            self._finish(OtaStatus.BEGIN_SEND_FAILED)
            return
        if not self._command_sender.send(
            build_ota_begin(len(artifact.data), artifact.filename),
            operation=SessionOperationClass.MUTATING,
        ):
            self._finish(OtaStatus.BEGIN_SEND_FAILED)
            return
        self._mutation_sent = True
        self._response_timer.start(OTA_BEGIN_TIMEOUT_MS)

    def _send_current_chunk(self) -> None:
        artifact = self._active_artifact
        if not self._active or artifact is None:
            return
        if self._sequence >= self._total_chunks:
            self._send_end()
            return
        offset = self._sequence * OTA_CHUNK_SIZE
        chunk = artifact.data[offset:offset + OTA_CHUNK_SIZE]
        self._set_state(OtaState.DATA)
        try:
            frame = build_ota_data(self._sequence, chunk)
        except CodecError:
            self._finish_unknown(
                OtaStatus.CHUNK_SEND_FAILED,
                "OTA data frame could not be built after the transfer had started",
                sequence=self._sequence,
            )
            return
        if not self._update_operation_context("data", sequence=self._sequence):
            self._finish_unknown(
                OtaStatus.CHUNK_SEND_FAILED,
                "OTA data context could not be frozen after the transfer had started",
                sequence=self._sequence,
            )
            return
        if not self._command_sender.send(
            frame,
            operation=SessionOperationClass.MUTATING,
        ):
            self._finish_unknown(
                OtaStatus.CHUNK_SEND_FAILED,
                "OTA data datagram could not be written after the transfer had started",
                sequence=self._sequence,
            )
            return
        self._mutation_sent = True
        self._response_timer.start(OTA_CHUNK_TIMEOUT_MS)

    def _send_end(self) -> None:
        artifact = self._active_artifact
        if not self._active or artifact is None:
            return
        self._set_state(OtaState.END)
        self.status_changed.emit(OtaStatus.VERIFYING, {})
        if not self._update_operation_context("end"):
            self._finish_unknown(
                OtaStatus.END_SEND_FAILED,
                "OTA end context could not be frozen after the transfer had started",
            )
            return
        if not self._command_sender.send(
            build_ota_end(artifact.crc32),
            operation=SessionOperationClass.MUTATING,
        ):
            self._finish_unknown(
                OtaStatus.END_SEND_FAILED,
                "OTA end datagram could not be written after the transfer had started",
            )
            return
        self._mutation_sent = True
        self._response_timer.start(OTA_END_TIMEOUT_MS)

    def _apply_response(self, response: CommandResponse) -> None:
        if not self._active:
            return
        if self._state is OtaState.BEGIN:
            self._apply_begin_response(response)
        elif self._state is OtaState.DATA:
            self._apply_data_response(response)
        elif self._state is OtaState.END:
            self._apply_end_response(response)

    def _apply_begin_response(self, response: CommandResponse) -> None:
        message = (response.msg or "").strip()
        if not message.startswith("OTA_BEGIN"):
            return
        if int(response.code) == int(RespCode.SUCCESS):
            if message != "OTA_BEGIN=READY":
                return
            self._response_timer.stop()
            self._retry = 0
            QTimer.singleShot(0, self._send_current_chunk)
            return
        self._finish(
            OtaStatus.BEGIN_REJECTED,
            detail=message or str(response.code),
        )

    def _apply_data_response(self, response: CommandResponse) -> None:
        expected = f"OTA_DATA={self._sequence}"
        message = (response.msg or "").strip()
        if message != expected:
            return
        if int(response.code) == int(RespCode.SUCCESS):
            self._response_timer.stop()
            self._sequence += 1
            self._retry = 0
            self._emit_progress()
            QTimer.singleShot(0, self._send_current_chunk)
            return
        self.status_changed.emit(
            OtaStatus.CHUNK_REJECTED,
            {
                "sequence": self._sequence,
                "detail": message or str(response.code),
            },
        )
        self._begin_terminal_abort(
            "Device rejected an OTA data chunk after the transfer had started"
        )

    def _apply_end_response(self, response: CommandResponse) -> None:
        message = (response.msg or "").strip()
        if not message.startswith("OTA_END"):
            return
        if int(response.code) == int(RespCode.SUCCESS):
            if message != "OTA_END=VERIFIED":
                return
            self._enter_wait_reboot()
            return
        self.status_changed.emit(
            OtaStatus.END_REJECTED,
            {"detail": message or str(response.code)},
        )
        self._begin_terminal_abort(
            "Device rejected OTA_END after receiving the transfer"
        )

    def _emit_progress(self) -> None:
        artifact = self._active_artifact
        if self._total_chunks <= 0 or artifact is None:
            return
        percent = int(self._sequence * 100 / self._total_chunks)
        self.progress_changed.emit(percent)
        elapsed = time.monotonic() - self._start_time
        transferred = min(self._sequence * OTA_CHUNK_SIZE, len(artifact.data))
        values: dict[str, Any] = {
            "sequence": self._sequence,
            "total": self._total_chunks,
            "percent": percent,
        }
        if elapsed > 0.1 and transferred > 0:
            speed = transferred / elapsed
            values["speed"] = speed / 1024.0
            values["remaining"] = int(
                max(0, len(artifact.data) - transferred) / speed
            )
        self.status_changed.emit(OtaStatus.TRANSFERRING, values)

    def _enter_wait_reboot(self) -> None:
        artifact = self._active_artifact
        if artifact is None:
            self._finish(OtaStatus.NO_FILE)
            return
        self._response_timer.stop()
        self._set_state(OtaState.WAIT_REBOOT)
        self.progress_changed.emit(100)
        self._session.set_handshake_retries_paused(False)
        self._firmware_before = (
            artifact.scope.product_firmware
            or artifact.scope.debug_firmware
            or self._firmware_current
        )
        self._returned_firmware_facts.clear()
        self._returned_identity_fact_names.clear()
        self._reboot_started = time.monotonic()
        self._reboot_deadline = self._reboot_started + OTA_REBOOT_TIMEOUT_S
        self.status_changed.emit(OtaStatus.REBOOTING, {"elapsed": 0})
        self._reboot_timer.start()

    @Slot()
    def _on_reboot_tick(self) -> None:
        if not self._active or self._state is not OtaState.WAIT_REBOOT:
            return
        now = time.monotonic()
        if now > self._reboot_deadline:
            self._finish_reboot_timeout()
            return
        self.status_changed.emit(
            OtaStatus.REBOOTING,
            {"elapsed": int(now - self._reboot_started)},
        )
        self._command_sender.send(
            build_request_meta_info(),
            operation=SessionOperationClass.IDENTITY,
        )

    def _finish_reboot_timeout(self) -> None:
        artifact = self._active_artifact
        if artifact is None or not self._returned_firmware_facts:
            self._finish_unknown(
                OtaStatus.REBOOT_TIMEOUT,
                "OTA reboot timed out before identity and firmware were confirmed",
                restore_debug=False,
            )
            return
        if (
            artifact.scope.has_immutable_identity
            and not self._returned_identity_complete(artifact.scope)
        ):
            self._finish_unknown(
                OtaStatus.RETURN_IDENTITY_UNCONFIRMED,
                "OTA return identity was incomplete at the verification deadline",
                restore_debug=False,
            )
            return
        if not self._returned_firmware_complete(artifact.scope):
            self._finish_unknown(
                OtaStatus.RETURN_FIRMWARE_UNCONFIRMED,
                "OTA return firmware facts were incomplete at the verification deadline",
                restore_debug=False,
            )
            return
        if not self._returned_firmware_consistent(artifact.scope):
            self._finish_unknown(
                OtaStatus.FIRMWARE_FACTS_CONFLICT,
                "OTA return firmware facts conflicted at the verification deadline",
                restore_debug=False,
            )
            return
        returned = self._returned_firmware_value(artifact.scope)
        if artifact.source is OtaArtifactSource.VERIFIED_PACKAGE:
            self._finish_unknown(
                OtaStatus.TARGET_VERSION_NOT_CONFIRMED,
                "OTA target version was not confirmed by returned firmware facts",
                restore_debug=False,
                target=artifact.target_version,
                actual=returned,
            )
            return
        self._finish_unknown(
            OtaStatus.APPLICATION_UNCONFIRMED,
            "OTA application was not independently confirmed at the verification deadline",
            restore_debug=False,
            version=returned,
        )

    @Slot()
    def _on_response_timeout(self) -> None:
        if not self._active:
            return
        if self._state is OtaState.DATA:
            if self._retry < OTA_CHUNK_MAX_RETRY:
                self._retry += 1
                self.status_changed.emit(
                    OtaStatus.CHUNK_RETRY,
                    {
                        "sequence": self._sequence,
                        "retry": self._retry,
                        "maximum": OTA_CHUNK_MAX_RETRY,
                    },
                )
                self._send_current_chunk()
            else:
                self._finish_unknown(
                    OtaStatus.CHUNK_TIMEOUT,
                    "OTA data chunk timed out without a matching acknowledgement",
                    sequence=self._sequence,
                )
        elif self._state is OtaState.BEGIN:
            self._finish_unknown(
                OtaStatus.BEGIN_TIMEOUT,
                "OTA begin timed out without a matching acknowledgement",
            )
        elif self._state is OtaState.END:
            self._finish_unknown(
                OtaStatus.END_TIMEOUT,
                "OTA end timed out without a matching acknowledgement",
            )
        elif self._state is OtaState.TERMINATING:
            self._finish_unknown(
                OtaStatus.ABORT_PENDING,
                "OTA abort has no matching protocol evidence; final state is unknown",
                restore_debug=False,
                sent=True,
            )

    def _update_operation_context(
        self,
        stage: str,
        *,
        sequence: int | None = None,
    ) -> bool:
        artifact = self._active_artifact
        if artifact is None:
            return False
        request_id = f"OTA:{artifact.token.value}:{str(stage).strip().lower()}"
        facts: list[tuple[str, object]] = [
            ("artifact_token", artifact.token.value),
            ("artifact_source", artifact.source.value),
            ("filename", artifact.filename),
            ("image_size", len(artifact.data)),
            ("crc32", f"{artifact.crc32:08x}"),
            ("stage", str(stage).strip().lower()),
        ]
        if sequence is not None:
            request_id = f"{request_id}:{int(sequence)}"
            facts.append(("sequence", int(sequence)))
        if not self._operation_gateway.update_operation_context(
            self._lease_token,
            request_id=request_id,
            target_facts=facts,
        ):
            return False
        self._operation_request_id = request_id
        return True

    def _finish_unknown(
        self,
        status: OtaStatus,
        reason: str,
        *,
        restore_debug: bool = False,
        **values: Any,
    ) -> None:
        """Publish an unconfirmed OTA result and release local ownership."""

        del restore_debug
        self._response_timer.stop()
        self._reboot_timer.stop()
        self._session.set_handshake_retries_paused(False)
        self._active = False
        self._mutation_sent = False
        self._operation_request_id = ""
        self._active_artifact = None
        self._returned_identity_fact_names.clear()
        self._returned_firmware_facts.clear()
        self._set_state(OtaState.IDLE)
        self._operation_gateway.release_operation(self._lease_token)
        self.status_changed.emit(status, values)

    def _finish(
        self,
        status: OtaStatus,
        *,
        restore_debug: bool = True,
        **values: Any,
    ) -> None:
        should_restore = restore_debug and self._restore_debug and self._connected
        self._active = False
        self._mutation_sent = False
        self._operation_request_id = ""
        self._response_timer.stop()
        self._reboot_timer.stop()
        self._active_artifact = None
        self._returned_identity_fact_names.clear()
        self._returned_firmware_facts.clear()
        self._session.set_handshake_retries_paused(False)
        self._set_state(OtaState.IDLE)
        self._operation_gateway.release_operation(self._lease_token)
        self.status_changed.emit(status, values)
        self._paused_debug = False
        self._restore_debug = False
        if should_restore:
            QTimer.singleShot(0, lambda: self.debug_mode_requested.emit(True))

    @Slot(object)
    def _on_destroyed(self, _obj=None) -> None:
        self._active = False
        self._operation_gateway.release_operation(self._lease_token)

    def _set_state(self, state: OtaState) -> None:
        if self._state is state:
            return
        self._state = state
        self.state_changed.emit(state)


__all__ = [
    "OTA_BEGIN_TIMEOUT_MS",
    "OTA_CHUNK_MAX_RETRY",
    "OTA_CHUNK_SIZE",
    "OTA_CHUNK_TIMEOUT_MS",
    "OTA_END_TIMEOUT_MS",
    "OTA_MAX_IMAGE_BYTES",
    "OtaArtifactSource",
    "OtaArtifactToken",
    "OtaCapabilityState",
    "OtaController",
    "OtaState",
    "OtaStatus",
]
