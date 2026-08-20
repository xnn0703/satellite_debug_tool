"""M19 GW Instek PSW 80-27 protocol and safety-state tests."""

from __future__ import annotations

from collections import deque
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
