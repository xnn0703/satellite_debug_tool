"""M48 批次流程、夹具相关性和设备图槽位的领域投影。"""

from __future__ import annotations

import pytest

from satellite_debug_tool.core.production.presentation import (
    SCENARIO_TEST_IDS,
    SUMMARY_TEST_IDS,
    TEST_ORDER,
    chart_slots,
    enabled_production_tests,
    fixture_requirements,
)
from satellite_debug_tool.core.production.recipe import ProductionRecipe
from satellite_debug_tool.tests.test_production_recipe import valid_recipe


def _recipe(*enabled: str, disabled: tuple[str, ...] = ()) -> ProductionRecipe:
    payload = valid_recipe()
    payload["tests"] = {
        **{test_id: {"enabled": True} for test_id in enabled},
        **{test_id: {"enabled": False} for test_id in disabled},
    }
    return ProductionRecipe.from_mapping(payload)


def test_test_groups_cover_known_flow_in_declared_order() -> None:
    assert TEST_ORDER == (
        "firmware_verification", "parameter_verification", "static_acquisition",
        "locked_rocking", "power_on_rocking", "locked_drive", "power_on_drive",
        "gnss", "imu_static", "external_ins", "whole_navigation",
    )
    assert SCENARIO_TEST_IDS == TEST_ORDER[2:7]
    assert SUMMARY_TEST_IDS == TEST_ORDER[7:]


def test_recipe_absence_has_empty_flow_and_fixture_requirements() -> None:
    assert enabled_production_tests(None) == ()
    assert fixture_requirements(None) == frozenset()


def test_explicitly_disabled_checks_stay_out_of_execution() -> None:
    recipe = _recipe("gnss", disabled=("firmware_verification", "parameter_verification"))
    assert enabled_production_tests(recipe) == ("gnss",)


@pytest.mark.parametrize("scenario", SCENARIO_TEST_IDS)
def test_scenario_preserves_explicitly_disabled_navigation(scenario: str) -> None:
    recipe = _recipe(scenario, disabled=("whole_navigation",))
    assert enabled_production_tests(recipe) == (scenario,)


def test_explicit_navigation_stays_visible_for_execution_readiness() -> None:
    recipe = _recipe("whole_navigation", "gnss")
    assert enabled_production_tests(recipe) == (
        "gnss", "whole_navigation",
    )


def test_known_tests_follow_flow_order_and_disabled_tests_are_absent() -> None:
    recipe = _recipe("external_ins", "power_on_rocking", "imu_static", "gnss", "static_acquisition",
                     disabled=("locked_rocking", "locked_drive"))
    assert enabled_production_tests(recipe) == (
        "static_acquisition", "power_on_rocking", "gnss", "imu_static", "external_ins",
    )


def test_unknown_enabled_tests_remain_visible_after_known_tests() -> None:
    recipe = _recipe("vendor_z", "gnss", "vendor_a", disabled=("vendor_disabled",))
    assert enabled_production_tests(recipe) == (
        "gnss", "vendor_a", "vendor_z",
    )


@pytest.mark.parametrize("test_id", ("gnss", "imu_static", "external_ins", "vendor_test"))
def test_standalone_summary_declares_power_only(test_id: str) -> None:
    assert fixture_requirements(_recipe(test_id)) == frozenset({"power"})


@pytest.mark.parametrize("scenario", SCENARIO_TEST_IDS[:3])
def test_bench_scenarios_require_motion_and_reference(scenario: str) -> None:
    assert fixture_requirements(_recipe(scenario)) == frozenset({"power", "motion", "reference"})


@pytest.mark.parametrize("scenario", SCENARIO_TEST_IDS[3:])
def test_vehicle_scenarios_require_operator_gate(scenario: str) -> None:
    assert fixture_requirements(_recipe(scenario)) == frozenset({"power", "vehicle"})


def test_combined_scenarios_preserve_all_fixture_requirements() -> None:
    assert fixture_requirements(_recipe("static_acquisition", "locked_drive")) == frozenset({
        "power", "motion", "reference", "vehicle",
    })


@pytest.mark.parametrize("count", (1, 2, 3, 4))
def test_chart_count_matches_requested_devices_before_freeze(count: int) -> None:
    assert chart_slots(count, (4, 2, 1, 3)) == (4, 2, 1, 3)[:count]


def test_chart_candidates_keep_priority_and_have_independent_unbound_slots() -> None:
    assert chart_slots(4, (3, 3, 1)) == (3, 1, None, None)
    assert chart_slots(1, ()) == (None,)


def test_frozen_slot_four_remains_the_only_chart_for_single_participant() -> None:
    assert chart_slots(1, (1, 2, 3, 4), (4,)) == (4,)


def test_frozen_participants_own_order_and_count() -> None:
    assert chart_slots(1, (1,), (4, 2, 4, 3)) == (4, 2, 3)
    assert chart_slots(4, (1, 2, 3, 4), (2,)) == (2,)
    assert chart_slots(2, (1, 2), ()) == ()


@pytest.mark.parametrize("count", (0, 5, -1, True, 1.0, "1"))
def test_chart_count_requires_integer_within_supported_capacity(count: object) -> None:
    with pytest.raises(ValueError, match="target_count"):
        chart_slots(count, ())  # type: ignore[arg-type]


@pytest.mark.parametrize("slot", (0, 5, -1, True, 1.0, None, "1"))
def test_chart_slot_requires_real_integer_identity(slot: object) -> None:
    with pytest.raises(ValueError, match="chart slots"):
        chart_slots(1, (slot,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="chart slots"):
        chart_slots(1, (), (slot,))  # type: ignore[arg-type]
