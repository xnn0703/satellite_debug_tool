"""Profile semantic roles and capability helpers.

This module is intentionally protocol-light: it stores semantic strings used by
UI code and JSON profile caches. Wire-level numeric subcommands are translated
by ProfileStore when protocol reports arrive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional


# Channel roles
CHANNEL_ROLE_GPS_LAT = "gps_lat"
CHANNEL_ROLE_GPS_LON = "gps_lon"
CHANNEL_ROLE_GPS_ALT = "gps_alt"
CHANNEL_ROLE_GPS_NUM_SV = "gps_num_sv"
CHANNEL_ROLE_GPS_SPEED = "gps_speed"
CHANNEL_ROLE_GPS_COG = "gps_cog"
CHANNEL_ROLE_GPS_VEL_N = "gps_vel_n"
CHANNEL_ROLE_GPS_VEL_E = "gps_vel_e"
CHANNEL_ROLE_GPS_VEL_D = "gps_vel_d"
CHANNEL_ROLE_GPS_COG_STD = "gps_cog_std"
CHANNEL_ROLE_ROLL = "roll"
CHANNEL_ROLE_PITCH = "pitch"
CHANNEL_ROLE_YAW = "yaw"
CHANNEL_ROLE_INTERNAL_INS_YAW = "internal_ins_yaw"
CHANNEL_ROLE_ANTENNA_AZ = "antenna_az"
CHANNEL_ROLE_ANTENNA_EL = "antenna_el"
CHANNEL_ROLE_TARGET_AZ = "target_az"
CHANNEL_ROLE_TARGET_EL = "target_el"
CHANNEL_ROLE_SNR = "snr"
CHANNEL_ROLE_POINTING_ERROR = "pointing_error"

# State roles
STATE_ROLE_TRACE_MODE = "trace_mode"
STATE_ROLE_LOCK_FLAG = "lock_flag"
STATE_ROLE_GPS_FIX = "gps_fix"
STATE_ROLE_INS_READY = "ins_ready"
STATE_ROLE_INS_STATUS = "ins_status"
STATE_ROLE_INTERNAL_INS_STATE = "internal_ins_state"
STATE_ROLE_INTERNAL_INS_YAW_REFERENCE = "internal_ins_yaw_reference"
STATE_ROLE_PLL_LOCKED = "pll_locked"
STATE_ROLE_MODEM_CONNECTED = "modem_connected"

# Control binding
CONTROL_SUBCMD_SET_TRACE_MODE = "SET_TRACE_MODE"
CONTROL_VALUE_FROM_ENUM_VALUE = "enum_value"


@dataclass
class ControlBinding:
    subcmd: str
    value_from: str = CONTROL_VALUE_FROM_ENUM_VALUE


@dataclass
class ChannelSemantic:
    roles: List[str] = field(default_factory=list)


@dataclass
class StateSemantic:
    role: Optional[str] = None
    control: Optional[ControlBinding] = None


@dataclass
class ProfileSemantics:
    channels: Dict[int, ChannelSemantic] = field(default_factory=dict)
    states: Dict[int, StateSemantic] = field(default_factory=dict)
    capabilities: Dict[str, bool] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.channels and not self.states and not self.capabilities


def _norm(text: str) -> str:
    return str(text).strip().lower()


def _unique(items: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        role = _norm(item)
        if role and role not in seen:
            seen.add(role)
            out.append(role)
    return out


_CHANNEL_EXACT_ROLE_MAP = {
    "gps_lat": CHANNEL_ROLE_GPS_LAT,
    "latitude": CHANNEL_ROLE_GPS_LAT,
    "lat": CHANNEL_ROLE_GPS_LAT,
    "gps_lon": CHANNEL_ROLE_GPS_LON,
    "gps_lng": CHANNEL_ROLE_GPS_LON,
    "longitude": CHANNEL_ROLE_GPS_LON,
    "lon": CHANNEL_ROLE_GPS_LON,
    "lng": CHANNEL_ROLE_GPS_LON,
    "gps_alt": CHANNEL_ROLE_GPS_ALT,
    "altitude": CHANNEL_ROLE_GPS_ALT,
    "gps_num_sv": CHANNEL_ROLE_GPS_NUM_SV,
    "gps_num_svs": CHANNEL_ROLE_GPS_NUM_SV,
    "num_sv": CHANNEL_ROLE_GPS_NUM_SV,
    "num_svs": CHANNEL_ROLE_GPS_NUM_SV,
    "gps_speed": CHANNEL_ROLE_GPS_SPEED,
    "gps_spd": CHANNEL_ROLE_GPS_SPEED,
    "gvel": CHANNEL_ROLE_GPS_SPEED,
    "gps_cog": CHANNEL_ROLE_GPS_COG,
    "gcog": CHANNEL_ROLE_GPS_COG,
    "cog": CHANNEL_ROLE_GPS_COG,
    "course_over_ground": CHANNEL_ROLE_GPS_COG,
    "gps_vel_n": CHANNEL_ROLE_GPS_VEL_N,
    "gps_vn": CHANNEL_ROLE_GPS_VEL_N,
    "vel_n": CHANNEL_ROLE_GPS_VEL_N,
    "gps_vel_e": CHANNEL_ROLE_GPS_VEL_E,
    "gps_ve": CHANNEL_ROLE_GPS_VEL_E,
    "vel_e": CHANNEL_ROLE_GPS_VEL_E,
    "gps_vel_d": CHANNEL_ROLE_GPS_VEL_D,
    "gps_vd": CHANNEL_ROLE_GPS_VEL_D,
    "vel_d": CHANNEL_ROLE_GPS_VEL_D,
    "gps_cog_std": CHANNEL_ROLE_GPS_COG_STD,
    "gps_cog_acc": CHANNEL_ROLE_GPS_COG_STD,
    "gps_cacc": CHANNEL_ROLE_GPS_COG_STD,
    "cacc": CHANNEL_ROLE_GPS_COG_STD,
    "roll": CHANNEL_ROLE_ROLL,
    "pitch": CHANNEL_ROLE_PITCH,
    "yaw": CHANNEL_ROLE_YAW,
    "heading": CHANNEL_ROLE_YAW,
    "internal_ins_yaw": CHANNEL_ROLE_INTERNAL_INS_YAW,
    "ant_az": CHANNEL_ROLE_ANTENNA_AZ,
    "antenna_az": CHANNEL_ROLE_ANTENNA_AZ,
    "ant_el": CHANNEL_ROLE_ANTENNA_EL,
    "antenna_el": CHANNEL_ROLE_ANTENNA_EL,
    "tgt_az": CHANNEL_ROLE_TARGET_AZ,
    "target_az": CHANNEL_ROLE_TARGET_AZ,
    "tgt_el": CHANNEL_ROLE_TARGET_EL,
    "target_el": CHANNEL_ROLE_TARGET_EL,
    "snr": CHANNEL_ROLE_SNR,
    "err_az": CHANNEL_ROLE_POINTING_ERROR,
    "err_el": CHANNEL_ROLE_POINTING_ERROR,
}


def infer_channel_roles(name: str) -> List[str]:
    """Infer semantic channel roles from legacy naming conventions."""
    n = _norm(name)
    roles: List[str] = []
    exact = _CHANNEL_EXACT_ROLE_MAP.get(n)
    if exact is not None:
        roles.append(exact)

    # Conservative suffix fallback for logs or vendor-specific prefixes.
    is_scoped_ins_attitude = n.startswith(("internal_ins_", "external_ins_"))
    if not is_scoped_ins_attitude and n.endswith("_roll") and CHANNEL_ROLE_ROLL not in roles:
        roles.append(CHANNEL_ROLE_ROLL)
    if not is_scoped_ins_attitude and n.endswith("_pitch") and CHANNEL_ROLE_PITCH not in roles:
        roles.append(CHANNEL_ROLE_PITCH)
    if not is_scoped_ins_attitude and n.endswith("_yaw") and CHANNEL_ROLE_YAW not in roles:
        roles.append(CHANNEL_ROLE_YAW)
    return _unique(roles)


_STATE_EXACT_ROLE_MAP = {
    "trace_mode": STATE_ROLE_TRACE_MODE,
    "lock_flag": STATE_ROLE_LOCK_FLAG,
    "gps_fix": STATE_ROLE_GPS_FIX,
    "ins_ready": STATE_ROLE_INS_READY,
    "ins_status": STATE_ROLE_INS_STATUS,
    "internal_ins_state": STATE_ROLE_INTERNAL_INS_STATE,
    "internal_ins_yaw_reference": STATE_ROLE_INTERNAL_INS_YAW_REFERENCE,
    "pll_locked": STATE_ROLE_PLL_LOCKED,
    "modem_connected": STATE_ROLE_MODEM_CONNECTED,
}


def infer_state_role(name: str) -> Optional[str]:
    """Infer semantic state role from legacy state names."""
    n = _norm(name)
    return _STATE_EXACT_ROLE_MAP.get(n)


def semantics_to_dict(semantics: ProfileSemantics) -> dict:
    return {
        "channels": {
            str(cid): {"roles": list(ch.roles)}
            for cid, ch in sorted(semantics.channels.items())
            if ch.roles
        },
        "states": {
            str(sid): _state_semantic_to_dict(st)
            for sid, st in sorted(semantics.states.items())
            if st.role is not None or st.control is not None
        },
        "capabilities": {
            str(name): bool(value)
            for name, value in sorted(semantics.capabilities.items())
        },
    }


def _state_semantic_to_dict(semantic: StateSemantic) -> dict:
    out: dict = {}
    if semantic.role is not None:
        out["role"] = semantic.role
    if semantic.control is not None:
        out["control"] = {
            "subcmd": semantic.control.subcmd,
            "value_from": semantic.control.value_from,
        }
    return out


def semantics_from_dict(data: object) -> ProfileSemantics:
    if not isinstance(data, dict):
        return ProfileSemantics()

    semantics = ProfileSemantics()
    channels = data.get("channels", {})
    if isinstance(channels, dict):
        for raw_id, raw_sem in channels.items():
            try:
                cid = int(raw_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(raw_sem, dict):
                continue
            roles = raw_sem.get("roles", [])
            if isinstance(roles, str):
                roles = [roles]
            if isinstance(roles, list):
                clean_roles = _unique(str(r) for r in roles)
                if clean_roles:
                    semantics.channels[cid] = ChannelSemantic(clean_roles)

    states = data.get("states", {})
    if isinstance(states, dict):
        for raw_id, raw_sem in states.items():
            try:
                sid = int(raw_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(raw_sem, dict):
                continue
            role = raw_sem.get("role")
            role_value = _norm(role) if role is not None else None
            control = _control_from_dict(raw_sem.get("control"))
            if role_value or control is not None:
                semantics.states[sid] = StateSemantic(role=role_value or None, control=control)

    capabilities = data.get("capabilities", {})
    if isinstance(capabilities, dict):
        semantics.capabilities = {
            str(name): bool(value) for name, value in capabilities.items()
        }
    return semantics


def _control_from_dict(data: object) -> Optional[ControlBinding]:
    if not isinstance(data, dict):
        return None
    subcmd = data.get("subcmd")
    if subcmd is None:
        return None
    value_from = data.get("value_from", CONTROL_VALUE_FROM_ENUM_VALUE)
    return ControlBinding(subcmd=str(subcmd), value_from=str(value_from))
