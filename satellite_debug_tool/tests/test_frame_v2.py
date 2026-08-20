"""frame_v2 数据类与常量的单元测试。"""

import pytest

from satellite_debug_tool.core.protocol import (
    CHANNEL_FLAG_CRITICAL,
    CHANNEL_FLAG_DEFAULT_VISIBLE,
    CmdType,
    ChannelDefEntry,
    DataType,
    EVENT_ID_USER_MARK,
    Level,
    PROTOCOL_VERSION,
    RespCode,
    STATE_FLAG_CRITICAL,
    STATE_FLAG_INVERSE,
    StateDefEntry,
    StateType,
    SubCmd,
)


class TestProtocolConstants:
    def test_version_is_2(self):
        assert PROTOCOL_VERSION == 0x02

    def test_user_mark_reserved(self):
        assert EVENT_ID_USER_MARK == 0xFFFF

    def test_cmd_values_match_spec(self):
        # 规范 §4：通用 Debug/GNSS 使用 0x01–0x10；AFD01 产品服务
        # 使用独立连续区间 0x20–0x2B，避免与旧设备扩展冲突。
        debug_values = [int(cmd) for cmd in CmdType if int(cmd) < 0x20]
        service_values = [int(cmd) for cmd in CmdType if 0x20 <= int(cmd) < 0x30]
        orbit_values = [int(cmd) for cmd in CmdType if int(cmd) >= 0x30]
        assert all(0x01 <= value <= 0x10 for value in debug_values)
        assert service_values == list(range(0x20, 0x2C))
        assert orbit_values == [0x30, 0x31]

    def test_subcmd_values_match_spec(self):
        # 规范 §5.3 + M13 扩展：子命令范围 0x01–0x13
        for sub in SubCmd:
            assert 0x01 <= int(sub) <= 0x13

    def test_resp_codes(self):
        assert RespCode.SUCCESS == 0
        assert RespCode.PARAM_ERROR == 2

    def test_data_type_float32(self):
        assert DataType.FLOAT32 == 0x01

    def test_state_type_values(self):
        assert StateType.BOOL == 0
        assert StateType.ENUM == 1

    def test_level_values(self):
        assert Level.DEBUG == 0
        assert Level.INFO == 1
        assert Level.WARN == 2
        assert Level.ERROR == 3


class TestChannelDefEntry:
    def test_flags_critical(self):
        ch = ChannelDefEntry(
            channel_id=0, data_type=1, group_id=0,
            flags=CHANNEL_FLAG_CRITICAL, name="roll", unit="°",
            display_min=-180.0, display_max=180.0,
        )
        assert ch.critical is True
        assert ch.default_visible is False

    def test_flags_default_visible(self):
        ch = ChannelDefEntry(
            channel_id=1, data_type=1, group_id=0,
            flags=CHANNEL_FLAG_DEFAULT_VISIBLE, name="pitch", unit="°",
            display_min=-90.0, display_max=90.0,
        )
        assert ch.critical is False
        assert ch.default_visible is True

    def test_flags_both(self):
        ch = ChannelDefEntry(
            channel_id=2, data_type=1, group_id=0,
            flags=CHANNEL_FLAG_CRITICAL | CHANNEL_FLAG_DEFAULT_VISIBLE,
            name="yaw", unit="°", display_min=-180.0, display_max=180.0,
        )
        assert ch.critical is True
        assert ch.default_visible is True


class TestStateDefEntry:
    def test_flags_critical(self):
        st = StateDefEntry(
            state_id=0, state_type=int(StateType.BOOL),
            flags=STATE_FLAG_CRITICAL, name="LOCK_FLAG",
        )
        assert st.critical is True
        assert st.inverse is False

    def test_flags_inverse(self):
        st = StateDefEntry(
            state_id=1, state_type=int(StateType.BOOL),
            flags=STATE_FLAG_INVERSE, name="WIZNET_LINK",
        )
        assert st.critical is False
        assert st.inverse is True
