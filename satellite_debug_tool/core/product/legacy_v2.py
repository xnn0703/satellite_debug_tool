"""Best-effort projection from dynamic Debug v2 data to customer semantics."""

from __future__ import annotations

import time
from typing import Optional

import numpy as np

from satellite_debug_tool.core.data import DataStore, StateStore
from satellite_debug_tool.core.profile import (
    CHANNEL_ROLE_ANTENNA_AZ,
    CHANNEL_ROLE_ANTENNA_EL,
    CHANNEL_ROLE_GPS_ALT,
    CHANNEL_ROLE_GPS_LAT,
    CHANNEL_ROLE_GPS_LON,
    CHANNEL_ROLE_PITCH,
    CHANNEL_ROLE_ROLL,
    CHANNEL_ROLE_SNR,
    CHANNEL_ROLE_YAW,
    STATE_ROLE_GPS_FIX,
    STATE_ROLE_INTERNAL_INS_STATE,
    STATE_ROLE_LOCK_FLAG,
    STATE_ROLE_TRACE_MODE,
    ProfileStore,
)
from satellite_debug_tool.core.protocol import StateDefEntry

from .models import (
    Availability,
    ControlMode,
    DeviceIdentity,
    NavigationState,
    OperationalSnapshot,
    ProductSnapshot,
    ProductSource,
    ProductValue,
    TrackingPhase,
    stamp_snapshot_source,
)
from .timestamps import unwrap_u32_series


_SERIAL_PLACEHOLDERS = {"", "-", "--", "unknown", "afd01-dev"}


class LegacyV2Projector:
    """Project only authoritative v2 fields; missing fields stay unsupported."""

    def __init__(
        self,
        profile_store: ProfileStore,
        data_store: DataStore,
        state_store: StateStore,
        *,
        stale_after_s: float = 3.0,
    ) -> None:
        self._profiles = profile_store
        self._data = data_store
        self._states = state_store
        self._stale_after_s = float(stale_after_s)

    def snapshot(self, *, now_monotonic: Optional[float] = None) -> ProductSnapshot:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        hw = self._profiles.current_hw_type()
        if hw is None:
            return stamp_snapshot_source(
                ProductSnapshot(source="legacy_v2"),
                ProductSource.LEGACY_V2,
            )

        profile = self._profiles.get_profile(hw)
        meta = None if profile is None else profile.meta
        identity = DeviceIdentity(
            model=ProductValue.valid(hw),
            serial_number=self._serial_value(None if meta is None else meta.device_sn),
            main_firmware=self._text_value(None if meta is None else meta.fw_ver),
            protocol_version=(
                ProductValue.unsupported()
                if meta is None
                else ProductValue.valid(f"v{meta.protocol_ver}")
            ),
        )

        trace = self._state_value(hw, STATE_ROLE_TRACE_MODE, now)
        control_mode, tracking_phase = self._trace_semantics(hw, trace)
        operation = OperationalSnapshot(
            control_mode=control_mode,
            tracking_phase=tracking_phase,
            locked=self._bool_state(hw, STATE_ROLE_LOCK_FLAG, now),
            navigation=self._navigation_state(hw, now),
            gnss_fix=self._enum_state(hw, STATE_ROLE_GPS_FIX, now),
            roll_deg=self._channel_value(hw, CHANNEL_ROLE_ROLL, now),
            pitch_deg=self._channel_value(hw, CHANNEL_ROLE_PITCH, now),
            yaw_deg=self._channel_value(hw, CHANNEL_ROLE_YAW, now),
            beam_az_deg=self._channel_value(hw, CHANNEL_ROLE_ANTENNA_AZ, now),
            beam_el_deg=self._channel_value(hw, CHANNEL_ROLE_ANTENNA_EL, now),
            snr_db=self._channel_value(hw, CHANNEL_ROLE_SNR, now),
            longitude_deg=self._channel_value(hw, CHANNEL_ROLE_GPS_LON, now),
            latitude_deg=self._channel_value(hw, CHANNEL_ROLE_GPS_LAT, now),
            altitude_m=self._channel_value(hw, CHANNEL_ROLE_GPS_ALT, now),
        )
        return stamp_snapshot_source(
            ProductSnapshot(identity=identity, operation=operation, source="legacy_v2"),
            ProductSource.LEGACY_V2,
        )

    def channel_history(
        self,
        role: str,
        *,
        window_s: float = 300.0,
        max_points: int = 30000,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return device-uptime seconds and values for one customer channel role."""
        hw = self._profiles.current_hw_type()
        if hw is None:
            return np.array([], dtype=np.float64), np.array([], dtype=np.float32)
        entry = self._profiles.find_channel_by_role(hw, role)
        if entry is None:
            return np.array([], dtype=np.float64), np.array([], dtype=np.float32)
        buf = self._data.get_channel_by_id(entry.channel_id)
        if buf is None:
            return np.array([], dtype=np.float64), np.array([], dtype=np.float32)
        times, values = buf.get_tail(max_points)
        if times.size == 0:
            return times, values
        unwrapped, accepted = unwrap_u32_series(times)
        if unwrapped.size == 0:
            return unwrapped, np.array([], dtype=np.float32)
        accepted_values = values[accepted]
        end = float(unwrapped[-1])
        cutoff = end - max(0.0, float(window_s)) * 1000.0
        mask = unwrapped >= cutoff
        return unwrapped[mask] / 1000.0, accepted_values[mask]

    @staticmethod
    def _text_value(text: Optional[str]) -> ProductValue[str]:
        value = "" if text is None else str(text).strip()
        return ProductValue.valid(value) if value else ProductValue.unsupported()

    @staticmethod
    def _serial_value(text: Optional[str]) -> ProductValue[str]:
        value = "" if text is None else str(text).strip()
        if value.lower() in _SERIAL_PLACEHOLDERS:
            return ProductValue.unsupported()
        return ProductValue.valid(value)

    def _channel_value(self, hw: str, role: str, now: float) -> ProductValue[float]:
        entry = self._profiles.find_channel_by_role(hw, role)
        if entry is None:
            return ProductValue.unsupported()
        buf = self._data.get_channel_by_id(entry.channel_id)
        if buf is None:
            return ProductValue.stale()
        latest = buf.get_latest()
        received = self._data.last_received_monotonic(entry.channel_id)
        if latest is None or received is None:
            return ProductValue.stale()
        timestamp, value = latest
        if now - received > self._stale_after_s:
            return ProductValue.stale(
                float(value),
                int(timestamp),
                received_monotonic_s=received,
            )
        return ProductValue.valid(
            float(value),
            int(timestamp),
            received_monotonic_s=received,
        )

    def _state_value(self, hw: str, role: str, now: float) -> ProductValue[int]:
        entry = self._profiles.find_state_by_role(hw, role)
        if entry is None:
            return ProductValue.unsupported()
        state = self._states.get(hw, entry.state_id)
        if state is None:
            return ProductValue.stale()
        if now - state.last_received_monotonic > self._stale_after_s:
            return ProductValue.stale(
                state.value,
                state.last_change_ms,
                received_monotonic_s=state.last_received_monotonic,
            )
        return ProductValue.valid(
            state.value,
            state.last_change_ms,
            received_monotonic_s=state.last_received_monotonic,
        )

    def _bool_state(self, hw: str, role: str, now: float) -> ProductValue[bool]:
        raw = self._state_value(hw, role, now)
        if raw.availability == Availability.UNSUPPORTED:
            return ProductValue.unsupported()
        value = None if raw.value is None else bool(raw.value)
        if raw.availability == Availability.STALE:
            return ProductValue.stale(
                value,
                raw.device_timestamp_ms,
                received_monotonic_s=raw.received_monotonic_s,
            )
        return ProductValue.valid(
            bool(value),
            raw.device_timestamp_ms,
            received_monotonic_s=raw.received_monotonic_s,
        )

    def _enum_state(self, hw: str, role: str, now: float) -> ProductValue[str]:
        raw = self._state_value(hw, role, now)
        if raw.availability == Availability.UNSUPPORTED:
            return ProductValue.unsupported()
        entry = self._profiles.find_state_by_role(hw, role)
        name = self._enum_name(entry, raw.value)
        if raw.availability == Availability.STALE:
            return ProductValue.stale(
                name,
                raw.device_timestamp_ms,
                received_monotonic_s=raw.received_monotonic_s,
            )
        return ProductValue.valid(
            name,
            raw.device_timestamp_ms,
            received_monotonic_s=raw.received_monotonic_s,
        )

    @staticmethod
    def _enum_name(entry: Optional[StateDefEntry], value: Optional[int]) -> str:
        if entry is not None and value is not None:
            for item in entry.enums:
                if item.value == value:
                    return item.name
        return "UNKNOWN" if value is None else str(value)

    def _trace_semantics(
        self, hw: str, raw: ProductValue[int]
    ) -> tuple[ProductValue[ControlMode], ProductValue[TrackingPhase]]:
        if raw.availability == Availability.UNSUPPORTED:
            return ProductValue.unsupported(), ProductValue.unsupported()
        entry = self._profiles.find_state_by_role(hw, STATE_ROLE_TRACE_MODE)
        name = self._enum_name(entry, raw.value).upper()
        mode = {
            "STANDBY": ControlMode.AUTO,
            "SCAN_GLOBAL": ControlMode.AUTO,
            "SCAN_WIDE": ControlMode.AUTO,
            "LOCK": ControlMode.AUTO,
            "MANUAL": ControlMode.MANUAL,
        }.get(name, ControlMode.UNKNOWN)
        phase = {
            "STANDBY": TrackingPhase.STANDBY,
            "SCAN_GLOBAL": TrackingPhase.ACQUIRING,
            "SCAN_WIDE": TrackingPhase.FINE_TRACKING,
            "LOCK": TrackingPhase.LOCKED,
            "MANUAL": TrackingPhase.STANDBY,
        }.get(name, TrackingPhase.UNKNOWN)
        if raw.availability == Availability.STALE:
            return (
                ProductValue.stale(
                    mode,
                    raw.device_timestamp_ms,
                    received_monotonic_s=raw.received_monotonic_s,
                ),
                ProductValue.stale(
                    phase,
                    raw.device_timestamp_ms,
                    received_monotonic_s=raw.received_monotonic_s,
                ),
            )
        return (
            ProductValue.valid(
                mode,
                raw.device_timestamp_ms,
                received_monotonic_s=raw.received_monotonic_s,
            ),
            ProductValue.valid(
                phase,
                raw.device_timestamp_ms,
                received_monotonic_s=raw.received_monotonic_s,
            ),
        )

    def _navigation_state(self, hw: str, now: float) -> ProductValue[NavigationState]:
        external = self._state_value(hw, "external_ins_status", now)
        external_entry = self._profiles.find_state_by_role(hw, "external_ins_status")
        if external.availability != Availability.UNSUPPORTED:
            name = self._enum_name(external_entry, external.value).upper()
            if name not in {"INACTIVE", "FREE", "UNKNOWN"}:
                value = {
                    "GOOD": NavigationState.READY,
                    "ALIGN_DONE": NavigationState.READY,
                    "HIGH_VAR": NavigationState.DEGRADED,
                    "ALIGNING": NavigationState.ALIGNING,
                    "DET_ORI": NavigationState.ALIGNING,
                    "WAIT_POS": NavigationState.ALIGNING,
                    "WAIT_AZ": NavigationState.ALIGNING,
                    "INIT_BIAS": NavigationState.INITIALIZING,
                    "MOTION_DET": NavigationState.INITIALIZING,
                }.get(name, NavigationState.UNKNOWN)
                return self._mapped_state(value, external)

        internal = self._state_value(hw, STATE_ROLE_INTERNAL_INS_STATE, now)
        if internal.availability == Availability.UNSUPPORTED:
            return ProductValue.unsupported()
        internal_entry = self._profiles.find_state_by_role(hw, STATE_ROLE_INTERNAL_INS_STATE)
        name = self._enum_name(internal_entry, internal.value).upper()
        value = {
            "NONE": NavigationState.UNAVAILABLE,
            "INITIALIZING": NavigationState.INITIALIZING,
            "ATTITUDE_READY": NavigationState.DEGRADED,
            "RP_READY": NavigationState.DEGRADED,
            "NAVIGATION_READY": NavigationState.READY,
        }.get(name, NavigationState.UNKNOWN)
        return self._mapped_state(value, internal)

    @staticmethod
    def _mapped_state(value, raw: ProductValue[int]):
        if raw.availability == Availability.STALE:
            return ProductValue.stale(
                value,
                raw.device_timestamp_ms,
                received_monotonic_s=raw.received_monotonic_s,
            )
        return ProductValue.valid(
            value,
            raw.device_timestamp_ms,
            received_monotonic_s=raw.received_monotonic_s,
        )
