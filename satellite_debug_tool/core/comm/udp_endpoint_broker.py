"""Process-wide UDP endpoint transport with explicit admission claims.

The broker owns one socket, preserves the source endpoint of every datagram and
routes only sources matched by an active exact or CIDR claim.  Business session
state deliberately lives in :mod:`satellite_debug_tool.core.session`.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
import ipaddress
import socket
import threading
import time
from typing import Callable, Iterator, Optional

from PySide6.QtCore import QObject, QThread, Signal


Endpoint = tuple[str, int]
DiscoveryFrameFactory = Callable[[], bytes]


class BrokerConfigurationError(ValueError):
    """Raised when a broker endpoint or claim is not safe to use."""


class AdmissionClaimKind(str, Enum):
    EXACT = "exact"
    CIDR = "cidr"


@dataclass(frozen=True)
class AdmissionClaimContext:
    """Immutable routing context attached to one accepted datagram."""

    owner: str
    facet: str
    kind: AdmissionClaimKind
    selector: str


@dataclass(frozen=True)
class EndpointDatagram:
    endpoint: Endpoint
    data: bytes
    wall_time_ns: int
    monotonic_ns: int
    matched_claims: tuple[AdmissionClaimContext, ...] = ()


@dataclass(frozen=True)
class UdpEndpointBrokerStatistics:
    discovery_datagrams: int = 0
    control_datagrams: int = 0
    received_datagrams: int = 0
    received_bytes: int = 0
    ignored_datagrams: int = 0
    send_failures: int = 0
    receive_failures: int = 0


@dataclass(frozen=True)
class _ClaimRule:
    token: int
    context: AdmissionClaimContext
    endpoint: Endpoint | None = None
    network: ipaddress.IPv4Network | None = None
    device_port: int | None = None

    def matches(self, endpoint: Endpoint) -> bool:
        if self.endpoint is not None:
            return endpoint == self.endpoint
        network = self.network
        if network is None or self.device_port != endpoint[1]:
            return False
        try:
            return ipaddress.ip_address(endpoint[0]) in network
        except ValueError:
            return False


@dataclass
class _DiscoveryRule:
    token: int
    claim_token: int
    network: ipaddress.IPv4Network
    device_port: int
    interval_s: float
    frame_factory: DiscoveryFrameFactory
    next_due: float = 0.0


class _OwnedLease:
    """Small idempotent ownership handle used by claims and broker demands."""

    def __init__(self, owner: str, release: Callable[[], None]) -> None:
        self.owner = str(owner)
        self._release = release
        self._released = False
        self._lock = threading.Lock()

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._release()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


class BrokerDemandLease(_OwnedLease):
    """Proof that one consumer requires the process-wide UDP socket."""


class AdmissionClaimLease(_OwnedLease):
    """Proof that one owner admits a source endpoint into broker routing."""

    def __init__(
        self,
        owner: str,
        context: AdmissionClaimContext,
        token: int,
        broker: "UdpEndpointBroker",
    ) -> None:
        self.context = context
        self.token = int(token)
        self._broker = broker
        super().__init__(owner, lambda: broker._release_claim(self.token))


class DiscoveryLease(_OwnedLease):
    """Periodic discovery sender tied to one active CIDR claim."""


class UdpEndpointBroker(QThread):
    """Own exactly one UDP socket for all admitted device endpoints.

    Receive happens on this QThread.  Claim, lease and session state are not
    mutated from the receive loop; the immutable datagram signal is queued to
    the owning Qt thread by normal Qt connection rules.
    """

    datagram_received = Signal(object)
    listening = Signal(str, int)
    stopped = Signal()
    error = Signal(str)
    statistics_changed = Signal(object)

    def __init__(
        self,
        *,
        local_port: int,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        port = int(local_port)
        if not (0 <= port <= 65535):
            raise BrokerConfigurationError("local UDP port is out of range")
        self._local_port_requested = port
        self._socket: socket.socket | None = None
        self._socket_lock = threading.RLock()
        self._stop_event = threading.Event()

        self._ownership_lock = threading.RLock()
        self._next_token = 0
        self._demands: dict[int, str] = {}
        self._claims: dict[int, _ClaimRule] = {}
        self._discoveries: dict[int, _DiscoveryRule] = {}
        self._retiring_endpoints: set[Endpoint] = set()
        self._global_resource_exclusion = False

        self._statistics = UdpEndpointBrokerStatistics()
        self._statistics_lock = threading.Lock()

    @property
    def local_port(self) -> int:
        with self._socket_lock:
            sock = self._socket
            if sock is not None:
                try:
                    return int(sock.getsockname()[1])
                except OSError:
                    pass
        return self._local_port_requested

    @property
    def active_local_port(self) -> int | None:
        """Return the bound port only while an active demand owns the socket."""

        with self._ownership_lock:
            if not self._demands:
                return None
        with self._socket_lock:
            sock = self._socket
            if sock is None:
                return None
            try:
                return int(sock.getsockname()[1])
            except OSError:
                return None

    @property
    def statistics(self) -> UdpEndpointBrokerStatistics:
        with self._statistics_lock:
            return self._statistics

    @property
    def demand_count(self) -> int:
        with self._ownership_lock:
            return len(self._demands)

    @property
    def has_active_demand(self) -> bool:
        with self._ownership_lock:
            return bool(self._demands)

    @property
    def claim_count(self) -> int:
        with self._ownership_lock:
            return len(self._claims)

    @property
    def discovery_count(self) -> int:
        with self._ownership_lock:
            return len(self._discoveries)

    def discovery_demands_for(self, endpoint: Endpoint) -> int:
        """Count active discovery owners that can reintroduce ``endpoint``."""

        normalized = normalize_endpoint(endpoint)
        with self._ownership_lock:
            if self._global_resource_exclusion:
                raise RuntimeError("global broker resource exclusion is active")
            return sum(
                1
                for rule in self._discoveries.values()
                if rule.device_port == normalized[1]
                and ipaddress.ip_address(normalized[0]) in rule.network
            )

    @contextmanager
    def hold_endpoint_retirement(self, endpoint: Endpoint) -> Iterator[None]:
        """Exclude new broker claims/discovery for one retirement decision."""

        normalized = normalize_endpoint(endpoint)
        with self._ownership_lock:
            if normalized in self._retiring_endpoints:
                raise RuntimeError("endpoint retirement is already in progress")
            self._retiring_endpoints.add(normalized)
        try:
            yield
        finally:
            with self._ownership_lock:
                self._retiring_endpoints.discard(normalized)

    @contextmanager
    def hold_global_resource_exclusion(self) -> Iterator[None]:
        """Exclude every new demand/claim/discovery during ledger rebuild."""

        with self._ownership_lock:
            if self._global_resource_exclusion or self._retiring_endpoints:
                raise RuntimeError("global broker resource exclusion is already active")
            self._global_resource_exclusion = True
        try:
            yield
        finally:
            with self._ownership_lock:
                self._global_resource_exclusion = False

    def acquire_demand(self, owner: str) -> BrokerDemandLease:
        """Start the socket for the first demand and stop after the last."""

        normalized_owner = _required_owner(owner)
        with self._ownership_lock:
            if self._global_resource_exclusion:
                raise RuntimeError("global resource exclusion blocks UDP demand")
            token = self._allocate_token_locked()
            first = not self._demands
            self._demands[token] = normalized_owner
        if first and not self.start_broker():
            with self._ownership_lock:
                self._demands.pop(token, None)
            raise RuntimeError("cannot start UDP endpoint broker")
        return BrokerDemandLease(
            normalized_owner,
            lambda: self._release_demand(token),
        )

    def register_exact_claim(
        self,
        endpoint: Endpoint,
        *,
        owner: str,
        facet: str,
    ) -> AdmissionClaimLease:
        normalized = normalize_endpoint(endpoint)
        normalized_owner = _required_owner(owner)
        normalized_facet = _required_facet(facet)
        context = AdmissionClaimContext(
            normalized_owner,
            normalized_facet,
            AdmissionClaimKind.EXACT,
            f"{normalized[0]}:{normalized[1]}",
        )
        with self._ownership_lock:
            if self._global_resource_exclusion:
                raise RuntimeError("global resource exclusion blocks exact claims")
            if normalized in self._retiring_endpoints:
                raise RuntimeError("endpoint retirement excludes new exact claims")
            token = self._allocate_token_locked()
            self._claims[token] = _ClaimRule(
                token,
                context,
                endpoint=normalized,
            )
        return AdmissionClaimLease(normalized_owner, context, token, self)

    def register_cidr_claim(
        self,
        discovery_cidr: str,
        *,
        device_port: int,
        owner: str,
        facet: str,
    ) -> AdmissionClaimLease:
        network = _normalize_network(discovery_cidr)
        port = _normalize_remote_port(device_port)
        normalized_owner = _required_owner(owner)
        normalized_facet = _required_facet(facet)
        context = AdmissionClaimContext(
            normalized_owner,
            normalized_facet,
            AdmissionClaimKind.CIDR,
            f"{network}@{port}",
        )
        with self._ownership_lock:
            if self._global_resource_exclusion:
                raise RuntimeError("global resource exclusion blocks CIDR claims")
            if any(
                retiring[1] == port
                and ipaddress.ip_address(retiring[0]) in network
                for retiring in self._retiring_endpoints
            ):
                raise RuntimeError("endpoint retirement excludes new CIDR claims")
            token = self._allocate_token_locked()
            self._claims[token] = _ClaimRule(
                token,
                context,
                network=network,
                device_port=port,
            )
        return AdmissionClaimLease(normalized_owner, context, token, self)

    def register_discovery(
        self,
        claim: AdmissionClaimLease,
        *,
        interval_s: float,
        frame_factory: DiscoveryFrameFactory,
    ) -> DiscoveryLease:
        """Register periodic frames for an active CIDR admission claim."""

        interval = float(interval_s)
        if not (0.2 <= interval <= 300.0):
            raise BrokerConfigurationError(
                "discovery interval must be 0.2..300 seconds"
            )
        if claim._broker is not self or claim.released:
            raise ValueError("discovery requires an active claim from this broker")
        with self._ownership_lock:
            if self._global_resource_exclusion:
                raise RuntimeError("global resource exclusion blocks discovery")
            rule = self._claims.get(claim.token)
            if rule is None or rule.network is None or rule.device_port is None:
                raise ValueError("discovery requires an active CIDR claim")
            if any(
                retiring[1] == rule.device_port
                and ipaddress.ip_address(retiring[0]) in rule.network
                for retiring in self._retiring_endpoints
            ):
                raise RuntimeError("endpoint retirement excludes new discovery")
            token = self._allocate_token_locked()
            self._discoveries[token] = _DiscoveryRule(
                token=token,
                claim_token=claim.token,
                network=rule.network,
                device_port=rule.device_port,
                interval_s=interval,
                frame_factory=frame_factory,
            )
        return DiscoveryLease(
            claim.owner,
            lambda: self._release_discovery(token),
        )

    def matched_claims(
        self,
        endpoint: Endpoint,
    ) -> tuple[AdmissionClaimContext, ...]:
        normalized = normalize_endpoint(endpoint)
        with self._ownership_lock:
            rules = tuple(self._claims.values())
        return tuple(rule.context for rule in rules if rule.matches(normalized))

    def claim_active(self, token: int) -> bool:
        with self._ownership_lock:
            return int(token) in self._claims

    def claim_allows(
        self,
        token: int,
        endpoint: Endpoint,
        *,
        facet: str | None = None,
    ) -> bool:
        """Validate that one active claim admits this endpoint and optional facet."""

        try:
            normalized = normalize_endpoint(endpoint)
            normalized_facet = None if facet is None else _required_facet(facet)
        except (TypeError, ValueError):
            return False
        with self._ownership_lock:
            rule = self._claims.get(int(token))
            return bool(
                rule is not None
                and rule.matches(normalized)
                and (
                    normalized_facet is None
                    or rule.context.facet == normalized_facet
                )
            )

    def start_broker(self) -> bool:
        if self.isRunning():
            return True
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self._local_port_requested))
            sock.settimeout(0.05)
        except OSError as exc:
            sock.close()
            self.error.emit(f"cannot bind endpoint UDP socket: {exc}")
            return False
        with self._socket_lock:
            if self._socket is not None:
                sock.close()
                return True
            self._socket = sock
            self._stop_event.clear()
        self.start()
        return True

    def stop_broker(self, timeout_ms: int = 3000) -> bool:
        self._stop_event.set()
        with self._socket_lock:
            sock = self._socket
            self._socket = None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self.isRunning() and QThread.currentThread() is not self:
            self.wait(max(0, int(timeout_ms)))
        return not self.isRunning()

    def shutdown(self, timeout_ms: int = 3000) -> bool:
        """Stop within ``timeout_ms`` only after every ownership lease is gone."""

        with self._ownership_lock:
            if self._demands or self._claims or self._discoveries:
                return False
        return self.stop_broker(timeout_ms)

    def close(self, timeout_ms: int = 3000) -> bool:
        return self.shutdown(timeout_ms)

    def send_to(self, endpoint: Endpoint, data: bytes) -> bool:
        """Synchronously write a complete datagram to an admitted endpoint."""

        try:
            normalized = normalize_endpoint(endpoint)
        except (TypeError, ValueError):
            return False
        frame = bytes(data)
        if not frame or not self.matched_claims(normalized):
            return False
        with self._socket_lock:
            sock = self._socket
            if sock is None:
                return False
            try:
                sent = sock.sendto(frame, normalized)
            except OSError:
                self._increment_statistics(send_failures=1)
                return False
        if sent != len(frame):
            self._increment_statistics(send_failures=1)
            return False
        self._increment_statistics(control_datagrams=1)
        return True

    def run(self) -> None:
        self.listening.emit("0.0.0.0", self.local_port)
        try:
            while not self._stop_event.is_set():
                self._send_due_discoveries(time.monotonic())
                self._receive_once()
        finally:
            with self._socket_lock:
                sock = self._socket
                self._socket = None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self.stopped.emit()

    def _send_due_discoveries(self, now: float) -> None:
        with self._ownership_lock:
            due = tuple(
                item
                for item in self._discoveries.values()
                if item.next_due <= now and item.claim_token in self._claims
            )
            for item in due:
                current = self._discoveries.get(item.token)
                if current is not None:
                    current.next_due = now + current.interval_s
        for item in due:
            try:
                frame = bytes(item.frame_factory())
            except Exception as exc:
                self._increment_statistics(send_failures=1)
                self.error.emit(f"cannot build UDP discovery frame: {exc}")
                continue
            sent = 0
            failures = 0
            hosts = item.network.hosts()
            if item.network.num_addresses == 1:
                hosts = iter((item.network.network_address,))
            for address in hosts:
                if self._stop_event.is_set():
                    break
                if self._send_raw((str(address), item.device_port), frame):
                    sent += 1
                else:
                    failures += 1
            if sent or failures:
                self._increment_statistics(
                    discovery_datagrams=sent,
                    send_failures=failures,
                )

    def _send_raw(self, endpoint: Endpoint, frame: bytes) -> bool:
        if not frame:
            return False
        with self._socket_lock:
            sock = self._socket
            if sock is None:
                return False
            try:
                return sock.sendto(frame, endpoint) == len(frame)
            except OSError:
                return False

    def _receive_once(self) -> None:
        with self._socket_lock:
            sock = self._socket
        if sock is None:
            return
        try:
            data, source = sock.recvfrom(65535)
        except socket.timeout:
            return
        except OSError as exc:
            if not self._stop_event.is_set():
                self._increment_statistics(receive_failures=1)
                self.error.emit(f"endpoint UDP receive failed: {exc}")
            return
        endpoint = (str(source[0]), int(source[1]))
        claims = self.matched_claims(endpoint)
        if not claims:
            self._increment_statistics(ignored_datagrams=1)
            return
        datagram = EndpointDatagram(
            endpoint=endpoint,
            data=bytes(data),
            wall_time_ns=time.time_ns(),
            monotonic_ns=time.monotonic_ns(),
            matched_claims=claims,
        )
        self._increment_statistics(
            received_datagrams=1,
            received_bytes=len(data),
        )
        self.datagram_received.emit(datagram)

    def _release_demand(self, token: int) -> None:
        with self._ownership_lock:
            self._demands.pop(int(token), None)
            stop = not self._demands
        if stop:
            self.stop_broker()

    def _release_claim(self, token: int) -> None:
        with self._ownership_lock:
            self._claims.pop(int(token), None)
            stale_discoveries = tuple(
                discovery_token
                for discovery_token, rule in self._discoveries.items()
                if rule.claim_token == int(token)
            )
            for discovery_token in stale_discoveries:
                self._discoveries.pop(discovery_token, None)

    def _release_discovery(self, token: int) -> None:
        with self._ownership_lock:
            self._discoveries.pop(int(token), None)

    def _allocate_token_locked(self) -> int:
        self._next_token += 1
        return self._next_token

    def _increment_statistics(self, **changes: int) -> None:
        with self._statistics_lock:
            values = {
                field: getattr(self._statistics, field) + int(delta)
                for field, delta in changes.items()
            }
            self._statistics = replace(self._statistics, **values)
            snapshot = self._statistics
        self.statistics_changed.emit(snapshot)


def normalize_endpoint(endpoint: Endpoint) -> Endpoint:
    try:
        address = ipaddress.ip_address(str(endpoint[0]).strip())
        port = _normalize_remote_port(endpoint[1])
    except (IndexError, TypeError, ValueError) as exc:
        raise BrokerConfigurationError(f"invalid UDP endpoint: {endpoint!r}") from exc
    if address.version != 4 or address.is_multicast or address.is_unspecified:
        raise BrokerConfigurationError("only IPv4 unicast endpoints are supported")
    return str(address), port


def _normalize_network(value: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(str(value).strip(), strict=False)
    except ValueError as exc:
        raise BrokerConfigurationError(f"invalid discovery CIDR: {exc}") from exc
    if network.version != 4:
        raise BrokerConfigurationError("only IPv4 discovery is supported")
    host_count = max(1, int(network.num_addresses) - 2)
    if host_count > 1024:
        raise BrokerConfigurationError(
            "discovery CIDR cannot contain more than 1024 hosts"
        )
    return network


def _normalize_remote_port(value: object) -> int:
    port = int(value)
    if not (1 <= port <= 65535):
        raise BrokerConfigurationError("device UDP port is out of range")
    return port


def _required_owner(value: object) -> str:
    owner = str(value).strip()
    if not owner:
        raise ValueError("owner is required")
    return owner


def _required_facet(value: object) -> str:
    facet = str(value).strip().lower()
    if not facet:
        raise ValueError("facet is required")
    return facet


__all__ = [
    "AdmissionClaimContext",
    "AdmissionClaimKind",
    "AdmissionClaimLease",
    "BrokerConfigurationError",
    "BrokerDemandLease",
    "DiscoveryLease",
    "Endpoint",
    "EndpointDatagram",
    "UdpEndpointBroker",
    "UdpEndpointBrokerStatistics",
    "normalize_endpoint",
]
