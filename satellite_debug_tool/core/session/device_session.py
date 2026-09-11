"""One authoritative protocol and telemetry state for a device endpoint."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.data import (
    EventLog,
    GnssStore,
    OrbitStore,
    StateStore,
    TelemetrySeriesStore,
)
from satellite_debug_tool.core.product import (
    Availability,
    CustomerServiceState,
    LegacyV2Projector,
    ProductServiceStore,
    ProductSnapshot,
    ProductSnapshotResolver,
    customer_service_state,
    verified_device_uid,
    verified_identity_text,
)
from satellite_debug_tool.core.profile import ProfileCache, ProfileStore
from satellite_debug_tool.core.security import device_firmware_versions_equal
from satellite_debug_tool.core.protocol import (
    CommandResponse,
    DataReport,
    EventReport,
    FrameReceiverV2,
    GnssCnrReport,
    GnssSatReport,
    GnssSignalReport,
    GnssSkyReport,
    Heartbeat,
    MetaInfo,
    StateReport,
    build_request_meta_info,
)
from satellite_debug_tool.core.protocol.handshake import Handshake


DeviceEndpoint = tuple[str, int]
Sender = Callable[[bytes], object]


@dataclass(frozen=True)
class DeviceSessionScope:
    """Identity facts that authorize one user-confirmed device operation."""

    generation: int
    endpoint: Optional[DeviceEndpoint]
    hardware_type: str
    debug_serial_number: str
    debug_firmware: str
    product_identity: str
    product_serial_number: str
    product_device_uid: str
    product_firmware: str

    @property
    def immutable_identity_facts(self) -> tuple[tuple[str, str], ...]:
        """Immutable identifiers that were actually proved for this device."""

        facts = (
            ("debug_serial_number", verified_identity_text(self.debug_serial_number)),
            ("product_serial_number", verified_identity_text(self.product_serial_number)),
            ("product_device_uid", verified_device_uid(self.product_device_uid)),
        )
        return tuple((name, value) for name, value in facts if value)

    @property
    def has_immutable_identity(self) -> bool:
        """Whether at least one immutable identifier can bind an operation."""

        return bool(self.immutable_identity_facts)

    @property
    def identity_facts_consistent(self) -> bool:
        """Whether Debug and Product Service agree on the production serial."""

        debug_serial = verified_identity_text(self.debug_serial_number)
        product_serial = verified_identity_text(self.product_serial_number)
        return bool(
            not debug_serial
            or not product_serial
            or debug_serial.casefold() == product_serial.casefold()
        )

    @property
    def product_identity_facts(self) -> tuple[tuple[str, str], ...]:
        """Product Service identifiers suitable for cross-reboot operations."""

        return tuple(
            (name, value)
            for name, value in self.immutable_identity_facts
            if name in {"product_serial_number", "product_device_uid"}
        )

    @property
    def has_product_identity(self) -> bool:
        """Whether Product Service proved a serial number or MCU UID."""

        return bool(self.product_identity_facts)

    @property
    def firmware_facts(self) -> tuple[tuple[str, str], ...]:
        """Firmware versions captured from each available protocol source."""

        facts = (
            ("debug_firmware", str(self.debug_firmware).strip()),
            ("product_firmware", str(self.product_firmware).strip()),
        )
        return tuple((name, value) for name, value in facts if value)

    @property
    def firmware_facts_consistent(self) -> bool:
        """Whether Debug and Product Service agree when both report firmware."""

        values = tuple(value for _name, value in self.firmware_facts)
        if len(values) < 2:
            return True
        try:
            return all(
                device_firmware_versions_equal(values[0], value)
                for value in values[1:]
            )
        except ValueError:
            return False


class DeviceSessionCore(QObject):
    """Own the parser, handshake, stores, and connection generation.

    Transport and presentation objects attach to this core.  Feeding one frame
    updates every canonical store before observers render the resulting record.
    """

    record_received = Signal(object)
    activity = Signal()
    heartbeat_received = Signal()
    command_response = Signal(object)
    profile_ready = Signal(str)
    link_lost = Signal()
    link_restored = Signal()
    connection_changed = Signal(bool)
    generation_changed = Signal(int, object)
    device_transaction_changed = Signal(bool)

    def __init__(
        self,
        *,
        endpoint: Optional[DeviceEndpoint] = None,
        profile_cache: Optional[ProfileCache] = None,
        profile_store: Optional[ProfileStore] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.endpoint = self.normalize_endpoint(endpoint) if endpoint is not None else None
        self.generation = 0
        self.transport: object | None = None
        self.meta_info: MetaInfo | None = None
        self.receiver = FrameReceiverV2()
        self.data_store = TelemetrySeriesStore()
        self.profile_store = profile_store or ProfileStore(
            cache=profile_cache or ProfileCache()
        )
        self.state_store = StateStore()
        self.event_log = EventLog()
        self.gnss_store = GnssStore(parent=self)
        self.orbit_store = OrbitStore(parent=self)
        self.product_store = ProductServiceStore(parent=self)
        self.legacy_projector = LegacyV2Projector(
            self.profile_store,
            self.data_store,
            self.state_store,
        )
        self.product_snapshot_resolver = ProductSnapshotResolver(
            self.product_store,
            self.legacy_projector,
        )
        self._handshake: Handshake | None = None
        self._sender: Sender | None = None
        self._connected = False
        self._request_id = 0
        self._device_transaction_lock = Lock()
        self._device_transaction_owner: object | None = None
        self._managed_transaction_active: Callable[[], bool] | None = None
        self._managed_transaction_available: Callable[[object | None], bool] | None = None

    @staticmethod
    def normalize_endpoint(endpoint: DeviceEndpoint) -> DeviceEndpoint:
        return str(endpoint[0]).strip(), int(endpoint[1])

    @property
    def handshake(self) -> Handshake | None:
        return self._handshake

    @property
    def connected(self) -> bool:
        return self._connected

    def bind_endpoint(self, endpoint: DeviceEndpoint) -> None:
        normalized = self.normalize_endpoint(endpoint)
        if self._connected and self.endpoint is not None and normalized != self.endpoint:
            raise ValueError("connected endpoint changes require begin_connection")
        self.endpoint = normalized

    def attach_transport(
        self,
        transport: object,
        sender: Sender,
    ) -> None:
        """Attach a transport; replacing a live transport starts a new epoch."""

        if self._connected and self.transport is not None and transport is not self.transport:
            self.begin_connection(
                transport=transport,
                sender=sender,
                handshake_enabled=False,
            )
            return
        self.transport = transport
        self._sender = sender
        self._set_connected(True)

    def begin_connection(
        self,
        *,
        endpoint: Optional[DeviceEndpoint] = None,
        transport: object | None = None,
        sender: Sender | None = None,
        handshake_enabled: bool = True,
    ) -> int:
        if endpoint is not None:
            self.endpoint = self.normalize_endpoint(endpoint)
        self.transport = transport
        self._sender = sender
        self.generation += 1
        self._clear_device_transaction()
        self.receiver.reset()
        self.clear_runtime_state()
        self.product_snapshot_resolver.reset()
        self._stop_handshake()
        if handshake_enabled and sender is not None:
            handshake = Handshake(self.profile_store, sender, parent=self)
            handshake.ready.connect(self.profile_ready)
            handshake.link_lost.connect(self.link_lost)
            handshake.link_restored.connect(self.link_restored)
            self._handshake = handshake
            handshake.start()
        self._set_connected(sender is not None)
        self.generation_changed.emit(self.generation, self.endpoint)
        return self.generation

    def end_connection(self) -> None:
        self._stop_handshake()
        self.transport = None
        self._sender = None
        self._set_connected(False)
        self._clear_device_transaction()

    def reset_stream(self) -> None:
        self.receiver.reset()

    def clear_runtime_state(self) -> None:
        """Clear values whose validity belongs to the active connection."""
        self.meta_info = None
        self.state_store.clear()
        self.gnss_store.clear()
        self.orbit_store.clear()
        self.product_store.clear()

    def set_handshake_retries_paused(self, paused: bool) -> None:
        if self._handshake is not None:
            self._handshake.set_retry_paused(bool(paused))

    def tick(self, dt_ms: int) -> None:
        if self._handshake is not None:
            self._handshake.tick(int(dt_ms))

    def send(self, frame: bytes) -> bool:
        sender = self._sender
        return bool(sender(frame)) if sender is not None else False

    @property
    def device_transaction_active(self) -> bool:
        """Whether one device-level command transaction currently owns the link."""

        managed = self._managed_transaction_active
        if managed is not None:
            return bool(managed())
        with self._device_transaction_lock:
            return self._device_transaction_owner is not None

    def device_transaction_available(self, owner: object | None = None) -> bool:
        """Return whether ``owner`` may synchronously claim the device transaction."""

        managed = self._managed_transaction_available
        if managed is not None:
            return bool(managed(owner))
        with self._device_transaction_lock:
            current = self._device_transaction_owner
            return current is None or (owner is not None and current is owner)

    def try_acquire_device_transaction(self, owner: object) -> bool:
        """Atomically claim the single device transaction for an opaque owner token."""

        if owner is None:
            raise ValueError("device transaction owner must not be None")
        if self._managed_transaction_active is not None:
            # Managed UDP callers must present an EndpointSendCapability via a
            # RuntimeOperationGateway; Core alone cannot mint that authority.
            return False
        changed = False
        with self._device_transaction_lock:
            current = self._device_transaction_owner
            if current is not None and current is not owner:
                return False
            if current is None:
                self._device_transaction_owner = owner
                changed = True
        if changed:
            self.device_transaction_changed.emit(True)
        return True

    def release_device_transaction(self, owner: object) -> bool:
        """Release the transaction only when ``owner`` is the current authority."""

        if self._managed_transaction_active is not None:
            return False
        with self._device_transaction_lock:
            if self._device_transaction_owner is not owner:
                return False
            self._device_transaction_owner = None
        try:
            self.device_transaction_changed.emit(False)
        except RuntimeError:
            # Qt may destroy the session before a child controller's destroyed hook runs.
            pass
        return True

    def _clear_device_transaction(self) -> None:
        """Release every operation lease when its connection authority ends."""

        if self._managed_transaction_active is not None:
            return
        with self._device_transaction_lock:
            if self._device_transaction_owner is None:
                return
            self._device_transaction_owner = None
        self.device_transaction_changed.emit(False)

    def bind_managed_transaction_view(
        self,
        *,
        active: Callable[[], bool],
        available: Callable[[object | None], bool],
    ) -> None:
        """Disable the legacy lock and project the Runtime's single operation CAS."""

        with self._device_transaction_lock:
            if self._device_transaction_owner is not None:
                raise RuntimeError("cannot bind Runtime while a legacy transaction is active")
        self._managed_transaction_active = active
        self._managed_transaction_available = available

    def request_meta_info(self) -> bool:
        """Request the canonical device identity and firmware metadata."""

        return self.send(build_request_meta_info())

    def next_request_id(self) -> int:
        self._request_id = (self._request_id + 1) & 0xFFFFFFFF
        if self._request_id == 0:
            self._request_id = 1
        return self._request_id

    def product_snapshot(self) -> ProductSnapshot:
        return self.product_snapshot_resolver.snapshot()

    def customer_service_state(self) -> CustomerServiceState:
        """Expose the registered Product Service state for customer presentation."""
        return customer_service_state(
            hardware_type=self.profile_store.current_hw_type(),
            product_identity=self.product_store.product_identity,
            service_protocol=self.product_store.service_protocol,
            capabilities=self.product_store.capabilities_record,
            connected=self.connected,
            telemetry_ready=self.product_store.telemetry_ready,
        )

    def device_scope(self) -> DeviceSessionScope:
        """Capture the current connection and every available identity fact."""

        meta = self.meta_info
        identity = self.product_store.snapshot().identity

        def valid_text(value) -> str:
            if value.availability != Availability.VALID or value.value is None:
                return ""
            return str(value.value).strip()

        hardware = (
            str(meta.hw_type).strip().lower()
            if meta is not None and meta.hw_type
            else str(self.profile_store.current_hw_type() or "").strip().lower()
        )
        return DeviceSessionScope(
            generation=int(self.generation),
            endpoint=self.endpoint,
            hardware_type=hardware,
            debug_serial_number=(
                str(meta.device_sn).strip() if meta is not None else ""
            ),
            debug_firmware=str(meta.fw_ver).strip() if meta is not None else "",
            product_identity=valid_text(identity.model).lower(),
            product_serial_number=valid_text(identity.serial_number),
            product_device_uid=valid_text(identity.device_uid),
            product_firmware=valid_text(identity.main_firmware),
        )

    def operation_scope_matches(self, scope: DeviceSessionScope) -> bool:
        """Prove the same online connection, identity, and firmware facts."""

        return bool(
            self.connected
            and isinstance(scope, DeviceSessionScope)
            and self.device_scope() == scope
        )

    def feed_bytes(
        self,
        data: bytes,
        *,
        received_monotonic: Optional[float] = None,
    ) -> tuple[object, ...]:
        records = tuple(self.receiver.feed(data))
        self.apply_records(records, received_monotonic=received_monotonic)
        return records

    def apply_records(
        self,
        records: tuple[object, ...],
        *,
        received_monotonic: Optional[float] = None,
    ) -> None:
        """Apply already-decoded records to the canonical session state once."""
        if not records:
            return
        received = time.monotonic() if received_monotonic is None else float(received_monotonic)
        self.activity.emit()
        for record in records:
            handled_by_domain = self.orbit_store.feed(record)
            handled_by_product = self.product_store.feed(
                record,
                received_monotonic=received,
            )

            if not handled_by_domain and not handled_by_product:
                handshake = self._handshake
                if handshake is not None:
                    handshake.feed(record)

                if isinstance(record, DataReport):
                    self.data_store.update(record)
                elif isinstance(record, MetaInfo):
                    self.meta_info = record
                elif isinstance(record, CommandResponse):
                    self.command_response.emit(record)
                elif isinstance(record, Heartbeat):
                    self.heartbeat_received.emit()
                elif isinstance(
                    record,
                    (GnssSkyReport, GnssCnrReport, GnssSatReport, GnssSignalReport),
                ):
                    self.gnss_store.update(record)
                else:
                    hardware = self.profile_store.current_hw_type()
                    if hardware is not None:
                        if isinstance(record, StateReport):
                            self.state_store.update(hardware, record)
                        elif isinstance(record, EventReport):
                            self.event_log.add(hardware, record, self.profile_store)

            # Observers always see a record after its canonical store has been updated.
            self.record_received.emit(record)

    def _set_connected(self, connected: bool) -> None:
        connected = bool(connected)
        if self._connected == connected:
            return
        self._connected = connected
        self.connection_changed.emit(connected)

    def _stop_handshake(self) -> None:
        handshake = self._handshake
        if handshake is None:
            return
        handshake.stop()
        handshake.deleteLater()
        self._handshake = None
