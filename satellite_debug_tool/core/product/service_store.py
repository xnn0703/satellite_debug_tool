"""State store for stable AFD01 product-service protocol records."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
import time
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.protocol import (
    ServiceCapabilities,
    ServiceComponentHealth,
    ServiceComponentValue,
    ServiceControlResponse,
    ServiceFastState,
    ServiceIdentity,
    ServiceLinkDetail,
    ServiceRfLockStatus,
    ServiceSlowState,
)

from .models import (
    ComponentHealth,
    ControlMode,
    DeviceIdentity,
    NavigationState,
    OperationalSnapshot,
    ProductSnapshot,
    ProductValue,
    RfCapabilities,
    SatelliteMode,
    TrackingPhase,
)
from .timestamps import U32UptimeUnwrapper


_TRACKING_PHASES = {
    0: TrackingPhase.STANDBY,
    1: TrackingPhase.ACQUIRING,
    2: TrackingPhase.FINE_TRACKING,
    3: TrackingPhase.LOCKED,
    4: TrackingPhase.REACQUIRING,
    5: TrackingPhase.FAULT,
}
_NAVIGATION_STATES = {
    0: NavigationState.UNAVAILABLE,
    1: NavigationState.INITIALIZING,
    2: NavigationState.ALIGNING,
    3: NavigationState.READY,
    4: NavigationState.DEGRADED,
    5: NavigationState.FAULT,
}
_GNSS_FIX_NAMES = {
    0: "NO_FIX",
    1: "2D",
    2: "3D",
    3: "RTK_FIXED",
    4: "DGNSS",
    5: "RTK_FLOAT",
    6: "STALE",
}
_CONTROL_MODES = {
    0: ControlMode.AUTO,
    1: ControlMode.MANUAL,
}
_SATELLITE_MODES = {
    0: SatelliteMode.UNKNOWN,
    1: SatelliteMode.GEO,
    2: SatelliteMode.LEO_TLE,
}

_SNR_HISTORY_SECONDS = 300.0
_SNR_MAX_RATE_HZ = 20
_SNR_HISTORY_CAPACITY = int(_SNR_HISTORY_SECONDS * _SNR_MAX_RATE_HZ) + _SNR_MAX_RATE_HZ


class ProductServiceStore(QObject):
    """Keep the newest product records and expose one canonical snapshot."""

    updated = Signal()
    control_response = Signal(object)

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        enforce_stale: bool = True,
    ) -> None:
        super().__init__(parent)
        self._enforce_stale = bool(enforce_stale)
        self._identity: Optional[ServiceIdentity] = None
        self._fast: Optional[ServiceFastState] = None
        self._slow: Optional[ServiceSlowState] = None
        self._link_detail: Optional[ServiceLinkDetail] = None
        self._rf_lock_status: Optional[ServiceRfLockStatus] = None
        self._components: Optional[ServiceComponentHealth] = None
        self._capabilities: Optional[ServiceCapabilities] = None
        self._received: dict[str, float] = {}
        self._snr_history: deque[tuple[int, float]] = deque(
            maxlen=_SNR_HISTORY_CAPACITY
        )
        self._snr_timestamp = U32UptimeUnwrapper()

    @property
    def service_available(self) -> bool:
        return self._identity is not None or self._capabilities is not None

    @property
    def capabilities_record(self) -> Optional[ServiceCapabilities]:
        return self._capabilities

    @property
    def slow_state_record(self) -> Optional[ServiceSlowState]:
        return self._slow

    def clear(self) -> None:
        self._identity = None
        self._fast = None
        self._slow = None
        self._link_detail = None
        self._rf_lock_status = None
        self._components = None
        self._capabilities = None
        self._received.clear()
        self._snr_history.clear()
        self._snr_timestamp.reset()
        self.updated.emit()

    def feed(self, record, *, received_wallclock: Optional[float] = None) -> bool:
        received = time.time() if received_wallclock is None else float(received_wallclock)
        key = ""
        if isinstance(record, ServiceIdentity):
            self._identity = record
            key = "identity"
        elif isinstance(record, ServiceFastState):
            self._fast = record
            key = "fast"
            if record.valid_mask & (1 << 11):
                timestamp = self._snr_timestamp.add(record.timestamp)
                if timestamp is not None:
                    self._snr_history.append((timestamp, float(record.snr_db)))
                    cutoff = timestamp - int(_SNR_HISTORY_SECONDS * 1000.0)
                    while self._snr_history and self._snr_history[0][0] < cutoff:
                        self._snr_history.popleft()
        elif isinstance(record, ServiceSlowState):
            self._slow = record
            key = "slow"
        elif isinstance(record, ServiceLinkDetail):
            self._link_detail = record
            key = "link_detail"
        elif isinstance(record, ServiceRfLockStatus):
            self._rf_lock_status = record
            key = "rf_lock_status"
        elif isinstance(record, ServiceComponentHealth):
            self._components = record
            key = "components"
        elif isinstance(record, ServiceCapabilities):
            self._capabilities = record
            key = "capabilities"
        elif isinstance(record, ServiceControlResponse):
            self.control_response.emit(record)
            return True
        else:
            return False
        self._received[key] = received
        self.updated.emit()
        return True

    def snapshot(
        self,
        fallback: Optional[ProductSnapshot] = None,
        *,
        now_wallclock: Optional[float] = None,
    ) -> ProductSnapshot:
        base = fallback or ProductSnapshot()
        now = time.time() if now_wallclock is None else float(now_wallclock)
        identity = self._identity_snapshot(base.identity)
        operation = self._operation_snapshot(base.operation, now)
        converter, tx_array, rx_array = self._component_snapshot(base, now)
        capabilities = self._capability_snapshot(base.rf_capabilities)
        source = "product_service" if self.service_available else base.source
        return ProductSnapshot(
            identity=identity,
            operation=operation,
            converter=converter,
            tx_array=tx_array,
            rx_array=rx_array,
            rf_capabilities=capabilities,
            source=source,
        )

    def snr_history(self, *, window_s: float = 300.0) -> tuple[np.ndarray, np.ndarray]:
        if not self._snr_history:
            return np.array([], dtype=np.float64), np.array([], dtype=np.float32)
        samples = list(self._snr_history)
        end = samples[-1][0]
        cutoff = end - int(max(0.0, window_s) * 1000.0)
        selected = [(timestamp, value) for timestamp, value in samples if timestamp >= cutoff]
        times = np.asarray([timestamp / 1000.0 for timestamp, _ in selected])
        values = np.asarray([value for _, value in selected], dtype=np.float32)
        return times, values

    def _identity_snapshot(self, fallback: DeviceIdentity) -> DeviceIdentity:
        record = self._identity
        if record is None:
            return fallback
        mask = record.valid_mask
        return DeviceIdentity(
            model=self._static(mask, 0, record.model, record.timestamp),
            serial_number=self._static(mask, 1, record.serial_number, record.timestamp),
            main_firmware=self._static(mask, 2, record.main_firmware, record.timestamp),
            boot_firmware=self._static(mask, 3, record.boot_firmware, record.timestamp),
            protocol_version=self._static(
                mask, 4, f"v{record.protocol_version}", record.timestamp
            ),
        )

    def _operation_snapshot(
        self, fallback: OperationalSnapshot, now: float
    ) -> OperationalSnapshot:
        operation = fallback
        fast = self._fast
        if fast is not None:
            stale = self._is_stale("fast", now, 1.0)
            operation = replace(
                operation,
                control_mode=self._dynamic(
                    fast.valid_mask,
                    0,
                    _CONTROL_MODES.get(fast.control_mode, ControlMode.UNKNOWN),
                    fast.timestamp,
                    stale,
                ),
                tracking_phase=self._dynamic(
                    fast.valid_mask,
                    1,
                    _TRACKING_PHASES.get(fast.tracking_phase, TrackingPhase.UNKNOWN),
                    fast.timestamp,
                    stale,
                ),
                locked=self._dynamic(fast.valid_mask, 2, fast.locked, fast.timestamp, stale),
                navigation=self._dynamic(
                    fast.valid_mask,
                    3,
                    _NAVIGATION_STATES.get(fast.navigation_state, NavigationState.UNKNOWN),
                    fast.timestamp,
                    stale,
                ),
                gnss_fix=self._dynamic(
                    fast.valid_mask,
                    4,
                    _GNSS_FIX_NAMES.get(fast.gnss_fix, "UNKNOWN"),
                    fast.timestamp,
                    stale,
                ),
                tx_enabled=self._dynamic(fast.valid_mask, 5, fast.tx_enabled, fast.timestamp, stale),
                roll_deg=self._dynamic(fast.valid_mask, 6, fast.roll_deg, fast.timestamp, stale),
                pitch_deg=self._dynamic(fast.valid_mask, 7, fast.pitch_deg, fast.timestamp, stale),
                yaw_deg=self._dynamic(fast.valid_mask, 8, fast.yaw_deg, fast.timestamp, stale),
                beam_az_deg=self._dynamic(fast.valid_mask, 9, fast.beam_az_deg, fast.timestamp, stale),
                beam_el_deg=self._dynamic(fast.valid_mask, 10, fast.beam_el_deg, fast.timestamp, stale),
                snr_db=self._dynamic(fast.valid_mask, 11, fast.snr_db, fast.timestamp, stale),
            )
        slow = self._slow
        if slow is not None:
            stale = self._is_stale("slow", now, 3.0)
            operation = replace(
                operation,
                latitude_deg=self._dynamic(slow.valid_mask, 0, slow.latitude_deg, slow.timestamp, stale),
                longitude_deg=self._dynamic(slow.valid_mask, 1, slow.longitude_deg, slow.timestamp, stale),
                altitude_m=self._dynamic(slow.valid_mask, 2, slow.altitude_m, slow.timestamp, stale),
                rx_frequency_mhz=self._dynamic(
                    slow.valid_mask, 3, slow.rx_frequency_mhz, slow.timestamp, stale
                ),
                tx_frequency_mhz=self._dynamic(
                    slow.valid_mask, 4, slow.tx_frequency_mhz, slow.timestamp, stale
                ),
                rx_polarization=self._dynamic(
                    slow.valid_mask, 5, slow.rx_polarization, slow.timestamp, stale
                ),
                tx_polarization=self._dynamic(
                    slow.valid_mask, 6, slow.tx_polarization, slow.timestamp, stale
                ),
                tx_enabled=self._dynamic(
                    slow.valid_mask, 7, slow.tx_enabled, slow.timestamp, stale
                ),
            )
        link = self._link_detail
        if link is not None:
            stale = self._is_stale("link_detail", now, 3.0)
            operation = replace(
                operation,
                modem_online=self._dynamic(
                    link.valid_mask, 0, link.modem_online, link.timestamp, stale
                ),
                rx_lo_mhz=self._dynamic(
                    link.valid_mask, 1, link.rx_lo_mhz, link.timestamp, stale
                ),
                tx_lo_mhz=self._dynamic(
                    link.valid_mask, 2, link.tx_lo_mhz, link.timestamp, stale
                ),
                satellite_mode=self._dynamic(
                    link.valid_mask,
                    3,
                    _SATELLITE_MODES.get(link.satellite_mode, SatelliteMode.UNKNOWN),
                    link.timestamp,
                    stale,
                ),
                satellite_longitude_deg=self._dynamic(
                    link.valid_mask,
                    4,
                    link.satellite_longitude_deg,
                    link.timestamp,
                    stale,
                ),
                satellite_id=self._dynamic(
                    link.valid_mask, 5, link.satellite_id, link.timestamp, stale
                ),
                satellite_name=self._dynamic(
                    link.valid_mask, 6, link.satellite_name, link.timestamp, stale
                ),
            )
        rf_lock_status = self._rf_lock_status
        if rf_lock_status is not None:
            stale = self._is_stale("rf_lock_status", now, 1.0)
            operation = replace(
                operation,
                clock_pll_locked=self._dynamic(
                    rf_lock_status.valid_mask,
                    0,
                    bool(rf_lock_status.lock_mask & (1 << 0)),
                    rf_lock_status.timestamp,
                    stale,
                ),
                tx_pll_locked=self._dynamic(
                    rf_lock_status.valid_mask,
                    1,
                    bool(rf_lock_status.lock_mask & (1 << 1)),
                    rf_lock_status.timestamp,
                    stale,
                ),
                rx_pll_locked=self._dynamic(
                    rf_lock_status.valid_mask,
                    2,
                    bool(rf_lock_status.lock_mask & (1 << 2)),
                    rf_lock_status.timestamp,
                    stale,
                ),
            )
        return operation

    def _component_snapshot(
        self, fallback: ProductSnapshot, now: float
    ) -> tuple[ComponentHealth, ComponentHealth, ComponentHealth]:
        record = self._components
        if record is None:
            return fallback.converter, fallback.tx_array, fallback.rx_array
        stale = self._is_stale("components", now, 3.0)
        return tuple(
            self._one_component(value, record.timestamp, stale)
            for value in (record.converter, record.tx_array, record.rx_array)
        )

    def _one_component(
        self, value: ServiceComponentValue, timestamp: int, stale: bool
    ) -> ComponentHealth:
        return ComponentHealth(
            online=self._dynamic(value.valid_mask, 0, value.online, timestamp, stale),
            temperature_c=self._dynamic(
                value.valid_mask, 1, value.temperature_c, timestamp, stale
            ),
            voltage_v=self._dynamic(value.valid_mask, 2, value.voltage_v, timestamp, stale),
            version=self._dynamic(
                value.valid_mask, 3, f"0x{value.version:02X}", timestamp, stale
            ),
            fault=ProductValue.unsupported(),
        )

    def _capability_snapshot(self, fallback: RfCapabilities) -> RfCapabilities:
        record = self._capabilities
        if record is None:
            return fallback
        mask = record.valid_mask
        return RfCapabilities(
            rx_frequency_min_mhz=self._static(mask, 0, record.rx_frequency_min_mhz, record.timestamp),
            rx_frequency_max_mhz=self._static(mask, 1, record.rx_frequency_max_mhz, record.timestamp),
            tx_frequency_min_mhz=self._static(mask, 2, record.tx_frequency_min_mhz, record.timestamp),
            tx_frequency_max_mhz=self._static(mask, 3, record.tx_frequency_max_mhz, record.timestamp),
            polarization_mask=self._static(mask, 4, record.polarization_mask, record.timestamp),
            independent_polarization=self._static(
                mask, 5, bool(record.feature_flags & 0x01), record.timestamp
            ),
            tx_control=self._static(
                mask, 6, bool(record.feature_flags & 0x02), record.timestamp
            ),
            support_full_capture=self._static(
                mask, 7, bool(record.capture_profile_mask & 0x02), record.timestamp
            ),
        )

    def _is_stale(self, key: str, now: float, threshold: float) -> bool:
        if not self._enforce_stale:
            return False
        received = self._received.get(key)
        return received is None or now - received > threshold

    @staticmethod
    def _static(mask: int, bit: int, value, timestamp: int):
        if not (mask & (1 << bit)):
            return ProductValue.unsupported()
        return ProductValue.valid(value, timestamp)

    @staticmethod
    def _dynamic(mask: int, bit: int, value, timestamp: int, stale: bool):
        if not (mask & (1 << bit)):
            return ProductValue.unsupported()
        if stale:
            return ProductValue.stale(value, timestamp)
        return ProductValue.valid(value, timestamp)
