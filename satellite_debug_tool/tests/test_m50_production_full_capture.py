"""M50 自动化测试完整采集协商与内容门禁回归。"""
from __future__ import annotations

from pathlib import Path
import struct

import pytest

from satellite_debug_tool.core.comm import UdpEndpointBroker
from satellite_debug_tool.core.production.fleet import (
    FleetController,
    FleetDatagram,
    ProductionCaptureState,
)
from satellite_debug_tool.core.protocol import CmdType, build_frame
from satellite_debug_tool.core.session import EndpointSessionDirectory
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.tests.test_production_fleet import _fast_snr, _identity


ENDPOINT = ("192.168.1.12", 4004)


def _datagram(frame: bytes, sequence: int) -> FleetDatagram:
    return FleetDatagram(
        ENDPOINT,
        frame,
        1_000_000_000 + sequence,
        2_000_000_000 + sequence,
    )


def _capabilities(*, full_capture: bool = True, timestamp_ms: int = 10) -> bytes:
    payload = struct.pack(
        "<BII4fBBB",
        1,
        timestamp_ms,
        0xFF,
        17700.0,
        21200.0,
        27500.0,
        31000.0,
        0x0F,
        0x03,
        0x03 if full_capture else 0x01,
    )
    return build_frame(CmdType.SERVICE_CAPABILITIES, payload)


def _capture_response(request_id: int, *, result_code: int = 0) -> bytes:
    payload = struct.pack(
        "<BIBBIBffBBB",
        1,
        request_id,
        4,
        result_code,
        1 << 6,
        0,
        0.0,
        0.0,
        0,
        0,
        0,
    )
    return build_frame(CmdType.SERVICE_CONTROL_RESPONSE, payload)


def _data_report(timestamp_ms: int = 20) -> bytes:
    return build_frame(
        CmdType.DATA_REPORT,
        struct.pack("<IBBf", timestamp_ms, 1, 0, 12.0),
    )


def _capture_request_id(frame: bytes) -> int:
    schema, request_id, operation, _profile = struct.unpack("<BIBB", frame[6:13])
    assert schema == 1
    assert operation == 4
    return request_id


def _controller(monkeypatch, *, timestamp_ms: int = 1):
    broker = UdpEndpointBroker(local_port=0)
    sent: list[bytes] = []
    monkeypatch.setattr(broker, "start_broker", lambda: True)
    monkeypatch.setattr(
        broker,
        "send_to",
        lambda endpoint, frame: sent.append(bytes(frame)) or endpoint == ENDPOINT,
    )
    directory = EndpointSessionDirectory(broker)
    controller = FleetController(
        discovery_cidr="192.168.1.0/24",
        local_port=0,
        broker=broker,
        session_directory=directory,
    )
    controller.start()
    controller.inject_datagram(_datagram(_identity("M50-FULL", timestamp=timestamp_ms), 1))
    controller.inject_datagram(_datagram(_capabilities(timestamp_ms=(timestamp_ms + 1) & 0xFFFFFFFF), 2))
    return controller, broker, sent


def test_production_requires_ack_and_real_analysis_frame_before_ready(
    qapplication_session, tmp_path: Path, monkeypatch
) -> None:
    controller, broker, sent = _controller(monkeypatch)
    try:
        assert controller.arm_batch_recording("M50-BATCH", tmp_path)
        session = controller.sessions()[0]
        request = sent[-1]
        assert request[3] == CmdType.SERVICE_CONTROL_REQUEST
        assert session.capture_state is ProductionCaptureState.REQUESTING
        assert not session.capture_ready

        # A frame preceding this request's exact ACK is not full-capture proof.
        controller.inject_datagram(_datagram(_data_report(15), 3))
        assert session.capture_state is ProductionCaptureState.REQUESTING
        assert not session.capture_ready

        controller.inject_datagram(
            _datagram(_capture_response(_capture_request_id(request)), 4)
        )
        assert session.capture_state is ProductionCaptureState.WAITING_DATA
        assert not session.capture_ready

        analysis_frame = _data_report()
        controller.inject_datagram(_datagram(analysis_frame, 5))
        assert session.capture_state is ProductionCaptureState.READY
        assert session.capture_ready

        assert controller.finalize_recordings()[ENDPOINT] is None
        restore_request = sent[-1]
        controller.inject_datagram(
            _datagram(_capture_response(_capture_request_id(restore_request)), 6)
        )
        path = controller.finalize_recordings()[ENDPOINT]
        assert path is not None
        sdb = DataImporter.open_sdb(path)
        assert any(type(item).__name__ == "DataReport" for item in sdb.iter_records())
        events = [
            item
            for item in sdb.metadata_events
            if item.get("type") == "production_capture_profile"
        ]
        assert [item["event"] for item in events] == [
            "requested",
            "confirmed",
            "data_observed",
            "restore_requested",
            "restored",
        ]
    finally:
        controller.stop()
        broker.stop_broker()


def test_production_rejects_summary_only_capture_capability(
    qapplication_session, tmp_path: Path, monkeypatch
) -> None:
    controller, broker, sent = _controller(monkeypatch)
    try:
        session = controller.sessions()[0]
        controller.inject_datagram(_datagram(_capabilities(full_capture=False), 3))
        sent_before = len(sent)
        assert controller.arm_batch_recording("M50-UNSUPPORTED", tmp_path)
        assert session.capture_state is ProductionCaptureState.UNSUPPORTED
        assert not session.capture_ready
        assert len(sent) == sent_before
    finally:
        controller.stop()
        broker.stop_broker()


def test_new_session_generation_requires_fresh_capture_ack_and_data(
    qapplication_session, tmp_path: Path, monkeypatch
) -> None:
    controller, broker, sent = _controller(monkeypatch)
    try:
        assert controller.arm_batch_recording("M50-RESTART", tmp_path)
        session = controller.sessions()[0]
        first_request = sent[-1]
        controller.inject_datagram(
            _datagram(_capture_response(_capture_request_id(first_request)), 3)
        )
        controller.inject_datagram(_datagram(_data_report(), 4))
        assert session.capture_ready

        session.core.generation_changed.emit(2, ENDPOINT)
        assert not session.capture_ready
        controller.inject_datagram(_datagram(_fast_snr(30, 11.0), 5))
        second_request = sent[-1]
        assert second_request != first_request
        assert session.capture_state is ProductionCaptureState.REQUESTING

        controller.inject_datagram(
            _datagram(_capture_response(_capture_request_id(second_request)), 6)
        )
        assert session.capture_state is ProductionCaptureState.WAITING_DATA
        controller.inject_datagram(_datagram(_data_report(40), 7))
        assert session.capture_ready
    finally:
        controller.stop()
        broker.stop_broker()


def test_abort_finalization_keeps_data_when_restore_ack_cannot_arrive(
    qapplication_session, tmp_path: Path, monkeypatch
) -> None:
    controller, broker, sent = _controller(monkeypatch)
    try:
        assert controller.arm_batch_recording("M50-ABORT", tmp_path)
        session = controller.sessions()[0]
        request = sent[-1]
        controller.inject_datagram(
            _datagram(_capture_response(_capture_request_id(request)), 3)
        )
        controller.inject_datagram(_datagram(_data_report(), 4))
        assert session.capture_ready

        path = controller.finalize_recordings(
            wait_for_capture_restore=False
        )[ENDPOINT]
        assert path is not None
        assert not session.recording_armed
        assert DataImporter.open_sdb(path).quality["complete"] is True
    finally:
        controller.stop()
        broker.stop_broker()


def _imu_trace(received_ms: int) -> bytes:
    header = struct.pack('<4BIIQQI', 1, 1, 3, 1, 1, 1,
                         received_ms * 1000, received_ms * 1000, 0)
    payload = header + struct.pack('<BBH8f', 0, 0, 0, .005, 25., 1., 2., 3., 4., 5., 6.)
    return build_frame(CmdType.INS_TRACE, payload)


def _capture_requests(sent):
    return [frame for frame in sent
            if frame[3] == CmdType.SERVICE_CONTROL_REQUEST and frame[11] == 4]


def _ready_before_power_cycle(monkeypatch, tmp_path, *, old_fast_ms=14377):
    from types import SimpleNamespace
    from satellite_debug_tool.core.production import fleet as fleet_module
    from satellite_debug_tool.tests.test_production_fleet import _hardware_identity
    # 仅复用现有monkeypatch；发包/ACK处理的单调时刻晚于本次新Service接收。
    monkeypatch.setattr(fleet_module, 'time', SimpleNamespace(monotonic_ns=lambda: 2_000_000_024))
    initial_ms = max(1, old_fast_ms - 1000)
    controller, broker, sent = _controller(monkeypatch, timestamp_ms=initial_ms)
    controller.inject_datagram(_datagram(_hardware_identity((1, 2, 3), timestamp=initial_ms), 3))
    controller.inject_datagram(_datagram(_fast_snr(old_fast_ms, 10.), 4))
    assert controller.arm_batch_recording('POWER-CYCLE', tmp_path)
    session = controller.sessions()[0]
    old_id = _capture_request_id(_capture_requests(sent)[-1])
    controller.inject_datagram(_datagram(_capture_response(old_id), 5))
    controller.inject_datagram(_datagram(_data_report(old_fast_ms - 10), 6))
    assert session.capture_ready
    return controller, broker, sent, session, old_id


def _fresh_power_identity(controller, *, timestamp=14559, sequence=21, uid=(1, 2, 3)):
    from satellite_debug_tool.tests.test_production_fleet import _hardware_identity
    controller.inject_datagram(_datagram(_identity('M50-FULL', timestamp=timestamp), sequence))
    controller.inject_datagram(_datagram(_hardware_identity(uid, timestamp=timestamp), sequence + 1))
    controller.inject_datagram(_datagram(_fast_snr(timestamp, 10.), sequence + 2))


def test_power_cycle_same_scope_requires_new_identity_request_ack_and_current_data(
    qapplication_session, tmp_path, monkeypatch,
):
    controller, broker, sent, session, old_id = _ready_before_power_cycle(monkeypatch, tmp_path)
    try:
        scope = session._capture_scope()
        restored_ns = 2_000_000_020
        session.prepare_capture_after_power_cycle(restored_ns)
        assert not session.capture_ready
        assert len(_capture_requests(sent)) == 1  # ON时不能向尚在Boot的缓存能力立即申请。
        session.prepare_capture_after_power_cycle(restored_ns)
        assert len(_capture_requests(sent)) == 1
        _fresh_power_identity(controller)
        assert session._capture_scope() == scope  # 实测14.377→14.559，无需伪造epoch。
        requests = _capture_requests(sent)
        assert len(requests) == 2
        new_id = _capture_request_id(requests[-1])
        assert new_id != old_id
        controller.inject_datagram(_datagram(_capture_response(old_id), 24))
        controller.inject_datagram(_datagram(_data_report(14570), 25))
        assert not session.capture_ready
        controller.inject_datagram(_datagram(_capture_response(new_id), 26))
        controller.inject_datagram(_datagram(_imu_trace(14367), 27))
        assert not session.capture_ready  # Trace不走TelemetryClock，必须由capture时间门拒绝。
        controller.inject_datagram(_datagram(_imu_trace(100000), 28))
        assert not session.capture_ready  # 旧boot高uptime也不能只因>新基线就通过。
        controller.inject_datagram(_datagram(_imu_trace(14580), 29))
        assert session.capture_ready
        controller.inject_datagram(_datagram(_fast_snr(14599, 10.), 30))
        assert len(_capture_requests(sent)) == 2
        session.prepare_capture_after_power_cycle(restored_ns)
        assert session.capture_ready
        path = controller.finalize_recordings(wait_for_capture_restore=False)[ENDPOINT]
        events = [e for e in DataImporter.open_sdb(path).metadata_events
                  if e.get('type') == 'production_capture_profile'
                  and e.get('restored_monotonic_ns') == restored_ns]
        proofs = [e for e in events if e['event'] in {'requested', 'confirmed', 'data_observed'}]
        assert [e['event'] for e in proofs] == ['requested', 'confirmed', 'data_observed']
        assert {e['request_id'] for e in proofs} == {new_id}
        count_after_finalize = len(_capture_requests(sent))
        session.prepare_capture_after_power_cycle(restored_ns + 100)
        assert len(_capture_requests(sent)) == count_after_finalize  # 停止录制后的晚回执不能恢复请求。
    finally:
        controller.stop()
        broker.stop_broker()


def test_power_cycle_waits_for_both_fresh_identity_cursors(qapplication_session, tmp_path, monkeypatch):
    from satellite_debug_tool.tests.test_production_fleet import _hardware_identity
    controller, broker, sent, session, _ = _ready_before_power_cycle(monkeypatch, tmp_path)
    try:
        session.prepare_capture_after_power_cycle(2_000_000_020)
        # 主线程迟到的断电前回调，即使Store的revision增加也不满足ON后到达边界。
        controller.inject_datagram(_datagram(_identity('M50-FULL', timestamp=14377), 10))
        controller.inject_datagram(_datagram(_hardware_identity((1, 2, 3), timestamp=14377), 11))
        controller.inject_datagram(_datagram(_fast_snr(14559, 10.), 23))
        assert len(_capture_requests(sent)) == 1
        controller.inject_datagram(_datagram(_identity('M50-FULL', timestamp=14560), 24))
        assert len(_capture_requests(sent)) == 1
        controller.inject_datagram(_datagram(_hardware_identity((1, 2, 3), timestamp=14560), 25))
        assert len(_capture_requests(sent)) == 2
    finally:
        controller.stop()
        broker.stop_broker()


def test_power_cycle_changed_uid_cannot_reuse_cached_identity_authorization(
    qapplication_session, tmp_path, monkeypatch,
):
    controller, broker, sent, session, _ = _ready_before_power_cycle(monkeypatch, tmp_path)
    try:
        session.prepare_capture_after_power_cycle(2_000_000_020)
        _fresh_power_identity(controller, uid=(9, 9, 9))
        assert not session.capture_ready
        assert len(_capture_requests(sent)) == 1
    finally:
        controller.stop()
        broker.stop_broker()


@pytest.mark.parametrize('received_ms', [14367, 100000])
def test_power_cycle_stale_or_future_data_report_cannot_confirm_capture(
    qapplication_session, tmp_path, monkeypatch, received_ms,
):
    controller, broker, sent, session, _ = _ready_before_power_cycle(monkeypatch, tmp_path)
    try:
        session.prepare_capture_after_power_cycle(2_000_000_020)
        _fresh_power_identity(controller)
        new_id = _capture_request_id(_capture_requests(sent)[-1])
        controller.inject_datagram(_datagram(_capture_response(new_id), 24))
        controller.inject_datagram(_datagram(_data_report(received_ms), 25))
        assert not session.capture_ready
    finally:
        controller.stop()
        broker.stop_broker()


def test_recovery_analysis_must_be_received_after_request_and_ack(
    qapplication_session, tmp_path, monkeypatch,
):
    from types import SimpleNamespace
    from satellite_debug_tool.core.production import fleet as fleet_module
    controller, broker, sent, session, _ = _ready_before_power_cycle(monkeypatch, tmp_path)
    try:
        session.prepare_capture_after_power_cycle(2_000_000_020)
        _fresh_power_identity(controller)
        new_id = _capture_request_id(_capture_requests(sent)[-1])
        # ACK处理时刻推进；已在ACK之前接收、之后才处理的分析包仍不能确认。
        monkeypatch.setattr(fleet_module, 'time', SimpleNamespace(monotonic_ns=lambda: 2_000_000_030))
        controller.inject_datagram(_datagram(_capture_response(new_id), 29))
        controller.inject_datagram(_datagram(_imu_trace(14580), 28))
        assert not session.capture_ready
        controller.inject_datagram(_datagram(_imu_trace(14581), 30))
        assert not session.capture_ready  # 接收时刻等于边界也失败关闭。
        controller.inject_datagram(_datagram(_imu_trace(14582), 31))
        assert session.capture_ready
    finally:
        controller.stop()
        broker.stop_broker()


def test_recovery_accepts_u32_service_wrap_with_u64_trace_time(
    qapplication_session, tmp_path, monkeypatch,
):
    controller, broker, sent, session, _ = _ready_before_power_cycle(
        monkeypatch, tmp_path, old_fast_ms=0xFFFFFFE0,
    )
    try:
        session.prepare_capture_after_power_cycle(2_000_000_020)
        _fresh_power_identity(controller, timestamp=0xFFFFFFF0)
        new_id = _capture_request_id(_capture_requests(sent)[-1])
        controller.inject_datagram(_datagram(_capture_response(new_id), 25))
        controller.inject_datagram(_datagram(_imu_trace(0x100000010), 26))
        assert session.capture_ready
    finally:
        controller.stop()
        broker.stop_broker()


def test_power_cycle_capture_borrows_frozen_batch_operation_without_releasing_it(
    qapplication_session, tmp_path, monkeypatch,
):
    from satellite_debug_tool.core.session import RuntimeOperationGateState
    controller, broker, sent, session, _ = _ready_before_power_cycle(monkeypatch, tmp_path)
    try:
        controller.freeze_batch_participants((session.identity_key,))
        owner = controller._batch_gate_owner
        gate = session.runtime.operation_gate
        assert gate.state is RuntimeOperationGateState.PRODUCTION_FROZEN
        session.prepare_capture_after_power_cycle(2_000_000_020)
        _fresh_power_identity(controller)
        assert session.capture_state is ProductionCaptureState.REQUESTING
        request_id = _capture_request_id(_capture_requests(sent)[-1])
        controller.inject_datagram(_datagram(_capture_response(request_id), 25))
        controller.inject_datagram(_datagram(_imu_trace(14580), 26))
        assert session.capture_ready
        assert gate.state is RuntimeOperationGateState.PRODUCTION_FROZEN
        assert gate.owner == owner
        assert gate.current_operation.request_id is None  # 不覆盖父批次的操作上下文。
    finally:
        controller.stop()
        broker.stop_broker()


@pytest.mark.parametrize('failure', ['reject', 'timeout', 'reset', 'send', 'destroy'])
def test_borrowed_capture_failure_preserves_parent_freeze_and_exclusion(
    qapplication_session, tmp_path, monkeypatch, failure,
):
    from PySide6.QtCore import QCoreApplication, QEvent
    from satellite_debug_tool.core.session import RuntimeOperationGateState
    controller, broker, sent, session, _ = _ready_before_power_cycle(monkeypatch, tmp_path)
    try:
        controller.freeze_batch_participants((session.identity_key,))
        owner = controller._batch_gate_owner
        gate = session.runtime.operation_gate
        if failure == 'send':
            def send(endpoint, frame):
                sent.append(bytes(frame))
                return not (frame[3] == CmdType.SERVICE_CONTROL_REQUEST and frame[11] == 4)
            monkeypatch.setattr(broker, 'send_to', send)
        session.prepare_capture_after_power_cycle(2_000_000_020)
        _fresh_power_identity(controller)
        capture = session._capture_controller
        if failure == 'reject':
            controller.inject_datagram(_datagram(
                _capture_response(capture.pending_request_id, result_code=4), 25,
            ))
        elif failure == 'timeout':
            capture.expire()
        elif failure == 'reset':
            capture.reset()
        elif failure == 'destroy':
            capture.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert gate.state is RuntimeOperationGateState.PRODUCTION_FROZEN
        assert gate.owner == owner
        assert gate.current_operation.request_id is None
        assert not session.attachment.new_operation_gateway().try_acquire_operation(
            object(), purpose='other-owner',
        )
        if failure == 'destroy':
            session._capture_controller = None
    finally:
        controller.stop()
        broker.stop_broker()


@pytest.mark.parametrize('wrong', ['owner', 'capability'])
def test_capture_cannot_borrow_wrong_batch_owner_or_capability(
    qapplication_session, tmp_path, monkeypatch, wrong,
):
    from satellite_debug_tool.core.session import CaptureProfileController, RuntimeOperationGateState
    controller, broker, sent, session, _ = _ready_before_power_cycle(monkeypatch, tmp_path)
    extra = None
    try:
        controller.freeze_batch_participants((session.identity_key,))
        owner = controller._batch_gate_owner
        gateway = session.attachment.new_operation_gateway()
        if wrong == 'capability':
            extra = controller._session_directory.acquire_production_attachment(ENDPOINT, 'another-production')
            _fresh_power_identity(controller)
            gateway = extra.new_operation_gateway()
        capture = CaptureProfileController(
            session.core, operation_gateway=gateway, recorder_kind=None,
            retain_full_capture_operation=False,
        )
        before = len(sent)
        assert not capture.request(True, operation_owner=object() if wrong == 'owner' else owner)
        assert len(sent) == before
        assert session.runtime.operation_gate.state is RuntimeOperationGateState.PRODUCTION_FROZEN
        assert session.runtime.operation_gate.owner == owner
        capture.deleteLater()
    finally:
        if extra is not None:
            extra.release()
        controller.stop()
        broker.stop_broker()


def test_off_presence_expiry_restores_same_batch_freeze_after_new_identity(
    qapplication_session, tmp_path, monkeypatch,
):
    from types import SimpleNamespace
    from PySide6.QtTest import QTest
    from satellite_debug_tool.core.production import fleet as fleet_module
    from satellite_debug_tool.core.session import RuntimeOperationGateState
    controller, broker, sent, session, _ = _ready_before_power_cycle(monkeypatch, tmp_path)
    try:
        controller.freeze_batch_participants((session.identity_key,))
        owner = controller._batch_gate_owner
        runtime = session.runtime
        runtime._presence_timer.stop()  # 本用例显式给Runtime输入同一单调轴的OFF空窗。
        runtime.refresh_presence(now_monotonic=6.0)  # 末Service在2s，OFF空窗4s>3s。
        assert runtime.operation_gate.state is RuntimeOperationGateState.TERMINATING
        monkeypatch.setattr(fleet_module, 'time', SimpleNamespace(monotonic_ns=lambda: 6_000_000_024))
        session.prepare_capture_after_power_cycle(6_000_000_020)
        _fresh_power_identity(controller, sequence=4_000_000_021)
        assert len(_capture_requests(sent)) == 1
        assert runtime.operation_gate.state is RuntimeOperationGateState.TERMINATING
        # 真实Qt终止宽限到期；不能由capture释放父租约或在TERMINATING时抢占。
        from time import monotonic
        deadline = monotonic() + runtime._terminal_timeout.interval() / 1000 + 1
        while runtime.operation_gate.state is RuntimeOperationGateState.TERMINATING and monotonic() < deadline:
            QTest.qWait(10)
        assert runtime.operation_gate.state is RuntimeOperationGateState.IDLE
        controller.inject_datagram(_datagram(_fast_snr(14600, 10.), 4_000_000_100))
        assert runtime.operation_gate.state is RuntimeOperationGateState.PRODUCTION_FROZEN
        assert runtime.operation_gate.owner == owner
        assert len(_capture_requests(sent)) == 2
        request_id = _capture_request_id(_capture_requests(sent)[-1])
        controller.inject_datagram(_datagram(_capture_response(request_id), 4_000_000_101))
        controller.inject_datagram(_datagram(_imu_trace(14620), 4_000_000_102))
        assert session.capture_ready
        assert runtime.operation_gate.owner == owner
        controller.finalize_recordings(wait_for_capture_restore=False)
        assert session._capture_batch_operation is None
        count = len(_capture_requests(sent))
        controller.inject_datagram(_datagram(_fast_snr(14640, 10.), 4_000_000_103))
        assert len(_capture_requests(sent)) == count
        assert runtime.operation_gate.owner != owner
    finally:
        controller.stop()
        broker.stop_broker()


def test_capture_borrow_on_idle_cannot_create_unreleased_mutating_operation(
    qapplication_session, tmp_path, monkeypatch,
):
    from satellite_debug_tool.core.session import RuntimeOperationGateState
    controller, broker, sent, session, _ = _ready_before_power_cycle(monkeypatch, tmp_path)
    try:
        gate = session.runtime.operation_gate
        assert gate.state is RuntimeOperationGateState.IDLE
        count = len(sent)
        assert not session._capture_controller.request(True, operation_owner=object())
        assert len(sent) == count
        assert gate.state is RuntimeOperationGateState.IDLE
    finally:
        controller.stop()
        broker.stop_broker()


def test_restore_busy_before_request_allocation_has_no_stale_capture_nonce(
    qapplication_session, tmp_path, monkeypatch,
):
    controller, broker, sent, session, old_id = _ready_before_power_cycle(monkeypatch, tmp_path)
    owner = object()
    gateway = session.attachment.new_operation_gateway()
    try:
        assert gateway.try_acquire_operation(owner, purpose='concurrent-operation')
        before = len(_capture_requests(sent))
        path = session.finalize_recording(wait_for_capture_restore=False)
        assert path is not None
        assert len(_capture_requests(sent)) == before  # BUSY发生在分配请求/发送之前。
        assert session.runtime.operation_gate.owner is owner
        events = [e for e in DataImporter.open_sdb(path).metadata_events
                  if e.get('type') == 'production_capture_profile']
        assert not any(e['event'] == 'restore_requested' for e in events)
        failure = [e for e in events if e['event'] == 'restore_failed'][-1]
        assert failure['request_id'] is None
        assert failure['request_monotonic_ns'] == 0
        assert failure['request_uptime_ms'] is None
        assert failure['reason'] == 'device_error:BUSY'
        assert old_id is not None
    finally:
        gateway.release_operation(owner)
        controller.stop()
        broker.stop_broker()
