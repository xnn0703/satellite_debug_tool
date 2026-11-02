"""M48 冻结批次配置和夹具指示灯的工作区回归。"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.production import (
    FixtureRuntime,
    FixtureRuntimeState,
    ResultStoreError,
)
from satellite_debug_tool.tests.test_production_configuration_workspace import _catalog
from satellite_debug_tool.tests.test_production_recipe import valid_recipe
from satellite_debug_tool.ui.production_workspace import ProductionWorkspace


class _LayoutWorkspace(ProductionWorkspace):
    """本组只验证配置和呈现，保持设备发现和硬件入口未启动。"""

    def _configure_fleet(self) -> None:
        self._fleet = None


@pytest.fixture
def layout_workspace(qapplication_session, tmp_path: Path):
    catalog = _catalog(tmp_path / "catalog.json")
    products, powers, references, stations = catalog.load_full_catalog()
    product = replace(products[0], tests={"static_acquisition": {"enabled": True}})
    catalog.save_catalog([product], powers, stations, references)
    runtime = FixtureRuntime(
        profile_store=catalog.fixture_profile_store,
        calibration_store=catalog.calibration_store,
    )
    settings = Settings(config_directory=tmp_path / "settings")
    workspace = _LayoutWorkspace(settings, configuration_store=catalog, fixture_runtime=runtime)
    workspace._batch_id_edit.setText("M48-FROZEN")
    workspace._operator_edit.setText("test-operator")
    workspace._output_edit.setText(str(tmp_path / "output"))
    yield workspace, catalog
    workspace.close()
    workspace.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapplication_session.processEvents()


@pytest.mark.parametrize("entry", ("catalog_refresh", "selection_signal"))
def test_created_batch_keeps_frozen_recipe_and_ready_facts_after_catalog_changes(
    layout_workspace, entry: str,
) -> None:
    workspace, catalog = layout_workspace
    workspace.create_batch()
    recipe = workspace.recipe
    resolved = workspace._resolved_configuration
    for fixture in ("power", "motion", "reference"):
        workspace._fixture_runtime.set_state(fixture, FixtureRuntimeState.READY, f"confirmed {fixture}")
    facts = {
        fixture: workspace._fixture_runtime.fact(fixture)
        for fixture in ("power", "motion", "reference", "vehicle")
    }

    products, powers, references, stations = catalog.load_full_catalog()
    catalog.save_catalog(
        [replace(products[0], revision=2, supply_voltage_v=18.0,
                 tests={"locked_drive": {"enabled": True}})],
        [replace(powers[0], revision=2, host="192.168.1.109")],
        [replace(stations[0], revision=2)],
        references,
    )
    changed = catalog.resolve("afd01c-standard", "line-1", 1)
    assert changed.sha256 != resolved.sha256

    if entry == "catalog_refresh":
        workspace.refresh_production_configurations()
    else:
        workspace._device_count_spin.valueChanged.emit(4)
    workspace.retranslate_ui()

    assert workspace.recipe is recipe
    assert workspace._resolved_configuration is resolved
    assert workspace.batch["recipe_sha256"] == recipe.sha256
    assert workspace._progress_panel.node_ids == ("static_acquisition",)
    assert "192.168.1.108" in workspace._configuration_summary.text()
    assert "192.168.1.109" not in workspace._configuration_summary.text()
    assert {fixture: workspace._fixture_runtime.fact(fixture) for fixture in facts} == facts
    assert not workspace._product_template_combo.isEnabled()
    assert not workspace._station_profile_combo.isEnabled()


def test_created_batch_explicitly_rejects_legacy_recipe_replacement(layout_workspace, tmp_path: Path) -> None:
    workspace, _catalog_store = layout_workspace
    workspace.create_batch()
    frozen = workspace.recipe
    payload = valid_recipe()
    payload["recipe_id"] = "AFD01C-REPLACEMENT"
    payload["product"] = "afd01c"
    path = tmp_path / "replacement.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ResultStoreError, match="frozen recipe"):
        workspace.load_recipe_file(path)

    assert workspace.recipe is frozen
    assert workspace.batch["recipe_sha256"] == frozen.sha256


@pytest.mark.parametrize("state, level, label", (
    (FixtureRuntimeState.NEEDS_BINDING, "pending", "Needs binding"),
    (FixtureRuntimeState.CONFIGURED, "pending", "Configured"),
    (FixtureRuntimeState.CONNECTING, "pending", "Connecting"),
    (FixtureRuntimeState.CONNECTED, "pending", "Connected"),
    (FixtureRuntimeState.READY, "good", "Ready"),
    (FixtureRuntimeState.RUNNING, "good", "Running"),
    (FixtureRuntimeState.FAULT, "pending", "Failed"),
))
def test_bench_fixture_lights_use_runtime_facts_and_keep_unrelated_vehicle_gray(
    layout_workspace, state: FixtureRuntimeState, level: str, label: str,
) -> None:
    workspace, _catalog_store = layout_workspace
    for fixture in ("power", "motion", "reference", "vehicle"):
        workspace._fixture_runtime.set_state(fixture, state, f"raw {fixture} evidence")

    rendered = {item.fixture_id: item for item in workspace._fixture_strip.fixtures}
    for fixture in ("power", "motion", "reference"):
        assert rendered[fixture].level == level
        assert rendered[fixture].state_source == label
        from satellite_debug_tool.i18n import tr
        raw_evidence = f"raw {fixture} evidence"
        expected = tr("MS precision is advisory for SNR tests. {details}", details=raw_evidence) if fixture == "reference" else raw_evidence
        assert rendered[fixture].evidence == expected
        chip = workspace._fixture_strip._chips[fixture]
        assert chip.indicator.property("level") == level
        assert f"raw {fixture} evidence" in chip.toolTip()
    assert rendered["vehicle"].level == "irrelevant"
    assert rendered["vehicle"].state_source == "Not applicable"
    assert workspace._fixture_runtime.fact("vehicle").state is state


def test_vehicle_recipe_marks_manual_gate_red_and_ready_bench_fixtures_gray(layout_workspace) -> None:
    workspace, catalog = layout_workspace
    products, powers, references, stations = catalog.load_full_catalog()
    catalog.save_catalog(
        [replace(products[0], tests={"locked_drive": {"enabled": True}})],
        powers, stations, references,
    )
    workspace.refresh_production_configurations()
    workspace._fixture_runtime.set_state("motion", FixtureRuntimeState.READY, "controller feedback confirmed")
    workspace._fixture_runtime.set_state("reference", FixtureRuntimeState.READY, "valid reference frames")

    rendered = {item.fixture_id: item for item in workspace._fixture_strip.fixtures}
    assert rendered["vehicle"].level == "pending"
    assert rendered["vehicle"].state_source == "Manual gate"
    assert "Operator confirmation required" in rendered["vehicle"].evidence
    assert rendered["motion"].level == "irrelevant"
    assert rendered["reference"].level == "irrelevant"
    assert rendered["power"].level == "pending"

    workspace._fixture_runtime.set_state("vehicle", FixtureRuntimeState.READY, "operator confirmed")
    ready = {item.fixture_id: item for item in workspace._fixture_strip.fixtures}
    assert ready["vehicle"].level == "good"
    assert ready["vehicle"].state_source == "Ready"
