"""M19 production recipe validation and immutable snapshots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from satellite_debug_tool.core.production import (
    ProductionRecipe,
    RecipeValidationError,
)
from satellite_debug_tool.core.product import production_recipe_product_policy


def valid_recipe(*, duration_s: int = 3600) -> dict:
    return {
        "schema_version": 1,
        "recipe_id": "AFD01-PILOT-R1",
        "product": "afd01",
        "expected": {
            "main_firmware": {"match": "exact", "value": "0.0.130 beta"},
            "parameters": {},
        },
        "fixtures": {
            "power_profile": "pilot_line_power_01",
            "motion_platform": {
                "driver": "lingjing_udp_v0_3",
                "command_family": "A6T",
                "center_pose": {
                    "roll_deg": 0,
                    "pitch_deg": 0,
                    "yaw_deg": 0,
                    "x_mm": 0,
                    "y_mm": 0,
                    "z_mm": 100,
                },
                "reset_pose": {
                    "roll_deg": 0,
                    "pitch_deg": 0,
                    "yaw_deg": 0,
                    "x_mm": 0,
                    "y_mm": 0,
                    "z_mm": 0,
                },
                "limits": {
                    "roll_abs_deg": 15,
                    "pitch_abs_deg": 15,
                    "yaw_abs_deg": 30,
                },
                "profile": {
                    "profile_id": "marine-v1",
                    "sample_period_ms": 100,
                    "steady_duration_s": duration_s,
                    "roll": {
                        "amplitude_deg": 15,
                        "frequency_hz": 0.25,
                        "phase_deg": 0,
                    },
                    "pitch": {
                        "amplitude_deg": 15,
                        "frequency_hz": 0.25,
                        "phase_deg": 90,
                    },
                    "yaw": {
                        "amplitude_deg": 15,
                        "frequency_hz": 0.1,
                        "phase_deg": 0,
                    },
                },
            },
            "vehicle": {"driver": "manual"},
        },
        "duration_policy": {
            "minimum_effective_observation_s": duration_s,
            "convergence_is_outside_observation": True,
            "report_first_last_window_s": 300,
        },
        "tests": {
            "static_acquisition": {"enabled": True},
            "locked_rocking": {"enabled": True},
        },
    }


def test_recipe_hash_is_independent_of_key_order() -> None:
    first = valid_recipe()
    second = json.loads(json.dumps(first))
    second = dict(reversed(list(second.items())))

    recipe_a = ProductionRecipe.from_mapping(first)
    recipe_b = ProductionRecipe.from_mapping(second)

    assert recipe_a.sha256 == recipe_b.sha256
    assert not recipe_a.engineering_only
    assert recipe_a.payload == first


def test_short_recipe_is_explicitly_engineering_only() -> None:
    recipe = ProductionRecipe.from_mapping(valid_recipe(duration_s=30))
    assert recipe.engineering_only


def test_afd01c_recipe_is_an_explicit_product_contract() -> None:
    payload = valid_recipe()
    payload["recipe_id"] = "AFD01C-PILOT-R1"
    payload["product"] = "afd01c"

    recipe = ProductionRecipe.from_mapping(payload)

    assert recipe.product == "afd01c"


def test_recipe_product_is_persisted_as_the_registered_canonical_value() -> None:
    payload = valid_recipe()
    payload["recipe_id"] = "AFD01C-PILOT-R1"
    payload["product"] = "  AFD01C  "

    recipe = ProductionRecipe.from_mapping(payload)

    assert recipe.product == "afd01c"
    assert recipe.payload["product"] == "afd01c"

    canonical_payload = valid_recipe()
    canonical_payload["recipe_id"] = "AFD01C-PILOT-R1"
    canonical_payload["product"] = "afd01c"
    assert recipe.sha256 == ProductionRecipe.from_mapping(canonical_payload).sha256


def test_recipe_products_are_resolved_from_the_registered_policy() -> None:
    assert production_recipe_product_policy("AFD01") is not None
    assert production_recipe_product_policy("AFD01A") is not None
    assert production_recipe_product_policy("AFD01B2") is not None
    assert production_recipe_product_policy("AFD01C") is not None
    assert production_recipe_product_policy("ESA01") is None


@pytest.mark.parametrize("product", ("afd01a", "afd01b2"))
def test_new_afd01_variants_have_independent_recipe_products(product: str) -> None:
    payload = valid_recipe()
    payload["product"] = product.upper()
    recipe = ProductionRecipe.from_mapping(payload)
    assert recipe.product == product


def test_recipe_snapshot_is_canonical_and_immutable(tmp_path: Path) -> None:
    source = valid_recipe()
    recipe = ProductionRecipe.from_mapping(source)
    source["recipe_id"] = "MUTATED"

    destination = recipe.write_snapshot(tmp_path / "recipe.snapshot.json")
    loaded = ProductionRecipe.from_path(destination)

    assert loaded.recipe_id == "AFD01-PILOT-R1"
    assert loaded.sha256 == recipe.sha256
    assert destination.read_text(encoding="utf-8").endswith("\n")


def test_missing_recipe_file_is_reported_as_validation_error(tmp_path: Path) -> None:
    with pytest.raises(RecipeValidationError, match="cannot read recipe"):
        ProductionRecipe.from_path(tmp_path / "missing.json")


def test_parameter_comparison_rules_are_validated() -> None:
    payload = valid_recipe()
    payload["expected"]["parameters"] = {
        "ext_ref_freq": {"type": "float", "value": 10.0, "abs_tol": 0.01},
        "modem_baud": {"type": "int", "one_of": [115200, 921600]},
        "ip": {"type": "ip", "per_device": True},
    }
    ProductionRecipe.from_mapping(payload)

    payload["expected"]["parameters"]["ext_ref_freq"]["range"] = [9.9, 10.1]
    with pytest.raises(RecipeValidationError, match="conflicting comparison"):
        ProductionRecipe.from_mapping(payload)


def test_motion_amplitude_cannot_exceed_fixture_limit() -> None:
    payload = valid_recipe()
    payload["fixtures"]["motion_platform"]["profile"]["roll"][
        "amplitude_deg"
    ] = 16
    with pytest.raises(RecipeValidationError, match="exceeds its limit"):
        ProductionRecipe.from_mapping(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data.update(schema_version=2), "schema_version"),
        (lambda data: data.update(product="esa01"), "product"),
        (
            lambda data: data["fixtures"]["motion_platform"].update(
                command_family="A3T"
            ),
            "command_family",
        ),
        (
            lambda data: data["fixtures"]["motion_platform"]["center_pose"].update(
                z_mm=0
            ),
            "center_pose.z_mm",
        ),
        (
            lambda data: data["duration_policy"].update(
                convergence_is_outside_observation=False
            ),
            "convergence_is_outside_observation",
        ),
    ],
)
def test_invalid_recipe_is_rejected(mutate, message: str) -> None:
    payload = valid_recipe()
    mutate(payload)
    with pytest.raises(RecipeValidationError, match=message):
        ProductionRecipe.from_mapping(payload)
