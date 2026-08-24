"""Whole-snapshot source selection for customer product semantics."""

from __future__ import annotations

from enum import Enum
import time
from typing import Optional

from .legacy_v2 import LegacyV2Projector
from .models import (
    ProductSnapshot,
    pending_product_snapshot,
)
from .service_store import ProductServiceStore


class ProductSourceState(str, Enum):
    PENDING = "pending"
    PRODUCT_SERVICE = "product_service"
    LEGACY_V2 = "legacy_v2"


class ProductSnapshotResolver:
    """Choose exactly one source for every customer snapshot."""

    def __init__(
        self,
        service_store: ProductServiceStore,
        legacy_projector: LegacyV2Projector,
        *,
        discovery_timeout_s: float = 5.0,
    ) -> None:
        self._service = service_store
        self._legacy = legacy_projector
        self._discovery_timeout_s = max(0.0, float(discovery_timeout_s))
        self._state = ProductSourceState.PENDING
        self._discovery_started = time.monotonic()

    @property
    def state(self) -> ProductSourceState:
        return self._state

    def reset(self, *, now_monotonic: Optional[float] = None) -> None:
        self._state = ProductSourceState.PENDING
        self._discovery_started = (
            time.monotonic() if now_monotonic is None else float(now_monotonic)
        )

    def snapshot(
        self,
        *,
        now_monotonic: Optional[float] = None,
    ) -> ProductSnapshot:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        if self._service.service_available or self._service.telemetry_ready:
            self._state = ProductSourceState.PRODUCT_SERVICE
        elif (
            self._state is ProductSourceState.PENDING
            and now - self._discovery_started >= self._discovery_timeout_s
        ):
            self._state = ProductSourceState.LEGACY_V2

        if self._state is ProductSourceState.PRODUCT_SERVICE:
            return self._service.snapshot(now_monotonic=now)
        if self._state is ProductSourceState.LEGACY_V2:
            return self._legacy.snapshot(now_monotonic=now)
        return pending_product_snapshot()
