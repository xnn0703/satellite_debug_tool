"""DeviceView capability gating tests."""

from __future__ import annotations

import pytest

from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    CommandResponse,
    MetaInfo,
    ParaEntry,
    ParaTableReport,
    ParaType,
    ProfileSemanticCapabilityEntry,
    ProfileSemanticsReport,
    RespCode,
    SubCmd,
)
from satellite_debug_tool.ui.device_view import DeviceView


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class FakeWorker:
    def __init__(self):
        self.sent: list[bytes] = []

    def send(self, frame: bytes) -> bool:
        self.sent.append(frame)
        return True


def _frame_data(frame: bytes) -> bytes:
    length = int.from_bytes(frame[4:6], "little")
    return frame[6:6 + length]


def test_esa01_without_capability_disables_parameters_and_ota(qapp):
    store = ProfileStore(cache=None)
    store.apply_meta(MetaInfo(2, "fw", "esa01", "sn"))
    view = DeviceView(profile_store=store)
    worker = FakeWorker()

    view.set_worker(worker)
    view._on_frame_received(MetaInfo(2, "fw", "esa01", "sn"))
    view._on_read_params()

    assert not view._read_all_btn.isEnabled()
    assert not view._factory_reset_btn.isEnabled()
    assert not view._ota_select_btn.isEnabled()
    assert worker.sent == []
    assert "未声明支持参数管理" in view._para_status_label.text()
    view.deleteLater()


def test_esa01_capability_enables_parameters_and_ota(qapp):
    store = ProfileStore(cache=None)
    store.apply_meta(MetaInfo(2, "fw", "esa01", "sn"))
    view = DeviceView(profile_store=store)
    worker = FakeWorker()

    view.set_worker(worker)
    view._on_frame_received(MetaInfo(2, "fw", "esa01", "sn"))
    store.apply_profile_semantics("esa01", ProfileSemanticsReport(
        table_ver=1,
        capabilities=[
            ProfileSemanticCapabilityEntry("parameters", True),
            ProfileSemanticCapabilityEntry("ota", True),
        ],
    ))

    assert view._read_all_btn.isEnabled()
    assert view._factory_reset_btn.isEnabled()
    assert view._ota_select_btn.isEnabled()
    view.deleteLater()


def test_read_all_merges_with_pending_para_request(qapp):
    store = ProfileStore(cache=None)
    store.apply_meta(MetaInfo(2, "fw", "esa01", "sn"))
    view = DeviceView(profile_store=store)
    worker = FakeWorker()

    view.set_worker(worker)
    view._on_frame_received(MetaInfo(2, "fw", "esa01", "sn"))
    store.apply_profile_semantics("esa01", ProfileSemanticsReport(
        table_ver=1,
        capabilities=[ProfileSemanticCapabilityEntry("parameters", True)],
    ))

    view._para_read_pending = True
    sent_count = len(worker.sent)
    view._on_read_params()

    assert len(worker.sent) == sent_count
    view.deleteLater()


def test_afd01_keeps_legacy_default_enabled(qapp):
    store = ProfileStore(cache=None)
    store.apply_meta(MetaInfo(2, "fw", "afd01", "sn"))
    view = DeviceView(profile_store=store)

    view.set_worker(FakeWorker())
    view._on_frame_received(MetaInfo(2, "fw", "afd01", "sn"))

    assert view._read_all_btn.isEnabled()
    assert view._factory_reset_btn.isEnabled()
    assert view._ota_select_btn.isEnabled()
    view.deleteLater()


def test_para_set_success_requires_readback_confirmation(qapp):
    store = ProfileStore(cache=None)
    store.apply_meta(MetaInfo(2, "fw", "esa01", "sn"))
    view = DeviceView(profile_store=store)
    worker = FakeWorker()

    view.set_worker(worker)
    view._on_frame_received(MetaInfo(2, "fw", "esa01", "sn"))
    store.apply_profile_semantics("esa01", ProfileSemanticsReport(
        table_ver=1,
        capabilities=[ProfileSemanticCapabilityEntry("parameters", True)],
    ))
    view._on_para_table_received(ParaTableReport(table_ver=1, params=[
        ParaEntry("modem_baud", int(ParaType.INT), 0, "921600"),
    ]))

    edit = view._para_table.cellWidget(0, 2)
    edit.setText("115200")
    view._on_para_apply(0)
    assert _frame_data(worker.sent[-1])[0] == SubCmd.PARA_SET

    sent_count = len(worker.sent)
    view._on_read_params()
    assert len(worker.sent) == sent_count

    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OK"))

    assert view._pending_request == "para_set"
    assert view._para_table.item(0, 4).text() != "✓ 成功"

    view._run_para_set_verify_read()
    assert _frame_data(worker.sent[-1]) == bytes([SubCmd.REQUEST_PARA_TABLE])

    view._on_para_table_received(ParaTableReport(table_ver=1, params=[
        ParaEntry("modem_baud", int(ParaType.INT), 0, "921600"),
    ]))
    assert view._pending_request == "para_set"
    assert view._para_table.item(0, 4).text() == "等待设备回读..."

    view._on_para_table_received(ParaTableReport(table_ver=1, params=[
        ParaEntry("modem_baud", int(ParaType.INT), 0, "115200"),
    ]))
    assert view._pending_request is None
    assert view._para_table.item(0, 4).text() == "✓ 成功"

    view._on_para_table_received(ParaTableReport(table_ver=1, params=[
        ParaEntry("modem_baud", int(ParaType.INT), 0, "115200"),
    ]))
    assert view._para_table.item(0, 4).text() == "✓ 成功"
    view.deleteLater()


def test_proactive_para_table_suppresses_auto_fallback(qapp):
    store = ProfileStore(cache=None)
    store.apply_meta(MetaInfo(2, "fw", "esa01", "sn"))
    view = DeviceView(profile_store=store)
    worker = FakeWorker()
    view.set_worker(worker)
    view._on_frame_received(MetaInfo(2, "fw", "esa01", "sn"))
    store.apply_profile_semantics("esa01", ProfileSemanticsReport(
        table_ver=1,
        capabilities=[ProfileSemanticCapabilityEntry("parameters", True)],
    ))

    view._on_para_table_received(ParaTableReport(table_ver=1, params=[]))
    view._on_auto_read_params("esa01")

    assert worker.sent == []
    view.deleteLater()


def test_contextual_para_set_waits_for_proactive_readback(qapp):
    store = ProfileStore(cache=None)
    store.apply_meta(MetaInfo(2, "fw", "esa01", "sn"))
    view = DeviceView(profile_store=store)
    worker = FakeWorker()
    view.set_worker(worker)
    view._on_frame_received(MetaInfo(2, "fw", "esa01", "sn"))
    store.apply_profile_semantics("esa01", ProfileSemanticsReport(
        table_ver=1,
        capabilities=[
            ProfileSemanticCapabilityEntry("parameters", True),
            ProfileSemanticCapabilityEntry("command_response_context", True),
        ],
    ))
    view._on_para_table_received(ParaTableReport(table_ver=1, params=[
        ParaEntry("modem_baud", int(ParaType.INT), 0, "921600"),
    ]))
    edit = view._para_table.cellWidget(0, 2)
    edit.setText("115200")
    view._on_para_apply(0)
    sent_count = len(worker.sent)

    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OK"))
    assert view._pending_request == "para_set"
    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "PARA_SET=modem_baud"))
    assert len(worker.sent) == sent_count
    assert view._para_table.item(0, 4).text() == "等待设备回读..."

    view._on_para_table_received(ParaTableReport(table_ver=1, params=[
        ParaEntry("modem_baud", int(ParaType.INT), 0, "115200"),
    ]))
    assert view._pending_request is None
    assert view._para_table.item(0, 4).text() == "✓ 成功"
    view.deleteLater()


def _make_ota_view() -> tuple[DeviceView, FakeWorker]:
    store = ProfileStore(cache=None)
    store.apply_meta(MetaInfo(2, "0.1.248", "esa01", "sn"))
    view = DeviceView(profile_store=store)
    worker = FakeWorker()
    view.set_worker(worker)
    view._on_frame_received(MetaInfo(2, "0.1.248", "esa01", "sn"))
    store.apply_profile_semantics("esa01", ProfileSemanticsReport(
        table_ver=1,
        capabilities=[
            ProfileSemanticCapabilityEntry("ota", True),
            ProfileSemanticCapabilityEntry("command_response_context", True),
        ],
    ))
    view._ota_file = b"A" * 700
    view._ota_filename = "esa01_application_v0.1.248.bin"
    view._ota_crc32 = 0x12345678
    return view, worker


def test_ota_quiesce_uses_live_debug_control_and_restores_on_failure(qapp):
    view, worker = _make_ota_view()
    view.set_debug_state(True)
    debug_requests: list[bool] = []
    transaction_states: list[bool] = []
    pause_states: list[bool] = []
    view.debug_mode_requested.connect(debug_requests.append)
    view.device_transaction_active_changed.connect(transaction_states.append)
    view.handshake_retry_pause_changed.connect(pause_states.append)

    view._on_ota_start()
    assert view._ota_state == "QUIESCE"
    assert debug_requests == [False]
    assert worker.sent == []

    view.on_debug_request_finished(False, False, "timeout")
    qapp.processEvents()
    assert view._ota_state == "IDLE"
    assert debug_requests == [False, True]
    assert transaction_states == [True, False]
    assert pause_states == [True, False]
    view.deleteLater()


def test_async_ota_matches_sequence_and_accepts_same_version_reconnect(qapp):
    view, worker = _make_ota_view()
    view._ota_pause_debug_cb.setChecked(False)

    view._on_ota_start()
    assert _frame_data(worker.sent[-1])[0] == SubCmd.OTA_BEGIN
    assert view._ota_state == "BEGIN"

    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OTA_BEGIN=READY"))
    qapp.processEvents()
    assert _frame_data(worker.sent[-1])[:3] == bytes([SubCmd.OTA_DATA, 0, 0])

    sent_count = len(worker.sent)
    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OTA_DATA=99"))
    assert len(worker.sent) == sent_count
    assert view._ota_seq == 0

    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OTA_DATA=0"))
    qapp.processEvents()
    assert _frame_data(worker.sent[-1])[:3] == bytes([SubCmd.OTA_DATA, 1, 0])
    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OTA_DATA=1"))
    qapp.processEvents()
    assert _frame_data(worker.sent[-1])[0] == SubCmd.OTA_END

    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OTA_END=VERIFIED"))
    assert view._ota_state == "WAIT_REBOOT"
    assert view._ota_active is True

    view._on_frame_received(MetaInfo(2, "0.1.248", "esa01", "sn"))
    assert view._ota_state == "WAIT_REBOOT"

    # 模拟隔离窗结束后同版本 app 重新上线。
    view._ota_reboot_meta_not_before = 0.0
    view._on_frame_received(MetaInfo(2, "0.1.248", "esa01", "sn"))
    assert view._ota_state == "IDLE"
    assert view._ota_active is False
    assert "与升级前相同" in view._ota_status_label.text()
    view.deleteLater()


def test_async_ota_data_timeout_retries_three_times(qapp):
    view, worker = _make_ota_view()
    view._ota_pause_debug_cb.setChecked(False)
    view._ota_file = b"A" * 100
    view._ota_total_chunks = 0
    view._on_ota_start()
    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OTA_BEGIN=READY"))
    qapp.processEvents()

    for _ in range(3):
        view._on_response_timeout()
        assert view._ota_active is True
    view._on_response_timeout()

    data_frames = [frame for frame in worker.sent if _frame_data(frame)[0] == SubCmd.OTA_DATA]
    assert len(data_frames) == 4
    assert view._ota_active is False
    assert "连续超时" in view._ota_status_label.text()
    view.deleteLater()
