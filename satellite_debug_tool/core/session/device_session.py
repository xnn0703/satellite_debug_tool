"""One authoritative protocol and telemetry state for a device endpoint."""

from __future__ import annotations

import time
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
    LegacyV2Projector,
    ProductServiceStore,
    ProductSnapshot,
    ProductSnapshotResolver,
)
from satellite_debug_tool.core.profile import ProfileCache, ProfileStore
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
        self.endpoint = self.normalize_endpoint(endpoint)

    def attach_transport(
        self,
        transport: object,
        sender: Sender,
    ) -> None:
        """Attach an already-established transport without starting a new epoch."""
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
            self.bind_endpoint(endpoint)
        self.transport = transport
        self._sender = sender
        self.generation += 1
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
