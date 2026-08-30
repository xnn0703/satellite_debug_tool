"""State store for stable customer Product Service protocol records."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import time
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.protocol import (
    ServiceCapabilities,
    ServiceComponentHealth,
    ServiceComponentValue,
    ServiceControlResponse,
    ServiceExternalInsDiagnostics,
    ServiceFastState,
    ServiceHardwareIdentity,
    ServiceIdentity,
    ServiceLinkDetail,
    ServiceMountStatus,
    ServiceNavigationSourceInfo,
    ServiceRfLockStatus,
    ServiceSlowState,
)

from .models import (
    ComponentHealth,
    ControlMode,
    DeviceIdentity,
    ExternalInsDiagnostics,
    ExternalInsState,
    NavigationState,
    NavigationSource,
    NavigationSourceInfo,
    OperationalSnapshot,
    ProductSnapshot,
    ProductSource,
    ProductValue,
    RfCapabilities,
    SatelliteMode,
    TrackingPhase,
    stamp_snapshot_received,
    stamp_snapshot_source,
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
_NAVIGATION_SOURCES = {
    0: NavigationSource.NONE,
    1: NavigationSource.ICM42688,
    2: NavigationSource.MG902,
    3: NavigationSource.BYNAV,
    4: NavigationSource.TRACE,
    5: NavigationSource.IAM20680,
    6: NavigationSource.MS6222,
    7: NavigationSource.DEBUG_ORACLE,
}
_EXTERNAL_INS_STATES = {
    0: ExternalInsState.NONE,
    1: ExternalInsState.STALE,
    2: ExternalInsState.UNALIGNED,
    3: ExternalInsState.ROLL_PITCH_READY,
    4: ExternalInsState.YAW_ALIGNED,
}

_SNR_HISTORY_SECONDS = 300.0
_SNR_MAX_RATE_HZ = 20
_SNR_HISTORY_CAPACITY = int(_SNR_HISTORY_SECONDS * _SNR_MAX_RATE_HZ) + _SNR_MAX_RATE_HZ


@dataclass(frozen=True)
class ProductTelemetryCursor:
    """One Product telemetry stream position inside an explicit data epoch.

    ``generation`` proves a record arrived after a captured cursor, while the
    device timestamp distinguishes a genuinely later measurement from a UDP
    duplicate or out-of-order record.  ``epoch`` advances only when the session
    owner clears connection-scoped Product state.
    """

    epoch: int
    generation: int
    timestamp_ms: Optional[int]


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
        self._hardware_identity: Optional[ServiceHardwareIdentity] = None
        self._navigation_source: Optional[ServiceNavigationSourceInfo] = None
        self._external_ins: Optional[ServiceExternalInsDiagnostics] = None
        self._fast: Optional[ServiceFastState] = None
        self._slow: Optional[ServiceSlowState] = None
        self._link_detail: Optional[ServiceLinkDetail] = None
        self._rf_lock_status: Optional[ServiceRfLockStatus] = None
        self._components: Optional[ServiceComponentHealth] = None
        self._capabilities: Optional[ServiceCapabilities] = None
        self._mount_status: Optional[ServiceMountStatus] = None
        self._received: dict[str, float] = {}
        self._telemetry_epoch = 0
        self._fast_telemetry_generation = 0
        self._slow_telemetry_generation = 0
        self._mount_status_generation = 0
        self._identity_generation = 0
        self._hardware_identity_generation = 0
        self._navigation_source_generation = 0
        self._snr_history: deque[tuple[int, float]] = deque(
            maxlen=_SNR_HISTORY_CAPACITY
        )
        self._snr_timestamp = U32UptimeUnwrapper()

    @property
    def service_available(self) -> bool:
        return (
            self._identity is not None
            or self._hardware_identity is not None
            or self._capabilities is not None
            or self._navigation_source is not None
            or self._external_ins is not None
            or self._fast is not None
            or self._slow is not None
            or self._link_detail is not None
            or self._rf_lock_status is not None
            or self._components is not None
            or self._mount_status is not None
        )

    @property
    def telemetry_ready(self) -> bool:
        """快速遥测已经到达，可作为订阅生效证据。"""
        return self._fast is not None

    @property
    def telemetry_epoch(self) -> int:
        """Return the connection-scoped Product telemetry epoch."""

        return self._telemetry_epoch

    @property
    def fast_telemetry_generation(self) -> int:
        """Monotonic generation of received FAST_STATE telemetry."""
        return self._fast_telemetry_generation

    @property
    def fast_telemetry_cursor(self) -> ProductTelemetryCursor:
        """Return the current FAST_STATE proof position."""

        return ProductTelemetryCursor(
            self._telemetry_epoch,
            self._fast_telemetry_generation,
            None if self._fast is None else int(self._fast.timestamp),
        )

    @property
    def slow_telemetry_generation(self) -> int:
        """Monotonic generation of received SLOW_STATE telemetry."""
        return self._slow_telemetry_generation

    @property
    def slow_telemetry_cursor(self) -> ProductTelemetryCursor:
        """Return the current SLOW_STATE proof position."""

        return ProductTelemetryCursor(
            self._telemetry_epoch,
            self._slow_telemetry_generation,
            None if self._slow is None else int(self._slow.timestamp),
        )

    @property
    def mount_status_generation(self) -> int:
        """Monotonic generation of received mount status records."""

        return self._mount_status_generation

    @property
    def mount_status_cursor(self) -> ProductTelemetryCursor:
        """Return the current mount-status proof position."""

        return ProductTelemetryCursor(
            self._telemetry_epoch,
            self._mount_status_generation,
            (
                None
                if self._mount_status is None
                else int(self._mount_status.timestamp)
            ),
        )

    @property
    def identity_generation(self) -> int:
        """Monotonic generation of product identity records."""

        return self._identity_generation

    @property
    def hardware_identity_generation(self) -> int:
        """Monotonic generation of hardware identity records."""

        return self._hardware_identity_generation

    @property
    def navigation_source_generation(self) -> int:
        """Monotonic generation of navigation-source configuration records."""

        return self._navigation_source_generation

    @property
    def service_protocol(self) -> Optional[int]:
        """Return the device-declared Product Service version when it is valid."""
        identity = self._identity
        if identity is None or not (identity.valid_mask & (1 << 4)):
            return None
        return int(identity.protocol_version)

    @property
    def product_identity(self) -> Optional[str]:
        """Return the device-declared product model when its valid bit is set."""

        identity = self._identity
        if identity is None or not (identity.valid_mask & (1 << 0)):
            return None
        model = identity.model.strip()
        return model or None

    @property
    def has_snr_stream(self) -> bool:
        return bool(self._snr_history)

    @property
    def capabilities_record(self) -> Optional[ServiceCapabilities]:
        return self._capabilities

    @property
    def mount_status_record(self) -> Optional[ServiceMountStatus]:
        """Return the current connection's authoritative mount status."""

        return self._mount_status

    @property
    def slow_state_record(self) -> Optional[ServiceSlowState]:
        return self._slow

    def clear(self) -> None:
        self._telemetry_epoch += 1
        self._identity = None
        self._hardware_identity = None
        self._navigation_source = None
        self._external_ins = None
        self._fast = None
        self._slow = None
        self._link_detail = None
        self._rf_lock_status = None
        self._components = None
        self._capabilities = None
        self._mount_status = None
        self._received.clear()
        self._snr_history.clear()
        self._snr_timestamp.reset()
        self.updated.emit()

    def feed(self, record, *, received_monotonic: Optional[float] = None) -> bool:
        received = (
            time.monotonic()
            if received_monotonic is None
            else float(received_monotonic)
        )
        key = ""
        if isinstance(record, ServiceIdentity):
            self._identity = record
            self._identity_generation += 1
            key = "identity"
        elif isinstance(record, ServiceHardwareIdentity):
            self._hardware_identity = record
            self._hardware_identity_generation += 1
            key = "hardware_identity"
        elif isinstance(record, ServiceNavigationSourceInfo):
            self._navigation_source = record
            self._navigation_source_generation += 1
            key = "navigation_source"
        elif isinstance(record, ServiceExternalInsDiagnostics):
            self._external_ins = record
            key = "external_ins"
        elif isinstance(record, ServiceFastState):
            self._fast = record
            key = "fast"
            self._fast_telemetry_generation += 1
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
            self._slow_telemetry_generation += 1
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
        elif isinstance(record, ServiceMountStatus):
            self._mount_status = record
            self._mount_status_generation += 1
            key = "mount_status"
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
        *,
        now_monotonic: Optional[float] = None,
    ) -> ProductSnapshot:
        base = ProductSnapshot()
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        identity = self._identity_snapshot(base.identity)
        operation = self._operation_snapshot(base.operation, now)
        converter, tx_array, rx_array = self._component_snapshot(base, now)
        capabilities = self._capability_snapshot(base.rf_capabilities)
        navigation_sources = self._navigation_source_snapshot(base.navigation_sources)
        external_ins = self._external_ins_snapshot(base.external_ins, now)
        source = "product_service" if self.service_available else base.source
        snapshot = ProductSnapshot(
            identity=identity,
            operation=operation,
            converter=converter,
            tx_array=tx_array,
            rx_array=rx_array,
            rf_capabilities=capabilities,
            navigation_sources=navigation_sources,
            external_ins=external_ins,
            source=source,
        )
        return stamp_snapshot_source(snapshot, ProductSource.PRODUCT_SERVICE)

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
        identity = fallback
        if record is not None:
            mask = record.valid_mask
            identity = replace(
                identity,
                model=self._static(mask, 0, record.model, record.timestamp),
                serial_number=self._static(mask, 1, record.serial_number, record.timestamp),
                main_firmware=self._static(mask, 2, record.main_firmware, record.timestamp),
                boot_firmware=self._static(mask, 3, record.boot_firmware, record.timestamp),
                protocol_version=self._static(
                    mask, 4, f"v{record.protocol_version}", record.timestamp
                ),
            )
            identity = stamp_snapshot_received(
                identity,
                self._received.get("identity"),
            )
        hardware = self._hardware_identity
        if hardware is not None:
            mask = hardware.valid_mask
            identity = replace(
                identity,
                device_uid=self._static(mask, 0, hardware.device_uid, hardware.timestamp),
                mac_address=self._static(mask, 1, hardware.mac_text, hardware.timestamp),
                mac_source=self._static(mask, 2, hardware.mac_source, hardware.timestamp),
            )
            identity = stamp_snapshot_received(
                identity,
                self._received.get("hardware_identity"),
            )
        return identity

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
            operation = stamp_snapshot_received(
                operation,
                self._received.get("fast"),
            )
        slow = self._slow
        if slow is not None:
            stale = self._is_stale("slow", now, 3.0)
            slow_tx = self._dynamic(
                slow.valid_mask, 7, slow.tx_enabled, slow.timestamp, stale
            )
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
                tx_enabled=(
                    slow_tx
                    if fast is None or not (fast.valid_mask & (1 << 5))
                    else operation.tx_enabled
                ),
            )
            operation = stamp_snapshot_received(
                operation,
                self._received.get("slow"),
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
            operation = stamp_snapshot_received(
                operation,
                self._received.get("link_detail"),
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
            operation = stamp_snapshot_received(
                operation,
                self._received.get("rf_lock_status"),
            )
        return operation

    def _component_snapshot(
        self, fallback: ProductSnapshot, now: float
    ) -> tuple[ComponentHealth, ComponentHealth, ComponentHealth]:
        record = self._components
        if record is None:
            return fallback.converter, fallback.tx_array, fallback.rx_array
        stale = self._is_stale("components", now, 3.0)
        received = self._received.get("components")
        return tuple(
            stamp_snapshot_received(
                self._one_component(value, record.timestamp, stale),
                received,
            )
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
        capabilities = RfCapabilities(
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
        return stamp_snapshot_received(
            capabilities,
            self._received.get("capabilities"),
        )

    def _navigation_source_snapshot(
        self, fallback: NavigationSourceInfo
    ) -> NavigationSourceInfo:
        record = self._navigation_source
        if record is None:
            return fallback
        mask = record.valid_mask
        flags = record.capability_flags
        sources = NavigationSourceInfo(
            gnss_source=self._static(
                mask,
                0,
                _NAVIGATION_SOURCES.get(record.gnss_source, NavigationSource.UNKNOWN),
                record.timestamp,
            ),
            imu_source=self._static(
                mask,
                1,
                _NAVIGATION_SOURCES.get(record.imu_source, NavigationSource.UNKNOWN),
                record.timestamp,
            ),
            attitude_source=self._static(
                mask,
                2,
                _NAVIGATION_SOURCES.get(record.attitude_source, NavigationSource.UNKNOWN),
                record.timestamp,
            ),
            external_ins_source=self._static(
                mask,
                3,
                _NAVIGATION_SOURCES.get(
                    record.external_ins_source, NavigationSource.UNKNOWN
                ),
                record.timestamp,
            ),
            external_role_mask=self._static(
                mask, 4, record.external_role_mask, record.timestamp
            ),
            external_ins_supported=self._static(
                mask, 5, bool(flags & (1 << 0)), record.timestamp
            ),
            external_ins_configured=self._static(
                mask, 5, bool(flags & (1 << 1)), record.timestamp
            ),
            external_data_seen=self._static(
                mask, 5, bool(flags & (1 << 2)), record.timestamp
            ),
            external_online=self._static(
                mask, 5, bool(flags & (1 << 3)), record.timestamp
            ),
            imu_mount_rotation=self._static(
                mask, 6, record.imu_mount_rotation, record.timestamp
            ),
        )
        return stamp_snapshot_received(
            sources,
            self._received.get("navigation_source"),
        )

    def _external_ins_snapshot(
        self, fallback: ExternalInsDiagnostics, now: float
    ) -> ExternalInsDiagnostics:
        record = self._external_ins
        if record is None:
            return fallback
        mask = record.valid_mask
        stale = self._is_stale("external_ins", now, 3.0)
        dynamic = self._dynamic
        timestamp = record.timestamp
        diagnostics = ExternalInsDiagnostics(
            source=dynamic(
                mask,
                0,
                _NAVIGATION_SOURCES.get(record.source, NavigationSource.UNKNOWN),
                timestamp,
                stale,
            ),
            role_mask=dynamic(mask, 0, record.role_mask, timestamp, stale),
            online=dynamic(mask, 1, record.online, timestamp, stale),
            state=dynamic(
                mask,
                2,
                _EXTERNAL_INS_STATES.get(record.state, ExternalInsState.UNKNOWN),
                timestamp,
                stale,
            ),
            aligned=dynamic(mask, 2, record.aligned, timestamp, stale),
            raw_ins_status=dynamic(
                mask, 2, record.raw_ins_status, timestamp, stale
            ),
            raw_position_type=dynamic(
                mask, 3, record.raw_position_type, timestamp, stale
            ),
            gnss_position_type=dynamic(
                mask, 3, record.gnss_position_type, timestamp, stale
            ),
            satellite_count=dynamic(mask, 4, record.num_svs, timestamp, stale),
            inspvax_count=dynamic(mask, 5, record.inspvax_count, timestamp, stale),
            rawimuxa_count=dynamic(mask, 5, record.rawimuxa_count, timestamp, stale),
            bestpvt_count=dynamic(mask, 5, record.bestpvt_count, timestamp, stale),
            inspvax_hz=dynamic(mask, 5, record.inspvax_hz, timestamp, stale),
            rawimuxa_hz=dynamic(mask, 5, record.rawimuxa_hz, timestamp, stale),
            bestpvt_hz=dynamic(mask, 5, record.bestpvt_hz, timestamp, stale),
            ascii_crc_errors=dynamic(
                mask, 6, record.ascii_crc_errors, timestamp, stale
            ),
            binary_crc_errors=dynamic(
                mask, 6, record.binary_crc_errors, timestamp, stale
            ),
            binary_format_errors=dynamic(
                mask, 6, record.binary_format_errors, timestamp, stale
            ),
            rx_overflow_bytes=dynamic(
                mask, 6, record.rx_overflow_bytes, timestamp, stale
            ),
            yaw_deg=dynamic(mask, 7, record.yaw_deg, timestamp, stale),
            pitch_deg=dynamic(mask, 7, record.pitch_deg, timestamp, stale),
            roll_deg=dynamic(mask, 7, record.roll_deg, timestamp, stale),
            yaw_std_deg=dynamic(mask, 8, record.yaw_std_deg, timestamp, stale),
            pitch_std_deg=dynamic(
                mask, 8, record.pitch_std_deg, timestamp, stale
            ),
            roll_std_deg=dynamic(mask, 8, record.roll_std_deg, timestamp, stale),
            latitude_std_m=dynamic(
                mask, 9, record.latitude_std_m, timestamp, stale
            ),
            longitude_std_m=dynamic(
                mask, 9, record.longitude_std_m, timestamp, stale
            ),
            height_std_m=dynamic(mask, 9, record.height_std_m, timestamp, stale),
            velocity_north_std_mps=dynamic(
                mask, 10, record.velocity_north_std_mps, timestamp, stale
            ),
            velocity_east_std_mps=dynamic(
                mask, 10, record.velocity_east_std_mps, timestamp, stale
            ),
            velocity_up_std_mps=dynamic(
                mask, 10, record.velocity_up_std_mps, timestamp, stale
            ),
            solution_age_s=dynamic(
                mask, 11, record.solution_age_s, timestamp, stale
            ),
            differential_age_s=dynamic(
                mask, 11, record.differential_age_s, timestamp, stale
            ),
        )
        return stamp_snapshot_received(
            diagnostics,
            self._received.get("external_ins"),
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
