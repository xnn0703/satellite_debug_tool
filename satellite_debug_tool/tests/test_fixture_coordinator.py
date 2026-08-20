"""Shared fixture barriers and evidence-level rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from satellite_debug_tool.core.production import (
    EvidenceLevel,
    FixtureCoordinator,
    FixtureCoordinatorError,
    FixtureReadiness,
    ProductionRecipe,
    ProductionResultStore,
    ReferenceStatus,
)
from satellite_debug_tool.tests.test_production_recipe import valid_recipe


def _ready(device_id: str, **overrides) -> FixtureReadiness:
    values = {
        "device_id": device_id,
        "recorder_armed": True,
        "identity_valid": True,
        "recipe_locked": True,
        "fixtures_ready": True,
        "safety_confirmed": True,
    }
    values.update(overrides)
    return FixtureReadiness(**values)


@pytest.fixture
def coordinator(tmp_path: Path):
    store = ProductionResultStore(tmp_path / "results.sqlite3")
    recipe = ProductionRecipe.from_mapping(valid_recipe())
    store.create_batch(
        "BATCH-001", recipe, operator="operator-a", output_dir=tmp_path / "out"
    )
    value = FixtureCoordinator(store)
    yield value, store
    store.close()


def test_barrier_rejects_any_unready_device(coordinator) -> None:
    value, _store = coordinator
    with pytest.raises(FixtureCoordinatorError, match="AFD01-B=recorder_not_armed"):
        value.prepare_action(
            "BATCH-001",
            "rocking",
            [_ready("AFD01-A"), _ready("AFD01-B", recorder_armed=False)],
        )


def test_evidence_can_only_increase(coordinator) -> None:
    value, store = coordinator
    action = value.prepare_action(
        "BATCH-001",
        "rocking",
        [_ready("AFD01-A"), _ready("AFD01-B")],
        command={"roll_deg": 1.0},
    )
    action = value.mark_command_sent(action.action_id)
    assert action.evidence_level == EvidenceLevel.COMMAND_SENT
    action = value.mark_motion_observed(action.action_id)
    assert action.evidence_level == EvidenceLevel.MOTION_OBSERVED

    with pytest.raises(FixtureCoordinatorError, match="cannot be downgraded"):
        value._update(
            action.action_id,
            status=action.status,
            evidence_level=EvidenceLevel.COMMAND_SENT,
        )
    assert any(
        event["event_type"] == "fixture_action_updated"
        for event in store.list_events("BATCH-001")
    )


def test_pose_verified_requires_qualified_reference(coordinator) -> None:
    value, _store = coordinator
    action = value.prepare_action("BATCH-001", "rocking", [_ready("AFD01-A")])
    value.mark_command_sent(action.action_id)
    value.mark_motion_observed(action.action_id)

    with pytest.raises(FixtureCoordinatorError, match="qualified reference"):
        value.mark_pose_verified(action.action_id)

    value.set_reference_status(
        ReferenceStatus(
            connected=True,
            qualified=True,
            qualification_id="MS6222-Q-2026-001",
        )
    )
    verified = value.mark_pose_verified(action.action_id)
    assert verified.evidence_level == EvidenceLevel.POSE_VERIFIED
    assert verified.result["reference_qualification_id"] == "MS6222-Q-2026-001"
