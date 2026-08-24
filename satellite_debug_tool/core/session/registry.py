"""Endpoint registry that enforces one authoritative device-session core."""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from PySide6.QtCore import QObject

from .device_session import DeviceEndpoint, DeviceSessionCore


class SessionAuthorityError(RuntimeError):
    """Raised when two cores attempt to own one endpoint."""


class SessionRegistry(QObject):
    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._sessions: dict[DeviceEndpoint, DeviceSessionCore] = {}
        self._owners: dict[DeviceSessionCore, set[str]] = defaultdict(set)

    def session(self, endpoint: DeviceEndpoint) -> DeviceSessionCore | None:
        return self._sessions.get(DeviceSessionCore.normalize_endpoint(endpoint))

    def get_or_create(
        self,
        endpoint: DeviceEndpoint,
        *,
        owner: str,
    ) -> DeviceSessionCore:
        normalized = DeviceSessionCore.normalize_endpoint(endpoint)
        core = self._sessions.get(normalized)
        if core is None:
            core = DeviceSessionCore(endpoint=normalized, parent=self)
            self._sessions[normalized] = core
        self._owners[core].add(str(owner))
        return core

    def register(
        self,
        endpoint: DeviceEndpoint,
        core: DeviceSessionCore,
        *,
        owner: str,
    ) -> DeviceSessionCore:
        normalized = DeviceSessionCore.normalize_endpoint(endpoint)
        existing = self._sessions.get(normalized)
        if existing is not None and existing is not core:
            raise SessionAuthorityError(
                f"endpoint {normalized[0]}:{normalized[1]} already has a session authority"
            )
        previous = core.endpoint
        if previous is not None and previous != normalized:
            if self._sessions.get(previous) is core:
                self._sessions.pop(previous)
        core.bind_endpoint(normalized)
        self._sessions[normalized] = core
        self._owners[core].add(str(owner))
        return core

    def rebind(
        self,
        core: DeviceSessionCore,
        endpoint: DeviceEndpoint,
        *,
        owner: str,
    ) -> None:
        self.register(endpoint, core, owner=owner)

    def release(self, core: DeviceSessionCore, *, owner: str) -> None:
        owners = self._owners.get(core)
        if owners is None:
            return
        owners.discard(str(owner))
        if owners:
            return
        self._owners.pop(core, None)
        endpoint = core.endpoint
        if endpoint is not None and self._sessions.get(endpoint) is core:
            self._sessions.pop(endpoint)
        core.deleteLater()

    def owners(self, core: DeviceSessionCore) -> frozenset[str]:
        return frozenset(self._owners.get(core, ()))

    def endpoints(self) -> tuple[DeviceEndpoint, ...]:
        return tuple(sorted(self._sessions))
