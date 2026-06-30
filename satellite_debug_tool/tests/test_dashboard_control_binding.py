"""Dashboard/LiveView control_binding dispatch tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class _Worker:
    def __init__(self):
        self.sent: list[bytes] = []

    def send(self, frame: bytes) -> bool:
        self.sent.append(frame)
        return True


def _data_of(frame: bytes) -> bytes:
    length = int.from_bytes(frame[4:6], "little")
    return frame[6:6 + length]


def test_liveview_uses_control_binding_for_nonzero_state(qapp):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.core.protocol import MetaInfo, SubCmd
    from satellite_debug_tool.ui.live_view import LiveView

    view = LiveView(settings=Settings())
    view._profile_store.apply_meta(MetaInfo(2, "fw", "hw", "sn"))
    view._profile_store.import_dict({
        "schema_version": 2,
        "hw_type": "hw",
        "channel_table_ver": 0,
        "state_table_ver": 1,
        "event_table_ver": 0,
        "states": [
            {
                "state_id": 7,
                "state_type": 1,
                "flags": 1,
                "name": "TRACE_MODE",
                "enums": [
                    {"value": 0, "level": 3, "name": "STANDBY"},
                    {"value": 3, "level": 0, "name": "LOCK"},
                ],
            }
        ],
        "channels": [],
        "events": [],
        "semantics": {
            "channels": {},
            "states": {
                "7": {
                    "role": "trace_mode",
                    "control": {"subcmd": "SET_TRACE_MODE", "value_from": "enum_value"},
                }
            },
            "capabilities": {},
        },
        "meta": {"protocol_ver": 2, "fw_ver": "fw", "hw_type": "hw", "device_sn": "sn"},
    })
    # import_dict 不改变 current_hw_type，保持前面 apply_meta 建立的当前设备。
    worker = _Worker()
    view._worker = worker
    view._is_connected = True

    view._on_dashboard_mode_requested(7, 3)

    assert len(worker.sent) == 1
    assert _data_of(worker.sent[0]) == bytes([SubCmd.SET_TRACE_MODE, 3])


def test_dashboard_rebuilds_when_semantics_changes_control_binding(qapp):
    from satellite_debug_tool.core.data import StateStore
    from satellite_debug_tool.core.profile import ProfileStore
    from satellite_debug_tool.core.protocol import (
        ProfileSemanticStateEntry,
        ProfileSemanticsReport,
        StateDefEntry,
        StateEnumItem,
    )
    from satellite_debug_tool.ui.dashboard_widget import DashboardWidget

    store = ProfileStore(cache=None)
    states = StateStore()
    widget = DashboardWidget(store, states)
    widget.set_hw_type("hw")
    store.apply_state_define("hw", 1, [
        StateDefEntry(
            state_id=0,
            state_type=1,
            flags=1,
            name="TRACE_MODE",
            enums=[StateEnumItem(0, 3, "STANDBY"), StateEnumItem(3, 0, "LOCK")],
        )
    ])
    assert 0 in widget._mode_groups

    store.apply_profile_semantics("hw", ProfileSemanticsReport(
        table_ver=1,
        states=[ProfileSemanticStateEntry(0, "trace_mode", 0, 0)],
    ))

    assert 0 not in widget._mode_groups
    assert 0 in widget._status_chips
