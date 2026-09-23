"""Shared UDP discovery, endpoint demultiplexing, and production sessions."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import inspect
import ipaddress
import math
from pathlib import Path
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal, Slot

from satellite_debug_tool.core.comm import (
    AdmissionClaimLease,
    BrokerConfigurationError,
    BrokerDemandLease,
    DiscoveryLease,
    EndpointDatagram,
    UdpEndpointBroker,
    UdpEndpointBrokerStatistics,
)

from satellite_debug_tool.core.product import (
    product_identity_matches,
    production_product_policy,
    verified_device_uid,
    verified_identity_text,
)
from satellite_debug_tool.core.profile.cache import profile_to_dict
from satellite_debug_tool.core.protocol import (
    FrameReceiverV2,
    RawFrame,
    ServiceFastState,
    ServiceHardwareIdentity,
    ServiceIdentity,
    build_service_subscribe,
)
from satellite_debug_tool.core.session import (
    DeviceSessionCore,
    EndpointSessionDirectory,
    EndpointSessionRuntime,
    ProductionAttachmentLease,
    RuntimeOperationGateway,
    SessionOperationClass,
    SessionRecorderKind,
    SessionRecorderLease,
    SessionRegistry,
)
from satellite_debug_tool.io.data_recorder import DataRecorder, SDB_VERSION_V3
from satellite_debug_tool.io.recording_path_registry import (
    RecordingPathError,
    RecordingPathRegistry,
)


Endpoint = tuple[str, int]
_SNR_HISTORY_SECONDS = 300.0
_SNR_CAPTURE_RATE_HZ = 20
_SNR_HISTORY_CAPACITY = int(_SNR_HISTORY_SECONDS * _SNR_CAPTURE_RATE_HZ) + (
    10 * _SNR_CAPTURE_RATE_HZ
)
_CANDIDATE_LIMIT = 64
_CANDIDATE_BYTE_LIMIT = 64 * 1024
_CANDIDATE_TIMEOUT_NS = 10_000_000_000


class FleetConfigurationError(ValueError):
    pass


class DeviceSessionState(str, Enum):
    DISCOVERED = "discovered"
    IDENTITY_PENDING = "identity_pending"
    IDENTIFIED = "identified"
    CONFLICT = "conflict"
    UNSUPPORTED = "unsupported"


FleetDatagram = EndpointDatagram


@dataclass(frozen=True)
class SnrSample:
    """One SNR sample on the workstation's shared monotonic timeline."""

    monotonic_ns: int
    device_uptime_ms: int
    value_db: float


@dataclass
class _CandidateSession:
    receiver: FrameReceiverV2
    decoded_records: list[object]
    datagrams: list[FleetDatagram]
    received_bytes: int
    last_monotonic_ns: int


FleetHubStatistics = UdpEndpointBrokerStatistics


def _production_identity_state(
    identity: ServiceIdentity,
) -> DeviceSessionState:
    """Classify one declared product without conflating support and missing SN."""

    model = identity.model.strip() if identity.valid_mask & (1 << 0) else ""
    policy = production_product_policy(model)
    if not product_identity_matches(policy, model):
        return DeviceSessionState.UNSUPPORTED
    if (
        not (identity.valid_mask & (1 << 4))
        or int(identity.protocol_version) not in policy.supported_service_protocols
    ):
        return DeviceSessionState.UNSUPPORTED
    serial = (
        verified_identity_text(identity.serial_number)
        if identity.valid_mask & (1 << 1)
        else ""
    )
    return (
        DeviceSessionState.IDENTIFIED
        if serial
        else DeviceSessionState.IDENTITY_PENDING
    )


class UdpFleetHub(QObject):
    """Production discovery facet over the process-wide UDP broker.

    The compatibility name remains for callers, but this object never owns a
    second socket.  Its leases only add the Production CIDR admission and
    discovery demand to the injected broker.
    """

    datagram_received = Signal(object)
    listening = Signal(str, int)
    stopped = Signal()
    error = Signal(str)
    statistics_changed = Signal(object)

    def __init__(
        self,
        *,
        discovery_cidr: str,
        local_port: int,
        device_port: int = 4004,
        discovery_interval_s: float = 5.0,
        fast_rate_hz: int = 1,
        broker: UdpEndpointBroker | None = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        try:
            network = ipaddress.ip_network(discovery_cidr, strict=False)
        except ValueError as exc:
            raise FleetConfigurationError(f"invalid discovery CIDR: {exc}") from exc
        if network.version != 4:
            raise FleetConfigurationError("only IPv4 discovery is supported")
        host_count = max(1, int(network.num_addresses) - 2)
        if host_count > 1024:
            raise FleetConfigurationError("discovery CIDR cannot contain more than 1024 hosts")
        if not (0 <= int(local_port) <= 65535):
            raise FleetConfigurationError("local UDP port is out of range")
        if not (1 <= int(device_port) <= 65535):
            raise FleetConfigurationError("device UDP port is out of range")
        if not (0.2 <= float(discovery_interval_s) <= 300.0):
            raise FleetConfigurationError("discovery interval must be 0.2..300 seconds")
        if not (1 <= int(fast_rate_hz) <= 20):
            raise FleetConfigurationError("product-service rate must be 1..20 Hz")

        self._network = network
        self._device_port = int(device_port)
        self._discovery_interval_s = float(discovery_interval_s)
        self._fast_rate_hz = int(fast_rate_hz)
        if broker is not None and int(local_port) not in {0, broker.local_port}:
            raise FleetConfigurationError(
                "Production local port must match the process-wide UDP broker"
            )
        try:
            self._broker = broker or UdpEndpointBroker(
                local_port=int(local_port),
                parent=self,
            )
        except BrokerConfigurationError as exc:
            raise FleetConfigurationError(str(exc)) from exc
        self._request_id = 0
        self._claim: AdmissionClaimLease | None = None
        self._discovery: DiscoveryLease | None = None
        self._demand: BrokerDemandLease | None = None
        self._broker.datagram_received.connect(self.datagram_received)
        self._broker.listening.connect(self.listening)
        self._broker.stopped.connect(self.stopped)
        self._broker.error.connect(self.error)
        self._broker.statistics_changed.connect(self.statistics_changed)

    @property
    def broker(self) -> UdpEndpointBroker:
        return self._broker

    @property
    def discovery_cidr(self) -> str:
        return str(self._network)

    @property
    def local_port(self) -> int:
        return self._broker.local_port

    @property
    def active_local_port(self) -> int | None:
        return self._broker.active_local_port

    @property
    def has_active_demand(self) -> bool:
        return self._broker.has_active_demand

    @property
    def demand_count(self) -> int:
        return self._broker.demand_count

    @property
    def discovery_count(self) -> int:
        return self._broker.discovery_count

    @property
    def device_port(self) -> int:
        return self._device_port

    @property
    def statistics(self) -> FleetHubStatistics:
        return self._broker.statistics

    @property
    def admission_claim(self) -> AdmissionClaimLease | None:
        return self._claim

    def isRunning(self) -> bool:  # noqa: N802 - Qt compatibility surface
        # The process-wide broker may already be running for Customer.  This
        # compatibility surface must describe the Production facet itself,
        # otherwise opening Production after Customer attach skips discovery.
        return all(
            lease is not None
            for lease in (self._claim, self._discovery, self._demand)
        )

    def start_hub(self) -> bool:
        if self._demand is not None:
            return True
        try:
            claim = self._broker.register_cidr_claim(
                str(self._network),
                device_port=self._device_port,
                owner=f"production-discovery:{id(self)}",
                facet="production",
            )
            discovery = self._broker.register_discovery(
                claim,
                interval_s=self._discovery_interval_s,
                frame_factory=self._next_discovery_frame,
            )
            demand = self._broker.acquire_demand(
                f"production-discovery:{id(self)}"
            )
        except Exception as exc:
            if "discovery" in locals():
                discovery.release()
            if "claim" in locals():
                claim.release()
            self.error.emit(f"cannot start Production UDP discovery: {exc}")
            return False
        self._claim = claim
        self._discovery = discovery
        self._demand = demand
        return True

    def stop_hub(self, timeout_ms: int = 3000) -> bool:
        discovery, self._discovery = self._discovery, None
        demand, self._demand = self._demand, None
        claim, self._claim = self._claim, None
        if discovery is not None:
            discovery.release()
        if demand is not None:
            demand.release()
        if claim is not None:
            claim.release()
        if self._broker.demand_count == 0:
            return self._broker.stop_broker(timeout_ms)
        return True

    def send_to(self, endpoint: Endpoint, data: bytes) -> bool:
        return self._broker.send_to(endpoint, data)

    # Runtime/Directory can use this compatibility facet as a broker.  These
    # methods delegate ownership to the same process-wide socket.
    def acquire_demand(self, owner: str) -> BrokerDemandLease:
        return self._broker.acquire_demand(owner)

    def register_exact_claim(self, endpoint, *, owner: str, facet: str):
        return self._broker.register_exact_claim(endpoint, owner=owner, facet=facet)

    def register_cidr_claim(self, discovery_cidr, *, device_port: int, owner: str, facet: str):
        return self._broker.register_cidr_claim(
            discovery_cidr,
            device_port=device_port,
            owner=owner,
            facet=facet,
        )

    def matched_claims(self, endpoint):
        return self._broker.matched_claims(endpoint)

    def claim_active(self, token: int) -> bool:
        return self._broker.claim_active(token)

    def claim_allows(self, token: int, endpoint, *, facet: str | None = None) -> bool:
        return self._broker.claim_allows(token, endpoint, facet=facet)

    def discovery_demands_for(self, endpoint: Endpoint) -> int:
        return self._broker.discovery_demands_for(endpoint)

    def hold_endpoint_retirement(self, endpoint: Endpoint):
        return self._broker.hold_endpoint_retirement(endpoint)

    def hold_global_resource_exclusion(self):
        return self._broker.hold_global_resource_exclusion()

    def shutdown(self, timeout_ms: int = 3000) -> bool:
        return self._broker.shutdown(timeout_ms)

    def close(self, timeout_ms: int = 3000) -> bool:
        return self.shutdown(timeout_ms)

    def _next_discovery_frame(self) -> bytes:
        self._request_id = (self._request_id + 1) & 0xFFFFFFFF
        return build_service_subscribe(self._request_id, self._fast_rate_hz)


class DeviceSession(QObject):
    """One endpoint-isolated decoder, product store, and SDB recorder."""

    updated = Signal(object)
    identity_changed = Signal(object)
    hardware_identity_changed = Signal(object)
    recording_changed = Signal(object)

    def __init__(
        self,
        endpoint: Endpoint,
        slot: int,
        *,
        session_core: Optional[DeviceSessionCore] = None,
        runtime: EndpointSessionRuntime | None = None,
        attachment: ProductionAttachmentLease | None = None,
        session_owner: str = "production",
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.endpoint = (str(endpoint[0]), int(endpoint[1]))
        self.slot = int(slot)
        self.session_owner = str(session_owner)
        if runtime is not None and session_core is not None and runtime.core is not session_core:
            raise ValueError("Production session Core must be owned by its Runtime")
        self.runtime = runtime
        self.attachment = attachment
        self.core = (runtime.core if runtime is not None else session_core) or DeviceSessionCore(
            endpoint=self.endpoint,
            parent=self,
        )
        self.receiver = self.core.receiver
        self.product_store = self.core.product_store
        self._identity_state = DeviceSessionState.DISCOVERED
        self._identity_conflict = False
        self.identity: Optional[ServiceIdentity] = None
        self.hardware_identity: Optional[ServiceHardwareIdentity] = None
        self.last_seen_monotonic_ns = 0
        self.received_datagrams = 0
        self.received_bytes = 0
        self._snr_history: deque[SnrSample] = deque(
            maxlen=_SNR_HISTORY_CAPACITY
        )
        self._recorder: Optional[DataRecorder] = None
        self._recorder_lease: SessionRecorderLease | None = None
        self._recording_batch_id = ""
        self._recording_root: Optional[Path] = None
        self._recording_final_path: Optional[Path] = None
        self._recording_complete: Optional[bool] = None
        self._runtime_record_overrides: dict[tuple[int, int, bytes], bool] = {}
        if runtime is not None:
            self._connect_runtime(runtime)

    @property
    def serial_number(self) -> str:
        identity = self.identity
        if identity is None or not (identity.valid_mask & (1 << 1)):
            return ""
        return verified_identity_text(identity.serial_number)

    @property
    def state(self) -> DeviceSessionState:
        """Return the single reduced production identity state.

        Identity conflicts are session-lifetime evidence.  A later identity
        report may refresh timestamps or fields, but cannot make that evidence
        disappear without creating a new session.
        """

        if self._identity_conflict:
            return DeviceSessionState.CONFLICT
        return self._identity_state

    @property
    def hardware_type(self) -> str:
        identity = self.identity
        if identity is None or not (identity.valid_mask & (1 << 0)):
            return ""
        return identity.model.strip()

    @property
    def device_uid(self) -> str:
        identity = self.hardware_identity
        if identity is None or not (identity.valid_mask & (1 << 0)):
            return ""
        return verified_device_uid(identity.device_uid)

    @property
    def mac_address(self) -> str:
        identity = self.hardware_identity
        if identity is None or not (identity.valid_mask & (1 << 1)):
            return ""
        return identity.mac_text

    @property
    def mac_source(self) -> Optional[int]:
        identity = self.hardware_identity
        if identity is None or not (identity.valid_mask & (1 << 2)):
            return None
        return int(identity.mac_source)

    @property
    def identity_key(self) -> str:
        if self.device_uid:
            return f"uid:{self.device_uid}"
        if self.serial_number:
            return f"sn:{self.serial_number}"
        return ""

    @property
    def recording_armed(self) -> bool:
        # A recorder whose bounded stop has not completed still owns its file
        # and Runtime lease.  Keep that ownership visible as armed/busy so a
        # second batch cannot replace the only retry handle.
        return self._recorder is not None

    @property
    def recording_finalize_pending(self) -> bool:
        recorder = self._recorder
        return recorder is not None and not recorder.is_recording

    @property
    def recording_path(self) -> Optional[Path]:
        if self._recording_final_path is not None:
            return self._recording_final_path
        return None if self._recorder is None else self._recorder.filepath

    @property
    def recording_complete(self) -> Optional[bool]:
        return self._recording_complete

    def is_online(self, *, now_monotonic_ns: Optional[int] = None, timeout_s: float = 3.0) -> bool:
        if self.runtime is not None:
            last = self.runtime.last_valid_record_at
            if last is None:
                return False
            now_ns = (
                time.monotonic_ns()
                if now_monotonic_ns is None
                else int(now_monotonic_ns)
            )
            return (
                now_ns - int(last * 1_000_000_000)
                <= int(float(timeout_s) * 1_000_000_000)
            )
        if self.last_seen_monotonic_ns <= 0:
            return False
        now = time.monotonic_ns() if now_monotonic_ns is None else int(now_monotonic_ns)
        return now - self.last_seen_monotonic_ns <= int(float(timeout_s) * 1_000_000_000)

    def snr_history(
        self,
        *,
        window_s: float = _SNR_HISTORY_SECONDS,
        now_monotonic_ns: Optional[int] = None,
    ) -> tuple[SnrSample, ...]:
        """Return recent samples without resampling or changing capture order."""

        if not self._snr_history:
            return ()
        now_ns = (
            time.monotonic_ns()
            if now_monotonic_ns is None
            else int(now_monotonic_ns)
        )
        cutoff_ns = now_ns - int(max(0.0, float(window_s)) * 1_000_000_000)
        while self._snr_history and self._snr_history[0].monotonic_ns < cutoff_ns:
            self._snr_history.popleft()
        return tuple(self._snr_history)

    def clear_snr_history(self) -> None:
        self._snr_history.clear()

    def arm_recording(self, batch_id: str, output_root: str | Path) -> bool:
        if self.recording_armed:
            if self._recording_batch_id != str(batch_id):
                raise RuntimeError(
                    "device evidence recording already belongs to another batch"
                )
            return not self.recording_finalize_pending
        recorder_lease: SessionRecorderLease | None = None
        attachment = self.attachment
        if attachment is not None:
            recorder_lease = attachment.new_operation_gateway().acquire_recorder(
                self.session_owner,
                kind=SessionRecorderKind.PRODUCTION_EVIDENCE,
            )
            if recorder_lease is None:
                return False
        root = Path(output_root).expanduser().resolve()
        staging = root / "_staging"
        staging.mkdir(parents=True, exist_ok=True)
        endpoint_slug = f"{self.endpoint[0].replace('.', '_')}-{self.endpoint[1]}"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = staging / f"slot{self.slot}-{endpoint_slug}-{stamp}.sdb"
        reservation = None
        try:
            reservation = RecordingPathRegistry.default().reserve_unique(path)
            hardware_type = self.core.profile_store.current_hw_type()
            profile = (
                self.core.profile_store.get_profile(hardware_type)
                if hardware_type
                else None
            )
            recorder = DataRecorder(
                reservation.path,
                profile_dict=None if profile is None else profile_to_dict(profile),
                format_version=SDB_VERSION_V3,
                metadata={
                    "mode": "production",
                    "batch_id": str(batch_id),
                    "slot": self.slot,
                    "endpoint": f"{self.endpoint[0]}:{self.endpoint[1]}",
                },
            )
        except (OSError, RecordingPathError, ValueError):
            if reservation is not None:
                reservation.discard_failed_file()
            if recorder_lease is not None:
                recorder_lease.release()
            return False
        start_parameters = inspect.signature(recorder.start).parameters
        if not start_parameters:
            # Compatibility for legacy recorder subclasses/test doubles whose
            # override predates reservation ownership transfer.
            reservation.discard_failed_file()
            started = recorder.start()
        else:
            started = recorder.start(reservation)
        if not started:
            if recorder_lease is not None:
                recorder_lease.release()
            return False
        self._recorder = recorder
        self._recorder_lease = recorder_lease
        self._recording_batch_id = str(batch_id)
        self._recording_root = root
        self._recording_final_path = None
        self._recording_complete = None
        self.recording_changed.emit(self)
        return True

    def feed_datagram(self, datagram: FleetDatagram, *, record: bool = True) -> tuple[object, ...]:
        if datagram.endpoint != self.endpoint:
            raise ValueError("datagram endpoint does not match device session")
        if self.runtime is not None:
            key = self._datagram_key(datagram)
            self._runtime_record_overrides[key] = bool(record)
            try:
                return self.runtime.feed_datagram(datagram)
            finally:
                self._runtime_record_overrides.pop(key, None)
        self._record_datagram(datagram, record=record)
        records = self.core.feed_bytes(
            datagram.data,
            received_monotonic=datagram.monotonic_ns / 1_000_000_000.0,
        )
        return self._apply_records(datagram, records)

    def feed_decoded_datagram(
        self,
        datagram: FleetDatagram,
        records: tuple[object, ...],
        *,
        record: bool = True,
    ) -> tuple[object, ...]:
        """Accept a discovery datagram whose envelope was already decoded once."""

        if datagram.endpoint != self.endpoint:
            raise ValueError("datagram endpoint does not match device session")
        if self.runtime is not None:
            key = self._datagram_key(datagram)
            self._runtime_record_overrides[key] = bool(record)
            try:
                return self.runtime.feed_decoded_datagram(datagram, records)
            finally:
                self._runtime_record_overrides.pop(key, None)
        self._record_datagram(datagram, record=record)
        self.core.apply_records(
            records,
            received_monotonic=datagram.monotonic_ns / 1_000_000_000.0,
        )
        return self._apply_records(datagram, records)

    def apply_observed_records(
        self,
        datagram: FleetDatagram,
        records: tuple[object, ...],
    ) -> tuple[object, ...]:
        """Project records already applied once by the authoritative Runtime."""

        if datagram.endpoint != self.endpoint:
            raise ValueError("datagram endpoint does not match device session")
        return self._apply_records(datagram, tuple(records))

    @Slot(object)
    def _on_runtime_datagram(self, datagram: FleetDatagram) -> None:
        if datagram.endpoint != self.endpoint:
            return
        self._record_datagram(
            datagram,
            record=self._runtime_record_overrides.get(
                self._datagram_key(datagram),
                True,
            ),
        )

    @Slot(object, object)
    def _on_runtime_records(
        self,
        datagram: FleetDatagram,
        records: tuple[object, ...],
    ) -> None:
        if datagram.endpoint == self.endpoint:
            self._apply_records(datagram, tuple(records))

    def release_attachment(self) -> bool:
        attachment = self.attachment
        if attachment is None:
            return True
        if not attachment.release():
            return False
        runtime = self.runtime
        if runtime is not None:
            self._disconnect_runtime(runtime)
        self.attachment = None
        self.runtime = None
        return True

    @staticmethod
    def _datagram_key(datagram: FleetDatagram) -> tuple[int, int, bytes]:
        return (
            int(datagram.wall_time_ns),
            int(datagram.monotonic_ns),
            bytes(datagram.data),
        )

    def _connect_runtime(self, runtime: EndpointSessionRuntime) -> None:
        runtime.datagram_received.connect(self._on_runtime_datagram)
        runtime.records_received.connect(self._on_runtime_records)

    def _disconnect_runtime(self, runtime: EndpointSessionRuntime) -> None:
        for signal, slot in (
            (runtime.datagram_received, self._on_runtime_datagram),
            (runtime.records_received, self._on_runtime_records),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _record_datagram(self, datagram: FleetDatagram, *, record: bool) -> None:
        if record and self._recorder is not None:
            self._recorder.write_frame(
                datagram.data,
                host_timestamp_ns=datagram.wall_time_ns,
            )

    def _apply_records(
        self,
        datagram: FleetDatagram,
        records: tuple[object, ...],
    ) -> tuple[object, ...]:
        if not records:
            return records
        self.last_seen_monotonic_ns = int(datagram.monotonic_ns)
        self.received_datagrams += 1
        self.received_bytes += len(datagram.data)
        identity_changed = False
        for item in records:
            if (
                isinstance(item, ServiceFastState)
                and item.valid_mask & (1 << 11)
                and math.isfinite(float(item.snr_db))
            ):
                self._snr_history.append(
                    SnrSample(
                        monotonic_ns=int(datagram.monotonic_ns),
                        device_uptime_ms=int(item.timestamp),
                        value_db=float(item.snr_db),
                    )
                )
            if isinstance(item, ServiceIdentity):
                changed = item != self.identity
                self.identity = item
                self._identity_state = _production_identity_state(item)
                if self._recorder is not None:
                    self._recorder.write_metadata_event(
                        {
                            "type": "device_identity",
                            "model": item.model,
                            "serial_number": item.serial_number,
                            "main_firmware": item.main_firmware,
                        },
                        host_timestamp_ns=datagram.wall_time_ns,
                    )
                if changed:
                    identity_changed = True
                    self.identity_changed.emit(item)
            elif isinstance(item, ServiceHardwareIdentity):
                changed = item != self.hardware_identity
                self.hardware_identity = item
                if self._recorder is not None:
                    self._recorder.write_metadata_event(
                        {
                            "type": "device_hardware_identity",
                            "device_uid": item.device_uid,
                            "mac_address": item.mac_text,
                            "mac_source": item.mac_source,
                        },
                        host_timestamp_ns=datagram.wall_time_ns,
                    )
                if changed:
                    identity_changed = True
                    self.hardware_identity_changed.emit(item)
        if identity_changed:
            self.updated.emit(self)
        return records

    def record_control_frame(self, frame: bytes) -> bool:
        recorder = self._recorder
        return recorder.write_control_frame(frame) if recorder is not None else False

    def rebind_endpoint(
        self,
        endpoint: Endpoint,
        *,
        wall_time_ns: Optional[int] = None,
    ) -> Endpoint:
        old_endpoint = self.endpoint
        self.endpoint = (str(endpoint[0]), int(endpoint[1]))
        self.core.bind_endpoint(self.endpoint)
        recorder = self._recorder
        if recorder is not None:
            recorder.write_metadata_event(
                {
                    "type": "endpoint_changed",
                    "old_endpoint": f"{old_endpoint[0]}:{old_endpoint[1]}",
                    "new_endpoint": f"{self.endpoint[0]}:{self.endpoint[1]}",
                },
                host_timestamp_ns=wall_time_ns,
            )
        self.updated.emit(self)
        return old_endpoint

    def rebind_runtime(
        self,
        endpoint: Endpoint,
        runtime: EndpointSessionRuntime,
        attachment: ProductionAttachmentLease,
        *,
        wall_time_ns: Optional[int] = None,
    ) -> Endpoint | None:
        """Move the Production facet without mutating an active Runtime endpoint."""

        previous_runtime = self.runtime
        previous_attachment = self.attachment
        if (
            previous_attachment is not None
            and previous_runtime is not None
            and previous_runtime.capability_has_active_operation(
                previous_attachment.capability
            )
        ):
            attachment.release()
            return None
        previous_recorder_lease = self._recorder_lease
        if previous_recorder_lease is not None and not previous_recorder_lease.release():
            attachment.release()
            return None
        if previous_attachment is not None and not previous_attachment.release():
            return None
        next_recorder_lease: SessionRecorderLease | None = None
        if self._recorder is not None:
            next_recorder_lease = attachment.new_operation_gateway().acquire_recorder(
                self.session_owner,
                kind=SessionRecorderKind.PRODUCTION_EVIDENCE,
            )
            if next_recorder_lease is None:
                # The old endpoint ownership has already ended.  Never leave a
                # writer active without a Runtime recorder owner.
                self._recorder.stop()
                self._recorder = None
                self._recording_complete = False
        if previous_runtime is not None:
            self._disconnect_runtime(previous_runtime)
        old_endpoint = self.endpoint
        self.endpoint = (str(endpoint[0]), int(endpoint[1]))
        self.runtime = runtime
        self.attachment = attachment
        self._recorder_lease = next_recorder_lease
        self.core = runtime.core
        self.receiver = runtime.core.receiver
        self.product_store = runtime.core.product_store
        self._connect_runtime(runtime)
        recorder = self._recorder
        if recorder is not None:
            recorder.write_metadata_event(
                {
                    "type": "endpoint_changed",
                    "old_endpoint": f"{old_endpoint[0]}:{old_endpoint[1]}",
                    "new_endpoint": f"{self.endpoint[0]}:{self.endpoint[1]}",
                },
                host_timestamp_ns=wall_time_ns,
            )
        self.updated.emit(self)
        return old_endpoint

    def mark_conflict(self) -> None:
        self._identity_conflict = True
        self.updated.emit(self)

    def finalize_recording(self) -> Optional[Path]:
        recorder = self._recorder
        if recorder is None:
            return self._recording_final_path
        source = recorder.filepath
        finalized = recorder.stop()
        if not finalized:
            # DataRecorder.stop() is deliberately bounded and re-entrant.  A
            # timeout means the writer, reservation and Runtime owner must all
            # remain reachable so the exact same finalize can be retried.
            self._recording_complete = None
            self.recording_changed.emit(self)
            return None
        recorder_lease = self._recorder_lease
        if recorder_lease is not None and not recorder_lease.release():
            self._recording_complete = None
            self.recording_changed.emit(self)
            return None
        self._recorder_lease = None
        complete = finalized and recorder.dropped_count == 0
        self._recording_complete = bool(complete)
        self._recorder = None
        if not source.exists():
            self._recording_complete = False
            self.recording_changed.emit(self)
            return None
        identity_dir = self.serial_number or self.device_uid or "_unidentified"
        root = self._recording_root or source.parent
        destination_dir = root / "devices" / identity_dir / "evidence"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source.name
        suffix = 1
        while destination.exists():
            destination = destination_dir / f"{source.stem}-{suffix}{source.suffix}"
            suffix += 1
        try:
            source.replace(destination)
        except OSError:
            destination = source
        self._recording_final_path = destination
        self.recording_changed.emit(self)
        return destination if complete else destination


class FleetController(QObject):
    """Manage up to four endpoint sessions without letting them own fixtures."""

    session_added = Signal(object)
    session_updated = Signal(object)
    identity_conflict = Signal(str)
    endpoint_migrated = Signal(str, str, str)
    endpoint_rejected = Signal(str)
    status_changed = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        *,
        discovery_cidr: str,
        local_port: int,
        device_port: int = 4004,
        max_devices: int = 4,
        hub: Optional[UdpFleetHub] = None,
        broker: UdpEndpointBroker | None = None,
        session_directory: EndpointSessionDirectory | None = None,
        session_registry: Optional[SessionRegistry] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        if max_devices not in range(1, 5):
            raise FleetConfigurationError("max_devices must be between 1 and 4")
        self._max_devices = int(max_devices)
        if session_directory is not None and session_registry is not None:
            if session_directory is not session_registry:
                raise FleetConfigurationError(
                    "Production requires one shared EndpointSessionDirectory"
                )
        if hub is not None and broker is not None and hub.broker is not broker:
            raise FleetConfigurationError(
                "Production hub and Directory must share one UDP broker"
            )
        self._hub = hub or UdpFleetHub(
            discovery_cidr=discovery_cidr,
            local_port=local_port,
            device_port=device_port,
            broker=broker,
            parent=self,
        )
        directory = session_directory or session_registry
        if directory is None:
            # Compatibility callers still get the same Runtime model.  The
            # facet wrapper is used so monkeypatched legacy hub.send_to tests
            # observe the exact Runtime send path.
            directory = EndpointSessionDirectory(self._hub, parent=self)
        directory_broker = getattr(directory.broker, "broker", directory.broker)
        if directory_broker is not self._hub.broker:
            raise FleetConfigurationError(
                "Production and Customer must use the same UDP broker"
            )
        self._session_directory = directory
        self._session_registry = directory
        self._sessions: dict[Endpoint, DeviceSession] = {}
        self._sessions_by_serial: dict[str, DeviceSession] = {}
        self._sessions_by_uid: dict[str, DeviceSession] = {}
        self._sessions_by_mac: dict[str, DeviceSession] = {}
        self._serial_for_endpoint: dict[Endpoint, str] = {}
        self._uid_for_endpoint: dict[Endpoint, str] = {}
        self._mac_for_endpoint: dict[Endpoint, str] = {}
        self._recording_batch_id = ""
        self._recording_root: Optional[Path] = None
        self._recording_participant_keys: Optional[frozenset[str]] = None
        self._batch_gate_owner = ""
        self._batch_gateways: dict[Endpoint, RuntimeOperationGateway] = {}
        self._subscription_request_id = 0
        self._subscription_sent_ns: dict[Endpoint, int] = {}
        self._retired_endpoints: dict[Endpoint, int] = {}
        self._candidates: dict[Endpoint, _CandidateSession] = {}
        self._candidate_cooldown_until: dict[Endpoint, int] = {}
        self._admitted_candidate_datagrams: dict[
            Endpoint, tuple[FleetDatagram, ...]
        ] = {}
        self._admission_in_progress: set[Endpoint] = set()
        self._candidate_rejections = 0
        self._admission_enabled = False
        self._observed_runtimes: dict[Endpoint, EndpointSessionRuntime] = {}
        self._hub.datagram_received.connect(self._on_broker_datagram)
        self._hub.listening.connect(self._on_listening)
        self._hub.error.connect(self.error)
        self._session_directory.runtime_added.connect(self._observe_runtime)
        self._session_directory.runtime_removed.connect(self._forget_runtime)
        for runtime in self._session_directory.runtimes():
            self._observe_runtime(runtime)

    @property
    def hub(self) -> UdpFleetHub:
        return self._hub

    @property
    def session_directory(self) -> EndpointSessionDirectory:
        return self._session_directory

    @property
    def is_running(self) -> bool:
        return self._admission_enabled and self._hub.isRunning()

    @property
    def recording_finalize_pending(self) -> bool:
        """Whether any Production evidence owner still requires finalization."""

        return any(session.recording_armed for session in self.sessions())

    @property
    def batch_gate_release_pending(self) -> bool:
        """Whether a frozen batch gate still needs a safe release retry."""

        return bool(self._batch_gateways)

    @property
    def shutdown_ready(self) -> bool:
        """Whether Production owns no recorder, gate, attachment, or hub lease."""

        return bool(
            not self.recording_finalize_pending
            and not self.batch_gate_release_pending
            and all(session.attachment is None for session in self.sessions())
            and not self._hub.isRunning()
        )

    def sessions(self) -> tuple[DeviceSession, ...]:
        return tuple(sorted(self._sessions.values(), key=lambda item: item.slot))

    @property
    def candidate_count(self) -> int:
        return len(self._candidates)

    @property
    def candidate_rejections(self) -> int:
        return self._candidate_rejections

    def start(self) -> bool:
        self._subscription_sent_ns.clear()
        self._retired_endpoints.clear()
        self._candidates.clear()
        self._candidate_cooldown_until.clear()
        self._admission_enabled = True
        started = self._hub.start_hub()
        if not started:
            self._admission_enabled = False
            self.status_changed.emit("failed")
        return started

    def stop(self) -> dict[Endpoint, Optional[Path]]:
        self._admission_enabled = False
        recordings = self.finalize_recordings()
        pending = tuple(
            session for session in self.sessions() if session.recording_armed
        )
        if pending:
            details = ", ".join(
                f"{session.endpoint[0]}:{session.endpoint[1]}"
                for session in pending
            )
            self.error.emit(
                "Production evidence recording finalize is still pending: "
                + details
            )
            self.status_changed.emit("finalize_pending")
            return recordings
        if self._batch_gateways:
            details = ", ".join(
                f"{endpoint[0]}:{endpoint[1]}"
                for endpoint in sorted(self._batch_gateways)
            )
            self.error.emit(
                "Production batch operation release is still pending: " + details
            )
            self.status_changed.emit("release_pending")
            return recordings
        release_failed: list[Endpoint] = []
        for session in self.sessions():
            if not session.release_attachment():
                release_failed.append(session.endpoint)
                self.error.emit(
                    "cannot release active Production operation at "
                    f"{session.endpoint[0]}:{session.endpoint[1]}"
                )
        if release_failed:
            self.status_changed.emit("release_pending")
            return recordings
        if not self._hub.stop_hub():
            self.error.emit("Production UDP discovery did not stop cleanly")
            self.status_changed.emit("release_pending")
            return recordings
        self.status_changed.emit("stopped")
        return recordings

    def arm_batch_recording(self, batch_id: str, output_root: str | Path) -> bool:
        normalized_batch_id = str(batch_id)
        if (
            self._recording_batch_id
            and self._recording_batch_id != normalized_batch_id
        ):
            raise RuntimeError(
                "fleet evidence recording already belongs to another batch"
            )
        conflicting = tuple(
            session.serial_number or f"{session.endpoint[0]}:{session.endpoint[1]}"
            for session in self.sessions()
            if session.recording_armed
            and session._recording_batch_id != normalized_batch_id
        )
        if conflicting:
            raise RuntimeError(
                "device evidence recording already belongs to another batch: "
                + ", ".join(conflicting)
            )
        self._recording_batch_id = normalized_batch_id
        self._recording_root = Path(output_root).expanduser().resolve()
        self._recording_participant_keys = None
        success = True
        for session in self.sessions():
            success = (
                session.arm_recording(normalized_batch_id, self._recording_root)
                and success
            )
        return success

    def freeze_batch_participants(
        self,
        participant_identity_keys: tuple[str, ...],
    ) -> dict[Endpoint, Optional[Path]]:
        """Keep evidence recording only for the frozen batch participants."""

        if not self._recording_batch_id or self._recording_root is None:
            raise RuntimeError("batch evidence recording has not been created")
        keys = tuple(str(value).strip() for value in participant_identity_keys)
        if not (1 <= len(keys) <= self._max_devices) or any(not value for value in keys):
            raise ValueError("one to four participant identity keys are required")
        if len(set(keys)) != len(keys):
            raise ValueError("participant identity keys must be unique")

        sessions_by_key = {
            session.identity_key: session
            for session in self.sessions()
            if session.identity_key
        }
        missing = [value for value in keys if value not in sessions_by_key]
        if missing:
            raise RuntimeError("unknown participant identity: " + ", ".join(missing))
        recording_pending = [
            sessions_by_key[value].serial_number or value
            for value in keys
            if not sessions_by_key[value].recording_armed
        ]
        if recording_pending:
            raise RuntimeError(
                "participant evidence recording is not ready: "
                + ", ".join(recording_pending)
            )

        frozen = frozenset(keys)
        gate_owner = f"fleet-batch:{id(self)}:{self._recording_batch_id}"
        acquired_gateways: dict[Endpoint, RuntimeOperationGateway] = {}
        allowed_operations = {
            SessionOperationClass.IDENTITY,
            SessionOperationClass.HANDSHAKE,
            SessionOperationClass.PRODUCT_SUBSCRIPTION,
            SessionOperationClass.READ_ONLY_QUERY,
            SessionOperationClass.MUTATING,
        }
        try:
            for key in keys:
                session = sessions_by_key[key]
                attachment = session.attachment
                if attachment is None:
                    raise RuntimeError(
                        "participant has no active Production attachment: " + key
                    )
                gateway = attachment.new_operation_gateway()
                if not gateway.try_acquire_operation(
                    gate_owner,
                    purpose="production-batch",
                    production_freeze=True,
                    allowed_operations=allowed_operations,
                ):
                    raise RuntimeError(
                        "participant operation gate is busy: " + key
                    )
                acquired_gateways[session.endpoint] = gateway
        except Exception:
            for gateway in reversed(tuple(acquired_gateways.values())):
                gateway.release_operation(gate_owner)
            raise

        excluded_recordings: dict[Endpoint, Optional[Path]] = {}
        for session in self.sessions():
            if session.identity_key not in frozen and session.recording_armed:
                excluded_recordings[session.endpoint] = session.finalize_recording()
                if session.recording_armed:
                    for gateway in reversed(tuple(acquired_gateways.values())):
                        gateway.release_operation(gate_owner)
                    raise RuntimeError(
                        "excluded device evidence recording did not finalize: "
                        f"{session.endpoint[0]}:{session.endpoint[1]}"
                    )
        self._recording_participant_keys = frozen
        self._batch_gate_owner = gate_owner
        self._batch_gateways = acquired_gateways
        return excluded_recordings

    def finalize_recordings(self) -> dict[Endpoint, Optional[Path]]:
        result = {
            session.endpoint: session.finalize_recording()
            for session in self.sessions()
        }
        if any(session.recording_armed for session in self.sessions()):
            return result
        gate_owner = self._batch_gate_owner
        pending_gateways: dict[Endpoint, RuntimeOperationGateway] = {}
        for endpoint, gateway in self._batch_gateways.items():
            if not gateway.release_operation(gate_owner):
                pending_gateways[endpoint] = gateway
        self._batch_gateways = pending_gateways
        if pending_gateways:
            return result
        self._batch_gate_owner = ""
        self._recording_batch_id = ""
        self._recording_root = None
        self._recording_participant_keys = None
        return result

    def clear_snr_histories(self) -> None:
        for session in self.sessions():
            session.clear_snr_history()

    def send(
        self,
        endpoint: Endpoint,
        frame: bytes,
        *,
        operation: SessionOperationClass = SessionOperationClass.MUTATING,
    ) -> bool:
        session = self._sessions.get(endpoint)
        gateway = self._batch_gateways.get(endpoint)
        if session is None or gateway is None:
            return False
        sent = gateway.send(frame, operation=operation)
        if sent:
            session.record_control_frame(frame)
        return sent

    def update_batch_operation_allowlist(
        self,
        allowed_operations: set[SessionOperationClass],
    ) -> bool:
        if not self._batch_gate_owner or not self._batch_gateways:
            return False
        return all(
            gateway.update_production_allowlist(
                self._batch_gate_owner,
                allowed_operations,
            )
            for gateway in self._batch_gateways.values()
        )

    @Slot(object)
    def _on_datagram(self, datagram: FleetDatagram) -> None:
        """Compatibility host-test injection with a fail-closed non-wire fallback."""

        self._process_datagram(datagram, allow_injected_transport=True)

    def inject_datagram(self, datagram: FleetDatagram) -> None:
        """Inject one immutable datagram for deterministic host testing."""

        self._process_datagram(datagram, allow_injected_transport=True)

    @Slot(object)
    def _on_broker_datagram(self, datagram: FleetDatagram) -> None:
        # The broker remains process-wide and may continue for Customer after
        # Production stops.  An inactive Production facet must not feed its
        # retained session wrappers, otherwise the shared Core parses the same
        # Customer datagram a second time.
        if not self._admission_enabled:
            return
        self._process_datagram(datagram, allow_injected_transport=False)

    def _process_datagram(
        self,
        datagram: FleetDatagram,
        *,
        allow_injected_transport: bool,
    ) -> None:
        now_ns = time.monotonic_ns()
        retired_until_ns = self._retired_endpoints.get(datagram.endpoint, 0)
        if retired_until_ns > now_ns:
            return
        self._retired_endpoints.pop(datagram.endpoint, None)
        session = self._sessions.get(datagram.endpoint)
        if session is not None:
            self._ensure_batch_evidence_recording(session)
            session.feed_datagram(datagram)
            return

        runtime = self._session_directory.runtime(datagram.endpoint)
        if runtime is not None and runtime.transport_active:
            decoded_records = runtime.feed_datagram(datagram)
            if decoded_records and datagram.endpoint not in self._sessions:
                self._process_decoded_admission(
                    datagram,
                    decoded_records,
                    records_already_applied=True,
                    allow_injected_transport=allow_injected_transport,
                )
            return

        decoded_records = self._feed_candidate(datagram)
        if decoded_records is None:
            return
        self._process_decoded_admission(
            datagram,
            decoded_records,
            records_already_applied=False,
            allow_injected_transport=allow_injected_transport,
        )

    def _process_decoded_admission(
        self,
        datagram: FleetDatagram,
        decoded_records: tuple[object, ...],
        *,
        records_already_applied: bool,
        allow_injected_transport: bool,
    ) -> None:
        if datagram.endpoint in self._sessions:
            return
        migration_records_applied = False
        identity = next(
                (item for item in decoded_records if isinstance(item, ServiceIdentity)),
                None,
        )
        hardware_identity = next(
            (
                item
                for item in decoded_records
                if isinstance(item, ServiceHardwareIdentity)
            ),
            None,
        )
        if identity is None and hardware_identity is None:
            return
        serial = (
            verified_identity_text(identity.serial_number)
            if identity is not None and identity.valid_mask & (1 << 1)
            else ""
        )
        uid = (
            verified_device_uid(hardware_identity.device_uid)
            if hardware_identity is not None
            and hardware_identity.valid_mask & (1 << 0)
            else ""
        )
        candidates = {
            candidate
            for candidate in (
                self._sessions_by_uid.get(uid) if uid else None,
                self._sessions_by_serial.get(serial) if serial else None,
            )
            if candidate is not None
        }
        if len(candidates) > 1:
            for candidate in candidates:
                candidate.mark_conflict()
            self.identity_conflict.emit(
                f"UID/SN resolve to different devices at {datagram.endpoint[0]}:{datagram.endpoint[1]}"
            )
            return
        existing = next(iter(candidates), None)
        if (
            existing is not None
            and existing.state in {
                DeviceSessionState.IDENTIFIED,
                DeviceSessionState.IDENTITY_PENDING,
            }
            and not existing.is_online(now_monotonic_ns=datagram.monotonic_ns)
        ):
            try:
                attachment = self._acquire_production_attachment(
                    datagram.endpoint,
                    existing.session_owner,
                    subscription_hz=20,
                    allow_injected_transport=allow_injected_transport,
                )
            except Exception as exc:
                self.error.emit(
                    "cannot acquire migrated Production session: " + str(exc)
                )
                return
            if existing.recording_armed and not records_already_applied:
                self._admission_in_progress.add(datagram.endpoint)
                try:
                    attachment.runtime.feed_decoded_datagram(
                        datagram,
                        decoded_records,
                    )
                finally:
                    self._admission_in_progress.discard(datagram.endpoint)
                migration_records_applied = True
            old_endpoint = existing.rebind_runtime(
                datagram.endpoint,
                attachment.runtime,
                attachment,
                wall_time_ns=datagram.wall_time_ns,
            )
            if old_endpoint is None:
                self.error.emit("cannot migrate an active Production operation")
                return
            self._sessions.pop(old_endpoint, None)
            self._sessions[datagram.endpoint] = existing
            self._serial_for_endpoint.pop(old_endpoint, None)
            self._uid_for_endpoint.pop(old_endpoint, None)
            self._mac_for_endpoint.pop(old_endpoint, None)
            if existing.serial_number:
                self._serial_for_endpoint[datagram.endpoint] = existing.serial_number
            if existing.device_uid:
                self._uid_for_endpoint[datagram.endpoint] = existing.device_uid
            if existing.mac_address:
                self._mac_for_endpoint[datagram.endpoint] = existing.mac_address
            self._subscription_sent_ns.pop(old_endpoint, None)
            self._retired_endpoints[old_endpoint] = (
                time.monotonic_ns() + 5_000_000_000
            )
            self.endpoint_migrated.emit(
                existing.serial_number or existing.device_uid,
                f"{old_endpoint[0]}:{old_endpoint[1]}",
                f"{datagram.endpoint[0]}:{datagram.endpoint[1]}",
            )
            session = existing
        else:
            session = self._create_session(
                datagram,
                allow_injected_transport=allow_injected_transport,
            )
            if session is None:
                self._admitted_candidate_datagrams.pop(datagram.endpoint, None)
                return

        if records_already_applied or migration_records_applied:
            self._ensure_batch_evidence_recording(session)
            candidate_datagrams = self._admitted_candidate_datagrams.pop(
                datagram.endpoint,
                (),
            )
            if candidate_datagrams:
                for candidate_datagram in candidate_datagrams:
                    session._record_datagram(candidate_datagram, record=True)
            else:
                session._record_datagram(datagram, record=True)
            session.apply_observed_records(datagram, decoded_records)
        else:
            candidate_datagrams = self._admitted_candidate_datagrams.pop(
                datagram.endpoint,
                (),
            )
            # Identity authorization is established by the one Runtime parse
            # before its Production recorder lease is acquired.  The admitted
            # candidate datagrams are then backfilled from immutable raw bytes;
            # no protocol record is decoded or applied twice.
            session.feed_decoded_datagram(datagram, decoded_records)
            self._ensure_batch_evidence_recording(session)
            for candidate_datagram in candidate_datagrams:
                session._record_datagram(candidate_datagram, record=True)

    @Slot(object)
    def _observe_runtime(self, runtime: EndpointSessionRuntime) -> None:
        previous = self._observed_runtimes.get(runtime.endpoint)
        if previous is runtime:
            return
        if previous is not None:
            try:
                previous.records_received.disconnect(self._on_runtime_admission_records)
            except (RuntimeError, TypeError):
                pass
        self._observed_runtimes[runtime.endpoint] = runtime
        runtime.records_received.connect(self._on_runtime_admission_records)

    @Slot(object)
    def _forget_runtime(self, endpoint: Endpoint) -> None:
        runtime = self._observed_runtimes.pop(endpoint, None)
        if runtime is None:
            return
        try:
            runtime.records_received.disconnect(self._on_runtime_admission_records)
        except (RuntimeError, TypeError):
            pass

    @Slot(object, object)
    def _on_runtime_admission_records(
        self,
        datagram: FleetDatagram,
        records: tuple[object, ...],
    ) -> None:
        if not self._admission_enabled or datagram.endpoint in self._sessions:
            return
        if datagram.endpoint in self._admission_in_progress:
            return
        runtime = self._session_directory.runtime(datagram.endpoint)
        if runtime is None or not runtime.transport_active:
            return
        self._process_decoded_admission(
            datagram,
            tuple(records),
            records_already_applied=True,
            allow_injected_transport=False,
        )

    def _feed_candidate(
        self,
        datagram: FleetDatagram,
    ) -> tuple[object, ...] | None:
        """Retain bounded parser state until one source proves Product identity."""

        now_ns = int(datagram.monotonic_ns)
        expired = tuple(
            endpoint
            for endpoint, candidate in self._candidates.items()
            if now_ns - candidate.last_monotonic_ns > _CANDIDATE_TIMEOUT_NS
        )
        for endpoint in expired:
            self._candidates.pop(endpoint, None)
        stale_cooldowns = tuple(
            endpoint
            for endpoint, until_ns in self._candidate_cooldown_until.items()
            if until_ns <= now_ns
        )
        for endpoint in stale_cooldowns:
            self._candidate_cooldown_until.pop(endpoint, None)
        if self._candidate_cooldown_until.get(datagram.endpoint, 0) > now_ns:
            return None

        candidate = self._candidates.get(datagram.endpoint)
        if candidate is None:
            if len(self._candidates) >= _CANDIDATE_LIMIT:
                self._candidate_rejections += 1
                self.endpoint_rejected.emit(
                    f"{datagram.endpoint[0]}:{datagram.endpoint[1]}"
                )
                return None
            candidate = _CandidateSession(
                receiver=FrameReceiverV2(),
                decoded_records=[],
                datagrams=[],
                received_bytes=0,
                last_monotonic_ns=now_ns,
            )
            self._candidates[datagram.endpoint] = candidate

        candidate.received_bytes += len(datagram.data)
        candidate.last_monotonic_ns = now_ns
        candidate.datagrams.append(datagram)
        if candidate.received_bytes > _CANDIDATE_BYTE_LIMIT:
            self._candidates.pop(datagram.endpoint, None)
            self._candidate_cooldown_until[datagram.endpoint] = (
                now_ns + _CANDIDATE_TIMEOUT_NS
            )
            self._candidate_rejections += 1
            self.endpoint_rejected.emit(
                f"{datagram.endpoint[0]}:{datagram.endpoint[1]}"
            )
            return None

        decoded = tuple(candidate.receiver.feed(datagram.data))
        candidate.decoded_records.extend(
            record for record in decoded if not isinstance(record, RawFrame)
        )
        admitted = any(
            isinstance(record, (ServiceIdentity, ServiceHardwareIdentity))
            for record in candidate.decoded_records
        )
        if not admitted:
            return None
        self._candidates.pop(datagram.endpoint, None)
        self._admitted_candidate_datagrams[datagram.endpoint] = tuple(
            candidate.datagrams
        )
        return tuple(candidate.decoded_records)

    def _create_session(
        self,
        datagram: FleetDatagram,
        *,
        allow_injected_transport: bool,
    ) -> Optional[DeviceSession]:
        if len(self._sessions) >= self._max_devices:
            endpoint_text = f"{datagram.endpoint[0]}:{datagram.endpoint[1]}"
            self.endpoint_rejected.emit(endpoint_text)
            return None
        occupied = {item.slot for item in self._sessions.values()}
        slot = next(
            value for value in range(1, self._max_devices + 1) if value not in occupied
        )
        owner = f"fleet:{id(self)}:slot:{slot}"
        try:
            attachment = self._acquire_production_attachment(
                datagram.endpoint,
                owner,
                subscription_hz=20,
                allow_injected_transport=allow_injected_transport,
            )
        except Exception as exc:
            self.error.emit(
                "cannot acquire Production endpoint session: " + str(exc)
            )
            return None
        session = DeviceSession(
            datagram.endpoint,
            slot,
            runtime=attachment.runtime,
            attachment=attachment,
            session_owner=owner,
            parent=self,
        )
        session.identity_changed.connect(
            lambda identity, current=session: self._on_session_identity(
                current, identity
            )
        )
        session.hardware_identity_changed.connect(
            lambda identity, current=session: self._on_session_hardware_identity(
                current, identity
            )
        )
        session.updated.connect(self.session_updated)
        session.recording_changed.connect(self.session_updated)
        self._sessions[datagram.endpoint] = session
        self.session_added.emit(session)
        return session

    def _acquire_production_attachment(
        self,
        endpoint: Endpoint,
        owner: str,
        *,
        subscription_hz: int,
        allow_injected_transport: bool,
    ) -> ProductionAttachmentLease:
        try:
            return self._session_directory.acquire_production_attachment(
                endpoint,
                owner,
                subscription_hz=subscription_hz,
                admission_claim=self._hub.admission_claim,
            )
        except RuntimeError as exc:
            if (
                not allow_injected_transport
                or str(exc) != "cannot start UDP endpoint broker"
            ):
                raise
        return self._session_directory.acquire_production_attachment(
            endpoint,
            owner,
            subscription_hz=subscription_hz,
            admission_claim=None,
            injected_transport=True,
        )

    def _ensure_batch_evidence_recording(self, session: DeviceSession) -> bool:
        """Keep retrying the READY-batch recording prerequisite on live input."""

        if (
            not self._recording_batch_id
            or self._recording_root is None
            or self._recording_participant_keys is not None
            or session.recording_armed
        ):
            return True
        if session.arm_recording(self._recording_batch_id, self._recording_root):
            return True
        self.error.emit(
            "cannot create evidence recording for "
            f"{session.endpoint[0]}:{session.endpoint[1]}"
        )
        return False

    def _subscribe_session(self, endpoint: Endpoint) -> None:
        """Raise only an accepted device from discovery rate to capture rate."""
        now_ns = time.monotonic_ns()
        last_sent_ns = self._subscription_sent_ns.get(endpoint, 0)
        if now_ns - last_sent_ns < 15_000_000_000:
            return
        self._subscription_request_id = (
            self._subscription_request_id + 1
        ) & 0xFFFFFFFF
        if self._hub.send_to(
            endpoint,
            build_service_subscribe(self._subscription_request_id, 20),
        ):
            self._subscription_sent_ns[endpoint] = now_ns

    def _on_session_identity(
        self,
        session: DeviceSession,
        identity: ServiceIdentity,
    ) -> None:
        serial = session.serial_number
        old_serial = self._serial_for_endpoint.get(session.endpoint, "")
        if session.state != DeviceSessionState.IDENTIFIED:
            self.session_updated.emit(session)
            return
        if old_serial and old_serial != serial:
            session.mark_conflict()
            self.identity_conflict.emit(
                f"endpoint identity changed: {old_serial} -> {serial}"
            )
            return
        existing = self._sessions_by_serial.get(serial)
        if existing is not None and existing is not session:
            existing.mark_conflict()
            session.mark_conflict()
            if existing.device_uid and session.device_uid and existing.device_uid != session.device_uid:
                details = f"serial number {serial} is bound to different MCU UIDs"
            else:
                details = (
                    f"duplicate serial number {serial} at "
                    f"{existing.endpoint[0]}:{existing.endpoint[1]} and "
                    f"{session.endpoint[0]}:{session.endpoint[1]}"
                )
            self.identity_conflict.emit(details)
            return
        self._serial_for_endpoint[session.endpoint] = serial
        self._sessions_by_serial[serial] = session
        self.session_updated.emit(session)

    def _on_session_hardware_identity(
        self,
        session: DeviceSession,
        identity: ServiceHardwareIdentity,
    ) -> None:
        uid = session.device_uid
        mac = session.mac_address
        old_uid = self._uid_for_endpoint.get(session.endpoint, "")
        old_mac = self._mac_for_endpoint.get(session.endpoint, "")
        if old_uid and uid and old_uid != uid:
            session.mark_conflict()
            self.identity_conflict.emit(
                f"endpoint MCU UID changed: {old_uid} -> {uid}"
            )
            return
        if old_mac and mac and old_mac != mac:
            session.mark_conflict()
            self.identity_conflict.emit(
                f"endpoint MAC changed: {old_mac} -> {mac}"
            )
            return

        duplicate_uid = self._sessions_by_uid.get(uid) if uid else None
        if duplicate_uid is not None and duplicate_uid is not session:
            duplicate_uid.mark_conflict()
            session.mark_conflict()
            self.identity_conflict.emit(
                f"duplicate MCU UID {uid} at "
                f"{duplicate_uid.endpoint[0]}:{duplicate_uid.endpoint[1]} and "
                f"{session.endpoint[0]}:{session.endpoint[1]}"
            )
            return
        duplicate_mac = self._sessions_by_mac.get(mac) if mac else None
        if duplicate_mac is not None and duplicate_mac is not session:
            duplicate_mac.mark_conflict()
            session.mark_conflict()
            self.identity_conflict.emit(
                f"duplicate MAC address {mac} at "
                f"{duplicate_mac.endpoint[0]}:{duplicate_mac.endpoint[1]} and "
                f"{session.endpoint[0]}:{session.endpoint[1]}"
            )
            return

        if uid:
            self._uid_for_endpoint[session.endpoint] = uid
            self._sessions_by_uid[uid] = session
        if mac:
            self._mac_for_endpoint[session.endpoint] = mac
            self._sessions_by_mac[mac] = session
        self.session_updated.emit(session)

    @Slot(str, int)
    def _on_listening(self, _address: str, port: int) -> None:
        self.status_changed.emit(f"listening:{port}")


__all__ = [
    "DeviceSession",
    "DeviceSessionState",
    "Endpoint",
    "FleetConfigurationError",
    "FleetController",
    "FleetDatagram",
    "FleetHubStatistics",
    "SnrSample",
    "UdpFleetHub",
]
