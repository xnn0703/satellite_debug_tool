"""M19-A.4 production configuration catalog and bundle tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from satellite_debug_tool.core.production import (
    PowerSupplyProfile,
    ProductTestTemplate,
    ProductionConfigurationError,
    ProductionConfigurationStore,
    ProductionRecipe,
    StationProfile,
)


def _product(*, revision: int = 1) -> ProductTestTemplate:
    return ProductTestTemplate(
        template_id="afd01c-standard",
        revision=revision,
        display_name="AFD01C 标准测试",
        product="afd01c",
        supply_voltage_v=12.0,
        per_device_current_a=8.0,
        expected={
            "main_firmware": {"match": "optional", "value": ""},
            "parameters": {},
        },
        tests={
            "firmware_verification": {"enabled": True},
            "static_acquisition": {"enabled": True},
        },
        duration_policy={
            "minimum_effective_observation_s": 3600,
            "convergence_is_outside_observation": True,
            "report_first_last_window_s": 300,
        },
    )


def _power(*, revision: int = 1, rated_current_a: float = 27.0) -> PowerSupplyProfile:
    return PowerSupplyProfile(
        profile_id="psw-main",
        revision=revision,
        display_name="主线 PSW80-27",
        driver_id="gwinstek_psw80_27",
        host="192.168.1.108",
        port=2268,
        manufacturer="GW-INSTEK",
        model="PSW 80-27",
        serial_number="GER120143",
        rated_voltage_v=80.0,
        rated_current_a=rated_current_a,
        rated_power_w=720.0,
    )


def _station(*, branch_count: int = 4, branch_current_a: float = 10.0) -> StationProfile:
    return StationProfile(
        station_profile_id="line-1",
        revision=1,
        display_name="一号测试工位",
        power_profile_id="psw-main",
        motion_profile_id="motion-main",
        reference_profile_id="ms6222-main",
        branch_count=branch_count,
        branch_current_a=branch_current_a,
        report_branding={
            "company_name": "软赫",
            "header": "试产质量中心",
            "footer": "受控文件",
            "tester_role": "测试员",
            "reviewer_role": "审核员",
        },
    )


def _store(tmp_path: Path, *, power=None, station=None) -> ProductionConfigurationStore:
    store = ProductionConfigurationStore(tmp_path / "production_configuration.json")
    store.save_catalog([_product()], [power or _power()], [station or _station()])
    return store


def test_afd01c_times_two_resolves_supply_and_recipe_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)

    resolved = store.resolve("afd01c-standard", "line-1", 2)
    recipe = ProductionRecipe.from_mapping(resolved.to_recipe_payload())

    assert resolved.total_current_a == pytest.approx(16.0)
    assert resolved.total_power_w == pytest.approx(192.0)
    assert recipe.product == "afd01c"
    assert recipe.target_device_count == 2
    assert recipe.payload["resolved_configuration"]["combined_supply"] == {
        "voltage_v": 12.0,
        "current_a": 16.0,
        "power_w": 192.0,
    }


@pytest.mark.parametrize(
    ("power", "station", "message"),
    [
        (_power(rated_current_a=15.0), _station(), "required current"),
        (_power(), _station(branch_count=1), "branch count"),
        (_power(), _station(branch_current_a=7.0), "branch rating"),
    ],
)
def test_resolve_rejects_insufficient_station_capacity(
    tmp_path: Path,
    power: PowerSupplyProfile,
    station: StationProfile,
    message: str,
) -> None:
    store = _store(tmp_path, power=power, station=station)
    with pytest.raises(ProductionConfigurationError, match=message):
        store.resolve("afd01c-standard", "line-1", 2)


def test_bundle_round_trip_and_revision_conflict_are_atomic(tmp_path: Path) -> None:
    source = _store(tmp_path / "source")
    bundle = source.export_bundle(tmp_path / "line-1.production.json")
    destination = ProductionConfigurationStore(
        tmp_path / "destination" / "production_configuration.json"
    )

    destination.import_bundle(bundle)
    resolved = destination.resolve("afd01c-standard", "line-1", 2)
    assert resolved.sha256 == source.resolve("afd01c-standard", "line-1", 2).sha256
    assert resolved.station_profile.report_branding["company_name"] == "软赫"

    payload = json.loads(bundle.read_text(encoding="utf-8"))
    payload["product_templates"][0]["display_name"] = "冲突内容"
    payload["product_templates"][0]["sha256"] = ProductTestTemplate.from_mapping(
        payload["product_templates"][0]
    ).sha256
    conflict = tmp_path / "conflict.production.json"
    conflict.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    before = destination.path.read_bytes()
    with pytest.raises(ProductionConfigurationError, match="configuration conflict"):
        destination.import_bundle(conflict)
    assert destination.path.read_bytes() == before


def test_bundle_rejects_tampered_hash_before_writing(tmp_path: Path) -> None:
    source = _store(tmp_path / "source")
    bundle = source.export_bundle(tmp_path / "bundle.json")
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    payload["power_profiles"][0]["host"] = "192.168.1.200"
    bundle.write_text(json.dumps(payload), encoding="utf-8")
    destination = ProductionConfigurationStore(tmp_path / "target.json")

    with pytest.raises(ProductionConfigurationError, match="SHA-256"):
        destination.import_bundle(bundle)
    assert not destination.path.exists()
