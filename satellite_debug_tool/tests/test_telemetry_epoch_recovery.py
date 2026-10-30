"""M44：通过真实会话入库复现同连接时间重建、迟到包与显示恢复。"""
from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtWidgets import QApplication

import pytest

from satellite_debug_tool.core.protocol import (
    ChannelSample, DataReport, Heartbeat, MetaInfo, ServiceFastState,
    ServiceIdentity, ServiceComponentHealth, ServiceComponentValue,
)
from satellite_debug_tool.core.session import DeviceSessionCore
from satellite_debug_tool.core.session.telemetry_clock import TelemetryClock
from satellite_debug_tool.core.product import CustomerPlaybackChannel
from satellite_debug_tool.core.playback import build_customer_playback, build_engineering_playback
from satellite_debug_tool.tests.test_customer_overview_view import _LiveDouble, _SettingsDouble, _channel
from satellite_debug_tool.tests.test_customer_playback_view import _fast, _data
from satellite_debug_tool.io.data_recorder import DataRecorder, SDB_VERSION_V3
from satellite_debug_tool.ui.customer_overview_view import CustomerOverviewView


def fast(t: int, v: float = 18.5) -> ServiceFastState:
    return ServiceFastState(1, t, 1 << 11, 0, 0, False, 0, 0, False, 0., 0., 0., 0., 0., v)


def component(t: int, v: float) -> ServiceComponentHealth:
    value = ServiceComponentValue(2, False, v, 0., 0)
    return ServiceComponentHealth(1, t, value, value, value)


def core_live() -> tuple[DeviceSessionCore, _LiveDouble]:
    core = DeviceSessionCore(endpoint=('192.168.1.12', 4004))
    core.begin_connection(endpoint=core.endpoint, transport=object(), sender=lambda _: True, handshake_enabled=False)
    live = _LiveDouble()
    live.products, live.data, live.states = core.product_store, core.data_store, core.state_store
    live.profiles = core.profile_store
    return core, live


@pytest.mark.parametrize('old_value', [0., math.nan, math.inf, -math.inf])
@pytest.mark.parametrize('clear_display', [False, True])
def test_same_connection_new_segment_restores_snr_temperature_and_axes(qapplication_session: QApplication, old_value: float, clear_display: bool) -> None:
    core, live = core_live()
    core.apply_records((ServiceIdentity(1, 600000, 0x17, 'AFD01', 'M44', '0.0.132', '', 3),
                        fast(600000, old_value), component(600000, 30.),
                        DataReport(600000, [ChannelSample(8, old_value)])), received_monotonic=10.)
    scope = core.device_scope()
    generation = core.generation
    if clear_display:
        core.product_store.clear_history()
        core.data_store.clear()
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)
    view._snr_plot.setYRange(-1, 1, padding=0)
    # 三个推进的新流样本确认采集段。确认前当前值与历史同时保留原事实。
    for i in range(3):
        core.apply_records((fast(1000 + i * 100, 18.5 + i),
                            component(1000 + i * 100, 50. + i),
                            DataReport(1000 + i * 100, [ChannelSample(8, 18.5 + i)])),
                           received_monotonic=11. + i * .1)
    view.refresh()
    times, values = view._snr_curve.getData()
    assert values.tolist() == pytest.approx([18.5, 19.5, 20.5])
    assert times.tolist() == pytest.approx([1., 1.1, 1.2])
    for key in ('converter', 'tx_array', 'rx_array'):
        assert core.product_store.component_temperature_history(key)[1][-1] == 52.
    assert core.data_store.get_channel_by_id(8).get_latest()[1] == 20.5
    low, high = view._snr_plot.getViewBox().viewRange()[1]
    assert low <= 18.5 < 20.5 <= high
    assert core.connected and core.generation == generation
    assert not core.operation_scope_matches(scope)
    # 迟到的旧段高 uptime 包不能重新污染当前值与曲线。
    core.apply_records((fast(600100, -99.), component(600100, -99.),
                        DataReport(600100, [ChannelSample(8, -99.)])), received_monotonic=11.3)
    assert core.product_store.snr_history()[1][-1] == 20.5
    assert core.data_store.get_channel_by_id(8).get_latest()[1] == 20.5
    view.close()


def test_debug_only_recovery_uses_same_epoch_owner(qapplication_session: QApplication) -> None:
    core, live = core_live()
    live.profiles.apply_meta(MetaInfo(2, '0.0.132', 'afd01', 'M44'))
    live.profiles.apply_channel_define('afd01', 1, [_channel(8, 'snr')])
    for i, t in enumerate([600000, 1000, 1100, 1200]):
        core.apply_records((DataReport(t, [ChannelSample(8, float(i))]),), received_monotonic=10+i*.1)
    view = CustomerOverviewView(live, _SettingsDouble(), enable_3d=False)
    view.refresh()
    assert view._snr_curve.getData()[1].tolist() == [1., 2., 3.]
    view.close()


def test_single_old_packet_preserves_current_scalar_and_history(qapplication_session: QApplication) -> None:
    core, _ = core_live()
    core.apply_records((fast(600000, 10.),), received_monotonic=10.)
    epoch = core.product_store.telemetry_epoch
    core.apply_records((fast(599000, -99.),), received_monotonic=10.1)
    assert core.product_store.snr_history()[1].tolist() == [10.]
    assert core.product_store.snapshot(now_monotonic=10.1).operation.snr_db.value == 10.
    core.apply_records((fast(600100, 11.),), received_monotonic=10.2)
    assert core.product_store.telemetry_epoch == epoch
    assert core.product_store.snr_history()[1].tolist() == [10., 11.]


def test_duplicates_and_u32_wrap_keep_epoch(qapplication_session: QApplication) -> None:
    core, _ = core_live()
    epoch = core.product_store.telemetry_epoch
    for i, t in enumerate([0xFFFFFF00, 0x100, 0x100, 0x200]):
        core.apply_records((fast(t),), received_monotonic=10+i)
    assert core.product_store.telemetry_epoch == epoch
    assert core.product_store.snr_history()[0][-1] == pytest.approx((2**32+512)/1000)
    for i in range(4):
        core.apply_records((fast(0),), received_monotonic=20+i*.1)
    assert core.product_store.telemetry_epoch == epoch


@pytest.mark.parametrize('pause', [.1, 20.])
def test_heartbeat_alone_confirms_epoch_and_other_endpoint_stays_independent(qapplication_session: QApplication, pause: float) -> None:
    core, _ = core_live()
    other, _ = core_live()
    for device in (core, other):
        device.apply_records((Heartbeat(600000, 0, 0, 0, 0), fast(600000, 10.)), received_monotonic=10.)
    epoch = core.product_store.telemetry_epoch
    for i in range(3):
        core.apply_records((Heartbeat(1000+i*1000, 0, 0, 0, 0),), received_monotonic=10+pause+i)
    core.apply_records((fast(3100, 19.),), received_monotonic=12.1+pause)
    assert core.product_store.telemetry_epoch == epoch+1
    assert core.product_store.snr_history()[1].tolist() == [19.]
    assert other.product_store.snr_history()[1].tolist() == [10.]


def test_candidate_expiry_and_old_forward_packet_cancel_restart() -> None:
    clock = TelemetryClock()
    assert clock.admit(fast(600000), 10).records
    assert not clock.admit(fast(1000), 11).records
    assert not clock.admit(fast(1100), 12).records
    assert clock.admit(fast(1200), 17).starts_epoch is False
    assert clock.admit(fast(600100), 18).records
    assert not clock.admit(fast(1300), 19).starts_epoch


@pytest.mark.parametrize('customer', [False, True])
def test_recording_retains_raw_frames_and_playback_splits_epochs(tmp_path: Path, customer: bool) -> None:
    path = tmp_path/'reboot.sdb'
    recorder = DataRecorder(path, format_version=SDB_VERSION_V3)
    assert recorder.start()
    frames = [_fast(t, v, 0.) if customer else _data(t, [(8, v)])
              for t, v in [(600000, 0.), (1000, 18.), (1100, 19.), (1200, 20.), (600100, -99.), (1300, 21.)]]
    for i, frame in enumerate(frames):
        assert recorder.write_frame(frame, host_timestamp_ns=10_000_000_000+i*100_000_000)
    assert recorder.stop()
    result = (build_customer_playback if customer else build_engineering_playback)(path)
    try:
        channel_id = int(CustomerPlaybackChannel.SNR) if customer else 8
        data = result.provider.load_window().get_channel_by_id(channel_id)
        values = data.get_values().tolist()
        times = data.get_times().tolist()
        assert values[0] == 0. and values[-1] == 21.
        assert -99. not in values
        assert any(math.isnan(v) for v in values)
        assert times == sorted(times)
        assert len(tuple(result.sdb.iter_timed_records())) == 6
    finally:
        result.close()


def test_ota_waiting_for_return_survives_telemetry_epoch(qapplication_session: QApplication) -> None:
    from satellite_debug_tool.tests.test_device_session_core import _ota_product_session, _verified_package
    from satellite_debug_tool.core.session import OtaStatus
    core, ota, statuses = _ota_product_session()
    core.apply_records((fast(600000),), received_monotonic=10.)
    token = ota.configure_verified_package(_verified_package('0.0.131'))
    assert token is not None and ota.start(pause_debug=False, required_artifact=token)
    ota._enter_wait_reboot()
    for i in range(3):
        core.apply_records((fast(1000+i*100),), received_monotonic=11+i*.1)
    assert ota.active
    assert statuses[-1] is not OtaStatus.TARGET_VERSION_CONFIRMED
    core.apply_records((MetaInfo(2, '0.0.131', 'afd01', 'AFD01-001'),
                        ServiceIdentity(1, 1300, 0x17, 'AFD01', 'AFD01-001', '0.0.131', '', 8)),
                       received_monotonic=11.3)
    assert statuses[-1] is OtaStatus.TARGET_VERSION_CONFIRMED
    assert not ota.active


def test_runtime_epoch_reauthorizes_identity_and_preserves_transport(qapplication_session: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    from satellite_debug_tool.tests.test_m25_session_runtime import _runtime_for_controller, _datagram, _meta_info
    runtime, capability, _ = _runtime_for_controller(monkeypatch, suffix=144)
    endpoint = runtime.endpoint
    generation = runtime.core.generation
    epoch = runtime.presence_epoch
    assert runtime.identity_authorization_scope is not None
    raw_seen = []
    runtime.datagram_received.connect(raw_seen.append)
    for i, t in enumerate([600000, 1000, 1100, 1200]):
        runtime.feed_datagram(_datagram(endpoint, _fast(t, 18., 0.), monotonic_s=2+i*.1))
    assert len(raw_seen) == 4
    assert runtime.core.generation == generation
    assert runtime.presence_epoch == epoch+1
    assert runtime.identity_authorization_scope is None
    assert runtime._valid_capability_state(capability) is not None
    runtime.feed_datagram(_datagram(endpoint, _meta_info('AFD01-144'), monotonic_s=2.4))
    assert runtime.identity_authorization_scope is not None


def test_parameter_write_finishes_unconfirmed_at_new_epoch(qapplication_session: QApplication) -> None:
    from satellite_debug_tool.core.protocol import ProfileSemanticsReport, ProfileSemanticCapabilityEntry
    from satellite_debug_tool.core.session import ParameterController, ParameterStatus, ParameterOperation
    core, _ = core_live()
    core.profile_store.apply_meta(MetaInfo(2, '0.0.140', 'afd01', 'M44'))
    core.profile_store.apply_profile_semantics('afd01', ProfileSemanticsReport(
        table_ver=1, capabilities=[ProfileSemanticCapabilityEntry('parameters', True),
                                  ProfileSemanticCapabilityEntry('command_response_context', True)]))
    core.apply_records((fast(600000),), received_monotonic=10.)
    controller = ParameterController(core)
    statuses = []
    controller.parameter_status_changed.connect(lambda _name, status, _values: statuses.append(status))
    assert controller.write('modem_baud', 0, '115200')
    for i in range(3):
        core.apply_records((fast(1000+i*100),), received_monotonic=11+i*.1)
    assert controller.operation is ParameterOperation.IDLE
    assert statuses[-1] is ParameterStatus.SESSION_CHANGED
    assert not core.device_transaction_active


def test_long_running_device_restart_and_late_half_range_packet(qapplication_session: QApplication) -> None:
    core, _ = core_live()
    core.apply_records((fast(3_000_000_000, 1.),), received_monotonic=10.)
    epoch = core.product_store.telemetry_epoch
    for i in range(3):
        core.apply_records((fast(1000+i*100, 18.+i),), received_monotonic=11+i*.1)
    assert core.product_store.telemetry_epoch == epoch+1
    assert core.product_store.snr_history()[0].tolist() == pytest.approx([1., 1.1, 1.2])
    for i in range(3):
        core.apply_records((fast(3_000_000_100+i*100, -99.),), received_monotonic=11.3+i*.1)
    assert core.product_store.snr_history()[1][-1] == 20.


def test_confirmed_epoch_clock_continues_through_long_uptime_and_wrap() -> None:
    clock = TelemetryClock()
    clock.admit(fast(600000), 0.)
    for i in range(3):
        clock.admit(fast(1000+i*100), 1.+i*.1)
    # 每天的真实经过时间推进围栏；25 天后继续接纳，49.7 天回绕保持连续。
    for day in range(1, 52):
        raw = (1200 + day*86_400_000) % 2**32
        admitted = clock.admit(fast(raw), 1.2+day*86400)
        assert admitted.records and not admitted.starts_epoch
