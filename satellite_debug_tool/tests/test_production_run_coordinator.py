"""First-flow automatic production coordinator tests."""

from __future__ import annotations

from pathlib import Path

from satellite_debug_tool.core.production import (
    AttemptStatus,
    DeviceStepResult,
    ProductionRecipe,
    ProductionResultStore,
    ProductionRunCoordinator,
    ProductionRunState,
)
from satellite_debug_tool.tests.test_production_recipe import valid_recipe


def _running_store(tmp_path: Path, test_ids: tuple[str, ...]) -> ProductionResultStore:
    store = ProductionResultStore(tmp_path / "automatic.sqlite3")
    recipe = ProductionRecipe.from_mapping(valid_recipe())
    store.create_batch(
        "BATCH-AUTO", recipe, operator="operator", output_dir=tmp_path / "output"
    )
    for slot, serial in enumerate(("AFD01C-A", "AFD01C-B"), start=1):
        store.register_device("BATCH-AUTO", serial_number=serial, slot=slot)
    store.start_batch("BATCH-AUTO", ("AFD01C-A", "AFD01C-B"), test_ids)
    return store


def test_coordinator_requires_explicit_evidence_and_preserves_device_results(
    tmp_path: Path,
) -> None:
    test_ids = ("firmware_verification", "parameter_verification")
    store = _running_store(tmp_path, test_ids)
    coordinator = ProductionRunCoordinator(store, "BATCH-AUTO", test_ids=test_ids)

    first = coordinator.start()
    assert first is not None
    assert first.test_id == "firmware_verification"
    assert coordinator.state == ProductionRunState.WAITING_PREREQUISITE
    coordinator.confirm_prerequisites(evidence={"identity_generation": 4})
    coordinator.confirm_started(evidence={"request_id": 17})
    second = coordinator.complete_step(
        {
            "AFD01C-A": DeviceStepResult(True, {"version": "1.0.0"}),
            "AFD01C-B": DeviceStepResult(False, {"reason": "version mismatch"}),
        }
    )

    assert second is not None
    assert second.test_id == "parameter_verification"
    attempts = {
        (row["serial_number"], row["test_id"]): row
        for row in store.list_attempts("BATCH-AUTO")
    }
    passed = attempts[("AFD01C-A", "firmware_verification")]
    failed = attempts[("AFD01C-B", "firmware_verification")]
    assert passed["status"] == AttemptStatus.PASS.value
    assert failed["status"] == AttemptStatus.FAIL.value
    assert passed["result_json"]["prerequisite_evidence"] == {
        "identity_generation": 4
    }
    assert passed["result_json"]["start_evidence"] == {"request_id": 17}
    assert failed["result_json"]["reason"] == "version mismatch"
    store.close()


def test_shared_fixture_failure_marks_each_current_attempt_incomplete(
    tmp_path: Path,
) -> None:
    test_ids = ("locked_rocking",)
    store = _running_store(tmp_path, test_ids)
    coordinator = ProductionRunCoordinator(store, "BATCH-AUTO", test_ids=test_ids)
    coordinator.start()
    coordinator.confirm_prerequisites(evidence={"calibration_id": "CAL-1"})
    coordinator.confirm_started(evidence={"fixture_action_id": "MOVE-1"})

    coordinator.fail_shared_step(
        "MS-6222 reference became stale", evidence={"age_ms": 1200}
    )

    assert coordinator.state == ProductionRunState.INCOMPLETE
    attempts = store.list_attempts("BATCH-AUTO")
    assert {row["status"] for row in attempts} == {AttemptStatus.INCOMPLETE.value}
    assert all(
        row["result_json"]["shared_evidence"] == {"age_ms": 1200}
        for row in attempts
    )
    store.close()


def test_first_flow_advances_to_complete_only_after_all_step_results(
    tmp_path: Path,
) -> None:
    test_ids = ("static_acquisition", "locked_rocking", "power_on_rocking")
    store = _running_store(tmp_path, test_ids)
    coordinator = ProductionRunCoordinator(store, "BATCH-AUTO", test_ids=test_ids)

    request = coordinator.start()
    while request is not None:
        coordinator.confirm_prerequisites(evidence={"ready": True})
        coordinator.confirm_started(evidence={"window_start_ns": request.ordinal})
        request = coordinator.complete_step(
            {
                serial: DeviceStepResult(True, {"window_complete": True})
                for serial in request.participant_serials
            }
        )

    assert coordinator.state == ProductionRunState.COMPLETE
    assert {
        row["status"] for row in store.list_attempts("BATCH-AUTO")
    } == {AttemptStatus.PASS.value}
    store.close()
