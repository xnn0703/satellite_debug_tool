"""M19 SQLite persistence, transitions, and crash recovery."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from satellite_debug_tool.core.production import (
    AttemptPhase,
    AttemptStatus,
    BatchStatus,
    ProductionRecipe,
    ProductionResultStore,
    ResultStoreError,
)
from satellite_debug_tool.tests.test_production_recipe import valid_recipe


@pytest.fixture
def recipe() -> ProductionRecipe:
    return ProductionRecipe.from_mapping(valid_recipe())


@pytest.fixture
def store(tmp_path: Path):
    value = ProductionResultStore(tmp_path / "results.sqlite3")
    yield value
    value.close()


def _create_batch(store: ProductionResultStore, recipe: ProductionRecipe) -> None:
    store.create_batch(
        "BATCH-001",
        recipe,
        operator="operator-a",
        output_dir=store.path.parent / "output",
    )


def test_batch_recipe_and_device_identity_are_persisted(
    store: ProductionResultStore, recipe: ProductionRecipe
) -> None:
    _create_batch(store, recipe)
    batch = store.get_batch("BATCH-001")
    assert batch["status"] == BatchStatus.READY.value
    assert batch["recipe_sha256"] == recipe.sha256
    assert batch["recipe_json"]["recipe_id"] == recipe.recipe_id

    for slot in range(1, 5):
        store.register_device(
            "BATCH-001",
            serial_number=f"AFD01-{slot}",
            slot=slot,
            endpoint_ip=f"192.168.1.{11 + slot}",
            endpoint_port=4004,
        )
    assert [row["slot"] for row in store.list_devices("BATCH-001")] == [1, 2, 3, 4]

    with pytest.raises(ResultStoreError, match="more than four"):
        store.register_device("BATCH-001", serial_number="AFD01-5", slot=1)


def test_hardware_identity_is_persisted_and_unique(
    store: ProductionResultStore, recipe: ProductionRecipe
) -> None:
    _create_batch(store, recipe)
    first = store.register_device(
        "BATCH-001",
        serial_number="AFD01-202607N001",
        slot=1,
        device_uid="123456789abcdef00badbeef",
        mac_address="4a-65-a6-99-9e-4b",
        mac_source=1,
    )
    assert first["device_uid"] == "123456789ABCDEF00BADBEEF"
    assert first["mac_address"] == "4A:65:A6:99:9E:4B"
    assert store.get_device_by_uid(
        "BATCH-001", "123456789ABCDEF00BADBEEF"
    )["serial_number"] == "AFD01-202607N001"

    store.register_device("BATCH-001", serial_number="AFD01-202607N002", slot=2)
    updated = store.update_device_identity(
        "BATCH-001",
        "AFD01-202607N002",
        device_uid="111111112222222233333333",
        mac_address="02:00:00:00:00:02",
        mac_source=1,
    )
    assert updated["device_uid"] == "111111112222222233333333"
    store.register_device("BATCH-001", serial_number="AFD01-202607N003", slot=3)
    with pytest.raises(ResultStoreError, match="duplicate UID or MAC"):
        store.update_device_identity(
            "BATCH-001",
            "AFD01-202607N003",
            device_uid="123456789ABCDEF00BADBEEF",
        )


def test_schema_v1_database_is_migrated_for_hardware_identity(
    tmp_path: Path, recipe: ProductionRecipe
) -> None:
    path = tmp_path / "schema-v1.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_info(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_info(key, value) VALUES ('schema_version', '1');
        CREATE TABLE devices(
            batch_id TEXT NOT NULL,
            serial_number TEXT NOT NULL,
            slot INTEGER NOT NULL,
            endpoint_ip TEXT NOT NULL DEFAULT '',
            endpoint_port INTEGER,
            hardware_type TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_utc TEXT NOT NULL,
            PRIMARY KEY(batch_id, serial_number),
            UNIQUE(batch_id, slot)
        );
        """
    )
    connection.close()

    migrated = ProductionResultStore(path)
    try:
        assert migrated._connection.execute(
            "SELECT value FROM schema_info WHERE key = 'schema_version'"
        ).fetchone()[0] == "3"
        columns = {
            row[1] for row in migrated._connection.execute("PRAGMA table_info(devices)")
        }
        assert {"device_uid", "mac_address", "mac_source"} <= columns
    finally:
        migrated.close()


def test_duplicate_slot_or_serial_is_rejected(
    store: ProductionResultStore, recipe: ProductionRecipe
) -> None:
    _create_batch(store, recipe)
    store.register_device("BATCH-001", serial_number="AFD01-A", slot=1)
    with pytest.raises(ResultStoreError, match="duplicate"):
        store.register_device("BATCH-001", serial_number="AFD01-B", slot=1)
    with pytest.raises(ResultStoreError, match="duplicate"):
        store.register_device("BATCH-001", serial_number="AFD01-A", slot=2)


def test_device_endpoint_change_is_persisted_as_an_event(
    store: ProductionResultStore, recipe: ProductionRecipe
) -> None:
    _create_batch(store, recipe)
    store.register_device(
        "BATCH-001",
        serial_number="AFD01-A",
        slot=1,
        endpoint_ip="192.168.1.12",
        endpoint_port=4004,
    )

    device = store.update_device_endpoint(
        "BATCH-001",
        "AFD01-A",
        endpoint_ip="192.168.1.22",
        endpoint_port=4004,
    )

    assert device["endpoint_ip"] == "192.168.1.22"
    event = store.list_events("BATCH-001")[-1]
    assert event["event_type"] == "device_endpoint_changed"
    assert event["payload_json"]["old_endpoint"] == "192.168.1.12:4004"


def test_start_batch_accepts_one_participant_and_freezes_attempts(
    store: ProductionResultStore, recipe: ProductionRecipe
) -> None:
    _create_batch(store, recipe)
    store.register_device(
        "BATCH-001",
        serial_number="AFD01-A",
        slot=1,
        device_uid="111111112222222233333333",
        mac_address="02:00:00:00:00:01",
    )
    store.register_device(
        "BATCH-001",
        serial_number="AFD01-OFFLINE",
        slot=2,
    )

    batch = store.start_batch(
        "BATCH-001",
        ("AFD01-A",),
        ("firmware_verification", "parameter_verification"),
    )

    assert batch["status"] == BatchStatus.RUNNING.value
    assert [
        (device["serial_number"], device["status"])
        for device in store.list_devices("BATCH-001")
    ] == [
        ("AFD01-A", "participant"),
        ("AFD01-OFFLINE", "excluded"),
    ]
    attempts = store.list_attempts("BATCH-001")
    assert len(attempts) == 2
    assert {attempt["serial_number"] for attempt in attempts} == {"AFD01-A"}
    frozen = next(
        event
        for event in store.list_events("BATCH-001")
        if event["event_type"] == "batch_participants_frozen"
    )
    assert frozen["payload_json"] == {
        "count": 1,
        "participants": [
            {
                "device_uid": "111111112222222233333333",
                "mac_address": "02:00:00:00:00:01",
                "serial_number": "AFD01-A",
                "slot": 1,
            }
        ],
    }
    with pytest.raises(ResultStoreError, match="participants are already frozen"):
        store.register_device(
            "BATCH-001",
            serial_number="AFD01-LATE",
            slot=3,
        )


def test_start_batch_rejects_empty_or_duplicate_participants(
    store: ProductionResultStore, recipe: ProductionRecipe
) -> None:
    _create_batch(store, recipe)
    store.register_device("BATCH-001", serial_number="AFD01-A", slot=1)

    with pytest.raises(ResultStoreError, match="one to four"):
        store.start_batch("BATCH-001", (), ("firmware_verification",))
    with pytest.raises(ResultStoreError, match="must be unique"):
        store.start_batch(
            "BATCH-001",
            ("AFD01-A", "AFD01-A"),
            ("firmware_verification",),
        )


def test_attempt_transition_is_append_only_and_validated(
    store: ProductionResultStore, recipe: ProductionRecipe
) -> None:
    _create_batch(store, recipe)
    store.register_device("BATCH-001", serial_number="AFD01-A", slot=1)
    store.transition_batch("BATCH-001", BatchStatus.RUNNING)

    first = store.create_attempt("BATCH-001", "AFD01-A", "locked_rocking")
    store.transition_attempt(first["attempt_id"], AttemptStatus.WAITING_PREREQUISITE)
    store.transition_attempt(first["attempt_id"], AttemptStatus.ARMED)
    store.transition_attempt(
        first["attempt_id"],
        AttemptStatus.RUNNING,
        phase=AttemptPhase.CONVERGING,
    )
    store.transition_attempt(first["attempt_id"], AttemptStatus.ANALYZING)
    finished = store.transition_attempt(
        first["attempt_id"], AttemptStatus.FAIL, result={"reason": "lost lock"}
    )
    assert finished["result_json"] == {"reason": "lost lock"}

    second = store.create_attempt("BATCH-001", "AFD01-A", "locked_rocking")
    assert second["ordinal"] == 2
    assert second["attempt_id"] != first["attempt_id"]
    with pytest.raises(ResultStoreError, match="invalid attempt transition"):
        store.transition_attempt(second["attempt_id"], AttemptStatus.PASS)


def test_recovery_marks_running_work_incomplete(
    store: ProductionResultStore, recipe: ProductionRecipe
) -> None:
    _create_batch(store, recipe)
    store.register_device("BATCH-001", serial_number="AFD01-A", slot=1)
    store.transition_batch("BATCH-001", BatchStatus.RUNNING)
    attempt = store.create_attempt("BATCH-001", "AFD01-A", "static_acquisition")
    store.transition_attempt(attempt["attempt_id"], AttemptStatus.WAITING_PREREQUISITE)
    store.transition_attempt(attempt["attempt_id"], AttemptStatus.ARMED)
    store.transition_attempt(attempt["attempt_id"], AttemptStatus.RUNNING)

    recovered = store.recover_interrupted()

    assert recovered == {"batches": 1, "attempts": 1}
    assert store.get_batch("BATCH-001")["status"] == BatchStatus.INCOMPLETE.value
    assert store.get_attempt(attempt["attempt_id"])["status"] == AttemptStatus.INCOMPLETE.value
    recovery_events = [
        event for event in store.list_events("BATCH-001")
        if event["event_type"].endswith("recovered_incomplete")
    ]
    assert [event["event_type"] for event in recovery_events] == [
        "attempt_recovered_incomplete",
        "batch_recovered_incomplete",
    ]
    assert recovery_events[0]["payload_json"]["from"] == AttemptStatus.RUNNING.value


def test_metric_keeps_json_value_and_evidence(
    store: ProductionResultStore, recipe: ProductionRecipe
) -> None:
    _create_batch(store, recipe)
    store.register_device("BATCH-001", serial_number="AFD01-A", slot=1)
    store.transition_batch("BATCH-001", BatchStatus.RUNNING)
    attempt = store.create_attempt("BATCH-001", "AFD01-A", "static_acquisition")

    metric_id = store.record_metric(
        attempt["attempt_id"],
        "roll_rms",
        0.12,
        unit="deg",
        algorithm_version="m19-attitude-v1",
        sample_count=360000,
        evidence_level="pose_verified",
    )
    assert metric_id > 0
