"""M19 GW Instek PSW 80-27 protocol and safety-state tests."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
import socket
import threading
import time

import pytest

from satellite_debug_tool.core.production import (
    GwInstekPswAdapter,
    PowerEvidenceLevel,
    PowerSupplyConfig,
    PowerSupplyError,
    PowerSupplyState,
    ScpiLineCodec,
    SocketScpiTransport,
    psw80_27_validation_policy,
)


class ScriptedTransport:
    def __init__(self, script: list[tuple[str, str | None]]) -> None:
        self.script = deque(script)
        self.commands: list[str] = []
        self.connected = False
        self._response: str | None = None
        self.fail_read = False

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def write(self, data: bytes) -> None:
        assert self.connected
        command = data.decode("ascii").rstrip("\n")
        expected, response = self.script.popleft()
        assert command == expected
        self.commands.append(command)
        self._response = response

    def read_line(self) -> str:
        if self.fail_read:
            raise PowerSupplyError("simulated timeout")
        assert self._response is not None
        response = self._response
        self._response = None
        return response


def _config() -> PowerSupplyConfig:
    return PowerSupplyConfig(
        host="192.168.1.108",
        expected_serial="PSW1234",
        voltage_set_v=14.0,
        current_set_a=2.0,
        output_voltage_min_v=13.5,
        output_voltage_max_v=14.5,
        off_voltage_max_v=0.5,
    )


def _adapter(
    script: list[tuple[str, str | None]],
) -> tuple[GwInstekPswAdapter, ScriptedTransport]:
    transport = ScriptedTransport(script)
    adapter = GwInstekPswAdapter(
        _config(),
        transport_factory=lambda _config: transport,
        wall_clock_ns=lambda: 100,
        monotonic_clock_ns=lambda: 200,
    )
    return adapter, transport


class FakeMonotonicClock:
    def __init__(self, *, sleep_advance_s: float | None = None) -> None:
        self.now_ns = 0
        self.sleep_advance_s = sleep_advance_s
        self.sleeps: list[float] = []

    def monotonic_ns(self) -> int:
        return self.now_ns

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        elapsed = seconds if self.sleep_advance_s is None else self.sleep_advance_s
        self.now_ns += int(elapsed * 1_000_000_000)


def test_psw80_27_policy_derives_limits_from_requested_setpoints() -> None:
    policy = psw80_27_validation_policy(12.0, 1.0)

    assert policy.voltage_setpoint_tolerance_v == pytest.approx(0.002)
    assert policy.current_setpoint_tolerance_a == pytest.approx(0.002)
    assert policy.output_voltage_min_v == pytest.approx(11.952)
    assert policy.output_voltage_max_v == pytest.approx(12.048)
    assert policy.off_voltage_max_v == pytest.approx(0.05)

    with pytest.raises(PowerSupplyError, match="exceeds 720 W"):
        psw80_27_validation_policy(80.0, 27.0)
    with pytest.raises(PowerSupplyError, match="exceeds 720 W"):
        replace(_config(), voltage_set_v=80.0, current_set_a=27.0).validate()


def test_scpi_line_codec_handles_fragmented_and_coalesced_tcp_data() -> None:
    codec = ScpiLineCodec(max_line_bytes=16)
    assert codec.feed(b"GW-IN") == ()
    assert codec.feed(b"STEK\r\n1\n2") == ("GW-INSTEK", "1")
    assert codec.feed(b"\n") == ("2",)

    with pytest.raises(PowerSupplyError, match="maximum line"):
        codec.feed(b"x" * 17)
    assert codec.feed(b"OK\n") == ("OK",)


def test_socket_transport_handles_fragmentation_and_rejects_extra_lines() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve_fragmented() -> None:
        connection, _address = server.accept()
        with connection:
            assert connection.recv(128) == b"*OPC?\n"
            connection.sendall(b"1")
            time.sleep(0.01)
            connection.sendall(b"\r\n")

    worker = threading.Thread(target=serve_fragmented)
    worker.start()
    transport = SocketScpiTransport(
        PowerSupplyConfig(
            host="127.0.0.1",
            port=port,
            voltage_set_v=1.0,
            current_set_a=1.0,
        )
    )
    transport.connect()
    transport.write(b"*OPC?\n")
    assert transport.read_line() == "1"
    transport.close()
    worker.join()
    server.close()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve_extra() -> None:
        connection, _address = server.accept()
        with connection:
            assert connection.recv(128) == b"OUTP?\n"
            connection.sendall(b"1\n2\n")

    worker = threading.Thread(target=serve_extra)
    worker.start()
    transport = SocketScpiTransport(
        PowerSupplyConfig(
            host="127.0.0.1",
            port=port,
            voltage_set_v=1.0,
            current_set_a=1.0,
        )
    )
    transport.connect()
    transport.write(b"OUTP?\n")
    with pytest.raises(PowerSupplyError, match="extra SCPI"):
        transport.read_line()
    transport.close()
    worker.join()
    server.close()


def test_identity_mismatch_closes_transport_and_blocks_control() -> None:
    adapter, transport = _adapter(
        [("*IDN?", "OTHER,PSW 80-27,PSW1234,1.70")]
    )
    with pytest.raises(PowerSupplyError, match="manufacturer"):
        adapter.connect()
    assert adapter.state == PowerSupplyState.DISCONNECTED
    assert not transport.connected
    with pytest.raises(PowerSupplyError, match="not connected"):
        adapter.release_local()


def test_prepare_enable_and_disable_require_closed_loop_evidence() -> None:
    script = [
        ("*IDN?", "GW-INSTEK,PSW 80-27,PSW1234,1.70"),
        ("OUTP 0", None),
        ("OUTP?", "0"),
        ("MEAS:ALL?", "0.08,0.01"),
        ("SOUR:VOLT 14", None),
        ("SOUR:CURR 2", None),
        ("*OPC?", "1"),
        ("SOUR:VOLT?", "14.000"),
        ("SOUR:CURR?", "2.000"),
        ("SYST:ERR?", '0,"No error"'),
        ("STAT:OPER:COND?", "0"),
        ("STAT:QUES:COND?", "0"),
        ("OUTP:PROT:TRIP?", "0"),
        ("OUTP 1", None),
        ("OUTP?", "1"),
        ("MEAS:ALL?", "14.02,1.31"),
        ("STAT:OPER:COND?", "32"),
        ("STAT:QUES:COND?", "0"),
        ("OUTP:PROT:TRIP?", "0"),
        ("OUTP 0", None),
        ("OUTP?", "0"),
        ("MEAS:ALL?", "0.12,0.00"),
        ("STAT:OPER:COND?", "0"),
        ("STAT:QUES:COND?", "0"),
        ("OUTP:PROT:TRIP?", "0"),
    ]
    adapter, transport = _adapter(script)
    identity = adapter.connect()
    assert identity.serial_number == "PSW1234"

    prepared = adapter.prepare_output_off(fixture_action_id="PWR-001")
    assert prepared.state == PowerSupplyState.READY_OFF
    assert prepared.evidence_level == PowerEvidenceLevel.OUTPUT_STATE_CONFIRMED

    enabled = adapter.enable_output(fixture_action_id="PWR-001")
    assert enabled.state == PowerSupplyState.ON_CONFIRMED
    assert enabled.evidence_level == PowerEvidenceLevel.VOLTAGE_CONFIRMED
    assert enabled.measurement.voltage_v == pytest.approx(14.02)

    disabled = adapter.disable_output(fixture_action_id="PWR-002")
    assert disabled.state == PowerSupplyState.OFF_CONFIRMED
    assert disabled.evidence_level == PowerEvidenceLevel.OUTPUT_STATE_CONFIRMED
    assert not transport.script
    assert all(record.connection_generation == 1 for record in adapter.records)
    assert adapter.records[13].fixture_action_id == "PWR-001"


def test_output_on_waits_for_voltage_ramp_before_confirming() -> None:
    transport = ScriptedTransport(
        [
            ("*IDN?", "GW-INSTEK,PSW 80-27,PSW1234,1.70"),
            ("OUTP 0", None),
            ("OUTP?", "0"),
            ("MEAS:ALL?", "0.000,0.000"),
            ("SOUR:VOLT 14", None),
            ("SOUR:CURR 2", None),
            ("*OPC?", "1"),
            ("SOUR:VOLT?", "14.000"),
            ("SOUR:CURR?", "2.000"),
            ("SYST:ERR?", '0,"No error"'),
            ("STAT:OPER:COND?", "0"),
            ("STAT:QUES:COND?", "0"),
            ("OUTP:PROT:TRIP?", "0"),
            ("OUTP 1", None),
            ("OUTP?", "1"),
            ("MEAS:ALL?", "-0.003,0.897"),
            ("STAT:OPER:COND?", "1024"),
            ("STAT:QUES:COND?", "0"),
            ("OUTP:PROT:TRIP?", "0"),
            ("OUTP?", "1"),
            ("MEAS:ALL?", "14.005,0.000"),
            ("STAT:OPER:COND?", "256"),
            ("STAT:QUES:COND?", "0"),
            ("OUTP:PROT:TRIP?", "0"),
        ]
    )
    clock = FakeMonotonicClock(sleep_advance_s=1.5)
    adapter = GwInstekPswAdapter(
        _config(),
        transport_factory=lambda _config: transport,
        monotonic_clock_ns=clock.monotonic_ns,
        sleep=clock.sleep,
    )
    adapter.connect()
    adapter.prepare_output_off(fixture_action_id="PWR-RAMP")

    result = adapter.enable_output(fixture_action_id="PWR-RAMP")

    assert result.state == PowerSupplyState.ON_CONFIRMED
    assert result.measurement.voltage_v == pytest.approx(14.005)
    assert clock.sleeps == [pytest.approx(0.1)]
    assert transport.commands.count("OUTP 1") == 1
    assert not transport.script
    assert {
        record.fixture_action_id
        for record in adapter.records
        if record.command != "*IDN?"
    } == {"PWR-RAMP"}


def test_output_off_waits_for_voltage_to_fall_below_automatic_threshold() -> None:
    transport = ScriptedTransport(
        [
            ("*IDN?", "GW-INSTEK,PSW 80-27,PSW1234,1.70"),
            ("OUTP 0", None),
            ("OUTP?", "0"),
            ("MEAS:ALL?", "8.077,0.000"),
            ("OUTP?", "0"),
            ("MEAS:ALL?", "0.018,0.000"),
            ("SOUR:VOLT 12", None),
            ("SOUR:CURR 1", None),
            ("*OPC?", "1"),
            ("SOUR:VOLT?", "12.000"),
            ("SOUR:CURR?", "1.000"),
            ("SYST:ERR?", '0,"No error"'),
            ("STAT:OPER:COND?", "0"),
            ("STAT:QUES:COND?", "0"),
            ("OUTP:PROT:TRIP?", "0"),
        ]
    )
    clock = FakeMonotonicClock(sleep_advance_s=0.5)
    policy = psw80_27_validation_policy(12.0, 1.0)
    adapter = GwInstekPswAdapter(
        PowerSupplyConfig(
            host="192.168.1.108",
            voltage_set_v=12.0,
            current_set_a=1.0,
            voltage_setpoint_tolerance_v=policy.voltage_setpoint_tolerance_v,
            current_setpoint_tolerance_a=policy.current_setpoint_tolerance_a,
            output_voltage_min_v=policy.output_voltage_min_v,
            output_voltage_max_v=policy.output_voltage_max_v,
            off_voltage_max_v=policy.off_voltage_max_v,
        ),
        transport_factory=lambda _config: transport,
        monotonic_clock_ns=clock.monotonic_ns,
        sleep=clock.sleep,
    )
    adapter.connect()

    result = adapter.prepare_output_off(fixture_action_id="PWR-FALL")

    assert result.state == PowerSupplyState.READY_OFF
    assert result.measurement.voltage_v == pytest.approx(0.018)
    assert clock.sleeps == [pytest.approx(0.1)]
    assert not transport.script


def test_output_on_reports_last_voltage_after_settle_timeout() -> None:
    status_cycle = [
        ("OUTP?", "1"),
        ("MEAS:ALL?", "0.819,0.000"),
        ("STAT:OPER:COND?", "1024"),
        ("STAT:QUES:COND?", "0"),
        ("OUTP:PROT:TRIP?", "0"),
    ]
    transport = ScriptedTransport(
        [
            ("*IDN?", "GW-INSTEK,PSW 80-27,PSW1234,1.70"),
            ("OUTP 0", None),
            ("OUTP?", "0"),
            ("MEAS:ALL?", "0.000,0.000"),
            ("SOUR:VOLT 14", None),
            ("SOUR:CURR 2", None),
            ("*OPC?", "1"),
            ("SOUR:VOLT?", "14.000"),
            ("SOUR:CURR?", "2.000"),
            ("SYST:ERR?", '0,"No error"'),
            ("STAT:OPER:COND?", "0"),
            ("STAT:QUES:COND?", "0"),
            ("OUTP:PROT:TRIP?", "0"),
            ("OUTP 1", None),
            *status_cycle,
            *status_cycle,
            *status_cycle,
        ]
    )
    clock = FakeMonotonicClock()
    adapter = GwInstekPswAdapter(
        replace(_config(), output_settle_timeout_s=0.2),
        transport_factory=lambda _config: transport,
        monotonic_clock_ns=clock.monotonic_ns,
        sleep=clock.sleep,
    )
    adapter.connect()
    adapter.prepare_output_off(fixture_action_id="PWR-TIMEOUT")

    with pytest.raises(
        PowerSupplyError,
        match=r"0\.8190 V, expected 13\.5000\.\.14\.5000 V",
    ):
        adapter.enable_output(fixture_action_id="PWR-TIMEOUT")

    assert adapter.state == PowerSupplyState.UNKNOWN
    assert adapter.evidence_level == PowerEvidenceLevel.OUTPUT_STATE_CONFIRMED
    assert transport.commands.count("OUTP 1") == 1
    assert not transport.script


def test_output_on_stops_stabilization_when_protection_trips() -> None:
    transport = ScriptedTransport(
        [
            ("*IDN?", "GW-INSTEK,PSW 80-27,PSW1234,1.70"),
            ("OUTP 0", None),
            ("OUTP?", "0"),
            ("MEAS:ALL?", "0.000,0.000"),
            ("SOUR:VOLT 14", None),
            ("SOUR:CURR 2", None),
            ("*OPC?", "1"),
            ("SOUR:VOLT?", "14.000"),
            ("SOUR:CURR?", "2.000"),
            ("SYST:ERR?", '0,"No error"'),
            ("STAT:OPER:COND?", "0"),
            ("STAT:QUES:COND?", "0"),
            ("OUTP:PROT:TRIP?", "0"),
            ("OUTP 1", None),
            ("OUTP?", "1"),
            ("MEAS:ALL?", "0.819,0.000"),
            ("STAT:OPER:COND?", "1024"),
            ("STAT:QUES:COND?", "4"),
            ("OUTP:PROT:TRIP?", "1"),
        ]
    )
    adapter = GwInstekPswAdapter(
        _config(), transport_factory=lambda _config: transport
    )
    adapter.connect()
    adapter.prepare_output_off(fixture_action_id="PWR-TRIP")

    with pytest.raises(PowerSupplyError, match="protection or questionable"):
        adapter.enable_output(fixture_action_id="PWR-TRIP")

    assert adapter.state == PowerSupplyState.PROTECTION_TRIPPED
    assert adapter.evidence_level != PowerEvidenceLevel.VOLTAGE_CONFIRMED
    assert transport.commands.count("OUTP 1") == 1


def test_output_on_requires_ready_state_and_action_id() -> None:
    adapter, _transport = _adapter(
        [("*IDN?", "GW-INSTEK,PSW 80-27,PSW1234,1.70")]
    )
    adapter.connect()
    with pytest.raises(PowerSupplyError, match="READY_OFF"):
        adapter.enable_output(fixture_action_id="PWR-001")


def test_query_timeout_marks_connection_state_unknown() -> None:
    adapter, transport = _adapter(
        [
            ("*IDN?", "GW-INSTEK,PSW 80-27,PSW1234,1.70"),
            ("OUTP?", "1"),
        ]
    )
    adapter.connect()
    transport.fail_read = True

    with pytest.raises(PowerSupplyError, match="query failed"):
        adapter.query("OUTP?")

    assert adapter.state == PowerSupplyState.UNKNOWN
    assert adapter.evidence_level == PowerEvidenceLevel.NONE


def test_reconnect_reads_identity_only_and_never_replays_output_on() -> None:
    first = ScriptedTransport(
        [("*IDN?", "GW-INSTEK,PSW 80-27,PSW1234,1.70")]
    )
    second = ScriptedTransport(
        [("*IDN?", "GW-INSTEK,PSW 80-27,PSW1234,1.70")]
    )
    transports = deque((first, second))
    adapter = GwInstekPswAdapter(
        _config(), transport_factory=lambda _config: transports.popleft()
    )

    adapter.connect()
    adapter.close()
    adapter.connect()

    assert first.commands == ["*IDN?"]
    assert second.commands == ["*IDN?"]
    assert adapter.connection_generation == 2
    assert adapter.state == PowerSupplyState.IDENTIFIED


def test_adapter_serializes_queries_across_threads() -> None:
    active = 0
    max_active = 0
    guard = threading.Lock()
    responses = deque(
        ["GW-INSTEK,PSW 80-27,PSW1234,1.70"] + ["1"] * 8
    )

    class SlowTransport:
        def connect(self) -> None:
            return None

        def close(self) -> None:
            return None

        def write(self, _data: bytes) -> None:
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)

        def read_line(self) -> str:
            nonlocal active
            time.sleep(0.01)
            with guard:
                active -= 1
            return responses.popleft()

    adapter = GwInstekPswAdapter(
        _config(), transport_factory=lambda _config: SlowTransport()
    )
    adapter.connect()
    threads = [threading.Thread(target=lambda: adapter.query("*OPC?")) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert max_active == 1
