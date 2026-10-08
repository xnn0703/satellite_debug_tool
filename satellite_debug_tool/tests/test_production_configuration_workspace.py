"""M19-A.4 visual batch configuration and exact-count gate tests."""

from __future__ import annotations

import json
from pathlib import Path
import time

from satellite_debug_tool.core.production import (
    FleetDatagram,
    GwInstekPswAdapter,
    PowerSupplyProfile,
    ProductTestTemplate,
    ProductionConfigurationStore,
    StationProfile,
    ProductionPowerWorker,
    psw80_27_validation_policy,
    PowerSupplyConfig,
)
from satellite_debug_tool.tests.test_power_supply_debug_workspace import (
    ScriptedTransport,
    _direct_enable_script,
)
from satellite_debug_tool.tests.test_production_fleet import _identity


def _catalog(path: Path) -> ProductionConfigurationStore:
    store = ProductionConfigurationStore(path)
    product = ProductTestTemplate(
        template_id="afd01c-standard",
        revision=1,
        display_name="AFD01C 标准测试",
        product="afd01c",
        supply_voltage_v=12.0,
        per_device_current_a=8.0,
        expected={
            "main_firmware": {"match": "optional", "value": ""},
            "parameters": {},
        },
        tests={"firmware_verification": {"enabled": True}},
        duration_policy={
            "minimum_effective_observation_s": 3600,
            "convergence_is_outside_observation": True,
            "report_first_last_window_s": 300,
        },
    )
    power = PowerSupplyProfile(
        profile_id="psw-main",
        revision=1,
        display_name="主线 PSW80-27",
        driver_id="gwinstek_psw80_27",
        host="192.168.1.108",
        port=2268,
        manufacturer="GW-INSTEK",
        model="PSW 80-27",
        serial_number="GER120143",
        rated_voltage_v=80,
        rated_current_a=27,
        rated_power_w=720,
    )
    station = StationProfile(
        station_profile_id="line-1",
        revision=1,
        display_name="一号工位",
        power_profile_id="psw-main",
        motion_profile_id="motion-main",
        reference_profile_id="ms6222-main",
        branch_count=4,
        branch_current_a=10,
    )
    store.save_catalog([product], [power], [station])
    return store


def _settings(tmp_path: Path, monkeypatch):
    from satellite_debug_tool.core.config import Settings

    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    settings.set("production.discovery_cidr", "127.0.0.1/32")
    return settings


def test_product_template_picker_lists_registered_production_products(
    qapplication_session, tmp_path: Path
) -> None:
    from satellite_debug_tool.ui.production_configuration_dialog import ProductionConfigurationDialog

    dialog = ProductionConfigurationDialog(
        store=ProductionConfigurationStore(tmp_path / "configuration.json")
    )
    choices = {
        dialog._product_type.itemData(index): dialog._product_type.itemText(index)
        for index in range(dialog._product_type.count())
    }
    assert choices == {
        "afd01": "AFD01",
        "afd01a": "AFD01A",
        "afd01b2": "AFD01B2",
        "afd01c": "AFD01C",
    }
    dialog.close()


def test_visual_selection_generates_afd01c_times_two_snapshot(
    qapplication_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    store = _catalog(tmp_path / "production_configuration.json")
    workspace = ProductionWorkspace(
        _settings(tmp_path, monkeypatch),
        configuration_store=store,
    )
    workspace._device_count_spin.setValue(2)
    workspace._batch_id_edit.setText("AFD01C-TWO")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))

    assert "AFD01C × 2" in workspace._configuration_summary.text()
    assert "12 V / 16 A / 192 W" in workspace._configuration_summary.text()
    batch = workspace.create_batch()

    assert batch["recipe_id"] == "afd01c-standard-R1"
    recipe = json.loads(
        (workspace.batch_output_dir / "recipe.json").read_text(encoding="utf-8")
    )
    snapshot = json.loads(
        (workspace.batch_output_dir / "production_configuration.json").read_text(
            encoding="utf-8"
        )
    )
    assert recipe["target_device_count"] == 2
    assert snapshot["combined_supply"] == {
        "current_a": 16.0,
        "power_w": 192.0,
        "voltage_v": 12.0,
    }
    assert snapshot["sha256"] == recipe["resolved_configuration_sha256"]
    assert not workspace._product_template_combo.isEnabled()
    assert not workspace._station_profile_combo.isEnabled()
    workspace.close()
    workspace.deleteLater()
    qapplication_session.processEvents()


def test_afd01c_times_two_requires_exactly_two_ready_devices(
    qapplication_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    workspace = ProductionWorkspace(
        _settings(tmp_path, monkeypatch),
        configuration_store=_catalog(tmp_path / "production_configuration.json"),
    )
    workspace._device_count_spin.setValue(2)
    workspace._batch_id_edit.setText("AFD01C-GATE")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))
    workspace.create_batch()

    def add(index: int) -> None:
        workspace._fleet._on_datagram(
            FleetDatagram(
                endpoint=(f"127.0.0.{index}", 4004),
                data=_identity(
                    f"AFD01C-{index}",
                    model="AFD01C",
                    service_protocol=8,
                ),
                wall_time_ns=time.time_ns(),
                monotonic_ns=time.monotonic_ns(),
            )
        )
        qapplication_session.processEvents()

    add(1)
    assert not workspace._start_button.isEnabled()
    assert "2" in workspace._start_button.toolTip()
    add(2)
    assert workspace._start_button.isEnabled()
    add(3)
    assert not workspace._start_button.isEnabled()
    assert "currently ready: 3" in workspace._start_button.toolTip() or "当前就绪：3" in (
        workspace._start_button.toolTip()
    )
    workspace.close()
    workspace.deleteLater()
    qapplication_session.processEvents()


def test_settings_dialog_exposes_production_configuration_manager(
    qapplication_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(_settings(tmp_path, monkeypatch))
    assert dialog._btn_production_configurations.isEnabled()
    assert dialog._btn_production_configurations.text()
    dialog.close()
    dialog.deleteLater()
    qapplication_session.processEvents()


def test_configuration_manager_creates_product_power_and_station_profiles(
    qapplication_session,
    tmp_path: Path,
) -> None:
    from satellite_debug_tool.ui.production_configuration_dialog import (
        ProductionConfigurationDialog,
    )

    store = ProductionConfigurationStore(tmp_path / "catalog.json")
    dialog = ProductionConfigurationDialog(store=store)
    dialog._new_product()
    dialog._product_id.setText("afd01c-standard")
    dialog._product_name.setText("AFD01C 标准测试")
    dialog._product_type.setCurrentIndex(dialog._product_type.findData("afd01c"))
    dialog._product_voltage.setValue(12.0)
    dialog._product_current.setValue(8.0)
    dialog._save_product()

    dialog._new_power()
    dialog._power_id.setText("psw-main")
    dialog._power_name.setText("主线 PSW80-27")
    dialog._power_host.setText("192.168.1.108")
    dialog._power_serial.setText("GER120143")
    dialog._save_power()

    dialog._new_station()
    dialog._station_id.setText("line-1")
    dialog._station_name.setText("一号工位")
    dialog._branch_count.setValue(4)
    dialog._branch_current.setValue(10.0)
    dialog._save_station()

    products, powers, stations = store.load_catalog()
    assert [(item.product, item.supply_voltage_v, item.per_device_current_a) for item in products] == [
        ("afd01c", 12.0, 8.0)
    ]
    assert [(item.model, item.host, item.serial_number) for item in powers] == [
        ("PSW 80-27", "192.168.1.108", "GER120143")
    ]
    assert [(item.power_profile_id, item.branch_count) for item in stations] == [
        ("psw-main", 4)
    ]
    dialog.close()
    dialog.deleteLater()
    qapplication_session.processEvents()


def test_formal_power_worker_confirms_on_then_verified_off(
    qapplication_session,
) -> None:
    script = _direct_enable_script() + [
        ("OUTP 0", None),
        ("OUTP?", "0"),
        ("MEAS:ALL?", "0.02,0.00"),
        ("STAT:OPER:COND?", "0"),
        ("STAT:QUES:COND?", "0"),
        ("OUTP:PROT:TRIP?", "0"),
    ]
    transport = ScriptedTransport(script)
    policy = psw80_27_validation_policy(14.0, 2.0)
    config = PowerSupplyConfig(
        host="192.168.1.108",
        expected_serial="PSW1234",
        voltage_set_v=14.0,
        current_set_a=2.0,
        voltage_setpoint_tolerance_v=policy.voltage_setpoint_tolerance_v,
        current_setpoint_tolerance_a=policy.current_setpoint_tolerance_a,
        output_voltage_min_v=policy.output_voltage_min_v,
        output_voltage_max_v=policy.output_voltage_max_v,
        off_voltage_max_v=policy.off_voltage_max_v,
    )
    worker = ProductionPowerWorker(
        config,
        adapter_factory=lambda supplied: GwInstekPswAdapter(
            supplied,
            transport_factory=lambda _config: transport,
        ),
    )
    ready = []
    stopped = []
    failed = []
    worker.power_ready.connect(lambda *args: ready.append(args))
    worker.power_stopped.connect(lambda *args: stopped.append(args))
    worker.power_failed.connect(lambda *args: failed.append(args))
    worker.start()

    deadline = time.monotonic() + 3.0
    while not ready and time.monotonic() < deadline:
        qapplication_session.processEvents()
        time.sleep(0.005)
    assert ready
    assert not failed
    assert transport.commands.count("OUTP 1") == 1

    worker.request_verified_off()
    deadline = time.monotonic() + 3.0
    while not stopped and time.monotonic() < deadline:
        qapplication_session.processEvents()
        time.sleep(0.005)
    assert stopped
    assert not failed
    assert worker.wait(1000)
    assert not transport.script
    worker.deleteLater()
    qapplication_session.processEvents()


def test_visual_batch_start_uses_selected_power_profile_and_exact_participants(
    qapplication_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.production import BatchStatus
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    transport = ScriptedTransport(
        [
            ("*IDN?", "GW-INSTEK,PSW 80-27,GER120143,1.70"),
            ("OUTP 0", None),
            ("OUTP?", "0"),
            ("MEAS:ALL?", "0.00,0.00"),
            ("SOUR:VOLT 12", None),
            ("SOUR:CURR 16", None),
            ("*OPC?", "1"),
            ("SOUR:VOLT?", "12.000"),
            ("SOUR:CURR?", "16.000"),
            ("SYST:ERR?", '0,"No error"'),
            ("STAT:OPER:COND?", "0"),
            ("STAT:QUES:COND?", "0"),
            ("OUTP:PROT:TRIP?", "0"),
            ("OUTP 1", None),
            ("OUTP?", "1"),
            ("MEAS:ALL?", "12.005,4.00"),
            ("STAT:OPER:COND?", "256"),
            ("STAT:QUES:COND?", "0"),
            ("OUTP:PROT:TRIP?", "0"),
            ("OUTP 0", None),
            ("OUTP?", "0"),
            ("MEAS:ALL?", "0.02,0.00"),
            ("STAT:OPER:COND?", "0"),
            ("STAT:QUES:COND?", "0"),
            ("OUTP:PROT:TRIP?", "0"),
        ]
    )
    workspace = ProductionWorkspace(
        _settings(tmp_path, monkeypatch),
        configuration_store=_catalog(tmp_path / "production_configuration.json"),
        production_power_adapter_factory=lambda config: GwInstekPswAdapter(
            config,
            transport_factory=lambda _config: transport,
        ),
    )
    workspace._device_count_spin.setValue(2)
    workspace._batch_id_edit.setText("AFD01C-POWERED")
    workspace._operator_edit.setText("operator-a")
    workspace._output_edit.setText(str(tmp_path / "output"))
    workspace.create_batch()
    for index in (1, 2):
        workspace._fleet._on_datagram(
            FleetDatagram(
                endpoint=(f"127.0.0.{index}", 4004),
                data=_identity(
                    f"AFD01C-{index}",
                    model="AFD01C",
                    service_protocol=8,
                ),
                wall_time_ns=time.time_ns(),
                monotonic_ns=time.monotonic_ns(),
            )
        )
    qapplication_session.processEvents()
    assert workspace._start_button.isEnabled()

    workspace._on_start_batch()
    deadline = time.monotonic() + 3.0
    while (
        workspace.batch["status"] != BatchStatus.RUNNING.value
        and time.monotonic() < deadline
    ):
        qapplication_session.processEvents()
        time.sleep(0.005)
    assert workspace.batch["status"] == BatchStatus.RUNNING.value
    assert workspace._participant_serials == ("AFD01C-1", "AFD01C-2")
    assert (workspace.batch_output_dir / "power_supply_records.jsonl").is_file()

    workspace._on_abort_batch()
    deadline = time.monotonic() + 3.0
    while (
        workspace.batch["status"] != BatchStatus.ABORTED.value
        and time.monotonic() < deadline
    ):
        qapplication_session.processEvents()
        time.sleep(0.005)
    assert workspace.batch["status"] == BatchStatus.ABORTED.value
    summary = json.loads(
        (workspace.batch_output_dir / "power_supply_summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["status"] == "off_confirmed"
    assert not transport.script
    workspace.close()
    workspace.deleteLater()
    qapplication_session.processEvents()
