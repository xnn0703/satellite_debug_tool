"""Shared fixture runtime ownership tests."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from satellite_debug_tool.core.production import (
    FixtureCalibrationStore,
    FixtureLeaseError,
    FixtureProfileStore,
    FixtureRuntime,
    FixtureRuntimeState,
    Ms6222DebugError,
)


def test_runtime_owns_one_motion_and_reference_lease(tmp_path) -> None:
    runtime = FixtureRuntime(
        profile_store=FixtureProfileStore(tmp_path / "profiles"),
        calibration_store=FixtureCalibrationStore(tmp_path / "calibrations"),
    )

    motion = runtime.fixture_lease.acquire("customer-dialog")
    reference = runtime.reference_lease.acquire("customer-dialog")

    assert runtime.ownership_snapshot() == {
        "motion_owner": "customer-dialog",
        "reference_owner": "customer-dialog",
    }
    with pytest.raises(FixtureLeaseError, match="customer-dialog"):
        runtime.fixture_lease.acquire("production-batch")
    with pytest.raises(Ms6222DebugError, match="customer-dialog"):
        runtime.reference_lease.acquire("production-batch")

    runtime.fixture_lease.release(motion)
    runtime.reference_lease.release(reference)
    assert runtime.ownership_snapshot() == {
        "motion_owner": "",
        "reference_owner": "",
    }


def test_runtime_publishes_affirmative_binding_facts(tmp_path) -> None:
    runtime = FixtureRuntime(
        profile_store=FixtureProfileStore(tmp_path / "profiles"),
        calibration_store=FixtureCalibrationStore(tmp_path / "calibrations"),
    )
    facts = []
    runtime.state_changed.connect(facts.append)

    runtime.bind_configuration(
        SimpleNamespace(
            motion_profile=SimpleNamespace(
                profile_id="motion-main",
                controller_ipv4="192.168.15.201",
                command_port=7408,
                local_ipv4="192.168.15.101",
                feedback_port=8410,
            ),
            reference_profile=SimpleNamespace(
                profile_id="ms6222-main",
                serial_port="COM7",
                baud_rate=460800,
            ),
        )
    )

    assert runtime.fact("motion").state is FixtureRuntimeState.CONFIGURED
    assert "192.168.15.201:7408" in runtime.fact("motion").evidence
    assert runtime.fact("reference").state is FixtureRuntimeState.CONFIGURED
    assert runtime.fact("reference").evidence == "COM7 @ 460800"
    assert [fact.fixture for fact in facts] == ["motion", "reference"]

    runtime.set_state("reference", FixtureRuntimeState.CONNECTED, "valid frames")
    assert runtime.fact("reference").state is FixtureRuntimeState.CONNECTED
    runtime.clear_bindings()
    assert runtime.fact("motion").state is FixtureRuntimeState.NEEDS_BINDING
