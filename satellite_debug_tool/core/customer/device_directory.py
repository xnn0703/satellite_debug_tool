"""Persistent customer endpoint directory projected onto shared runtimes.

This module owns customer configuration intent only.  It never creates a
``DeviceSessionCore`` or transport worker directly; configured endpoints are
kept alive exclusively through ``EndpointSessionDirectory`` configuration
leases.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
import ipaddress
import uuid
from typing import Any, Callable, Mapping, Optional, Protocol

from PySide6.QtCore import QObject, QThread, Signal

from satellite_debug_tool.core.session import CustomerAttachmentScope


Endpoint = tuple[str, int]
MAX_CUSTOMER_DEVICES = 4


class CustomerDeviceDirectoryError(RuntimeError):
    """Base error for customer-directory configuration transitions."""


class CustomerDeviceValidationError(CustomerDeviceDirectoryError, ValueError):
    """Raised when an endpoint or persisted customer list is invalid."""


class CustomerDeviceCapacityError(CustomerDeviceDirectoryError):
    """Raised when a fifth configured endpoint is requested."""


class CustomerDeviceNotFoundError(CustomerDeviceDirectoryError, LookupError):
    """Raised when an intent targets an endpoint outside the customer list."""


class CustomerDeviceBusyError(CustomerDeviceDirectoryError):
    """Raised when Edit/Delete would cross an active endpoint operation."""


class CustomerDevicePersistenceError(CustomerDeviceDirectoryError):
    """Raised when Settings cannot atomically commit a customer mutation."""


class CustomerDeviceLeaseError(CustomerDeviceDirectoryError):
    """Raised when the shared Directory cannot establish configuration ownership."""


class CustomerDeviceConfigurationBlocked(CustomerDeviceDirectoryError):
    """Raised while device settings are not safely writable."""


class _SettingsProtocol(Protocol):
    def get(self, key_path: str, default: Any = None) -> Any: ...

    def set(self, key_path: str, value: Any) -> None: ...

    def save(self) -> None: ...


class _ConfigurationLeaseProtocol(Protocol):
    runtime: object
    released: bool

    def release(self) -> bool: ...


class _EndpointSessionDirectoryProtocol(Protocol):
    def runtime(self, endpoint: Endpoint) -> object | None: ...

    def acquire_configuration(
        self,
        endpoint: Endpoint,
        owner: str,
    ) -> _ConfigurationLeaseProtocol: ...

    def acquire_customer_attachment(
        self,
        endpoint: Endpoint,
        owner: str,
        attachment_scope: CustomerAttachmentScope,
        *,
        subscription_hz: int = 10,
    ) -> object: ...


@dataclass(frozen=True)
class CustomerDeviceSnapshot:
    """UI-facing facts for one configured endpoint.

    Field names intentionally match ``CustomerWorkspace``'s duck adapter while
    keeping the core independent from the UI package.
    """

    key: str
    endpoint: Endpoint
    display_identity: str
    connection_phase: str
    business_state: str
    recording_active: bool
    operation_busy: bool
    identity_pending: bool
    identity_conflict: bool


@dataclass(frozen=True)
class CustomerDeviceSupplementalFacts:
    """Facts owned outside Runtime, such as recorder and view state."""

    business_state: str = ""
    recording_active: bool = False
    operation_busy: bool = False

    @classmethod
    def from_value(cls, value: object) -> "CustomerDeviceSupplementalFacts":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            field = value.get
        else:
            field = lambda name, default=None: getattr(value, name, default)
        return cls(
            business_state=str(field("business_state", "") or ""),
            recording_active=bool(field("recording_active", False)),
            operation_busy=bool(field("operation_busy", False)),
        )


@dataclass(frozen=True)
class CustomerDeviceMutationResult:
    """Deterministic result suitable for dialog/status presentation."""

    action: str
    endpoint: Optional[Endpoint]
    changed: bool
    duplicate_selected: bool = False
    previous_endpoint: Optional[Endpoint] = None


SupplementalFactsProvider = Callable[
    [Endpoint, object], CustomerDeviceSupplementalFacts | Mapping[str, object] | object
]


def normalize_customer_endpoint(value: object) -> Endpoint:
    """Normalize one explicit IPv4-unicast customer endpoint."""

    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise CustomerDeviceValidationError("customer endpoint must be an (IPv4, port) pair")
    raw_ip, raw_port = value
    try:
        address = ipaddress.ip_address(str(raw_ip).strip())
    except ValueError as exc:
        raise CustomerDeviceValidationError(
            f"invalid customer IPv4 endpoint: {raw_ip}"
        ) from exc
    if (
        address.version != 4
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or int(address) == 0xFFFFFFFF
    ):
        raise CustomerDeviceValidationError(
            f"customer endpoint is not IPv4 unicast: {raw_ip}"
        )
    if isinstance(raw_port, bool):
        raise CustomerDeviceValidationError("customer endpoint port must be an integer")
    if isinstance(raw_port, int):
        port = raw_port
    elif isinstance(raw_port, str) and raw_port.isascii() and raw_port.isdecimal():
        port = int(raw_port, 10)
    else:
        raise CustomerDeviceValidationError("customer endpoint port must be an integer")
    if not 1 <= port <= 65535:
        raise CustomerDeviceValidationError("customer endpoint port must be in 1..65535")
    return str(address), port


def _enum_token(value: object, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, Enum):
        value = value.value if isinstance(value.value, str) else value.name
    return str(value).strip().upper().rsplit(".", 1)[-1]


class CustomerDeviceDirectory(QObject):
    """Settings-backed customer device list over one shared Runtime Directory."""

    devices_changed = Signal()
    active_endpoint_changed = Signal(object)
    device_changed = Signal(object)
    attachment_changed = Signal(object, bool)
    attachment_failed = Signal(object, str)

    def __init__(
        self,
        settings: _SettingsProtocol,
        session_directory: _EndpointSessionDirectoryProtocol,
        *,
        supplemental_facts_provider: Optional[SupplementalFactsProvider] = None,
        owner_prefix: str = "customer-config",
        subscription_hz: int = 10,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        prefix = str(owner_prefix).strip()
        if not prefix:
            raise ValueError("customer configuration owner prefix is required")
        rate = int(subscription_hz)
        if not 1 <= rate <= 20:
            raise ValueError("customer subscription rate must be in 1..20 Hz")
        self._settings = settings
        self._directory = session_directory
        self._supplemental_facts_provider = supplemental_facts_provider
        self._owner_prefix = prefix
        self._subscription_hz = rate
        self._configured_endpoints: tuple[Endpoint, ...] = ()
        self._records: dict[Endpoint, dict[str, object]] = {}
        self._active_endpoint: Optional[Endpoint] = None
        self._leases: dict[Endpoint, _ConfigurationLeaseProtocol] = {}
        self._attachments: dict[Endpoint, object] = {}
        self._attachment_epochs: dict[Endpoint, int] = {}
        self._runtime_bindings: dict[Endpoint, list[tuple[object, object]]] = {}
        self._closed = False
        self._configuration_error = ""

        if self._settings_recovery_blocked():
            self._configuration_error = str(
                getattr(settings, "device_configuration_error", "")
                or "customer device settings require recovery"
            )
            return

        endpoints, active = self._read_settings_configuration()
        acquired: dict[Endpoint, _ConfigurationLeaseProtocol] = {}
        try:
            for endpoint in endpoints:
                acquired[endpoint] = self._acquire_configuration(endpoint)
        except Exception as exc:
            for lease in reversed(tuple(acquired.values())):
                lease.release()
            raise CustomerDeviceLeaseError(
                "failed to establish customer configuration ownership"
            ) from exc
        self._configured_endpoints = endpoints
        self._active_endpoint = active
        self._leases = acquired
        for endpoint in endpoints:
            self._wire_runtime(endpoint)
        self._connect_optional_signal(
            getattr(session_directory, "runtime_removed", None),
            self._on_runtime_removed,
        )
        provider_signal = getattr(supplemental_facts_provider, "device_changed", None)
        self._connect_optional_signal(provider_signal, self.refresh_endpoint)

    @staticmethod
    def _connect_optional_signal(signal: object, slot: object) -> bool:
        if signal is None or not hasattr(signal, "connect"):
            return False
        signal.connect(slot)
        return True

    def _assert_owner_thread(self) -> None:
        if QThread.currentThread() is not self.thread():
            raise RuntimeError("customer device directory must run on its Qt owner thread")

    def _assert_open_and_mutable(self) -> None:
        self._assert_owner_thread()
        if self._closed:
            raise CustomerDeviceDirectoryError("customer device directory is closed")
        if self.configuration_blocked:
            raise CustomerDeviceConfigurationBlocked(self.configuration_error)

    def _settings_recovery_blocked(self) -> bool:
        return bool(
            getattr(self._settings, "read_only_recovery", False)
            or getattr(self._settings, "device_configuration_blocked", False)
        )

    @property
    def configuration_blocked(self) -> bool:
        return bool(self._configuration_error or self._settings_recovery_blocked())

    @property
    def configuration_error(self) -> str:
        dynamic = str(
            getattr(self._settings, "device_configuration_error", "") or ""
        )
        return dynamic or self._configuration_error or "customer device settings are blocked"

    def _read_settings_configuration(
        self,
    ) -> tuple[tuple[Endpoint, ...], Optional[Endpoint]]:
        raw_devices = self._settings.get("customer.devices", [])
        if not isinstance(raw_devices, list):
            raise CustomerDeviceValidationError("customer.devices must be a list")
        if len(raw_devices) > MAX_CUSTOMER_DEVICES:
            raise CustomerDeviceCapacityError("customer.devices supports at most 4 endpoints")
        endpoints: list[Endpoint] = []
        seen: set[Endpoint] = set()
        for raw in raw_devices:
            if not isinstance(raw, Mapping):
                raise CustomerDeviceValidationError("customer device must be an object")
            endpoint = normalize_customer_endpoint((raw.get("ip"), raw.get("port")))
            if endpoint in seen:
                raise CustomerDeviceValidationError(
                    f"duplicate customer endpoint: {endpoint[0]}:{endpoint[1]}"
                )
            seen.add(endpoint)
            endpoints.append(endpoint)
            self._records[endpoint] = copy.deepcopy(dict(raw))
        raw_active = self._settings.get("customer.active_endpoint", None)
        active: Optional[Endpoint] = None
        if raw_active is not None:
            if not isinstance(raw_active, Mapping):
                raise CustomerDeviceValidationError(
                    "customer.active_endpoint must be an object or null"
                )
            active = normalize_customer_endpoint(
                (raw_active.get("ip"), raw_active.get("port"))
            )
            if active not in seen:
                raise CustomerDeviceValidationError(
                    "customer.active_endpoint must belong to customer.devices"
                )
        return tuple(endpoints), active

    def _owner_for(self, endpoint: Endpoint) -> str:
        return f"{self._owner_prefix}:{endpoint[0]}:{endpoint[1]}"

    def _acquire_configuration(
        self,
        endpoint: Endpoint,
    ) -> _ConfigurationLeaseProtocol:
        lease = self._directory.acquire_configuration(
            endpoint,
            self._owner_for(endpoint),
        )
        runtime = self._directory.runtime(endpoint)
        if runtime is None or getattr(lease, "runtime", runtime) is not runtime:
            lease.release()
            raise CustomerDeviceLeaseError(
                "configuration lease did not resolve to the Directory runtime"
            )
        return lease

    def endpoints(self) -> tuple[Endpoint, ...]:
        return self._configured_endpoints

    def device_record(self, endpoint: Endpoint) -> dict[str, object]:
        normalized = normalize_customer_endpoint(endpoint)
        if normalized not in self._configured_endpoints:
            raise CustomerDeviceNotFoundError("customer device is not configured")
        return copy.deepcopy(self._records[normalized])

    def device_id(self, endpoint: Endpoint) -> str:
        return str(self.device_record(endpoint)["id"])

    def update_accessories(
        self,
        endpoint: Endpoint,
        *,
        external_power: Optional[Mapping[str, object]] = None,
        iperf: Optional[Mapping[str, object]] = None,
    ) -> None:
        self._assert_open_and_mutable()
        normalized = normalize_customer_endpoint(endpoint)
        if normalized not in self._configured_endpoints:
            raise CustomerDeviceNotFoundError("customer device is not configured")
        updated = copy.deepcopy(self._records[normalized])
        if external_power is not None:
            profile = copy.deepcopy(dict(external_power))
            host = str(profile.get("host", "")).strip()
            if host:
                try:
                    address = ipaddress.ip_address(host)
                except ValueError as exc:
                    raise CustomerDeviceValidationError(
                        "external power host must be an IPv4 address"
                    ) from exc
                if address.version != 4 or address.is_unspecified or address.is_multicast:
                    raise CustomerDeviceValidationError(
                        "external power host must be an IPv4 address"
                    )
                host = str(address)
                for other_endpoint, other_record in self._records.items():
                    if other_endpoint == normalized:
                        continue
                    other_profile = other_record.get("external_power", {})
                    if (
                        isinstance(other_profile, Mapping)
                        and str(other_profile.get("host", "")).strip() == host
                    ):
                        raise CustomerDeviceValidationError(
                            f"external power {host}:2268 is already assigned to "
                            f"{other_endpoint[0]}:{other_endpoint[1]}"
                        )
            profile["host"] = host
            updated["external_power"] = profile
        if iperf is not None:
            updated["iperf"] = copy.deepcopy(dict(iperf))
        records = dict(self._records)
        records[normalized] = updated
        self._persist_configuration(self._configured_endpoints, self._active_endpoint, records)
        self._records = records
        self.device_changed.emit(normalized)

    def active_endpoint(self) -> Optional[Endpoint]:
        return self._active_endpoint

    def runtime(self, endpoint: Endpoint) -> object | None:
        endpoint = normalize_customer_endpoint(endpoint)
        if endpoint not in self._configured_endpoints:
            return None
        return self._directory.runtime(endpoint)

    def attachment(self, endpoint: Endpoint) -> object | None:
        endpoint = normalize_customer_endpoint(endpoint)
        if endpoint not in self._configured_endpoints:
            return None
        lease = self._attachments.get(endpoint)
        if lease is not None and bool(getattr(lease, "released", False)):
            self._attachments.pop(endpoint, None)
            return None
        return lease

    def attach(self, endpoint: Endpoint) -> object:
        """Attach one configured endpoint without changing active selection."""

        self._assert_open_and_mutable()
        normalized = normalize_customer_endpoint(endpoint)
        if normalized not in self._configured_endpoints:
            raise CustomerDeviceNotFoundError("attached endpoint is not configured")
        existing = self.attachment(normalized)
        if existing is not None:
            return existing
        next_epoch = self._attachment_epochs.get(normalized, 0) + 1
        scope = CustomerAttachmentScope(normalized, next_epoch)
        owner = (
            f"customer-attachment:{normalized[0]}:{normalized[1]}:{next_epoch}"
        )
        try:
            lease = self._directory.acquire_customer_attachment(
                normalized,
                owner,
                scope,
                subscription_hz=self._subscription_hz,
            )
        except Exception as exc:
            raise CustomerDeviceLeaseError(
                f"failed to attach customer endpoint {normalized[0]}:{normalized[1]}"
            ) from exc
        self._attachment_epochs[normalized] = next_epoch
        self._attachments[normalized] = lease
        self.attachment_changed.emit(normalized, True)
        self.device_changed.emit(normalized)
        return lease

    def detach(self, endpoint: Endpoint) -> bool:
        """Release one Customer attachment, retaining it on any safety refusal."""

        self._assert_owner_thread()
        if self._closed:
            raise CustomerDeviceDirectoryError("customer device directory is closed")
        normalized = normalize_customer_endpoint(endpoint)
        if normalized not in self._configured_endpoints:
            raise CustomerDeviceNotFoundError("detached endpoint is not configured")
        lease = self.attachment(normalized)
        if lease is None:
            return True
        runtime = self._directory.runtime(normalized)
        supplemental = self._supplemental_facts(normalized, runtime)
        if supplemental.recording_active:
            reason = "stop endpoint recording before disconnecting"
            self.attachment_failed.emit(normalized, reason)
            return False
        snapshot = self._snapshot(normalized)
        if snapshot.operation_busy:
            reason = "finish the endpoint operation before disconnecting"
            self.attachment_failed.emit(normalized, reason)
            return False
        try:
            released = bool(lease.release())
        except Exception as exc:
            reason = f"customer attachment release failed: {exc}"
            self.attachment_failed.emit(normalized, reason)
            return False
        if not released:
            reason = "customer attachment release was refused by the active runtime"
            self.attachment_failed.emit(normalized, reason)
            return False
        self._attachments.pop(normalized, None)
        self.attachment_changed.emit(normalized, False)
        self.device_changed.emit(normalized)
        return True

    def devices(self) -> tuple[CustomerDeviceSnapshot, ...]:
        return tuple(self._snapshot(endpoint) for endpoint in self._configured_endpoints)

    def _snapshot(self, endpoint: Endpoint) -> CustomerDeviceSnapshot:
        runtime = self._directory.runtime(endpoint)
        supplemental = self._supplemental_facts(endpoint, runtime)
        if runtime is None:
            return CustomerDeviceSnapshot(
                key=f"{endpoint[0]}:{endpoint[1]}",
                endpoint=endpoint,
                display_identity="",
                connection_phase="DISCONNECTED",
                business_state=supplemental.business_state or "NOT_READY",
                recording_active=supplemental.recording_active,
                operation_busy=supplemental.operation_busy,
                identity_pending=False,
                identity_conflict=False,
            )

        customer_attached = self.attachment(endpoint) is not None
        phase = _enum_token(getattr(runtime, "presence_phase", None), "DORMANT")
        if not customer_attached:
            connection_phase = "DISCONNECTED"
        elif phase == "ONLINE":
            connection_phase = "ONLINE"
        elif phase == "STALE":
            connection_phase = "RECONNECTING"
        else:
            connection_phase = "WAITING"

        identity_conflict = bool(getattr(runtime, "identity_conflicted", False))
        scope = getattr(runtime, "identity_authorization_scope", None)
        display_identity = self._display_identity(runtime, scope)
        gate = getattr(runtime, "operation_gate", None)
        gate_state = _enum_token(getattr(gate, "state", None), "IDLE")
        legacy_operation_busy = bool(
            getattr(getattr(runtime, "core", None), "device_transaction_active", False)
        )
        gate_operation_busy = gate_state in {
            "MUTATING",
            "PRODUCTION_FROZEN",
            "TERMINATING",
        }
        operation_busy = bool(
            supplemental.operation_busy
            or legacy_operation_busy
            or gate_operation_busy
        )
        derived_business = {
            "MUTATING": "OPERATION_BUSY",
            "PRODUCTION_FROZEN": "PRODUCTION_FROZEN",
            "TERMINATING": "TERMINATING",
        }.get(gate_state, "")
        business_state = supplemental.business_state or derived_business
        return CustomerDeviceSnapshot(
            key=f"{endpoint[0]}:{endpoint[1]}",
            endpoint=endpoint,
            display_identity=display_identity,
            connection_phase=connection_phase,
            business_state=business_state,
            recording_active=supplemental.recording_active,
            operation_busy=operation_busy,
            identity_pending=bool(
                customer_attached and scope is None and not identity_conflict
            ),
            identity_conflict=identity_conflict,
        )

    def _supplemental_facts(
        self,
        endpoint: Endpoint,
        runtime: object | None,
    ) -> CustomerDeviceSupplementalFacts:
        provider = self._supplemental_facts_provider
        if provider is None:
            return CustomerDeviceSupplementalFacts()
        if callable(provider):
            value = provider(endpoint, runtime)
        else:
            facts = getattr(provider, "facts", None)
            if not callable(facts):
                raise TypeError("supplemental_facts_provider must be callable or expose facts()")
            value = facts(endpoint, runtime)
        return CustomerDeviceSupplementalFacts.from_value(value)

    @staticmethod
    def _display_identity(runtime: object, scope: object | None) -> str:
        if scope is None:
            return ""
        identity_facts = dict(getattr(scope, "identity_facts", ()))
        serial = str(identity_facts.get("sn", "") or "").strip()
        uid = str(identity_facts.get("uid", "") or "").strip()
        source_names = {name for name, _cursor in getattr(scope, "source_cursors", ())}
        hardware = ""
        if "debug_sn" in source_names:
            profile_store = getattr(getattr(runtime, "core", None), "profile_store", None)
            current_hw_type = getattr(profile_store, "current_hw_type", None)
            if callable(current_hw_type):
                hardware = str(current_hw_type() or "").strip().upper()
        if serial and hardware:
            return f"{hardware} {serial}"
        if serial:
            return serial
        return f"UID {uid}" if uid else ""

    def _wire_runtime(self, endpoint: Endpoint) -> None:
        self._unwire_runtime(endpoint)
        runtime = self._directory.runtime(endpoint)
        if runtime is None:
            return
        bindings: list[tuple[object, object]] = []

        def bind(signal: object) -> None:
            if signal is None or not hasattr(signal, "connect"):
                return
            slot = lambda *_args, endpoint=endpoint: self.refresh_endpoint(endpoint)
            signal.connect(slot)
            bindings.append((signal, slot))

        bind(getattr(runtime, "presence_changed", None))
        bind(getattr(runtime, "identity_authorization_changed", None))
        bind(getattr(runtime, "transport_changed", None))
        bind(getattr(runtime, "identity_conflict", None))
        gate = getattr(runtime, "operation_gate", None)
        bind(getattr(gate, "changed", None))
        core = getattr(runtime, "core", None)
        bind(getattr(core, "device_transaction_changed", None))
        profile_store = getattr(core, "profile_store", None)
        bind(getattr(profile_store, "profile_changed", None))
        self._runtime_bindings[endpoint] = bindings

    def _unwire_runtime(self, endpoint: Endpoint) -> None:
        for signal, slot in self._runtime_bindings.pop(endpoint, ()):  # type: ignore[arg-type]
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def refresh_endpoint(self, endpoint: object, *_args) -> None:
        if self._closed:
            return
        try:
            normalized = normalize_customer_endpoint(endpoint)
        except CustomerDeviceValidationError:
            return
        if normalized in self._configured_endpoints:
            self.device_changed.emit(normalized)

    def _on_runtime_removed(self, endpoint: object) -> None:
        if self._closed:
            return
        try:
            normalized = normalize_customer_endpoint(endpoint)
        except CustomerDeviceValidationError:
            return
        if normalized in self._configured_endpoints:
            self._configuration_error = (
                f"configured runtime disappeared: {normalized[0]}:{normalized[1]}"
            )
            self.device_changed.emit(normalized)

    def select_endpoint(self, endpoint: Optional[Endpoint]) -> CustomerDeviceMutationResult:
        self._assert_open_and_mutable()
        normalized = (
            normalize_customer_endpoint(endpoint) if endpoint is not None else None
        )
        if normalized is not None and normalized not in self._configured_endpoints:
            raise CustomerDeviceNotFoundError("active endpoint must belong to customer.devices")
        previous = self._active_endpoint
        if normalized == previous:
            return CustomerDeviceMutationResult("select", normalized, False)
        self._persist_configuration(self._configured_endpoints, normalized)
        self._active_endpoint = normalized
        self.active_endpoint_changed.emit(normalized)
        return CustomerDeviceMutationResult(
            "select",
            normalized,
            True,
            previous_endpoint=previous,
        )

    def select(self, endpoint: Optional[Endpoint]) -> CustomerDeviceMutationResult:
        return self.select_endpoint(endpoint)

    def add(self, endpoint: Endpoint) -> CustomerDeviceMutationResult:
        self._assert_open_and_mutable()
        normalized = normalize_customer_endpoint(endpoint)
        if normalized in self._configured_endpoints:
            selected = self.select_endpoint(normalized)
            return CustomerDeviceMutationResult(
                "add",
                normalized,
                selected.changed,
                duplicate_selected=True,
                previous_endpoint=selected.previous_endpoint,
            )
        if len(self._configured_endpoints) >= MAX_CUSTOMER_DEVICES:
            raise CustomerDeviceCapacityError("customer device capacity is 4")
        lease = self._acquire_configuration(normalized)
        new_endpoints = (*self._configured_endpoints, normalized)
        records = dict(self._records)
        records[normalized] = self._new_record(normalized)
        previous_active = self._active_endpoint
        try:
            self._persist_configuration(new_endpoints, normalized, records)
        except Exception:
            lease.release()
            raise
        self._configured_endpoints = tuple(new_endpoints)
        self._records = records
        self._active_endpoint = normalized
        self._leases[normalized] = lease
        self._wire_runtime(normalized)
        self.devices_changed.emit()
        if previous_active != normalized:
            self.active_endpoint_changed.emit(normalized)
        return CustomerDeviceMutationResult(
            "add",
            normalized,
            True,
            previous_endpoint=previous_active,
        )

    def add_endpoint(self, endpoint: Endpoint) -> CustomerDeviceMutationResult:
        return self.add(endpoint)

    def edit(
        self,
        source_endpoint: Endpoint,
        target_endpoint: Endpoint,
    ) -> CustomerDeviceMutationResult:
        self._assert_open_and_mutable()
        source = normalize_customer_endpoint(source_endpoint)
        target = normalize_customer_endpoint(target_endpoint)
        if source not in self._configured_endpoints:
            raise CustomerDeviceNotFoundError("edited endpoint is not configured")
        if source == target:
            return CustomerDeviceMutationResult("edit", target, False)
        if target in self._configured_endpoints:
            selected = self.select_endpoint(target)
            return CustomerDeviceMutationResult(
                "edit",
                target,
                selected.changed,
                duplicate_selected=True,
                previous_endpoint=source,
            )
        self._assert_reconfigurable(source)
        target_lease = self._acquire_configuration(target)
        source_index = self._configured_endpoints.index(source)
        replacement = list(self._configured_endpoints)
        replacement[source_index] = target
        records = dict(self._records)
        record = copy.deepcopy(records.pop(source))
        record["ip"], record["port"] = target
        records[target] = record
        previous_active = self._active_endpoint
        new_active = target if previous_active == source else previous_active
        try:
            self._persist_configuration(tuple(replacement), new_active, records)
        except Exception:
            target_lease.release()
            raise
        source_lease = self._leases.pop(source)
        self._unwire_runtime(source)
        self._configured_endpoints = tuple(replacement)
        self._records = records
        self._active_endpoint = new_active
        self._leases[target] = target_lease
        self._wire_runtime(target)
        if not source_lease.release():
            raise CustomerDeviceLeaseError("edited endpoint configuration lease did not release")
        self.devices_changed.emit()
        if new_active != previous_active:
            self.active_endpoint_changed.emit(new_active)
        return CustomerDeviceMutationResult(
            "edit",
            target,
            True,
            previous_endpoint=source,
        )

    def edit_endpoint(
        self,
        source_endpoint: Endpoint,
        target_endpoint: Endpoint,
    ) -> CustomerDeviceMutationResult:
        return self.edit(source_endpoint, target_endpoint)

    def delete(self, endpoint: Endpoint) -> CustomerDeviceMutationResult:
        self._assert_open_and_mutable()
        normalized = normalize_customer_endpoint(endpoint)
        if normalized not in self._configured_endpoints:
            raise CustomerDeviceNotFoundError("deleted endpoint is not configured")
        self._assert_reconfigurable(normalized)
        previous_active = self._active_endpoint
        old_index = self._configured_endpoints.index(normalized)
        remaining = tuple(
            item for item in self._configured_endpoints if item != normalized
        )
        records = dict(self._records)
        records.pop(normalized, None)
        if previous_active != normalized:
            new_active = previous_active
        elif old_index < len(remaining):
            new_active = remaining[old_index]
        elif remaining:
            new_active = remaining[-1]
        else:
            new_active = None
        self._persist_configuration(remaining, new_active, records)
        lease = self._leases.pop(normalized)
        self._unwire_runtime(normalized)
        self._configured_endpoints = remaining
        self._records = records
        self._active_endpoint = new_active
        if not lease.release():
            raise CustomerDeviceLeaseError("deleted endpoint configuration lease did not release")
        self.devices_changed.emit()
        if new_active != previous_active:
            self.active_endpoint_changed.emit(new_active)
        return CustomerDeviceMutationResult(
            "delete",
            new_active,
            True,
            previous_endpoint=normalized,
        )

    def delete_endpoint(self, endpoint: Endpoint) -> CustomerDeviceMutationResult:
        return self.delete(endpoint)

    def _assert_reconfigurable(self, endpoint: Endpoint) -> None:
        runtime = self._directory.runtime(endpoint)
        supplemental = self._supplemental_facts(endpoint, runtime)
        if runtime is None:
            raise CustomerDeviceLeaseError("configured endpoint runtime is unavailable")
        if self.attachment(endpoint) is not None:
            raise CustomerDeviceBusyError("disconnect the endpoint before Edit/Delete")
        gate = getattr(runtime, "operation_gate", None)
        gate_state = _enum_token(getattr(gate, "state", None), "IDLE")
        core_busy = bool(
            getattr(getattr(runtime, "core", None), "device_transaction_active", False)
        )
        if gate_state in {"MUTATING", "PRODUCTION_FROZEN", "TERMINATING"}:
            raise CustomerDeviceBusyError("finish the endpoint operation before Edit/Delete")
        if core_busy or supplemental.operation_busy:
            raise CustomerDeviceBusyError("finish the endpoint operation before Edit/Delete")
        if supplemental.recording_active:
            raise CustomerDeviceBusyError("stop endpoint recording before Edit/Delete")

    @staticmethod
    def _new_record(endpoint: Endpoint) -> dict[str, object]:
        return {
            "id": str(uuid.uuid4()),
            "ip": endpoint[0],
            "port": endpoint[1],
            "external_power": {
                "host": "",
                "voltage_set_v": 12.0,
                "current_set_a": 12.0,
            },
            "iperf": {
                "server": "60.205.157.141",
                "local_host": "",
                "protocol": "udp",
                "direction": "both",
                "ul_port": 5201,
                "dl_port": 5202,
                "ul_rate": "491K",
                "dl_rate": "200K",
                "continuous": True,
                "duration_hours": 24.0,
            },
        }

    @staticmethod
    def _settings_entry(endpoint: Endpoint) -> dict[str, object]:
        return {"ip": endpoint[0], "port": endpoint[1]}

    def _persist_configuration(
        self,
        endpoints: tuple[Endpoint, ...],
        active: Optional[Endpoint],
        records: Optional[Mapping[Endpoint, Mapping[str, object]]] = None,
    ) -> None:
        old_devices = copy.deepcopy(self._settings.get("customer.devices", []))
        old_active = copy.deepcopy(
            self._settings.get("customer.active_endpoint", None)
        )
        source_records = records if records is not None else self._records
        new_devices = [
            copy.deepcopy(dict(source_records.get(endpoint, self._new_record(endpoint))))
            for endpoint in endpoints
        ]
        new_active = self._settings_entry(active) if active is not None else None
        try:
            self._settings.set("customer.devices", new_devices)
            self._settings.set("customer.active_endpoint", new_active)
            self._settings.save()
        except Exception as exc:
            try:
                self._settings.set("customer.devices", old_devices)
                self._settings.set("customer.active_endpoint", old_active)
            except Exception as restore_exc:
                self._configuration_error = (
                    "customer settings failed and in-memory rollback also failed"
                )
                raise CustomerDevicePersistenceError(self._configuration_error) from restore_exc
            if self._settings_recovery_blocked():
                self._configuration_error = (
                    str(getattr(self._settings, "device_configuration_error", ""))
                    or "customer settings commit durability is unconfirmed"
                )
            raise CustomerDevicePersistenceError(
                f"customer device settings were not committed: {exc}"
            ) from exc

    def shutdown(self) -> None:
        self._assert_owner_thread()
        if self._closed:
            return
        failed_attachments: list[Endpoint] = []
        for endpoint in reversed(self._configured_endpoints):
            if endpoint not in self._attachments:
                continue
            if not self.detach(endpoint):
                failed_attachments.append(endpoint)
        if failed_attachments:
            targets = ", ".join(
                f"{ip}:{port}" for ip, port in failed_attachments
            )
            raise CustomerDeviceLeaseError(
                f"customer attachments did not release: {targets}"
            )
        self._closed = True
        failed: list[Endpoint] = []
        for endpoint in reversed(self._configured_endpoints):
            self._unwire_runtime(endpoint)
            lease = self._leases.get(endpoint)
            if lease is not None and not lease.release():
                failed.append(endpoint)
        self._leases.clear()
        if failed:
            targets = ", ".join(f"{ip}:{port}" for ip, port in failed)
            raise CustomerDeviceLeaseError(
                f"customer configuration leases did not release: {targets}"
            )


__all__ = [
    "CustomerDeviceBusyError",
    "CustomerDeviceCapacityError",
    "CustomerDeviceConfigurationBlocked",
    "CustomerDeviceDirectory",
    "CustomerDeviceDirectoryError",
    "CustomerDeviceLeaseError",
    "CustomerDeviceMutationResult",
    "CustomerDeviceNotFoundError",
    "CustomerDevicePersistenceError",
    "CustomerDeviceSnapshot",
    "CustomerDeviceSupplementalFacts",
    "CustomerDeviceValidationError",
    "Endpoint",
    "MAX_CUSTOMER_DEVICES",
    "SupplementalFactsProvider",
    "normalize_customer_endpoint",
]
