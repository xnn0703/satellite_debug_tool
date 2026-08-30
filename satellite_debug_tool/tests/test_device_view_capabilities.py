"""DeviceView capability gating tests."""

from __future__ import annotations

import pytest

from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.session import OtaState, ParameterOperation
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
from satellite_debug_tool.ui.device_view import CapabilityUiState, DeviceView


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
    assert view._para_capability_state is CapabilityUiState.WAITING_PROFILE
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
            ProfileSemanticCapabilityEntry("command_response_context", True),
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
        capabilities=[
            ProfileSemanticCapabilityEntry("parameters", True),
            ProfileSemanticCapabilityEntry("command_response_context", True),
        ],
    ))

    view._parameter_controller._read_pending = True
    sent_count = len(worker.sent)
    view._on_read_params()

    assert len(worker.sent) == sent_count
    view.deleteLater()


def test_afd01_waits_for_explicit_capability_declaration(qapp):
    store = ProfileStore(cache=None)
    store.apply_meta(MetaInfo(2, "fw", "afd01", "sn"))
    view = DeviceView(profile_store=store)

    view.set_worker(FakeWorker())
    view._on_frame_received(MetaInfo(2, "fw", "afd01", "sn"))

    assert not view._read_all_btn.isEnabled()
    assert not view._factory_reset_btn.isEnabled()
    assert not view._ota_select_btn.isEnabled()
    view.deleteLater()


def test_para_set_mismatched_requested_readback_terminates_transaction(qapp):
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
    assert _frame_data(worker.sent[-1])[0] == SubCmd.PARA_SET

    sent_count = len(worker.sent)
    view._on_read_params()
    assert len(worker.sent) == sent_count

    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OK"))

    assert view._parameter_controller.operation is ParameterOperation.WRITING
    assert view._para_table.item(0, 4).text() != "✓ 成功"

    view._on_command_response(CommandResponse(
        int(RespCode.SUCCESS),
        "PARA_SET=modem_baud",
    ))
    view._run_para_set_verify_read()
    assert _frame_data(worker.sent[-1]) == bytes([SubCmd.REQUEST_PARA_TABLE])

    view._on_frame_received(ParaTableReport(table_ver=1, params=[
        ParaEntry("modem_baud", int(ParaType.INT), 0, "921600"),
    ]))
    assert view._parameter_controller.operation is ParameterOperation.IDLE
    assert view._para_table.item(0, 4).text() == "未回读到目标值"
    assert not view._session_core.device_transaction_active
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
        capabilities=[
            ProfileSemanticCapabilityEntry("parameters", True),
            ProfileSemanticCapabilityEntry("command_response_context", True),
        ],
    ))

    view._on_frame_received(ParaTableReport(table_ver=1, params=[]))
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
    assert view._parameter_controller.operation is ParameterOperation.WRITING
    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "PARA_SET=modem_baud"))
    assert len(worker.sent) == sent_count
    assert view._para_table.item(0, 4).text() == "等待设备回读..."

    view._on_command_response(
        CommandResponse(int(RespCode.INTERNAL_ERROR), "PARA_SET=modem_baud")
    )
    assert view._parameter_controller.operation is ParameterOperation.VERIFYING
    assert view._session_core.device_transaction_active
    assert view._para_table.item(0, 4).text() == "等待设备回读..."

    view._on_frame_received(ParaTableReport(table_ver=1, params=[
        ParaEntry("modem_baud", int(ParaType.INT), 0, "115200"),
    ]))
    assert view._parameter_controller.operation is ParameterOperation.IDLE
    assert view._para_table.item(0, 4).text() == "✓ 成功"
    view.deleteLater()


def test_parameter_write_reports_session_change_as_direct_cancellation(qapp):
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

    view._session_core.begin_connection(
        endpoint=("192.168.1.12", 4004),
        transport=object(),
        sender=lambda _frame: True,
        handshake_enabled=False,
    )

    assert view._parameter_controller.operation is ParameterOperation.IDLE
    assert view._para_status_by_name["modem_baud"] == (
        "Device session changed; parameter write was cancelled",
        {},
    )
    assert view._para_table.item(0, 4).text() == (
        "Device session changed; parameter write was cancelled"
    )
    view.deleteLater()


def test_parameter_disconnect_cancellation_survives_reconnect_table_render(qapp):
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
    parameter = ParaEntry("modem_baud", int(ParaType.INT), 0, "921600")
    view._on_para_table_received(ParaTableReport(table_ver=1, params=[parameter]))
    edit = view._para_table.cellWidget(0, 2)
    edit.setText("115200")
    view._on_para_apply(0)
    assert view._parameter_controller.operation is ParameterOperation.WRITING

    view._session_core.end_connection()

    assert view._parameter_controller.operation is ParameterOperation.IDLE
    assert view._para_status_by_name["modem_baud"] == (
        "Device session changed; parameter write was cancelled",
        {},
    )

    view.set_worker(FakeWorker())
    view._on_frame_received(MetaInfo(2, "fw", "esa01", "sn"))
    view._on_frame_received(ParaTableReport(table_ver=2, params=[parameter]))

    assert view._para_table.item(0, 4).text() == (
        "Device session changed; parameter write was cancelled"
    )
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
    view._ota_controller.configure_engineering_file(
        b"A" * 700,
        "esa01_application_v0.1.248.bin",
    )
    return view, worker


def test_ota_quiesce_uses_live_debug_control_and_restores_on_failure(qapp):
    view, worker = _make_ota_view()
    view.set_debug_state(True)
    debug_requests: list[bool] = []
    transaction_states: list[bool] = []
    view.debug_mode_requested.connect(debug_requests.append)
    view._session_core.device_transaction_changed.connect(transaction_states.append)

    view._on_ota_start()
    assert view._ota_controller.state is OtaState.QUIESCE
    assert debug_requests == [False]
    assert worker.sent == []

    view.on_debug_request_finished(False, False, "timeout")
    qapp.processEvents()
    assert view._ota_controller.state is OtaState.IDLE
    assert debug_requests == [False, True]
    assert transaction_states == [True, False]
    view.deleteLater()


def test_shared_device_transaction_interlocks_customer_ota_controls(qapp):
    view, _worker = _make_ota_view()
    states: list[bool] = []
    view._session_core.device_transaction_changed.connect(states.append)
    transaction_owner = object()

    assert view._session_core.try_acquire_device_transaction(transaction_owner)
    assert not view.customer_ota_available()
    assert not view._ota_upload_btn.isEnabled()

    assert view._session_core.release_device_transaction(transaction_owner)
    assert view.customer_ota_available()
    assert states == [True, False]
    view.deleteLater()


def test_async_ota_matches_sequence_and_reports_same_version_as_unconfirmed(qapp):
    view, worker = _make_ota_view()
    view._ota_pause_debug_cb.setChecked(False)

    view._on_ota_start()
    assert _frame_data(worker.sent[-1])[0] == SubCmd.OTA_BEGIN
    assert view._ota_controller.state is OtaState.BEGIN

    view._on_command_response(
        CommandResponse(int(RespCode.INTERNAL_ERROR), "PARA_SET=failed")
    )
    assert view._ota_controller.state is OtaState.BEGIN

    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OTA_BEGIN=READY"))
    qapp.processEvents()
    assert _frame_data(worker.sent[-1])[:3] == bytes([SubCmd.OTA_DATA, 0, 0])

    view._on_command_response(
        CommandResponse(int(RespCode.INTERNAL_ERROR), "PARA_SET=failed")
    )
    assert view._ota_controller.state is OtaState.DATA

    sent_count = len(worker.sent)
    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OTA_DATA=99"))
    view._on_command_response(
        CommandResponse(int(RespCode.INTERNAL_ERROR), "OTA_DATA=99")
    )
    assert len(worker.sent) == sent_count
    assert view._ota_controller.sequence == 0
    assert view._ota_controller.state is OtaState.DATA
    assert view._session_core.device_transaction_active

    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OTA_DATA=0"))
    qapp.processEvents()
    assert _frame_data(worker.sent[-1])[:3] == bytes([SubCmd.OTA_DATA, 1, 0])
    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OTA_DATA=1"))
    qapp.processEvents()
    assert _frame_data(worker.sent[-1])[0] == SubCmd.OTA_END

    view._on_command_response(
        CommandResponse(int(RespCode.INTERNAL_ERROR), "DEBUG_ENABLE=failed")
    )
    assert view._ota_controller.state is OtaState.END

    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OTA_END=VERIFIED"))
    assert view._ota_controller.state is OtaState.WAIT_REBOOT
    assert view._ota_controller.active is True

    view._on_frame_received(MetaInfo(2, "0.1.248", "esa01", "sn"))
    assert view._ota_controller.state is OtaState.IDLE
    assert view._ota_controller.active is False
    assert view._ota_status_source == (
        "Firmware {version} was reported, but applying this package was not independently confirmed"
    )
    view.deleteLater()


def test_ota_wait_reboot_can_be_cancelled_without_sending_stale_frame(qapp):
    view, worker = _make_ota_view()
    view._ota_pause_debug_cb.setChecked(False)
    view._on_ota_start()
    view._ota_controller._enter_wait_reboot()
    sent_count = len(worker.sent)

    assert view._ota_controller.state is OtaState.WAIT_REBOOT
    assert view._ota_abort_btn.isEnabled()
    view._on_ota_abort()

    assert view._ota_controller.state is OtaState.IDLE
    assert view._ota_controller.active is False
    assert len(worker.sent) == sent_count
    assert view._ota_status_source == "Aborted by user"
    assert view._ota_status_values == {}
    view.deleteLater()


def test_async_ota_data_timeout_retries_three_times(qapp):
    view, worker = _make_ota_view()
    view._ota_pause_debug_cb.setChecked(False)
    view._ota_controller.configure_engineering_file(b"A" * 100, "esa01.bin")
    view._on_ota_start()
    view._on_command_response(CommandResponse(int(RespCode.SUCCESS), "OTA_BEGIN=READY"))
    qapp.processEvents()

    for _ in range(3):
        view._on_response_timeout()
        assert view._ota_controller.active is True
    view._on_response_timeout()

    data_frames = [frame for frame in worker.sent if _frame_data(frame)[0] == SubCmd.OTA_DATA]
    assert len(data_frames) == 4
    assert view._ota_controller.active is False
    assert "连续超时" in view._ota_status_label.text()
    view.deleteLater()
