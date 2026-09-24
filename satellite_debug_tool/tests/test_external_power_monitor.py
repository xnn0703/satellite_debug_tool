"""Device-bound external PSW owner, Store and Customer presentation tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QElapsedTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from satellite_debug_tool.core.external_power_monitor import (
    EXTERNAL_POWER_HISTORY_SECONDS,
    ExternalPowerAction,
    ExternalPowerConfig,
    ExternalPowerPhase,
    ExternalPowerSample,
    ExternalPowerSnapshot,
    ExternalPowerStore,
    ExternalPowerWorker,
)
from satellite_debug_tool.core.production.power_supply import (
    PowerIdentity,
    PowerSupplyError,
    normalize_power_identity_field,
)
from satellite_debug_tool.ui.external_power_window import ExternalPowerHistoryWindow
from satellite_debug_tool.ui.customer_session_bundle import (
    CustomerEndpointSessionBundleFactory,
)


def _identity() -> PowerIdentity:
    return PowerIdentity("GW-INSTEK", "PSW 80-27", "PSW1234", "1.70", "raw")


def _sample(
    monotonic_ns: int,
    *,
    voltage: float = 12.0,
    current: float = 1.5,
    generation: int = 1,
) -> ExternalPowerSample:
    return ExternalPowerSample(
        host_timestamp_ns=1_800_000_000_000_000_000 + monotonic_ns,
        monotonic_ns=monotonic_ns,
        connection_generation=generation,
        identity=_identity(),
        voltage_v=voltage,
        current_a=current,
        power_w=voltage * current,
        output_enabled=True,
        operation_condition=1,
        questionable_condition=0,
        protection_tripped=False,
    )


def test_external_power_config_accepts_only_ipv4_and_fixed_port() -> None:
    ExternalPowerConfig("192.168.1.108").validate()
    with pytest.raises(PowerSupplyError, match="IPv4"):
        ExternalPowerConfig("not-an-ip").validate()
    with pytest.raises(PowerSupplyError, match="2268"):
        ExternalPowerConfig("192.168.1.108", port=5025).validate()


def test_store_bounds_history_and_revokes_previous_live_sample(
    qapplication_session,
) -> None:
    store = ExternalPowerStore()
    store.apply_snapshot(
        ExternalPowerSnapshot(
            ExternalPowerPhase.CONNECTING,
            host="192.168.1.108",
        )
    )
    store.apply_sample(_sample(0, voltage=11.0))
    store.apply_sample(
        _sample(
            int(EXTERNAL_POWER_HISTORY_SECONDS * 1e9) + 1,
            voltage=12.0,
            generation=2,
        )
    )

    times, voltages, currents = store.history()
    assert times.size == voltages.size == currents.size == 1
    assert voltages.tolist() == pytest.approx([12.0])
    assert store.snapshot.phase is ExternalPowerPhase.ONLINE

    store.apply_snapshot(
        ExternalPowerSnapshot(
            ExternalPowerPhase.READ_FAILED,
            host="192.168.1.108",
            generation=2,
            error="timeout",
        )
    )
    assert store.snapshot.sample is None
    assert store.snapshot.error == "timeout"

    store.clear()
    assert store.snapshot.phase is ExternalPowerPhase.UNCONFIGURED
    assert not store.history()[0].size


def test_worker_reconnects_with_new_generation_and_stops(
    qapplication_session,
) -> None:
    created: list[object] = []

    class _Session:
        def __init__(self) -> None:
            self.identity = None
            self.reads = 0
            self.closed = False

        def connect(self):
            self.identity = _identity()
            return self.identity

        def read_sample(self, generation: int):
            self.reads += 1
            if self.reads > 1:
                raise PowerSupplyError("forced reconnect")
            return _sample(self.reads, generation=generation)

        def close(self):
            self.closed = True

    def factory(_config):
        session = _Session()
        created.append(session)
        return session

    worker = ExternalPowerWorker(
        ExternalPowerConfig(
            "192.168.1.108",
            poll_interval_s=0.01,
            reconnect_interval_s=0.01,
        ),
        session_factory=factory,
    )
    samples: list[ExternalPowerSample] = []
    worker.sample_ready.connect(samples.append)
    worker.start()
    timer = QElapsedTimer()
    timer.start()
    while len(samples) < 2 and timer.elapsed() < 1000:
        QTest.qWait(5)
    worker.requestInterruption()
    assert worker.wait(1000)

    assert [sample.connection_generation for sample in samples[:2]] == [1, 2]
    assert len(created) >= 2
    assert all(session.closed for session in created)


def test_worker_serializes_control_action_on_the_polling_session(
    qapplication_session,
) -> None:
    executed: list[tuple[ExternalPowerAction, str, int]] = []

    class _Session:
        identity = _identity()

        def connect(self):
            return self.identity

        def read_sample(self, generation: int):
            return _sample(1, generation=generation)

        def execute(self, action, action_id: str, generation: int):
            executed.append((action, action_id, generation))
            return _sample(2, voltage=0.0, current=0.0, generation=generation)

        def close(self):
            return None

    worker = ExternalPowerWorker(
        ExternalPowerConfig(
            "192.168.1.108",
            poll_interval_s=0.01,
            reconnect_interval_s=0.01,
        ),
        session_factory=lambda _config: _Session(),
    )
    outcomes = []
    worker.action_finished.connect(outcomes.append)
    worker.start()
    worker.request_action(ExternalPowerAction.DISABLE, "customer-disable-1")
    timer = QElapsedTimer()
    timer.start()
    while not outcomes and timer.elapsed() < 1000:
        QTest.qWait(5)
    worker.requestInterruption()
    assert worker.wait(1000)

    assert executed == [
        (ExternalPowerAction.DISABLE, "customer-disable-1", 1),
    ]
    assert outcomes[0].succeeded is True
    assert outcomes[0].sample is not None


def test_history_window_renders_voltage_and_current_and_reuses_native_window(
    qapplication_session,
) -> None:
    store = ExternalPowerStore()
    store.apply_sample(_sample(1_000_000_000, voltage=12.0, current=1.0))
    store.apply_sample(_sample(2_000_000_000, voltage=12.2, current=1.2))
    anchor = QWidget()
    anchor.resize(200, 40)
    anchor.show()
    window = ExternalPowerHistoryWindow(store)

    try:
        window.present_near(anchor)
        qapplication_session.processEvents()
        voltage_x, voltage_y = window._voltage_curve.getData()
        current_x, current_y = window._current_curve.getData()
        assert voltage_x.tolist() == pytest.approx(current_x.tolist())
        assert voltage_y.tolist() == pytest.approx([12.0, 12.2])
        assert current_y.tolist() == pytest.approx([1.0, 1.2])
        assert "14.64 W" in window._status.text()
        assert window.isWindow()
        first_identity = id(window)
        window.present_near(anchor)
        assert id(window) == first_identity
    finally:
        window.close()
        anchor.close()


def test_device_power_window_saves_its_own_profile(
    qapplication_session,
) -> None:
    saved: list[dict[str, object]] = []
    window = ExternalPowerHistoryWindow(
        ExternalPowerStore(),
        device_label="192.168.1.12:4004",
        profile={
            "host": "192.168.1.18",
            "voltage_set_v": 12.0,
            "current_set_a": 8.0,
        },
        save_profile=lambda profile: saved.append(dict(profile)),
    )
    window._host.setText("192.168.1.28")
    window._voltage.setValue(13.5)
    window._current.setValue(9.0)

    window._save_and_connect()

    assert saved == [
        {
            "host": "192.168.1.28",
            "voltage_set_v": 13.5,
            "current_set_a": 9.0,
        }
    ]
    assert "192.168.1.12:4004" in window.windowTitle()
    window.close()


def test_legacy_global_sample_broadcast_is_removed(
    qapplication_session,
) -> None:
    class _Live:
        def __init__(self, accepted: bool) -> None:
            self.accepted = accepted
            self.samples: list[ExternalPowerSample] = []

        def record_external_power_sample(self, sample: ExternalPowerSample) -> bool:
            if self.accepted:
                self.samples.append(sample)
            return self.accepted

    recording = _Live(True)
    idle = _Live(False)
    factory = CustomerEndpointSessionBundleFactory(
        settings=object(),
        session_directory=object(),
    )
    factory._bundles = {
        ("192.168.1.12", 4004): SimpleNamespace(live=recording),
        ("192.168.1.13", 4004): SimpleNamespace(live=idle),
    }
    sample = _sample(1)

    factory.record_external_power_sample(sample)

    assert recording.samples == []
    assert idle.samples == []


def test_identity_accepts_real_psw80_27_model_without_space() -> None:
    assert normalize_power_identity_field("PSW80-27") == "PSW8027"
    assert normalize_power_identity_field("PSW 80-27") == "PSW8027"
