"""内部 INS 航向 profile 语义和 Live 选择合同测试。"""

from __future__ import annotations

import pytest

from satellite_debug_tool.core.profile import (
    CHANNEL_ROLE_INTERNAL_INS_YAW,
    CHANNEL_ROLE_YAW,
)
from satellite_debug_tool.core.profile.ins_yaw_display import (
    INTERNAL_INS_YAW_ABSOLUTE,
    INTERNAL_INS_YAW_RELATIVE,
    INTERNAL_INS_YAW_UNAVAILABLE,
    select_attitude_yaw_channel,
)
from satellite_debug_tool.core.profile.semantics import infer_channel_roles
from satellite_debug_tool.core.protocol import ChannelDefEntry
from satellite_debug_tool.core.protocol.codec_v2 import (
    decode_channel_define,
    decode_data_report,
)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_internal_and_external_ins_yaw_do_not_fallback_to_business_yaw():
    assert infer_channel_roles("yaw") == [CHANNEL_ROLE_YAW]
    assert infer_channel_roles("internal_ins_yaw") == [CHANNEL_ROLE_INTERNAL_INS_YAW]
    assert CHANNEL_ROLE_YAW not in infer_channel_roles("internal_ins_yaw")
    assert CHANNEL_ROLE_YAW not in infer_channel_roles("external_ins_yaw")


def test_attitude_yaw_selection_preserves_legacy_and_reference_contract():
    assert select_attitude_yaw_channel(
        reference_state_defined=False,
        reference_value=None,
        business_yaw_channel="ch_02",
        internal_yaw_channel="ch_32",
    ) == ("ch_02", "legacy")
    assert select_attitude_yaw_channel(
        reference_state_defined=True,
        reference_value=INTERNAL_INS_YAW_UNAVAILABLE,
        business_yaw_channel="ch_02",
        internal_yaw_channel="ch_32",
    ) == ("", "unavailable")
    assert select_attitude_yaw_channel(
        reference_state_defined=True,
        reference_value=INTERNAL_INS_YAW_RELATIVE,
        business_yaw_channel="ch_02",
        internal_yaw_channel="ch_32",
    ) == ("ch_32", "relative")
    assert select_attitude_yaw_channel(
        reference_state_defined=True,
        reference_value=INTERNAL_INS_YAW_ABSOLUTE,
        business_yaw_channel="ch_02",
        internal_yaw_channel="ch_32",
    ) == ("ch_02", "absolute")


def test_absolute_reference_can_fallback_to_internal_channel():
    assert select_attitude_yaw_channel(
        reference_state_defined=True,
        reference_value=INTERNAL_INS_YAW_ABSOLUTE,
        business_yaw_channel="",
        internal_yaw_channel="ch_32",
    ) == ("ch_32", "absolute")


def test_live_view_routes_runtime_reference_state():
    from types import SimpleNamespace

    from satellite_debug_tool.ui.live_view import LiveView

    class ProfileStub:
        def __init__(self, state_defined: bool):
            self._state_defined = state_defined

        def find_state_by_role(self, hw_type: str, role: str):
            assert hw_type == "afd01"
            assert role == "internal_ins_yaw_reference"
            return SimpleNamespace(state_id=13) if self._state_defined else None

    class StateStub:
        def __init__(self, value: int):
            self._value = value

        def get_value(self, hw_type: str, state_id: int):
            assert (hw_type, state_id) == ("afd01", 13)
            return self._value

    view = SimpleNamespace(
        _attitude_business_yaw_ch="ch_02",
        _attitude_internal_yaw_ch="ch_32",
        _profile_store=ProfileStub(True),
        _state_store=StateStub(INTERNAL_INS_YAW_RELATIVE),
    )
    assert LiveView._attitude_yaw_input(view, "afd01") == ("ch_32", "relative")

    view._state_store = StateStub(INTERNAL_INS_YAW_ABSOLUTE)
    assert LiveView._attitude_yaw_input(view, "afd01") == ("ch_02", "absolute")

    view._profile_store = ProfileStub(False)
    assert LiveView._attitude_yaw_input(view, "afd01") == ("ch_02", "legacy")


def test_protocol_accepts_channel_id_32_definition_and_data():
    name = b"internal_ins_yaw"
    unit = b"deg"
    define = (
        bytes([1, 1, 32, 1, 0, 0, len(name)])
        + name
        + bytes([len(unit)])
        + unit
        + b"\x00\x00\x34\xc3\x00\x00\x34\x43"
    )
    table = decode_channel_define(define)
    assert table.channels == [
        ChannelDefEntry(32, 1, 0, 0, "internal_ins_yaw", "deg", -180.0, 180.0)
    ]

    report = decode_data_report(b"\x7b\x00\x00\x00\x01\x20\x00\x00\x20\xc1")
    assert report.timestamp == 123
    assert report.samples[0].channel_id == 32
    assert report.samples[0].value == -10.0


def test_attitude_widget_marks_reference(qapp):
    from satellite_debug_tool.ui.attitude_widget import AttitudeWidget

    widget = AttitudeWidget()
    widget.set_yaw_reference("relative")
    assert widget.yaw_reference() == "relative"
    assert "[RELATIVE]" in widget._yaw_name_lbl.text()

    widget.set_yaw_reference("absolute")
    assert widget.yaw_reference() == "absolute"
    assert "[ABSOLUTE]" in widget._yaw_name_lbl.text()

    widget.set_yaw_reference("unavailable")
    assert "[UNAVAILABLE]" in widget._yaw_name_lbl.text()
