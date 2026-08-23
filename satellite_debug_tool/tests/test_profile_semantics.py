"""Profile semantic role/schema tests."""

from __future__ import annotations

from satellite_debug_tool.core.profile import (
    CHANNEL_ROLE_GPS_LAT,
    CHANNEL_ROLE_GPS_LON,
    CHANNEL_ROLE_GPS_COG,
    CHANNEL_ROLE_GPS_COG_STD,
    CHANNEL_ROLE_GPS_NUM_SV,
    CHANNEL_ROLE_GPS_SPEED,
    CHANNEL_ROLE_GPS_VEL_D,
    CHANNEL_ROLE_GPS_VEL_E,
    CHANNEL_ROLE_GPS_VEL_N,
    CHANNEL_ROLE_ROLL,
    CONTROL_SUBCMD_SET_TRACE_MODE,
    CONTROL_VALUE_FROM_ENUM_VALUE,
    STATE_ROLE_TRACE_MODE,
    ChannelSemantic,
    CapabilitySupport,
    ControlBinding,
    DeviceProfile,
    ProfileSemantics,
    ProfileStore,
    StateSemantic,
)
from satellite_debug_tool.core.profile.cache import profile_from_dict, profile_to_dict
from satellite_debug_tool.core.profile.semantics import infer_channel_roles
from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    MetaInfo,
    ProfileSemanticCapabilityEntry,
    ProfileSemanticChannelEntry,
    ProfileSemanticStateEntry,
    ProfileSemanticsReport,
    StateDefEntry,
    StateEnumItem,
    SubCmd,
)


def _channel(cid: int, name: str) -> ChannelDefEntry:
    return ChannelDefEntry(cid, 1, 0, 0, name, "", -1.0, 1.0)


def _trace_state(sid: int = 0) -> StateDefEntry:
    return StateDefEntry(
        state_id=sid,
        state_type=1,
        flags=1,
        name="TRACE_MODE",
        enums=[StateEnumItem(0, 3, "STANDBY"), StateEnumItem(3, 0, "LOCK")],
    )


def test_schema_v1_uses_name_fallback():
    store = ProfileStore()
    hw = store.import_dict({
        "schema_version": 1,
        "hw_type": "legacy",
        "channel_table_ver": 1,
        "state_table_ver": 1,
        "event_table_ver": 0,
        "channels": [
            {"channel_id": 8, "data_type": 1, "group_id": 4, "flags": 0,
             "name": "gps_lat", "unit": "", "display_min": -90, "display_max": 90},
            {"channel_id": 9, "data_type": 1, "group_id": 4, "flags": 0,
             "name": "gps_lon", "unit": "", "display_min": -180, "display_max": 180},
        ],
        "states": [
            {"state_id": 0, "state_type": 1, "flags": 1, "name": "TRACE_MODE",
             "enums": [{"value": 0, "level": 3, "name": "STANDBY"}]},
        ],
        "events": [],
        "meta": None,
    })

    assert hw == "legacy"
    assert store.find_channel_by_role("legacy", CHANNEL_ROLE_GPS_LAT).channel_id == 8
    assert store.find_channel_by_role("legacy", CHANNEL_ROLE_GPS_LON).channel_id == 9
    assert store.get_state_control_binding("legacy", 0).subcmd == CONTROL_SUBCMD_SET_TRACE_MODE


def test_schema_v2_roundtrip_explicit_roles_and_capabilities():
    profile = DeviceProfile(hw_type="semantic")
    profile.channels = {
        1: _channel(1, "lat_deg"),
        2: _channel(2, "roll_sensor"),
    }
    profile.states = {7: _trace_state(7)}
    profile.semantics_table_ver = 4
    profile.semantics = ProfileSemantics(
        channels={
            1: ChannelSemantic([CHANNEL_ROLE_GPS_LAT]),
            2: ChannelSemantic([CHANNEL_ROLE_ROLL]),
        },
        states={
            7: StateSemantic(
                role=STATE_ROLE_TRACE_MODE,
                control=ControlBinding(
                    CONTROL_SUBCMD_SET_TRACE_MODE,
                    CONTROL_VALUE_FROM_ENUM_VALUE,
                ),
            )
        },
        capabilities={"parameters": True, "channel_enable_mask": False},
    )

    data = profile_to_dict(profile)
    assert data["schema_version"] == 2
    loaded = profile_from_dict(data)

    assert loaded.semantics_table_ver == 4
    assert loaded.semantics.channels[1].roles == [CHANNEL_ROLE_GPS_LAT]
    assert loaded.semantics.states[7].control.subcmd == CONTROL_SUBCMD_SET_TRACE_MODE
    assert loaded.semantics.capabilities["parameters"] is True
    assert loaded.semantics.capabilities["channel_enable_mask"] is False


def test_profile_store_applies_wire_semantics_and_explicit_readonly_state():
    store = ProfileStore()
    store.apply_meta(MetaInfo(2, "fw", "hw", "sn"))
    store.apply_channel_define("hw", 1, [_channel(3, "latitude_deg")])
    store.apply_state_define("hw", 1, [_trace_state(0)])

    report = ProfileSemanticsReport(
        table_ver=9,
        channels=[ProfileSemanticChannelEntry(3, [CHANNEL_ROLE_GPS_LAT])],
        states=[ProfileSemanticStateEntry(0, STATE_ROLE_TRACE_MODE, 0, 0)],
        capabilities=[ProfileSemanticCapabilityEntry("sample_rate", True)],
    )
    store.apply_profile_semantics("hw", report)

    assert store.find_channel_by_role("hw", CHANNEL_ROLE_GPS_LAT).channel_id == 3
    assert store.find_state_by_role("hw", STATE_ROLE_TRACE_MODE).state_id == 0
    assert store.has_capability("hw", "sample_rate") is True
    assert store.has_capability("hw", "ota") is False
    assert store.capability_status("hw", "sample_rate") is CapabilitySupport.SUPPORTED
    assert store.capability_status("hw", "ota") is CapabilitySupport.UNKNOWN
    # 显式声明了 trace_mode 但没有 control，不能再走旧 state_id==0 fallback。
    assert store.get_state_control_binding("hw", 0) is None


def test_explicit_control_binding_can_use_nonzero_state_id():
    store = ProfileStore()
    store.apply_meta(MetaInfo(2, "fw", "hw", "sn"))
    store.apply_state_define("hw", 1, [_trace_state(7)])
    store.apply_profile_semantics("hw", ProfileSemanticsReport(
        table_ver=1,
        states=[
            ProfileSemanticStateEntry(
                state_id=7,
                role=STATE_ROLE_TRACE_MODE,
                control_subcmd=SubCmd.SET_TRACE_MODE,
                control_value_from=0,
            )
        ],
    ))

    binding = store.get_state_control_binding("hw", 7)
    assert binding is not None
    assert binding.subcmd == CONTROL_SUBCMD_SET_TRACE_MODE
    assert binding.value_from == CONTROL_VALUE_FROM_ENUM_VALUE


def test_state_zero_requires_trace_mode_semantics():
    store = ProfileStore()
    store.apply_meta(MetaInfo(2, "fw", "third-party", "sn"))
    store.apply_state_define(
        "third-party",
        1,
        [StateDefEntry(0, 0, 0, "POWER_READY")],
    )

    assert store.get_state_control_binding("third-party", 0) is None


def test_gnss_motion_role_name_fallback():
    cases = {
        "gps_num_sv": CHANNEL_ROLE_GPS_NUM_SV,
        "gvel": CHANNEL_ROLE_GPS_SPEED,
        "gps_cog": CHANNEL_ROLE_GPS_COG,
        "gcog": CHANNEL_ROLE_GPS_COG,
        "gps_vel_n": CHANNEL_ROLE_GPS_VEL_N,
        "gps_vel_e": CHANNEL_ROLE_GPS_VEL_E,
        "gps_vel_d": CHANNEL_ROLE_GPS_VEL_D,
        "gps_cacc": CHANNEL_ROLE_GPS_COG_STD,
    }

    for name, role in cases.items():
        assert infer_channel_roles(name) == [role]
