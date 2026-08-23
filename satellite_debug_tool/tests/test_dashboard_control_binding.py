"""Dashboard/LiveView control_binding dispatch tests."""

from __future__ import annotations

import os
import struct
import time

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


def _data_report_frame() -> bytes:
    from satellite_debug_tool.core.protocol import CmdType, build_frame

    payload = bytearray()
    payload.extend((123).to_bytes(4, "little"))
    payload.append(1)
    payload.append(0)
    payload.extend(struct.pack("<f", 1.0))
    return build_frame(CmdType.DATA_REPORT, bytes(payload))


def _command_response_frame(code: int, msg: str) -> bytes:
    from satellite_debug_tool.core.protocol import CmdType, build_frame

    return build_frame(CmdType.COMMAND_RESPONSE, bytes([code]) + msg.encode("utf-8"))


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


def test_liveview_fps_counts_data_reports_only(qapp):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.ui.live_view import LiveView

    view = LiveView(settings=Settings())

    view._update_display()
    assert view._fps_label.text() == "FPS 0"
    assert view._frame_count_label.text() == "FRM 0"

    view._on_data_received(_data_report_frame())
    view._update_display()

    assert view._fps_label.text() == "FPS 1"
    assert view._frame_count_label.text() == "FRM 1"


def test_liveview_debug_waits_for_specific_ack(qapp):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.core.protocol import RespCode, SubCmd
    from satellite_debug_tool.ui.live_view import LiveView

    view = LiveView(settings=Settings())
    worker = _Worker()
    view._worker = worker
    view._is_connected = True
    view._debug_btn.setEnabled(True)

    view._on_debug_toggled()

    assert _data_of(worker.sent[-1]) == bytes([SubCmd.DEBUG_ENABLE, 1])
    assert view._debug_enabled is False
    assert view._debug_pending_target is True
    assert view._debug_btn.isEnabled() is False

    view._on_data_received(_command_response_frame(int(RespCode.SUCCESS), "OK"))
    assert view._debug_enabled is False
    assert view._debug_pending_target is True

    view._on_data_received(_command_response_frame(int(RespCode.SUCCESS), "DEBUG_ENABLE=0"))
    assert view._debug_enabled is False
    assert view._debug_pending_target is True

    view._on_data_received(_command_response_frame(int(RespCode.SUCCESS), "DEBUG_ENABLE=1"))
    assert view._debug_enabled is True
    assert view._debug_pending_target is None
    assert view._debug_btn.isEnabled() is True
    from satellite_debug_tool.i18n import tr
    assert view._debug_btn.text() == tr("Debug: {state}", state="ON")


def test_liveview_debug_click_trace(qapp, monkeypatch, capsys):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.core.link_trace import TRACE_ENV
    from satellite_debug_tool.ui.live_view import LiveView

    monkeypatch.setenv(TRACE_ENV, "1")
    view = LiveView(settings=Settings())
    view._worker = _Worker()
    view._is_connected = True
    view._debug_btn.setEnabled(True)

    view._on_debug_toggled()

    output = capsys.readouterr().out
    assert "[DBG_UI " in output
    assert "CLICK DEBUG target=1 connected=1 pending=None" in output
    assert "[DBG_CTRL " in output
    assert "send DEBUG_ENABLE target=1" in output


def test_liveview_debug_data_report_requires_exact_ack(qapp):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.core.protocol import SubCmd
    from satellite_debug_tool.ui.live_view import LiveView

    view = LiveView(settings=Settings())
    worker = _Worker()
    view._worker = worker
    view._is_connected = True
    view._debug_btn.setEnabled(True)

    view._on_debug_toggled()

    assert _data_of(worker.sent[-1]) == bytes([SubCmd.DEBUG_ENABLE, 1])
    assert view._debug_pending_target is True

    view._on_data_received(_data_report_frame())

    assert view._debug_enabled is False
    assert view._debug_pending_target is True
    assert view._debug_btn.isEnabled() is False


def test_liveview_debug_timeout_does_not_retry(qapp):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.ui.live_view import LiveView

    view = LiveView(settings=Settings())
    worker = _Worker()
    view._worker = worker
    view._is_connected = True
    view._debug_btn.setEnabled(True)

    view._on_debug_toggled()
    assert len(worker.sent) == 1

    view._on_debug_ack_timeout()

    assert len(worker.sent) == 1
    assert view._debug_enabled is False
    assert view._debug_pending_target is None
    assert view._debug_btn.isEnabled() is True


def test_liveview_debug_accepts_recent_late_matching_ack(qapp):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.core.protocol import RespCode
    from satellite_debug_tool.ui.live_view import LiveView

    view = LiveView(settings=Settings())
    view._is_connected = True
    view._debug_enabled = False
    view._debug_last_requested_target = True
    view._debug_last_request_at = time.monotonic()

    view._on_data_received(_command_response_frame(int(RespCode.SUCCESS), "DEBUG_ENABLE=1"))

    assert view._debug_enabled is True
    assert view._debug_pending_target is None
    assert view._debug_btn.isEnabled() is True
    from satellite_debug_tool.i18n import tr
    assert view._debug_btn.text() == tr("Debug: {state}", state="ON")


def test_liveview_debug_off_requires_exact_ack_even_when_data_arrives(qapp):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.core.protocol import RespCode, SubCmd
    from satellite_debug_tool.ui.live_view import DEBUG_ACK_TIMEOUT_MS, LiveView

    view = LiveView(settings=Settings())
    worker = _Worker()
    view._worker = worker
    view._is_connected = True
    view._debug_enabled = True
    results: list[tuple[bool, bool, str]] = []
    view.debug_request_finished.connect(lambda target, ok, detail: results.append((target, ok, detail)))

    view.request_debug_mode(False)
    assert DEBUG_ACK_TIMEOUT_MS == 3000
    assert _data_of(worker.sent[-1]) == bytes([SubCmd.DEBUG_ENABLE, 0])
    view._on_data_received(_data_report_frame())
    assert view._debug_pending_target is False
    assert results == []

    view._on_data_received(_command_response_frame(int(RespCode.SUCCESS), "DEBUG_ENABLE=0"))
    assert view._debug_enabled is False
    assert results == [(False, True, "ack")]


def test_liveview_device_transaction_locks_only_the_button(qapp):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.ui.live_view import LiveView

    view = LiveView(settings=Settings())
    worker = _Worker()
    view._worker = worker
    view._is_connected = True
    view._update_debug_button_enabled()
    assert view._debug_btn.isEnabled()

    view.set_device_transaction_active(True)
    assert not view._debug_btn.isEnabled()

    # Device 仍可通过统一控制入口发命令，不依赖按钮可用状态。
    view.request_debug_mode(True)
    assert view._debug_pending_target is True


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


def test_dashboard_state_cleared_resets_previous_lock(qapp):
    from satellite_debug_tool.core.data import StateStore
    from satellite_debug_tool.core.profile import ProfileStore
    from satellite_debug_tool.core.protocol import (
        StateDefEntry,
        StateEnumItem,
        StateReport,
        StateSample,
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

    states.update("hw", StateReport(timestamp=100, states=[StateSample(0, 3)]))
    assert widget._mode_groups[0]._buttons[3].isChecked()

    states.clear("hw")

    assert not widget._mode_groups[0]._buttons[3].isChecked()
    assert widget._mode_groups[0]._current_value is None


def test_gps_fix_unknown_is_consistent_in_status_strip_and_dashboard(qapp):
    from satellite_debug_tool.core.data import StateStore
    from satellite_debug_tool.core.profile import ProfileStore
    from satellite_debug_tool.core.protocol import (
        StateDefEntry,
        StateEnumItem,
        StateReport,
        StateSample,
    )
    from satellite_debug_tool.ui.dashboard_widget import DashboardWidget
    from satellite_debug_tool.ui.status_strip_widget import StatusStripWidget

    profiles = ProfileStore(cache=None)
    states = StateStore()
    status_strip = StatusStripWidget(profiles, states)
    dashboard = DashboardWidget(profiles, states)
    status_strip.set_hw_type("afd01")
    dashboard.set_hw_type("afd01")
    profiles.apply_state_define("afd01", 1, [
        StateDefEntry(
            state_id=2,
            state_type=1,
            flags=1,
            name="GPS_FIX",
            enums=[StateEnumItem(0, 2, "NO_FIX"), StateEnumItem(2, 0, "3D")],
        )
    ])

    assert status_strip._state_chips[2]._text.text() == "GPS_FIX: UNKNOWN"
    assert dashboard._status_chips[2]._value_label.text() == "UNKNOWN"

    states.update("afd01", StateReport(timestamp=100, states=[StateSample(2, 2)]))
    assert status_strip._state_chips[2]._text.text() == "GPS_FIX: 3D"
    assert dashboard._status_chips[2]._value_label.text() == "3D"

    states.clear("afd01")
    assert status_strip._state_chips[2]._text.text() == "GPS_FIX: UNKNOWN"
    assert dashboard._status_chips[2]._value_label.text() == "UNKNOWN"
