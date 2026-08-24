"""Shared UDP discovery, endpoint demultiplexing, and production sessions."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import ipaddress
import math
from pathlib import Path
import queue
import socket
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from satellite_debug_tool.core.protocol import (
    FrameReceiverV2,
    ServiceFastState,
    ServiceHardwareIdentity,
    ServiceIdentity,
    build_service_subscribe,
)
from satellite_debug_tool.core.session import DeviceSessionCore, SessionRegistry
from satellite_debug_tool.io.data_recorder import DataRecorder, SDB_VERSION_V3


Endpoint = tuple[str, int]
_SNR_HISTORY_SECONDS = 300.0
_SNR_CAPTURE_RATE_HZ = 20
_SNR_HISTORY_CAPACITY = int(_SNR_HISTORY_SECONDS * _SNR_CAPTURE_RATE_HZ) + (
    10 * _SNR_CAPTURE_RATE_HZ
)


class FleetConfigurationError(ValueError):
    pass


class DeviceSessionState(str, Enum):
    DISCOVERED = "discovered"
    IDENTIFIED = "identified"
    CONFLICT = "conflict"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class FleetDatagram:
    endpoint: Endpoint
    data: bytes
    wall_time_ns: int
    monotonic_ns: int


@dataclass(frozen=True)
class SnrSample:
    """One SNR sample on the workstation's shared monotonic timeline."""

    monotonic_ns: int
    device_uptime_ms: int
    value_db: float


@dataclass(frozen=True)
class FleetHubStatistics:
    discovery_datagrams: int = 0
    control_datagrams: int = 0
    received_datagrams: int = 0
    received_bytes: int = 0
    ignored_datagrams: int = 0
    send_failures: int = 0
    receive_failures: int = 0


class UdpFleetHub(QThread):
    """Own exactly one UDP socket and preserve each datagram's source endpoint."""

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
        self._local_port_requested = int(local_port)
        self._device_port = int(device_port)
        self._discovery_interval_s = float(discovery_interval_s)
        self._fast_rate_hz = int(fast_rate_hz)
        self._socket: Optional[socket.socket] = None
        self._stop_event = threading.Event()
        self._outbound: "queue.Queue[tuple[Endpoint, bytes]]" = queue.Queue(maxsize=512)
        self._request_id = 0
        self._stats = FleetHubStatistics()
        self._stats_lock = threading.Lock()

    @property
    def discovery_cidr(self) -> str:
        return str(self._network)

    @property
    def local_port(self) -> int:
        sock = self._socket
        if sock is not None:
            try:
                return int(sock.getsockname()[1])
            except OSError:
                pass
        return self._local_port_requested

    @property
    def device_port(self) -> int:
        return self._device_port

    @property
    def statistics(self) -> FleetHubStatistics:
        with self._stats_lock:
            return self._stats

    def start_hub(self) -> bool:
        if self.isRunning():
            return True
        while True:
            try:
                self._outbound.get_nowait()
            except queue.Empty:
                break
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self._local_port_requested))
            sock.settimeout(0.05)
        except OSError as exc:
            sock.close()
            self.error.emit(f"cannot bind fleet UDP socket: {exc}")
            return False
        self._socket = sock
        self._stop_event.clear()
        self.start()
        return True

    def stop_hub(self, timeout_ms: int = 3000) -> bool:
        self._stop_event.set()
        sock = self._socket
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self.isRunning():
            self.wait(max(0, int(timeout_ms)))
        return not self.isRunning()

    def send_to(self, endpoint: Endpoint, data: bytes) -> bool:
        try:
            address = ipaddress.ip_address(endpoint[0])
        except ValueError:
            return False
        if address not in self._network or int(endpoint[1]) != self._device_port:
            return False
        if not self.isRunning() or not data:
            return False
        try:
            self._outbound.put_nowait(((str(address), int(endpoint[1])), bytes(data)))
            return True
        except queue.Full:
            self._increment_stats(send_failures=1)
            return False

    def run(self) -> None:
        self.listening.emit("0.0.0.0", self.local_port)
        next_discovery = 0.0
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_discovery:
                self._send_discovery()
                next_discovery = now + self._discovery_interval_s
            self._drain_outbound()
            self._receive_once()
        self._socket = None
        self.stopped.emit()

    def _send_discovery(self) -> None:
        sock = self._socket
        if sock is None:
            return
        self._request_id = (self._request_id + 1) & 0xFFFFFFFF
        frame = build_service_subscribe(self._request_id, self._fast_rate_hz)
        sent = 0
        failures = 0
        hosts = self._network.hosts()
        if self._network.num_addresses == 1:
            hosts = iter((self._network.network_address,))
        for address in hosts:
            if self._stop_event.is_set():
                break
            try:
                sock.sendto(frame, (str(address), self._device_port))
                sent += 1
            except OSError:
                failures += 1
        self._increment_stats(discovery_datagrams=sent, send_failures=failures)

    def _drain_outbound(self) -> None:
        sock = self._socket
        if sock is None:
            return
        sent = 0
        failures = 0
        for _ in range(64):
            try:
                endpoint, data = self._outbound.get_nowait()
            except queue.Empty:
                break
            try:
                sock.sendto(data, endpoint)
                sent += 1
            except OSError:
                failures += 1
        if sent or failures:
            self._increment_stats(control_datagrams=sent, send_failures=failures)

    def _receive_once(self) -> None:
        sock = self._socket
        if sock is None:
            return
        try:
            data, source = sock.recvfrom(65535)
        except socket.timeout:
            return
        except OSError as exc:
            if not self._stop_event.is_set():
                self._increment_stats(receive_failures=1)
                self.error.emit(f"fleet UDP receive failed: {exc}")
            return
        endpoint = (str(source[0]), int(source[1]))
        try:
            allowed = (
                ipaddress.ip_address(endpoint[0]) in self._network
                and endpoint[1] == self._device_port
            )
        except ValueError:
            allowed = False
        if not allowed:
            self._increment_stats(ignored_datagrams=1)
            return
        datagram = FleetDatagram(
            endpoint=endpoint,
            data=bytes(data),
            wall_time_ns=time.time_ns(),
            monotonic_ns=time.monotonic_ns(),
        )
        self._increment_stats(received_datagrams=1, received_bytes=len(data))
        self.datagram_received.emit(datagram)

    def _increment_stats(self, **changes: int) -> None:
        with self._stats_lock:
            values = {
                field: getattr(self._stats, field) + int(delta)
                for field, delta in changes.items()
            }
            self._stats = replace(self._stats, **values)
            snapshot = self._stats
        self.statistics_changed.emit(snapshot)


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
        session_owner: str = "production",
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.endpoint = (str(endpoint[0]), int(endpoint[1]))
        self.slot = int(slot)
        self.session_owner = str(session_owner)
        self.core = session_core or DeviceSessionCore(
            endpoint=self.endpoint,
            parent=self,
        )
        self.receiver = self.core.receiver
        self.product_store = self.core.product_store
        self.state = DeviceSessionState.DISCOVERED
        self.identity: Optional[ServiceIdentity] = None
        self.hardware_identity: Optional[ServiceHardwareIdentity] = None
        self.last_seen_monotonic_ns = 0
        self.received_datagrams = 0
        self.received_bytes = 0
        self._snr_history: deque[SnrSample] = deque(
            maxlen=_SNR_HISTORY_CAPACITY
        )
        self._recorder: Optional[DataRecorder] = None
        self._recording_batch_id = ""
        self._recording_root: Optional[Path] = None
        self._recording_final_path: Optional[Path] = None
        self._recording_complete: Optional[bool] = None

    @property
    def serial_number(self) -> str:
        identity = self.identity
        if identity is None or not (identity.valid_mask & (1 << 1)):
            return ""
        return identity.serial_number.strip()

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
        return identity.device_uid

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
        return self._recorder is not None and self._recorder.is_recording

    @property
    def recording_path(self) -> Optional[Path]:
        if self._recording_final_path is not None:
            return self._recording_final_path
        return None if self._recorder is None else self._recorder.filepath

    @property
    def recording_complete(self) -> Optional[bool]:
        return self._recording_complete

    def is_online(self, *, now_monotonic_ns: Optional[int] = None, timeout_s: float = 3.0) -> bool:
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
                raise RuntimeError("session is already recording another batch")
            return True
        root = Path(output_root).expanduser().resolve()
        staging = root / "_staging"
        staging.mkdir(parents=True, exist_ok=True)
        endpoint_slug = f"{self.endpoint[0].replace('.', '_')}-{self.endpoint[1]}"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = staging / f"slot{self.slot}-{endpoint_slug}-{stamp}.sdb"
        recorder = DataRecorder(
            path,
            format_version=SDB_VERSION_V3,
            metadata={
                "mode": "production",
                "batch_id": str(batch_id),
                "slot": self.slot,
                "endpoint": f"{self.endpoint[0]}:{self.endpoint[1]}",
            },
        )
        if not recorder.start():
            return False
        self._recorder = recorder
        self._recording_batch_id = str(batch_id)
        self._recording_root = root
        self._recording_final_path = None
        self._recording_complete = None
        self.recording_changed.emit(self)
        return True

    def feed_datagram(self, datagram: FleetDatagram, *, record: bool = True) -> tuple[object, ...]:
        if datagram.endpoint != self.endpoint:
            raise ValueError("datagram endpoint does not match device session")
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
        self._record_datagram(datagram, record=record)
        self.core.apply_records(
            records,
            received_monotonic=datagram.monotonic_ns / 1_000_000_000.0,
        )
        return self._apply_records(datagram, records)

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
                if self.hardware_type.lower() == "afd01" and self.serial_number:
                    self.state = DeviceSessionState.IDENTIFIED
                else:
                    self.state = DeviceSessionState.UNSUPPORTED
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

    def mark_conflict(self) -> None:
        self.state = DeviceSessionState.CONFLICT
        self.updated.emit(self)

    def finalize_recording(self) -> Optional[Path]:
        recorder = self._recorder
        if recorder is None:
            return self._recording_final_path
        source = recorder.filepath
        finalized = recorder.stop()
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
        session_registry: Optional[SessionRegistry] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        if max_devices not in range(1, 5):
            raise FleetConfigurationError("max_devices must be between 1 and 4")
        self._max_devices = int(max_devices)
        self._session_registry = session_registry
        self._hub = hub or UdpFleetHub(
            discovery_cidr=discovery_cidr,
            local_port=local_port,
            device_port=device_port,
            parent=self,
        )
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
        self._subscription_request_id = 0
        self._subscription_sent_ns: dict[Endpoint, int] = {}
        self._retired_endpoints: dict[Endpoint, int] = {}
        self._hub.datagram_received.connect(self._on_datagram)
        self._hub.listening.connect(self._on_listening)
        self._hub.error.connect(self.error)

    @property
    def hub(self) -> UdpFleetHub:
        return self._hub

    @property
    def is_running(self) -> bool:
        return self._hub.isRunning()

    def sessions(self) -> tuple[DeviceSession, ...]:
        return tuple(sorted(self._sessions.values(), key=lambda item: item.slot))

    def start(self) -> bool:
        self._subscription_sent_ns.clear()
        self._retired_endpoints.clear()
        started = self._hub.start_hub()
        if not started:
            self.status_changed.emit("failed")
        return started

    def stop(self) -> dict[Endpoint, Optional[Path]]:
        self._hub.stop_hub()
        recordings = self.finalize_recordings()
        self.status_changed.emit("stopped")
        return recordings

    def arm_batch_recording(self, batch_id: str, output_root: str | Path) -> bool:
        if self._recording_batch_id and self._recording_batch_id != str(batch_id):
            raise RuntimeError("fleet is already armed for another batch")
        self._recording_batch_id = str(batch_id)
        self._recording_root = Path(output_root).expanduser().resolve()
        self._recording_participant_keys = None
        success = True
        for session in self.sessions():
            success = session.arm_recording(batch_id, self._recording_root) and success
        return success

    def freeze_batch_participants(
        self,
        participant_identity_keys: tuple[str, ...],
    ) -> dict[Endpoint, Optional[Path]]:
        """Keep evidence recording only for the frozen batch participants."""

        if not self._recording_batch_id or self._recording_root is None:
            raise RuntimeError("fleet recording is not armed for a batch")
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
        unarmed = [
            sessions_by_key[value].serial_number or value
            for value in keys
            if not sessions_by_key[value].recording_armed
        ]
        if unarmed:
            raise RuntimeError("participant recording is not armed: " + ", ".join(unarmed))

        frozen = frozenset(keys)
        excluded_recordings: dict[Endpoint, Optional[Path]] = {}
        for session in self.sessions():
            if session.identity_key not in frozen and session.recording_armed:
                excluded_recordings[session.endpoint] = session.finalize_recording()
        self._recording_participant_keys = frozen
        return excluded_recordings

    def finalize_recordings(self) -> dict[Endpoint, Optional[Path]]:
        result = {
            session.endpoint: session.finalize_recording()
            for session in self.sessions()
        }
        self._recording_batch_id = ""
        self._recording_root = None
        self._recording_participant_keys = None
        return result

    def clear_snr_histories(self) -> None:
        for session in self.sessions():
            session.clear_snr_history()

    def send(self, endpoint: Endpoint, frame: bytes) -> bool:
        session = self._sessions.get(endpoint)
        if session is None:
            return False
        session.record_control_frame(frame)
        return self._hub.send_to(endpoint, frame)

    @Slot(object)
    def _on_datagram(self, datagram: FleetDatagram) -> None:
        now_ns = time.monotonic_ns()
        retired_until_ns = self._retired_endpoints.get(datagram.endpoint, 0)
        if retired_until_ns > now_ns:
            return
        self._retired_endpoints.pop(datagram.endpoint, None)
        session = self._sessions.get(datagram.endpoint)
        decoded_records: tuple[object, ...] | None = None
        if session is None:
            probe = FrameReceiverV2()
            decoded_records = tuple(probe.feed(datagram.data))
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
                identity.serial_number.strip()
                if identity is not None and identity.valid_mask & (1 << 1)
                else ""
            )
            uid = (
                hardware_identity.device_uid
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
                and existing.state == DeviceSessionState.IDENTIFIED
                and not existing.is_online(now_monotonic_ns=datagram.monotonic_ns)
            ):
                old_endpoint = existing.rebind_endpoint(
                    datagram.endpoint,
                    wall_time_ns=datagram.wall_time_ns,
                )
                if self._session_registry is not None:
                    self._session_registry.rebind(
                        existing.core,
                        datagram.endpoint,
                        owner=existing.session_owner,
                    )
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
                self._retired_endpoints[old_endpoint] = now_ns + 5_000_000_000
                self.endpoint_migrated.emit(
                    existing.serial_number or existing.device_uid,
                    f"{old_endpoint[0]}:{old_endpoint[1]}",
                    f"{datagram.endpoint[0]}:{datagram.endpoint[1]}",
                )
                session = existing
            else:
                session = self._create_session(datagram)
                if session is None:
                    return
        if decoded_records is None:
            session.feed_datagram(datagram)
        else:
            session.feed_decoded_datagram(datagram, decoded_records)
        if session.state == DeviceSessionState.IDENTIFIED:
            self._subscribe_session(datagram.endpoint)

    def _create_session(self, datagram: FleetDatagram) -> Optional[DeviceSession]:
        if len(self._sessions) >= self._max_devices:
            endpoint_text = f"{datagram.endpoint[0]}:{datagram.endpoint[1]}"
            self.endpoint_rejected.emit(endpoint_text)
            return None
        occupied = {item.slot for item in self._sessions.values()}
        slot = next(
            value for value in range(1, self._max_devices + 1) if value not in occupied
        )
        owner = f"fleet:{id(self)}:slot:{slot}"
        core = (
            self._session_registry.get_or_create(
                datagram.endpoint,
                owner=owner,
            )
            if self._session_registry is not None
            else None
        )
        session = DeviceSession(
            datagram.endpoint,
            slot,
            session_core=core,
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
        if (
            self._recording_batch_id
            and self._recording_root is not None
            and self._recording_participant_keys is None
        ):
            if not session.arm_recording(
                self._recording_batch_id, self._recording_root
            ):
                self.error.emit(
                    f"cannot arm recording for {datagram.endpoint[0]}:{datagram.endpoint[1]}"
                )
        self.session_added.emit(session)
        return session

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
        model = session.hardware_type
        old_serial = self._serial_for_endpoint.get(session.endpoint, "")
        if model.lower() != "afd01" or not serial:
            session.state = DeviceSessionState.UNSUPPORTED
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
        session.state = DeviceSessionState.IDENTIFIED
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
