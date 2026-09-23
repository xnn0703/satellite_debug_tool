"""Read-only external PSW owner, Store and Customer presentation tests."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QElapsedTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from satellite_debug_tool.core.external_power_monitor import (
    EXTERNAL_POWER_HISTORY_SECONDS,
    ExternalPowerConfig,
    ExternalPowerPhase,
    ExternalPowerSample,
    ExternalPowerSnapshot,
    ExternalPowerStore,
    ExternalPowerWorker,
    PswReadOnlySession,
)
from satellite_debug_tool.core.production.power_supply import (
    PowerIdentity,
    PowerSupplyError,
)
from satellite_debug_tool.ui.external_power_window import ExternalPowerHistoryWindow
from satellite_debug_tool.ui.customer_session_bundle import (
    CustomerEndpointSessionBundleFactory,
)


class _SocketDouble:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = list(responses)
        self.commands: list[str] = []
        self.closed = False
        self.timeout = 0.0

    def settimeout(self, timeout: float) -> None:
        self.timeout = float(timeout)

    def sendall(self, payload: bytes) -> None:
        command = payload.decode("ascii")
        assert command.endswith("\n")
        self.commands.append(command.rstrip("\n"))

    def recv(self, _size: int) -> bytes:
        if not self._responses:
            raise socket.timeout("script exhausted")
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True


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


def test_read_only_session_uses_only_fixed_query_whitelist_and_parses_sample() -> None:
    sock = _SocketDouble(
        [
            b"GW-INSTEK,PSW 80-27,PSW1234,1.70\n",
            b"1\n",
            b"12.04,1.",
            b"250\n",
            b"4\n",
            b"0\n",
            b"0\n",
        ]
    )
    session = PswReadOnlySession(
        ExternalPowerConfig("192.168.1.108"),
        socket_factory=lambda *_args, **_kwargs: sock,
        wall_clock_ns=lambda: 1234,
        monotonic_clock_ns=lambda: 5678,
    )

    identity = session.connect()
    sample = session.read_sample(3)
    session.close()

    assert identity.serial_number == "PSW1234"
    assert sample.voltage_v == pytest.approx(12.04)
    assert sample.current_a == pytest.approx(1.25)
    assert sample.power_w == pytest.approx(15.05)
    assert sample.connection_generation == 3
    assert sock.commands == [
        "*IDN?",
        "OUTP?",
        "MEAS:ALL?",
        "STAT:OPER:COND?",
        "STAT:QUES:COND?",
        "OUTP:PROT:TRIP?",
    ]
    assert all(command.endswith("?") for command in sock.commands)
    assert sock.closed


def test_read_only_session_rejects_wrong_identity_and_closes_socket() -> None:
    sock = _SocketDouble([b"OTHER,PSW 80-27,PSW1234,1.70\n"])
    session = PswReadOnlySession(
        ExternalPowerConfig("192.168.1.108"),
        socket_factory=lambda *_args, **_kwargs: sock,
    )

    with pytest.raises(PowerSupplyError, match="manufacturer"):
        session.connect()

    assert sock.closed
    assert session.identity is None


def test_read_only_session_reports_timeout_and_non_finite_measurement() -> None:
    timeout_socket = _SocketDouble(
        [b"GW-INSTEK,PSW 80-27,PSW1234,1.70\n"]
    )
    timeout_session = PswReadOnlySession(
        ExternalPowerConfig("192.168.1.108"),
        socket_factory=lambda *_args, **_kwargs: timeout_socket,
    )
    timeout_session.connect()
    with pytest.raises(PowerSupplyError, match="timed out|script exhausted"):
        timeout_session.read_sample(1)
    timeout_session.close()

    non_finite_socket = _SocketDouble(
        [
            b"GW-INSTEK,PSW 80-27,PSW1234,1.70\n",
            b"1\n",
            b"nan,1.0\n",
        ]
    )
    non_finite_session = PswReadOnlySession(
        ExternalPowerConfig("192.168.1.108"),
        socket_factory=lambda *_args, **_kwargs: non_finite_socket,
    )
    non_finite_session.connect()
    with pytest.raises(PowerSupplyError, match="non-finite|finite"):
        non_finite_session.read_sample(1)
    non_finite_session.close()


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


def test_shared_sample_is_broadcast_only_to_existing_customer_bundles(
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

    assert recording.samples == [sample]
    assert idle.samples == []
