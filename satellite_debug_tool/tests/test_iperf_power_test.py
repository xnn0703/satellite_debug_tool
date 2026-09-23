"""M27 iperf3 process owner, evidence and customer presentation tests."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import socket
import subprocess

import pytest
from PySide6.QtCore import QElapsedTimer, QProcess
from PySide6.QtTest import QTest

from satellite_debug_tool.core.external_power_monitor import (
    ExternalPowerSample,
    ExternalPowerStore,
)
from satellite_debug_tool.core.iperf_test import (
    IperfDirection,
    IperfLanePhase,
    IperfLaneSnapshot,
    IperfProtocol,
    IperfSessionWriter,
    IperfTestConfig,
    IperfTestController,
    IperfTestSnapshot,
    IperfTestPhase,
    IperfTestStore,
    IperfValidationError,
)
from satellite_debug_tool.core.production.power_supply import PowerIdentity


def _config(executable: str) -> IperfTestConfig:
    return IperfTestConfig(
        executable=executable,
        server="60.205.157.141",
        local_host="10.79.61.98",
        protocol=IperfProtocol.UDP,
        direction=IperfDirection.BOTH,
    )


def _sample() -> ExternalPowerSample:
    return ExternalPowerSample(
        host_timestamp_ns=1_800_000_000_000_000_000,
        monotonic_ns=123,
        connection_generation=1,
        identity=PowerIdentity("GW-INSTEK", "PSW 80-27", "P1", "1.0", "raw"),
        voltage_v=12.0,
        current_a=1.5,
        power_w=18.0,
        output_enabled=True,
        operation_condition=1,
        questionable_condition=0,
        protection_tripped=False,
    )


def test_config_rejects_ambiguous_or_unsafe_inputs(tmp_path: Path) -> None:
    executable = tmp_path / "iperf3"
    executable.write_text("placeholder", encoding="utf-8")
    _config(str(executable)).validate()

    with pytest.raises(IperfValidationError, match="different ports"):
        _config(str(executable)).__class__(
            **{**_config(str(executable)).__dict__, "dl_port": 5201}
        ).validate()
    with pytest.raises(IperfValidationError, match="IPv4"):
        _config(str(executable)).__class__(
            **{**_config(str(executable)).__dict__, "local_host": "wifi"}
        ).validate()
    with pytest.raises(IperfValidationError, match="bitrate"):
        _config(str(executable)).__class__(
            **{**_config(str(executable)).__dict__, "ul_rate": "0"}
        ).validate()


def test_command_builder_keeps_directions_independent(
    qapplication_session, tmp_path: Path
) -> None:
    store = IperfTestStore()
    power = ExternalPowerStore()
    controller = IperfTestController(store, power, tmp_path)
    config = _config("/opt/iperf3")
    controller._config = config

    ul = controller._arguments("ul", 600)
    dl = controller._arguments("dl", 45)

    assert ul == [
        "-c", "60.205.157.141", "-p", "5201", "-B", "10.79.61.98",
        "-b", "491K", "-t", "600", "-i", "1", "--json-stream",
        "--forceflush", "-u",
    ]
    assert dl[-2:] == ["-u", "-R"]
    assert dl[dl.index("-p") + 1] == "5202"
    assert dl[dl.index("-b") + 1] == "200K"
    assert dl[dl.index("-t") + 1] == "45"
    assert "--bidir" not in ul + dl


def test_json_stream_updates_udp_metrics_and_blocks_wrong_route(
    qapplication_session, tmp_path: Path
) -> None:
    store = IperfTestStore()
    power = ExternalPowerStore()
    controller = IperfTestController(store, power, tmp_path)
    controller._config = _config("/opt/iperf3")
    controller._monotonic_started = 0.0
    store.begin(IperfTestSnapshot(ul=store.snapshot.ul, dl=store.snapshot.dl))

    controller._handle_payload("ul", {
        "event": "start",
        "data": {"connected": [{"local_host": "10.79.61.98"}]},
    })
    controller._handle_payload("ul", {
        "event": "interval",
        "data": {"sum": {
            "bits_per_second": 491000.0,
            "bytes": 61375,
            "jitter_ms": 1.25,
            "lost_packets": 2,
            "packets": 100,
            "lost_percent": 2.0,
            "out_of_order": 3,
        }},
    })
    assert store.snapshot.ul.phase is IperfLanePhase.RUNNING
    assert store.snapshot.ul.bits_per_second == pytest.approx(491000.0)
    assert store.snapshot.ul.out_of_order == 3
    assert store.measurements()[-1].lost_percent == pytest.approx(2.0)

    controller._handle_payload("dl", {
        "event": "start",
        "data": {"connected": [{"local_host": "192.168.10.213"}]},
    })
    assert store.snapshot.dl.phase is IperfLanePhase.BLOCKED
    assert "selected satellite" in store.snapshot.dl.error


def test_tcp_end_uses_sender_retransmits_without_inventing_udp_quality(
    qapplication_session, tmp_path: Path
) -> None:
    store = IperfTestStore()
    controller = IperfTestController(store, ExternalPowerStore(), tmp_path)
    controller._config = IperfTestConfig(
        **{**_config("/opt/iperf3").__dict__, "protocol": IperfProtocol.TCP}
    )
    store.begin(IperfTestSnapshot(
        ul=IperfLaneSnapshot(phase=IperfLanePhase.RUNNING),
    ))

    controller._handle_payload("ul", {
        "event": "end",
        "data": {
            "sum_received": {"bits_per_second": 490000.0, "bytes": 1000},
            "sum_sent": {"bits_per_second": 491000.0, "bytes": 1000, "retransmits": 4},
        },
    })

    assert store.snapshot.ul.retransmits == 4
    assert store.snapshot.ul.jitter_ms is None
    assert store.snapshot.ul.lost_percent is None


def test_session_writer_keeps_raw_power_csv_and_final_summary(tmp_path: Path) -> None:
    executable = tmp_path / "iperf3"
    executable.write_text("placeholder", encoding="utf-8")
    config = _config(str(executable))
    writer = IperfSessionWriter(tmp_path, "session", config)
    writer.raw("ul", {"event": "start", "data": {}})
    writer.event("warning", "power_unavailable", "no sample")
    writer.power(_sample())
    writer.finalize("completed", "completed")

    directory = tmp_path / "session"
    assert json.loads((directory / "config.json").read_text())["direction"] == "both"
    assert '"event":"start"' in (directory / "iperf_ul.raw.jsonl").read_text()
    assert "18.0" in (directory / "measurements.csv").read_text()
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["outcome"] == "completed"
    assert summary["power_samples"] == 1
    assert (directory / "summary.csv").is_file()


def test_executable_inspection_rejects_old_or_missing_json_stream(
    monkeypatch, tmp_path: Path
) -> None:
    executable = tmp_path / "iperf3"
    executable.write_text("placeholder", encoding="utf-8")

    class _Result:
        def __init__(self, text: str) -> None:
            self.returncode = 0
            self.stdout = text
            self.stderr = ""

    monkeypatch.setattr(
        "satellite_debug_tool.core.iperf_test.subprocess.run",
        lambda command, **_kwargs: _Result(
            "iperf 3.17\n" if command[-1] == "--version" else "--json-stream"
        ),
    )
    with pytest.raises(IperfValidationError, match="3.18"):
        IperfTestController.inspect_executable(str(executable))

    monkeypatch.setattr(
        "satellite_debug_tool.core.iperf_test.subprocess.run",
        lambda command, **_kwargs: _Result(
            "iperf 3.18\n" if command[-1] == "--version" else "ordinary help"
        ),
    )
    with pytest.raises(IperfValidationError, match="json-stream"):
        IperfTestController.inspect_executable(str(executable))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize("protocol", [IperfProtocol.UDP, IperfProtocol.TCP])
def test_real_loopback_runs_independent_ul_and_dl_processes(
    qapplication_session,
    tmp_path: Path,
    protocol: IperfProtocol,
) -> None:
    executable = shutil.which("iperf3")
    if not executable:
        pytest.skip("iperf3 is not installed")
    ul_port, dl_port = _free_port(), _free_port()
    servers = [
        subprocess.Popen(
            [executable, "-s", "-p", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for port in (ul_port, dl_port)
    ]
    try:
        store = IperfTestStore()
        power = ExternalPowerStore()
        controller = IperfTestController(store, power, tmp_path)
        controller.start(IperfTestConfig(
            executable=executable,
            server="127.0.0.1",
            local_host="127.0.0.1",
            protocol=protocol,
            direction=IperfDirection.BOTH,
            ul_port=ul_port,
            dl_port=dl_port,
            ul_rate="491K",
            dl_rate="200K",
            continuous=False,
            duration_seconds=2,
        ))
        timer = QElapsedTimer()
        timer.start()
        while controller.active and timer.elapsed() < 10000:
            QTest.qWait(50)

        assert not controller.active
        assert store.snapshot.phase.value == "completed"
        assert store.snapshot.ul.sessions == 1
        assert store.snapshot.dl.sessions == 1
        assert {item.direction for item in store.measurements()} == {"ul", "dl"}
        assert Path(store.snapshot.session_directory, "summary.json").is_file()
        assert controller.shutdown()
    finally:
        for server in servers:
            server.terminate()
            try:
                server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=3)


def test_real_connection_refusal_enters_retry_without_false_success(
    qapplication_session, tmp_path: Path
) -> None:
    executable = shutil.which("iperf3")
    if not executable:
        pytest.skip("iperf3 is not installed")
    store = IperfTestStore()
    controller = IperfTestController(store, ExternalPowerStore(), tmp_path)
    controller.start(IperfTestConfig(
        executable=executable,
        server="127.0.0.1",
        local_host="127.0.0.1",
        protocol=IperfProtocol.UDP,
        direction=IperfDirection.UL,
        ul_port=_free_port(),
        continuous=True,
    ))
    timer = QElapsedTimer()
    timer.start()
    while store.snapshot.ul.phase is not IperfLanePhase.RETRYING and timer.elapsed() < 5000:
        QTest.qWait(25)

    assert store.snapshot.ul.phase is IperfLanePhase.RETRYING
    assert store.snapshot.phase.value == "degraded"
    assert store.snapshot.ul.retries == 1
    assert any(event["code"] == "retry_scheduled" for event in store.events())
    controller.stop("test complete")
    QTest.qWait(50)
    assert controller.shutdown()


def test_failed_start_retries_once_and_late_finish_cannot_reopen_session(
    qapplication_session, tmp_path: Path
) -> None:
    store = IperfTestStore()
    controller = IperfTestController(store, ExternalPowerStore(), tmp_path)
    controller._finalized = False
    controller._config = _config("/missing/iperf3")
    process = QProcess(controller)
    controller._processes["ul"] = process
    store.begin(IperfTestSnapshot(
        phase=IperfTestPhase.STARTING,
        ul=IperfLaneSnapshot(phase=IperfLanePhase.STARTING),
    ))

    controller._on_process_error("ul", QProcess.ProcessError.FailedToStart)
    assert store.snapshot.ul.phase is IperfLanePhase.RETRYING
    assert store.snapshot.ul.retries == 1
    controller._on_process_error("ul", QProcess.ProcessError.FailedToStart)
    assert store.snapshot.ul.retries == 1

    controller._finish(IperfTestPhase.STOPPED, "done")
    stopped = store.snapshot
    controller._on_finished("ul", 1, QProcess.ExitStatus.CrashExit)
    assert store.snapshot == stopped


def test_shutdown_terminates_active_process_and_finalizes_evidence(
    qapplication_session, tmp_path: Path
) -> None:
    executable = shutil.which("iperf3")
    if not executable:
        pytest.skip("iperf3 is not installed")
    port = _free_port()
    server = subprocess.Popen(
        [executable, "-s", "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        store = IperfTestStore()
        controller = IperfTestController(store, ExternalPowerStore(), tmp_path)
        controller.start(IperfTestConfig(
            executable=executable,
            server="127.0.0.1",
            local_host="127.0.0.1",
            direction=IperfDirection.UL,
            ul_port=port,
            continuous=True,
        ))
        timer = QElapsedTimer()
        timer.start()
        while store.snapshot.ul.phase is not IperfLanePhase.RUNNING and timer.elapsed() < 5000:
            QTest.qWait(25)
        assert store.snapshot.ul.phase is IperfLanePhase.RUNNING

        assert controller.shutdown()
        assert store.snapshot.phase is IperfTestPhase.STOPPED
        assert store.snapshot.ul.phase is IperfLanePhase.STOPPED
        summary = json.loads(
            Path(store.snapshot.session_directory, "summary.json").read_text()
        )
        assert summary["outcome"] == "stopped"
    finally:
        server.terminate()
        try:
            server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=3)
