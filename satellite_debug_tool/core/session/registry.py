"""Unique endpoint-to-runtime directory and legacy SessionRegistry facade."""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from PySide6.QtCore import QObject, Signal, Slot

from satellite_debug_tool.core.comm import (
    AdmissionClaimLease,
    Endpoint,
    EndpointDatagram,
    UdpEndpointBroker,
    normalize_endpoint,
)

from .device_session import DeviceSessionCore
from .runtime import (
    CustomerAttachmentLease,
    CustomerAttachmentScope,
    EndpointSendCapability,
    EndpointSessionRuntime,
    ProductionAttachmentLease,
    RuntimeLease,
    RuntimeLeaseError,
    SubscriptionDemandLease,
)


class SessionAuthorityError(RuntimeError):
    """Raised when two cores attempt to own one endpoint."""


class EndpointSessionDirectory(QObject):
    """The only endpoint -> Runtime/Core authority in one application process."""

    runtime_added = Signal(object)
    runtime_removed = Signal(object)
    identity_conflict = Signal(str)

    def __init__(
        self,
        broker: UdpEndpointBroker | None = None,
        *,
        local_port: int = 0,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.broker = broker or UdpEndpointBroker(local_port=local_port, parent=self)
        self._closed = False
        self._runtimes: dict[Endpoint, EndpointSessionRuntime] = {}
        self._runtime_for_core: dict[DeviceSessionCore, EndpointSessionRuntime] = {}
        self._compatibility_leases: dict[tuple[DeviceSessionCore, str], RuntimeLease] = {}
        self._rebuilding_identity = False
        self._duplicate_identity_keys: frozenset[str] = frozenset()
        self.broker.datagram_received.connect(self._on_datagram)

    def runtime(self, endpoint: Endpoint) -> EndpointSessionRuntime | None:
        return self._runtimes.get(normalize_endpoint(endpoint))

    def runtime_for_core(
        self,
        core: DeviceSessionCore,
    ) -> EndpointSessionRuntime | None:
        return self._runtime_for_core.get(core)

    def runtimes(self) -> tuple[EndpointSessionRuntime, ...]:
        return tuple(self._runtimes[endpoint] for endpoint in sorted(self._runtimes))

    def endpoints(self) -> tuple[Endpoint, ...]:
        return tuple(sorted(self._runtimes))

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def active_local_port(self) -> int | None:
        return self.broker.active_local_port

    @property
    def has_active_demand(self) -> bool:
        return self.broker.has_active_demand

    def shutdown(self, timeout_ms: int = 3000) -> bool:
        """Close only after every Runtime and broker ownership lease is released."""

        if self._closed:
            return True
        for runtime in tuple(self._runtimes.values()):
            self._maybe_destroy_runtime(runtime)
        if self._runtimes or self._compatibility_leases:
            return False
        if not self.broker.shutdown(timeout_ms):
            return False
        try:
            self.broker.datagram_received.disconnect(self._on_datagram)
        except (RuntimeError, TypeError):
            pass
        self._closed = True
        return True

    def close(self, timeout_ms: int = 3000) -> bool:
        return self.shutdown(timeout_ms)

    def get_or_create_runtime(
        self,
        endpoint: Endpoint,
        *,
        core: DeviceSessionCore | None = None,
    ) -> EndpointSessionRuntime:
        if self._closed:
            raise SessionAuthorityError("endpoint session directory is closed")
        normalized = normalize_endpoint(endpoint)
        existing = self._runtimes.get(normalized)
        if existing is not None:
            if core is not None and existing.core is not core:
                raise SessionAuthorityError(
                    f"endpoint {normalized[0]}:{normalized[1]} already has a session authority"
                )
            return existing
        if core is not None:
            other = self._runtime_for_core.get(core)
            if other is not None and other.endpoint != normalized:
                raise SessionAuthorityError("one core cannot own two endpoint runtimes")
        runtime = EndpointSessionRuntime(
            normalized,
            broker=self.broker,
            core=core,
            parent=self,
        )
        runtime._customer_attachment_factory = self.acquire_customer_attachment
        runtime.lease_changed.connect(
            lambda current=runtime: self._on_runtime_lease_changed(current)
        )
        runtime.identity_authorization_changed.connect(
            lambda _scope, current=runtime: self._on_runtime_identity_changed(current)
        )
        runtime.identity_conflict.connect(self.identity_conflict)
        runtime.identity_conflict.connect(
            lambda _message, current=runtime: self._on_runtime_identity_changed(current)
        )
        self._runtimes[normalized] = runtime
        self._runtime_for_core[runtime.core] = runtime
        self.runtime_added.emit(runtime)
        return runtime

    def acquire_configuration(self, endpoint: Endpoint, owner: str) -> RuntimeLease:
        return self.get_or_create_runtime(endpoint).acquire_configuration(owner)

    def acquire_transport(self, endpoint: Endpoint, owner: str) -> RuntimeLease:
        return self.get_or_create_runtime(endpoint).acquire_transport(owner)

    def acquire_observer(
        self,
        endpoint: Endpoint,
        owner: str,
        *,
        facet: str,
    ) -> RuntimeLease:
        return self.get_or_create_runtime(endpoint).acquire_observer(owner, facet=facet)

    def acquire_handshake(
        self,
        endpoint: Endpoint,
        owner: str,
        *,
        facet: str,
    ) -> RuntimeLease:
        return self.get_or_create_runtime(endpoint).acquire_handshake(owner, facet=facet)

    def acquire_subscription(
        self,
        endpoint: Endpoint,
        owner: str,
        fast_rate_hz: int,
        *,
        facet: str,
    ) -> SubscriptionDemandLease:
        return self.get_or_create_runtime(endpoint).acquire_subscription(
            owner,
            fast_rate_hz,
            facet=facet,
        )

    def issue_capability(
        self,
        endpoint: Endpoint,
        *,
        owner: str,
        facet: str,
        parent_transport_owner: str | None = None,
        parent_transport_lease: RuntimeLease | None = None,
        admission_claim: AdmissionClaimLease | None = None,
        attachment_scope: CustomerAttachmentScope | None = None,
    ) -> EndpointSendCapability:
        return self.get_or_create_runtime(endpoint).issue_capability(
            owner=owner,
            facet=facet,
            parent_transport_owner=parent_transport_owner,
            parent_transport_lease=parent_transport_lease,
            admission_claim=admission_claim,
            attachment_scope=attachment_scope,
        )

    def acquire_customer_attachment(
        self,
        endpoint: Endpoint,
        owner: str,
        attachment_scope: CustomerAttachmentScope,
        *,
        subscription_hz: int = 10,
    ) -> CustomerAttachmentLease:
        """Acquire exact claim -> transport -> observer -> capability -> demands."""

        normalized = normalize_endpoint(endpoint)
        if attachment_scope.endpoint != normalized:
            raise RuntimeLeaseError("customer attachment endpoint does not match")
        normalized_owner = str(owner).strip()
        if not normalized_owner:
            raise ValueError("owner is required")
        runtime = self.get_or_create_runtime(normalized)
        acquired: list[object] = []
        capability: EndpointSendCapability | None = None
        try:
            claim = self.broker.register_exact_claim(
                normalized,
                owner=normalized_owner,
                facet="customer",
            )
            acquired.append(claim)
            transport = runtime.acquire_transport(normalized_owner)
            acquired.append(transport)
            observer = runtime.acquire_observer(normalized_owner, facet="customer")
            acquired.append(observer)
            capability = runtime.issue_capability(
                owner=normalized_owner,
                facet="customer",
                parent_transport_owner=normalized_owner,
                parent_transport_lease=transport,
                admission_claim=claim,
                attachment_scope=attachment_scope,
            )
            handshake = runtime.acquire_handshake(normalized_owner, facet="customer")
            acquired.append(handshake)
            subscription = runtime.acquire_subscription(
                normalized_owner,
                subscription_hz,
                facet="customer",
            )
            acquired.append(subscription)
        except Exception:
            if capability is not None:
                runtime.release_capability(capability)
            for lease in reversed(acquired):
                lease.release()
            self._maybe_destroy_runtime(runtime)
            raise
        return CustomerAttachmentLease(
            runtime=runtime,
            owner=normalized_owner,
            attachment_scope=attachment_scope,
            exact_claim=claim,
            transport=transport,
            observer=observer,
            handshake=handshake,
            subscription=subscription,
            capability=capability,
        )

    def acquire_production_attachment(
        self,
        endpoint: Endpoint,
        owner: str,
        *,
        subscription_hz: int = 20,
        admission_claim: AdmissionClaimLease | None = None,
        injected_transport: bool = False,
    ) -> ProductionAttachmentLease:
        """Acquire observer -> transport -> capability -> subscription atomically."""

        normalized = normalize_endpoint(endpoint)
        normalized_owner = str(owner).strip()
        if not normalized_owner:
            raise ValueError("owner is required")
        runtime = self.get_or_create_runtime(normalized)
        acquired: list[object] = []
        capability: EndpointSendCapability | None = None
        try:
            observer = runtime.acquire_observer(normalized_owner, facet="production")
            acquired.append(observer)
            transport = (
                runtime.acquire_injected_transport(normalized_owner)
                if injected_transport
                else runtime.acquire_transport(normalized_owner)
            )
            acquired.append(transport)
            capability = runtime.issue_capability(
                owner=normalized_owner,
                facet="production",
                parent_transport_owner=normalized_owner,
                parent_transport_lease=transport,
                admission_claim=admission_claim,
            )
            subscription = runtime.acquire_subscription(
                normalized_owner,
                subscription_hz,
                facet="production",
            )
            acquired.append(subscription)
        except Exception:
            if capability is not None:
                runtime.release_capability(capability)
            for lease in reversed(acquired):
                lease.release()
            self._maybe_destroy_runtime(runtime)
            raise
        return ProductionAttachmentLease(
            runtime=runtime,
            owner=normalized_owner,
            observer=observer,
            transport=transport,
            subscription=subscription,
            capability=capability,
        )

    # ----- M21 compatibility surface: same map, no parallel Registry authority -----

    def session(self, endpoint: Endpoint) -> DeviceSessionCore | None:
        runtime = self.runtime(endpoint)
        return None if runtime is None else runtime.core

    def get_or_create(self, endpoint: Endpoint, *, owner: str) -> DeviceSessionCore:
        runtime = self.get_or_create_runtime(endpoint)
        key = (runtime.core, str(owner))
        if key not in self._compatibility_leases:
            self._compatibility_leases[key] = runtime.acquire_configuration(owner)
        return runtime.core

    def register(
        self,
        endpoint: Endpoint,
        core: DeviceSessionCore,
        *,
        owner: str,
    ) -> DeviceSessionCore:
        runtime = self.get_or_create_runtime(endpoint, core=core)
        key = (core, str(owner))
        if key not in self._compatibility_leases:
            self._compatibility_leases[key] = runtime.acquire_configuration(owner)
        return runtime.core

    def rebind(
        self,
        core: DeviceSessionCore,
        endpoint: Endpoint,
        *,
        owner: str,
    ) -> None:
        runtime = self._runtime_for_core.get(core)
        if runtime is None:
            self.register(endpoint, core, owner=owner)
            return
        if runtime.transport_active:
            raise RuntimeLeaseError("active endpoint runtime cannot be rebound")
        normalized = normalize_endpoint(endpoint)
        existing = self._runtimes.get(normalized)
        if existing is not None and existing is not runtime:
            raise SessionAuthorityError(
                f"endpoint {normalized[0]}:{normalized[1]} already has a session authority"
            )
        self._runtimes.pop(runtime.endpoint, None)
        runtime.endpoint = normalized
        core.bind_endpoint(normalized)
        self._runtimes[normalized] = runtime
        self.get_or_create(normalized, owner=owner)

    def release(self, core: DeviceSessionCore, *, owner: str) -> None:
        lease = self._compatibility_leases.pop((core, str(owner)), None)
        if lease is not None:
            lease.release()

    def owners(self, core: DeviceSessionCore) -> frozenset[str]:
        runtime = self._runtime_for_core.get(core)
        return frozenset() if runtime is None else runtime.configuration_owners

    @Slot(object)
    def _on_datagram(self, datagram: EndpointDatagram) -> None:
        runtime = self._runtimes.get(datagram.endpoint)
        if runtime is None or not runtime.transport_active:
            return
        runtime.feed_datagram(datagram)

    def _on_runtime_lease_changed(self, runtime: EndpointSessionRuntime) -> None:
        self._maybe_destroy_runtime(runtime)

    def _maybe_destroy_runtime(self, runtime: EndpointSessionRuntime) -> None:
        if runtime.has_lifecycle_owners:
            return
        if self._runtimes.get(runtime.endpoint) is not runtime:
            return
        self._runtimes.pop(runtime.endpoint, None)
        self._runtime_for_core.pop(runtime.core, None)
        stale = tuple(
            key for key in self._compatibility_leases if key[0] is runtime.core
        )
        for key in stale:
            self._compatibility_leases.pop(key, None)
        self.runtime_removed.emit(runtime.endpoint)
        runtime.deleteLater()
        self._rebuild_identity_index()

    def _on_runtime_identity_changed(self, _runtime: EndpointSessionRuntime) -> None:
        self._rebuild_identity_index()

    def _rebuild_identity_index(self) -> None:
        if self._rebuilding_identity:
            return
        self._rebuilding_identity = True
        try:
            by_key: dict[str, list[EndpointSessionRuntime]] = defaultdict(list)
            for runtime in self._runtimes.values():
                for key in runtime.current_identity_keys:
                    by_key[key].append(runtime)
            duplicate_keys = frozenset(
                key for key, runtimes in by_key.items() if len(set(runtimes)) > 1
            )
            conflicted = {
                runtime
                for key in duplicate_keys
                for runtime in by_key.get(key, ())
            }
            for runtime in self._runtimes.values():
                runtime.set_directory_identity_conflict(runtime in conflicted)
            new_keys = duplicate_keys - self._duplicate_identity_keys
            self._duplicate_identity_keys = duplicate_keys
            for key in sorted(new_keys):
                endpoints = ", ".join(
                    f"{runtime.endpoint[0]}:{runtime.endpoint[1]}"
                    for runtime in by_key[key]
                )
                self.identity_conflict.emit(f"duplicate stable identity {key} at {endpoints}")
        finally:
            self._rebuilding_identity = False


class SessionRegistry(EndpointSessionDirectory):
    """Compatibility name for the single evolved endpoint directory."""


__all__ = [
    "EndpointSessionDirectory",
    "SessionAuthorityError",
    "SessionRegistry",
]
