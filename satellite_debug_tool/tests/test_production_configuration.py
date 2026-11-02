"""M19-A.4 production configuration catalog and bundle tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from satellite_debug_tool.core.production import (
    PowerSupplyProfile,
    ReferenceSensorProfile,
    FixtureCalibration,
    FixtureCalibrationStore,
    FixtureProfileStore,
    ProductTestTemplate,
    ProductionConfigurationError,
    ProductionConfigurationStore,
    ProductionRecipe,
    StationProfile,
    fixture_model_preset,
    builtin_product_templates,
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


def test_m51_builtin_electrical_catalog_and_custom_copy_preserve_hidden_fields() -> None:
    catalog = builtin_product_templates()

    assert [(item.product, item.supply_voltage_v, item.per_device_current_a) for item in catalog] == [
        (product, voltage, 8.0 if product == "afd01b2" and voltage == 24.0 else current)
        for product in ("afd01b2", "afd01c", "afd01d")
        for voltage, current in ((12.0, 9.0), (24.0, 5.0))
    ]
    assert {item.revision for item in catalog} == {2}
    source = catalog[0]
    copied = source.custom_copy(template_id="customer-48v", display_name="客户 48 V")
    assert copied.source == "custom"
    assert copied.source_template_id == source.template_id
    assert copied.source_revision == source.revision
    assert copied.tests == source.tests
    assert copied.expected == source.expected
    assert copied.tests is not source.tests


def test_m54_new_recipe_freezes_per_test_time_and_policy_without_rewriting_template(tmp_path: Path) -> None:
    from dataclasses import replace
    store = _store(tmp_path)
    resolved = store.resolve("afd01c-standard", "line-1", 1)
    source = replace(resolved.product_template, tests={
        "static_acquisition": {"enabled": True, "duration_s": 1800, "customer_note": "retained"},
        "locked_rocking": {"enabled": True, "duration_s": 3600},
    })
    original = source.to_payload()
    payload = replace(resolved, product_template=source).to_recipe_payload()
    recipe = ProductionRecipe.from_mapping(payload)
    assert recipe.schema_version == 2
    assert recipe.duration_for("static_acquisition") == 1800
    assert recipe.duration_for("locked_rocking") == 3600
    motion = payload["fixtures"]["motion_platform"]["profile"]
    assert motion["execution_mode"] == "until_observation_complete"
    assert "steady_duration_s" not in motion
    assert payload["duration_policy"]["tracking_criteria"]["acquisition_timeout_s"] == 300
    assert payload["duration_policy"]["window_includes_acquisition"] is True
    assert "minimum_effective_observation_s" not in payload["duration_policy"]
    assert "convergence_is_outside_observation" not in payload["duration_policy"]
    assert source.to_payload() == original
    assert recipe.payload["tests"]["static_acquisition"]["customer_note"] == "retained"


@pytest.mark.parametrize("count,current,power", [(1, 8, 192), (2, 16, 384), (3, 24, 576)])
def test_m54_b2_24v_capacity_uses_eight_amps_per_device(tmp_path, count, current, power):
    store = _store(tmp_path)
    resolved = store.resolve("builtin-afd01b2-24v", "line-1", count)
    assert resolved.total_current_a == current
    assert resolved.total_power_w == power


def test_m54_four_b2_units_exceed_psw_capacity_and_five_amp_branch_is_insufficient(tmp_path):
    store = _store(tmp_path / "full")
    with pytest.raises(ProductionConfigurationError, match="required current"):
        store.resolve("builtin-afd01b2-24v", "line-1", 4)
    limited = _store(tmp_path / "limited", station=_station(branch_current_a=5))
    with pytest.raises(ProductionConfigurationError, match="branch rating"):
        limited.resolve("builtin-afd01b2-24v", "line-1", 1)


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
            "company_name": "客户公司",
            "header": "试产质量中心",
            "footer": "受控文件",
            "tester_role": "测试员",
            "reviewer_role": "审核员",
        },
    )


def _store(tmp_path: Path, *, power=None, station=None) -> ProductionConfigurationStore:
    profile_store = FixtureProfileStore(tmp_path / "fixture_profiles")
    calibration_store = FixtureCalibrationStore(tmp_path / "fixture_calibrations")
    profile = replace(
        fixture_model_preset("lingjing-a6-200mm").create_profile("motion-main"),
        calibration_id="MS6222-CAL-TEST-0001",
    )
    profile_store.save(profile)
    calibration = FixtureCalibration(
        calibration_id=profile.calibration_id,
        profile_id=profile.profile_id,
        profile_sha256=profile.sha256,
        created_utc=datetime.now(timezone.utc).isoformat(),
        sensor_axis_for_logical=("roll", "pitch", "yaw"),
        logical_signs=(1, 1, 1),
        zero_offsets_deg=(0.0, 0.0, 0.0),
        response_matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        cross_coupling_ratio=(0.0, 0.0, 0.0),
        static_noise_std_deg=(0.01, 0.01, 0.01),
        sample_coverage_ratio=1.0,
        confirmed=True,
    )
    calibration_store.save(calibration)
    reference = ReferenceSensorProfile(
        profile_id="ms6222-main",
        revision=1,
        display_name="MS-6222 主参考",
        driver_id="ms6222_serial_v1",
        serial_port="COM7",
        baud_rate=460800,
        installation_id="ms6222-main-installation",
        calibration_id=profile.calibration_id,
    )
    store = ProductionConfigurationStore(
        tmp_path / "production_configuration.json",
        fixture_profile_store=profile_store,
        calibration_store=calibration_store,
    )
    store.save_catalog(
        [_product()], [power or _power()], [station or _station()], [reference]
    )
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
        tmp_path / "destination" / "production_configuration.json",
        fixture_profile_store=FixtureProfileStore(tmp_path / "destination" / "fixture_profiles"),
        calibration_store=FixtureCalibrationStore(tmp_path / "destination" / "fixture_calibrations"),
    )

    destination.import_bundle(bundle)
    resolved = destination.resolve("afd01c-standard", "line-1", 2)
    assert resolved.sha256 == source.resolve("afd01c-standard", "line-1", 2).sha256
    assert resolved.station_profile.report_branding["company_name"] == "客户公司"

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
    destination = ProductionConfigurationStore(
        tmp_path / "target.json",
        fixture_profile_store=FixtureProfileStore(tmp_path / "target_fixture_profiles"),
        calibration_store=FixtureCalibrationStore(tmp_path / "target_fixture_calibrations"),
    )

    with pytest.raises(ProductionConfigurationError, match="SHA-256"):
        destination.import_bundle(bundle)
    assert not destination.path.exists()
