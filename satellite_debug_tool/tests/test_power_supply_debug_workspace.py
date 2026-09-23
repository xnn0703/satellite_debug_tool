"""M19-A.3 standalone power-supply diagnostics UI tests."""

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import time

import pytest

from satellite_debug_tool.core.production import (
    FixtureControlLease,
    FixtureLeaseError,
    GwInstekPswAdapter,
    PowerDebugOperation,
    PowerSupplyConfig,
    PowerSupplyError,
    PowerSupplyState,
)


class ScriptedTransport:
    def __init__(self, script: list[tuple[str, str | None]]) -> None:
        self.script = deque(script)
        self.commands: list[str] = []
        self.connected = False
        self._response: str | None = None

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
        assert self._response is not None
        response = self._response
        self._response = None
        return response


def _wait_until(qapplication_session, predicate, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapplication_session.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    qapplication_session.processEvents()
    return bool(predicate())


def _settings(tmp_path: Path, monkeypatch):
    from satellite_debug_tool.core.config import Settings

    monkeypatch.setenv("HOME", str(tmp_path))
    return Settings()


def _fill_config(workspace) -> None:
    workspace._host.setText("192.168.1.108")
    workspace._serial.setText("PSW1234")
    workspace._voltage_set.setText("14")
    workspace._current_set.setText("2")


def _full_script() -> list[tuple[str, str | None]]:
    return [
        ("*IDN?", "GW-INSTEK,PSW 80-27,PSW1234,1.70"),
        ("OUTP?", "0"),
        ("MEAS:ALL?", "0.05,0.00"),
        ("STAT:OPER:COND?", "0"),
        ("STAT:QUES:COND?", "0"),
        ("OUTP:PROT:TRIP?", "0"),
        ("OUTP 0", None),
        ("OUTP?", "0"),
        ("MEAS:ALL?", "0.05,0.00"),
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
        ("MEAS:ALL?", "0.02,0.00"),
        ("STAT:OPER:COND?", "0"),
        ("STAT:QUES:COND?", "0"),
        ("OUTP:PROT:TRIP?", "0"),
        ("SYST:COMM:RLST LOC", None),
    ]


def _direct_enable_script() -> list[tuple[str, str | None]]:
    return [
        ("*IDN?", "GW-INSTEK,PSW 80-27,PSW1234,1.70"),
        ("OUTP 0", None),
        ("OUTP?", "0"),
        ("MEAS:ALL?", "0.00,0.00"),
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
        ("MEAS:ALL?", "14.01,0.00"),
        ("STAT:OPER:COND?", "256"),
        ("STAT:QUES:COND?", "0"),
        ("OUTP:PROT:TRIP?", "0"),
    ]


def test_standalone_power_workflow_records_closed_loop_evidence(
    qapplication_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.ui.power_supply_debug_workspace import (
        PowerSupplyDebugWorkspace,
    )

    transport = ScriptedTransport(_full_script())

    def adapter_factory(config: PowerSupplyConfig) -> GwInstekPswAdapter:
        return GwInstekPswAdapter(config, transport_factory=lambda _config: transport)

    workspace = PowerSupplyDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        adapter_factory=adapter_factory,
        session_root=tmp_path / "sessions",
    )
    _fill_config(workspace)
    session_dir = workspace.connect_power_supply()
    assert _wait_until(
        qapplication_session,
        lambda: workspace.state == PowerSupplyState.IDENTIFIED,
    )
    assert transport.commands == ["*IDN?"]
    assert "GEN 1" in workspace._identity_label.text()

    for operation, expected_state in (
        (PowerDebugOperation.INSPECT, PowerSupplyState.OFF_CONFIRMED),
        (PowerDebugOperation.PREPARE_OFF, PowerSupplyState.READY_OFF),
        (PowerDebugOperation.ENABLE, PowerSupplyState.ON_CONFIRMED),
        (PowerDebugOperation.DISABLE, PowerSupplyState.OFF_CONFIRMED),
    ):
        workspace._submit(operation)
        assert _wait_until(
            qapplication_session,
            lambda expected=expected_state: (
                workspace.state == expected and not workspace._busy_operation
            ),
        )

    workspace._submit(PowerDebugOperation.RELEASE_LOCAL)
    assert _wait_until(
        qapplication_session,
        lambda: not workspace._busy_operation and not transport.script,
    )
    workspace._submit(PowerDebugOperation.DISCONNECT)
    assert _wait_until(
        qapplication_session,
        lambda: not workspace.session_active and workspace._worker is None,
    )

    config = json.loads((session_dir / "config.json").read_text(encoding="utf-8"))
    summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (session_dir / "scpi_records.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    events = [
        json.loads(line)
        for line in (session_dir / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert config["result_class"] == "ENGINEERING_ONLY"
    assert summary["status"] == "complete"
    assert summary["final_state"] == PowerSupplyState.OFF_CONFIRMED.value
    assert manifest["status"] == "complete"
    assert records[0]["command"] == "*IDN?"
    assert any(record["command"] == "OUTP 1" for record in records)
    assert any(record["command"] == "OUTP 0" for record in records)
    assert any(
        event["event_type"] == "operation_completed"
        and event["details"].get("result", {}).get("voltage_v") == 14.02
        for event in events
    )
    workspace.close()


def test_ui_derives_psw80_27_validation_limits_from_requested_setpoints(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.ui.power_supply_debug_workspace import (
        PowerSupplyDebugWorkspace,
    )

    workspace = PowerSupplyDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
    )
    workspace._host.setText("192.168.1.108")
    workspace._voltage_set.setText("12")
    workspace._current_set.setText("1")

    config = workspace._build_config()

    assert config.voltage_setpoint_tolerance_v == pytest.approx(0.002)
    assert config.current_setpoint_tolerance_a == pytest.approx(0.002)
    assert config.output_voltage_min_v == pytest.approx(11.952)
    assert config.output_voltage_max_v == pytest.approx(12.048)
    assert config.off_voltage_max_v == pytest.approx(0.05)
    assert "11.952..12.048 V" in workspace._validation_policy_label.text()
    assert not hasattr(workspace, "_setpoint_tolerance")
    assert not hasattr(workspace, "_output_min")
    assert not hasattr(workspace, "_output_max")
    assert not hasattr(workspace, "_off_max")
    workspace.close()


def test_identity_rejection_releases_shared_lease_and_marks_incomplete(
    qapplication_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.ui.power_supply_debug_workspace import (
        PowerSupplyDebugWorkspace,
    )

    transport = ScriptedTransport(
        [("*IDN?", "OTHER,PSW 80-27,PSW1234,1.70")]
    )

    def adapter_factory(config: PowerSupplyConfig) -> GwInstekPswAdapter:
        return GwInstekPswAdapter(config, transport_factory=lambda _config: transport)

    lease = FixtureControlLease()
    workspace = PowerSupplyDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        lease,
        adapter_factory=adapter_factory,
        session_root=tmp_path / "sessions",
    )
    _fill_config(workspace)
    session_dir = workspace.connect_power_supply()
    assert _wait_until(
        qapplication_session,
        lambda: not workspace.session_active and workspace._worker is None,
    )

    summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "incomplete"
    assert "manufacturer" in summary["incomplete_reason"]
    replacement = lease.acquire("fixture-debug")
    lease.release(replacement)
    workspace.close()


def test_enable_from_identified_state_prepares_and_turns_on_in_one_operation(
    qapplication_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.ui.power_supply_debug_workspace import (
        PowerSupplyDebugWorkspace,
    )

    transport = ScriptedTransport(_direct_enable_script())

    def adapter_factory(config: PowerSupplyConfig) -> GwInstekPswAdapter:
        return GwInstekPswAdapter(config, transport_factory=lambda _config: transport)

    workspace = PowerSupplyDebugWorkspace(
        _settings(tmp_path, monkeypatch),
        FixtureControlLease(),
        adapter_factory=adapter_factory,
        session_root=tmp_path / "sessions-direct-enable",
    )
    _fill_config(workspace)
    workspace.connect_power_supply()
    assert _wait_until(
        qapplication_session,
        lambda: workspace.state == PowerSupplyState.IDENTIFIED,
    )
    assert workspace._enable_output.isEnabled()

    workspace._submit(PowerDebugOperation.ENABLE)
    assert _wait_until(
        qapplication_session,
        lambda: (
            workspace.state == PowerSupplyState.ON_CONFIRMED
            and not workspace._busy_operation
        ),
    )

    assert transport.commands.count("OUTP 0") == 1
    assert transport.commands.count("OUTP 1") == 1
    action_ids = {
        workspace._records.item(row, 4).text()
        for row in range(1, workspace._records.rowCount())
    }
    assert len(action_ids) == 1
    assert not transport.script
    workspace._submit(PowerDebugOperation.DISCONNECT)
    assert _wait_until(
        qapplication_session,
        lambda: not workspace.session_active and workspace._worker is None,
    )
    workspace.close()


def test_production_workspace_lazily_adds_power_page_and_shares_lease(
    qapplication_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(_settings(tmp_path, monkeypatch))
    assert workspace._subpages.count() == 4
    assert workspace._power_debug is None
    assert not workspace._power_page_button.text() == ""
    assert workspace._power_host.view is None

    workspace.activate_view()
    workspace._switch_subpage(3)
    assert workspace._subpages.currentIndex() == 3
    assert workspace._power_debug is not None
    assert workspace._power_host.view is workspace._power_debug
    assert workspace._power_debug._voltage_set.text() == ""
    assert workspace._power_debug._current_set.text() == ""

    with pytest.raises(PowerSupplyError):
        workspace._power_debug.connect_power_supply()

    handle = workspace._fixture_control_lease.acquire("batch-test")
    _fill_config(workspace._power_debug)
    with pytest.raises(FixtureLeaseError, match="already held"):
        workspace._power_debug.connect_power_supply()
    workspace._fixture_control_lease.release(handle)

    power_handle = workspace._fixture_control_lease.acquire("power-supply-debug")
    workspace._power_debug._lease_handle = power_handle
    workspace._switch_subpage(0)
    assert workspace._subpages.currentIndex() == 3
    workspace._fixture_control_lease.release(power_handle)
    workspace._power_debug._lease_handle = None
    workspace.close()
    workspace.deleteLater()
    qapplication_session.processEvents()


def test_power_workspace_retranslates_static_and_dynamic_status(
    qapplication_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.i18n import initialize_translation_manager
    from satellite_debug_tool.ui.power_supply_debug_workspace import (
        PowerSupplyDebugWorkspace,
    )

    monkeypatch.delenv("SATELLITE_DEBUG_LOCALE", raising=False)
    settings = _settings(tmp_path, monkeypatch)
    settings.set("ui.language", "zh_CN")
    manager = initialize_translation_manager(qapplication_session, settings)
    manager.set_preference("zh_CN")
    workspace = PowerSupplyDebugWorkspace(settings, FixtureControlLease())
    qapplication_session.processEvents()
    assert workspace._connect.text() == "连接并识别"
    assert "已断开" in workspace._state_label.text()

    manager.set_preference("en_US")
    qapplication_session.processEvents()
    assert workspace._connect.text() == "Connect and identify"
    assert "Disconnected" in workspace._state_label.text()

    manager.set_preference("zh_CN")
    qapplication_session.processEvents()
    workspace.close()
