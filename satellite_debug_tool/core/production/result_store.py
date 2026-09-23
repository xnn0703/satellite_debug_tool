"""Crash-safe SQLite persistence for production batches and evidence."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator, Mapping, Optional, Sequence

from .models import (
    ATTEMPT_TRANSITIONS,
    BATCH_TRANSITIONS,
    AttemptPhase,
    AttemptStatus,
    BatchStatus,
    EvidenceLevel,
    FixtureAction,
    coerce_attempt_phase,
    coerce_attempt_status,
    coerce_batch_status,
    coerce_evidence_level,
)
from .recipe import ProductionRecipe


_SCHEMA_VERSION = 3
_ACTIVE_ATTEMPT_STATUSES = (
    AttemptStatus.WAITING_PREREQUISITE.value,
    AttemptStatus.ARMED.value,
    AttemptStatus.RUNNING.value,
    AttemptStatus.ANALYZING.value,
)


class ResultStoreError(RuntimeError):
    pass


class ProductionResultStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(path), timeout=5.0, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if str(path) != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "ProductionResultStore":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def create_batch(
        self,
        batch_id: str,
        recipe: ProductionRecipe,
        *,
        operator: str,
        output_dir: str | Path,
        notes: str = "",
    ) -> dict[str, Any]:
        batch_id = _required_text(batch_id, "batch_id", 96)
        operator = _required_text(operator, "operator", 128)
        output = str(Path(output_dir).expanduser().resolve())
        now = _utc_now()
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO batches(
                        batch_id, recipe_id, recipe_sha256, recipe_json,
                        engineering_only, operator, output_dir, status,
                        created_utc, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        recipe.recipe_id,
                        recipe.sha256,
                        recipe.canonical_json,
                        int(recipe.engineering_only),
                        operator,
                        output,
                        BatchStatus.READY.value,
                        now,
                        str(notes),
                    ),
                )
                self._insert_event(
                    connection,
                    batch_id,
                    None,
                    "batch_created",
                    {"recipe_sha256": recipe.sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise ResultStoreError(f"batch already exists: {batch_id}") from exc
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        row = self._fetchone("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
        if row is None:
            raise ResultStoreError(f"unknown batch: {batch_id}")
        return _row_dict(row)

    def list_batches(self) -> list[dict[str, Any]]:
        return [
            _row_dict(row)
            for row in self._fetchall(
                "SELECT * FROM batches ORDER BY created_utc DESC, batch_id DESC"
            )
        ]

    def transition_batch(
        self, batch_id: str, status: BatchStatus | str
    ) -> dict[str, Any]:
        target = coerce_batch_status(status)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                raise ResultStoreError(f"unknown batch: {batch_id}")
            current = BatchStatus(row["status"])
            if target not in BATCH_TRANSITIONS[current]:
                raise ResultStoreError(
                    f"invalid batch transition: {current.value} -> {target.value}"
                )
            fields: dict[str, Any] = {"status": target.value}
            if target == BatchStatus.RUNNING:
                fields["started_utc"] = _utc_now()
            elif target in {
                BatchStatus.COMPLETED,
                BatchStatus.ABORTED,
                BatchStatus.INCOMPLETE,
            }:
                fields["finished_utc"] = _utc_now()
            _update_fields(connection, "batches", "batch_id", batch_id, fields)
            self._insert_event(
                connection,
                batch_id,
                None,
                "batch_status_changed",
                {"from": current.value, "to": target.value},
            )
        return self.get_batch(batch_id)

    def start_batch(
        self,
        batch_id: str,
        participant_serials: Sequence[str],
        test_ids: Sequence[str],
    ) -> dict[str, Any]:
        """Freeze one to four participants and create their first attempts atomically."""

        participants = tuple(
            _required_text(value, "participant serial number", 128)
            for value in participant_serials
        )
        tests = tuple(_required_text(value, "test_id", 96) for value in test_ids)
        if not (1 <= len(participants) <= 4):
            raise ResultStoreError("a running batch requires one to four participants")
        if len(set(participants)) != len(participants):
            raise ResultStoreError("batch participants must be unique")
        if not tests:
            raise ResultStoreError("a running batch requires at least one test")
        if len(set(tests)) != len(tests):
            raise ResultStoreError("batch test IDs must be unique")

        with self.transaction() as connection:
            batch = connection.execute(
                "SELECT status FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise ResultStoreError(f"unknown batch: {batch_id}")
            if batch["status"] != BatchStatus.READY.value:
                raise ResultStoreError("only a ready batch can be started")

            placeholders = ", ".join("?" for _ in participants)
            rows = connection.execute(
                f"""
                SELECT serial_number, slot, device_uid, mac_address
                FROM devices
                WHERE batch_id = ? AND serial_number IN ({placeholders})
                ORDER BY slot
                """,
                (batch_id, *participants),
            ).fetchall()
            found = {str(row["serial_number"]) for row in rows}
            missing = [serial for serial in participants if serial not in found]
            if missing:
                raise ResultStoreError(
                    "unknown batch participant: " + ", ".join(missing)
                )

            started_utc = _utc_now()
            connection.execute(
                "UPDATE devices SET status = 'excluded' WHERE batch_id = ?",
                (batch_id,),
            )
            connection.execute(
                f"""
                UPDATE devices SET status = 'participant'
                WHERE batch_id = ? AND serial_number IN ({placeholders})
                """,
                (batch_id, *participants),
            )
            connection.execute(
                """
                UPDATE batches SET status = ?, started_utc = ?
                WHERE batch_id = ?
                """,
                (BatchStatus.RUNNING.value, started_utc, batch_id),
            )

            participant_evidence = [
                {
                    "serial_number": str(row["serial_number"]),
                    "slot": int(row["slot"]),
                    "device_uid": str(row["device_uid"] or ""),
                    "mac_address": str(row["mac_address"] or ""),
                }
                for row in rows
            ]
            self._insert_event(
                connection,
                batch_id,
                None,
                "batch_participants_frozen",
                {
                    "count": len(participant_evidence),
                    "participants": participant_evidence,
                },
            )
            self._insert_event(
                connection,
                batch_id,
                None,
                "batch_status_changed",
                {"from": BatchStatus.READY.value, "to": BatchStatus.RUNNING.value},
            )

            for row in rows:
                serial_number = str(row["serial_number"])
                for test_id in tests:
                    cursor = connection.execute(
                        """
                        INSERT INTO attempts(
                            batch_id, serial_number, test_id, ordinal, status,
                            created_utc
                        ) VALUES (?, ?, ?, 1, ?, ?)
                        """,
                        (
                            batch_id,
                            serial_number,
                            test_id,
                            AttemptStatus.PENDING.value,
                            started_utc,
                        ),
                    )
                    self._insert_event(
                        connection,
                        batch_id,
                        int(cursor.lastrowid),
                        "attempt_created",
                        {"test_id": test_id, "ordinal": 1},
                    )
        return self.get_batch(batch_id)

    def register_device(
        self,
        batch_id: str,
        *,
        serial_number: str,
        slot: int,
        endpoint_ip: str = "",
        endpoint_port: Optional[int] = None,
        hardware_type: str = "",
        device_uid: str = "",
        mac_address: str = "",
        mac_source: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        serial = _required_text(serial_number, "serial_number", 128)
        uid = _normalize_device_uid(device_uid)
        mac = _normalize_mac_address(mac_address)
        source = _normalize_mac_source(mac_source)
        if slot not in range(1, 5):
            raise ResultStoreError("slot must be between 1 and 4")
        with self.transaction() as connection:
            batch = connection.execute(
                "SELECT status FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise ResultStoreError(f"unknown batch: {batch_id}")
            if batch["status"] != BatchStatus.READY.value:
                raise ResultStoreError("batch participants are already frozen")
            count = connection.execute(
                "SELECT COUNT(*) FROM devices WHERE batch_id = ?", (batch_id,)
            ).fetchone()[0]
            if count >= 4:
                raise ResultStoreError("a batch cannot contain more than four devices")
            try:
                connection.execute(
                    """
                    INSERT INTO devices(
                        batch_id, serial_number, slot, endpoint_ip, endpoint_port,
                        hardware_type, device_uid, mac_address, mac_source,
                        status, metadata_json, created_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        serial,
                        slot,
                        str(endpoint_ip),
                        endpoint_port,
                        str(hardware_type),
                        uid,
                        mac,
                        source,
                        "registered",
                        _json(metadata or {}),
                        _utc_now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ResultStoreError(
                    f"duplicate device identity or slot in batch: {serial} / {slot}"
                ) from exc
            self._insert_event(
                connection,
                batch_id,
                None,
                "device_registered",
                {
                    "serial_number": serial,
                    "device_uid": uid,
                    "mac_address": mac,
                    "mac_source": source,
                    "slot": slot,
                },
            )
        return self.get_device(batch_id, serial)

    def get_device(self, batch_id: str, serial_number: str) -> dict[str, Any]:
        row = self._fetchone(
            "SELECT * FROM devices WHERE batch_id = ? AND serial_number = ?",
            (batch_id, serial_number),
        )
        if row is None:
            raise ResultStoreError(f"unknown device: {serial_number}")
        return _row_dict(row)

    def list_devices(self, batch_id: str) -> list[dict[str, Any]]:
        return [
            _row_dict(row)
            for row in self._fetchall(
                "SELECT * FROM devices WHERE batch_id = ? ORDER BY slot",
                (batch_id,),
            )
        ]

    def get_device_by_uid(self, batch_id: str, device_uid: str) -> dict[str, Any]:
        uid = _normalize_device_uid(device_uid)
        if not uid:
            raise ResultStoreError("device_uid is required")
        row = self._fetchone(
            "SELECT * FROM devices WHERE batch_id = ? AND device_uid = ?",
            (batch_id, uid),
        )
        if row is None:
            raise ResultStoreError(f"unknown device UID: {uid}")
        return _row_dict(row)

    def update_device_identity(
        self,
        batch_id: str,
        serial_number: str,
        *,
        device_uid: str = "",
        mac_address: str = "",
        mac_source: Optional[int] = None,
    ) -> dict[str, Any]:
        uid = _normalize_device_uid(device_uid)
        mac = _normalize_mac_address(mac_address)
        source = _normalize_mac_source(mac_source)
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT device_uid, mac_address, mac_source FROM devices
                WHERE batch_id = ? AND serial_number = ?
                """,
                (batch_id, serial_number),
            ).fetchone()
            if row is None:
                raise ResultStoreError(f"unknown device: {serial_number}")
            old_uid = str(row["device_uid"] or "")
            old_mac = str(row["mac_address"] or "")
            old_source = row["mac_source"]
            if old_uid and uid and old_uid != uid:
                raise ResultStoreError(
                    f"device UID changed for {serial_number}: {old_uid} -> {uid}"
                )
            if old_mac and mac and old_mac != mac:
                raise ResultStoreError(
                    f"MAC address changed for {serial_number}: {old_mac} -> {mac}"
                )
            if old_source is not None and source is not None and int(old_source) != source:
                raise ResultStoreError(
                    f"MAC source changed for {serial_number}: {old_source} -> {source}"
                )
            merged_uid = uid or old_uid
            merged_mac = mac or old_mac
            merged_source = source if source is not None else old_source
            try:
                connection.execute(
                    """
                    UPDATE devices
                    SET device_uid = ?, mac_address = ?, mac_source = ?
                    WHERE batch_id = ? AND serial_number = ?
                    """,
                    (
                        merged_uid,
                        merged_mac,
                        merged_source,
                        batch_id,
                        serial_number,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ResultStoreError(
                    f"duplicate UID or MAC in batch for {serial_number}"
                ) from exc
            if (merged_uid, merged_mac, merged_source) != (
                old_uid,
                old_mac,
                old_source,
            ):
                self._insert_event(
                    connection,
                    batch_id,
                    None,
                    "device_hardware_identity_updated",
                    {
                        "serial_number": serial_number,
                        "device_uid": merged_uid,
                        "mac_address": merged_mac,
                        "mac_source": merged_source,
                    },
                )
        return self.get_device(batch_id, serial_number)

    def update_device_endpoint(
        self,
        batch_id: str,
        serial_number: str,
        *,
        endpoint_ip: str,
        endpoint_port: int,
    ) -> dict[str, Any]:
        try:
            address = ipaddress.ip_address(endpoint_ip)
        except ValueError as exc:
            raise ResultStoreError("device endpoint must be a valid IP address") from exc
        if address.version != 4:
            raise ResultStoreError("device endpoint must use IPv4")
        if not (1 <= int(endpoint_port) <= 65535):
            raise ResultStoreError("device endpoint port is out of range")
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT endpoint_ip, endpoint_port FROM devices
                WHERE batch_id = ? AND serial_number = ?
                """,
                (batch_id, serial_number),
            ).fetchone()
            if row is None:
                raise ResultStoreError(f"unknown device: {serial_number}")
            connection.execute(
                """
                UPDATE devices SET endpoint_ip = ?, endpoint_port = ?
                WHERE batch_id = ? AND serial_number = ?
                """,
                (str(address), int(endpoint_port), batch_id, serial_number),
            )
            self._insert_event(
                connection,
                batch_id,
                None,
                "device_endpoint_changed",
                {
                    "serial_number": serial_number,
                    "old_endpoint": f"{row['endpoint_ip']}:{row['endpoint_port']}",
                    "new_endpoint": f"{address}:{int(endpoint_port)}",
                },
            )
        return self.get_device(batch_id, serial_number)

    def create_attempt(
        self,
        batch_id: str,
        serial_number: str,
        test_id: str,
        *,
        fixture_action_id: Optional[str] = None,
    ) -> dict[str, Any]:
        test_id = _required_text(test_id, "test_id", 96)
        with self.transaction() as connection:
            batch = connection.execute(
                "SELECT status FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise ResultStoreError(f"unknown batch: {batch_id}")
            if batch["status"] != BatchStatus.RUNNING.value:
                raise ResultStoreError("attempts can only be created in a running batch")
            if connection.execute(
                "SELECT 1 FROM devices WHERE batch_id = ? AND serial_number = ?",
                (batch_id, serial_number),
            ).fetchone() is None:
                raise ResultStoreError(f"unknown device: {serial_number}")
            ordinal = connection.execute(
                """
                SELECT COALESCE(MAX(ordinal), 0) + 1 FROM attempts
                WHERE batch_id = ? AND serial_number = ? AND test_id = ?
                """,
                (batch_id, serial_number, test_id),
            ).fetchone()[0]
            cursor = connection.execute(
                """
                INSERT INTO attempts(
                    batch_id, serial_number, test_id, ordinal, status,
                    fixture_action_id, created_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    serial_number,
                    test_id,
                    ordinal,
                    AttemptStatus.PENDING.value,
                    fixture_action_id,
                    _utc_now(),
                ),
            )
            attempt_id = int(cursor.lastrowid)
            self._insert_event(
                connection,
                batch_id,
                attempt_id,
                "attempt_created",
                {"test_id": test_id, "ordinal": ordinal},
            )
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: int) -> dict[str, Any]:
        row = self._fetchone("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,))
        if row is None:
            raise ResultStoreError(f"unknown attempt: {attempt_id}")
        return _row_dict(row)

    def list_attempts(self, batch_id: str) -> list[dict[str, Any]]:
        return [
            _row_dict(row)
            for row in self._fetchall(
                """
                SELECT * FROM attempts
                WHERE batch_id = ?
                ORDER BY serial_number, test_id, ordinal
                """,
                (batch_id,),
            )
        ]

    def list_metrics(self, attempt_id: int) -> list[dict[str, Any]]:
        return [
            _row_dict(row)
            for row in self._fetchall(
                "SELECT * FROM metrics WHERE attempt_id = ? ORDER BY metric_id",
                (int(attempt_id),),
            )
        ]

    def list_fixture_actions(self, batch_id: str) -> list[dict[str, Any]]:
        return [
            _row_dict(row)
            for row in self._fetchall(
                """
                SELECT * FROM fixture_actions
                WHERE batch_id = ? ORDER BY created_utc, action_id
                """,
                (batch_id,),
            )
        ]

    def freeze_report_snapshot(
        self,
        batch_id: str,
        serial_number: str,
        snapshot: Mapping[str, Any],
        *,
        automatic_verdict: str,
        report_id: str,
    ) -> dict[str, Any]:
        """Persist the sole immutable input used by all report versions."""

        verdict = str(automatic_verdict).upper()
        if verdict not in {"PASS", "FAIL", "INCOMPLETE", "ABORTED"}:
            raise ResultStoreError(f"unsupported automatic verdict: {verdict}")
        snapshot_json = _json(snapshot)
        snapshot_sha256 = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        now = _utc_now()
        with self.transaction() as connection:
            if connection.execute(
                """
                SELECT 1 FROM devices
                WHERE batch_id = ? AND serial_number = ?
                """,
                (batch_id, serial_number),
            ).fetchone() is None:
                raise ResultStoreError(f"unknown device: {serial_number}")
            try:
                connection.execute(
                    """
                    INSERT INTO report_snapshots(
                        batch_id, serial_number, report_id, automatic_verdict,
                        snapshot_json, snapshot_sha256, created_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        serial_number,
                        _required_text(report_id, "report_id", 255),
                        verdict,
                        snapshot_json,
                        snapshot_sha256,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ResultStoreError(
                    f"report snapshot is already frozen: {serial_number}"
                ) from exc
            self._insert_event(
                connection,
                batch_id,
                None,
                "device_report_snapshot_frozen",
                {
                    "serial_number": serial_number,
                    "report_id": report_id,
                    "automatic_verdict": verdict,
                    "snapshot_sha256": snapshot_sha256,
                },
            )
        return self.get_report_snapshot(batch_id, serial_number)

    def get_report_snapshot(
        self, batch_id: str, serial_number: str
    ) -> dict[str, Any]:
        row = self._fetchone(
            """
            SELECT * FROM report_snapshots
            WHERE batch_id = ? AND serial_number = ?
            """,
            (batch_id, serial_number),
        )
        if row is None:
            raise ResultStoreError(f"report snapshot is not frozen: {serial_number}")
        return _row_dict(row)

    def record_report_artifact(
        self,
        batch_id: str,
        serial_number: str,
        *,
        version: int,
        status: str,
        final_verdict: str,
        document_path: str = "",
        document_sha256: str = "",
        evidence_path: str = "",
        evidence_sha256: str = "",
        manifest_path: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        if version not in {1, 2}:
            raise ResultStoreError("report version must be 1 or 2")
        if status not in {
            "not_generated", "generated_v1", "reviewed_v2", "generation_failed"
        }:
            raise ResultStoreError(f"unsupported report status: {status}")
        now = _utc_now()
        with self.transaction() as connection:
            snapshot = connection.execute(
                """
                SELECT 1 FROM report_snapshots
                WHERE batch_id = ? AND serial_number = ?
                """,
                (batch_id, serial_number),
            ).fetchone()
            if snapshot is None:
                raise ResultStoreError(f"report snapshot is not frozen: {serial_number}")
            existing = connection.execute(
                """
                SELECT status FROM report_artifacts
                WHERE batch_id = ? AND serial_number = ? AND version = ?
                """,
                (batch_id, serial_number, version),
            ).fetchone()
            if existing is not None and existing["status"] != "generation_failed":
                raise ResultStoreError(
                    f"report version already exists: {serial_number} V{version:02d}"
                )
            try:
                if existing is not None:
                    connection.execute(
                        """
                        DELETE FROM report_artifacts
                        WHERE batch_id = ? AND serial_number = ? AND version = ?
                        """,
                        (batch_id, serial_number, version),
                    )
                connection.execute(
                    """
                    INSERT INTO report_artifacts(
                        batch_id, serial_number, version, status, final_verdict,
                        document_path, document_sha256, evidence_path,
                        evidence_sha256, manifest_path, error, created_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id, serial_number, version, status,
                        str(final_verdict).upper(), str(document_path),
                        str(document_sha256), str(evidence_path),
                        str(evidence_sha256), str(manifest_path), str(error), now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ResultStoreError(
                    f"report version already exists: {serial_number} V{version:02d}"
                ) from exc
            self._insert_event(
                connection,
                batch_id,
                None,
                "device_report_status_changed",
                {
                    "serial_number": serial_number,
                    "version": version,
                    "status": status,
                    "final_verdict": str(final_verdict).upper(),
                    "error": str(error),
                },
            )
        return self.get_report_artifact(batch_id, serial_number, version)

    def get_report_artifact(
        self, batch_id: str, serial_number: str, version: int
    ) -> dict[str, Any]:
        row = self._fetchone(
            """
            SELECT * FROM report_artifacts
            WHERE batch_id = ? AND serial_number = ? AND version = ?
            """,
            (batch_id, serial_number, int(version)),
        )
        if row is None:
            raise ResultStoreError(
                f"report artifact is missing: {serial_number} V{version:02d}"
            )
        return _row_dict(row)

    def list_report_artifacts(
        self, batch_id: str, serial_number: Optional[str] = None
    ) -> list[dict[str, Any]]:
        if serial_number is None:
            sql = """
                SELECT * FROM report_artifacts
                WHERE batch_id = ? ORDER BY serial_number, version
            """
            parameters: Sequence[Any] = (batch_id,)
        else:
            sql = """
                SELECT * FROM report_artifacts
                WHERE batch_id = ? AND serial_number = ? ORDER BY version
            """
            parameters = (batch_id, serial_number)
        return [_row_dict(row) for row in self._fetchall(sql, parameters)]

    def create_report_review(
        self,
        batch_id: str,
        serial_number: str,
        *,
        reviewer: str,
        final_verdict: str,
        reason: str,
        source_version: int = 1,
    ) -> dict[str, Any]:
        reviewer_text = _required_text(reviewer, "reviewer", 128)
        reason_text = _required_text(reason, "review reason", 2000)
        verdict = str(final_verdict).upper()
        if verdict not in {"PASS", "FAIL"}:
            raise ResultStoreError("review verdict must be PASS or FAIL")
        now = _utc_now()
        with self.transaction() as connection:
            snapshot = connection.execute(
                """
                SELECT automatic_verdict FROM report_snapshots
                WHERE batch_id = ? AND serial_number = ?
                """,
                (batch_id, serial_number),
            ).fetchone()
            if snapshot is None:
                raise ResultStoreError(f"report snapshot is not frozen: {serial_number}")
            if snapshot["automatic_verdict"] not in {"PASS", "FAIL"}:
                raise ResultStoreError("incomplete or aborted results cannot be approved")
            artifact = connection.execute(
                """
                SELECT 1 FROM report_artifacts
                WHERE batch_id = ? AND serial_number = ? AND version = ?
                  AND status = 'generated_v1'
                """,
                (batch_id, serial_number, int(source_version)),
            ).fetchone()
            if artifact is None:
                raise ResultStoreError("a generated V1 report is required for review")
            try:
                connection.execute(
                    """
                    INSERT INTO report_reviews(
                        batch_id, serial_number, source_version,
                        automatic_verdict, final_verdict, reviewer, reason,
                        reviewed_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id, serial_number, int(source_version),
                        snapshot["automatic_verdict"], verdict,
                        reviewer_text, reason_text, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ResultStoreError(
                    f"report review already exists: {serial_number}"
                ) from exc
            self._insert_event(
                connection,
                batch_id,
                None,
                "device_report_reviewed",
                {
                    "serial_number": serial_number,
                    "source_version": source_version,
                    "automatic_verdict": snapshot["automatic_verdict"],
                    "final_verdict": verdict,
                    "reviewer": reviewer_text,
                    "reason": reason_text,
                },
            )
        return self.get_report_review(batch_id, serial_number)

    def get_report_review(self, batch_id: str, serial_number: str) -> dict[str, Any]:
        row = self._fetchone(
            """
            SELECT * FROM report_reviews
            WHERE batch_id = ? AND serial_number = ?
            """,
            (batch_id, serial_number),
        )
        if row is None:
            raise ResultStoreError(f"report review is missing: {serial_number}")
        return _row_dict(row)

    def transition_attempt(
        self,
        attempt_id: int,
        status: AttemptStatus | str,
        *,
        phase: AttemptPhase | str | None = None,
        result: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        target = coerce_attempt_status(status)
        target_phase = coerce_attempt_phase(phase)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT batch_id, status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise ResultStoreError(f"unknown attempt: {attempt_id}")
            current = AttemptStatus(row["status"])
            if target not in ATTEMPT_TRANSITIONS[current]:
                raise ResultStoreError(
                    f"invalid attempt transition: {current.value} -> {target.value}"
                )
            fields: dict[str, Any] = {"status": target.value}
            if target_phase is not None:
                fields["phase"] = target_phase.value
            if target == AttemptStatus.RUNNING:
                fields["started_monotonic_ns"] = time.monotonic_ns()
            elif target in {
                AttemptStatus.PASS,
                AttemptStatus.FAIL,
                AttemptStatus.INCOMPLETE,
                AttemptStatus.SKIPPED,
                AttemptStatus.ABORTED,
            }:
                fields["ended_monotonic_ns"] = time.monotonic_ns()
            if result is not None:
                fields["result_json"] = _json(result)
            _update_fields(connection, "attempts", "attempt_id", attempt_id, fields)
            self._insert_event(
                connection,
                row["batch_id"],
                attempt_id,
                "attempt_status_changed",
                {"from": current.value, "to": target.value},
            )
        return self.get_attempt(attempt_id)

    def create_fixture_action(self, action: FixtureAction) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO fixture_actions(
                    action_id, batch_id, action_type, status, evidence_level,
                    participant_ids_json, command_json, result_json,
                    started_monotonic_ns, ended_monotonic_ns, created_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.action_id,
                    action.batch_id,
                    action.action_type,
                    action.status.value,
                    action.evidence_level.value,
                    _json(action.participant_ids),
                    _json(action.command),
                    _json(action.result),
                    action.started_monotonic_ns,
                    action.ended_monotonic_ns,
                    _utc_now(),
                ),
            )
            self._insert_event(
                connection,
                action.batch_id,
                None,
                "fixture_action_created",
                {"action_id": action.action_id, "action_type": action.action_type},
            )

    def update_fixture_action(
        self,
        action_id: str,
        *,
        status: str,
        evidence_level: EvidenceLevel | str,
        result: Optional[Mapping[str, Any]] = None,
        ended_monotonic_ns: Optional[int] = None,
    ) -> None:
        evidence = coerce_evidence_level(evidence_level)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT batch_id FROM fixture_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                raise ResultStoreError(f"unknown fixture action: {action_id}")
            fields: dict[str, Any] = {
                "status": str(status),
                "evidence_level": evidence.value,
            }
            if result is not None:
                fields["result_json"] = _json(result)
            if ended_monotonic_ns is not None:
                fields["ended_monotonic_ns"] = int(ended_monotonic_ns)
            _update_fields(connection, "fixture_actions", "action_id", action_id, fields)
            self._insert_event(
                connection,
                row["batch_id"],
                None,
                "fixture_action_updated",
                {
                    "action_id": action_id,
                    "status": str(status),
                    "evidence_level": evidence.value,
                },
            )

    def record_metric(
        self,
        attempt_id: int,
        name: str,
        value: Any,
        *,
        unit: str = "",
        verdict: str = "",
        algorithm_version: str = "",
        sample_count: Optional[int] = None,
        evidence_level: EvidenceLevel | str = EvidenceLevel.NONE,
    ) -> int:
        evidence = coerce_evidence_level(evidence_level)
        with self.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone() is None:
                raise ResultStoreError(f"unknown attempt: {attempt_id}")
            cursor = connection.execute(
                """
                INSERT INTO metrics(
                    attempt_id, name, value_json, unit, verdict,
                    algorithm_version, sample_count, evidence_level, created_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    _required_text(name, "metric name", 128),
                    _json(value),
                    str(unit),
                    str(verdict),
                    str(algorithm_version),
                    sample_count,
                    evidence.value,
                    _utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def record_event(
        self,
        batch_id: str,
        event_type: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        attempt_id: Optional[int] = None,
        monotonic_ns: Optional[int] = None,
    ) -> int:
        with self.transaction() as connection:
            return self._insert_event(
                connection,
                batch_id,
                attempt_id,
                event_type,
                payload or {},
                monotonic_ns=monotonic_ns,
            )

    def list_events(self, batch_id: str) -> list[dict[str, Any]]:
        return [
            _row_dict(row)
            for row in self._fetchall(
                "SELECT * FROM events WHERE batch_id = ? ORDER BY event_id",
                (batch_id,),
            )
        ]

    def recover_interrupted(self) -> dict[str, int]:
        with self.transaction() as connection:
            active_attempts = list(
                connection.execute(
                    f"""
                    SELECT attempt_id, batch_id, status FROM attempts
                    WHERE status IN ({','.join('?' for _ in _ACTIVE_ATTEMPT_STATUSES)})
                    """,
                    _ACTIVE_ATTEMPT_STATUSES,
                ).fetchall()
            )
            active_batches = list(
                connection.execute(
                    "SELECT batch_id, status FROM batches WHERE status = ?",
                    (BatchStatus.RUNNING.value,),
                ).fetchall()
            )
            ended_monotonic_ns = time.monotonic_ns()
            finished_utc = _utc_now()
            attempts = connection.execute(
                f"""
                UPDATE attempts SET status = ?, ended_monotonic_ns = ?
                WHERE status IN ({','.join('?' for _ in _ACTIVE_ATTEMPT_STATUSES)})
                """,
                (
                    AttemptStatus.INCOMPLETE.value,
                    ended_monotonic_ns,
                    *_ACTIVE_ATTEMPT_STATUSES,
                ),
            ).rowcount
            batches = connection.execute(
                """
                UPDATE batches SET status = ?, finished_utc = ?
                WHERE status = ?
                """,
                (
                    BatchStatus.INCOMPLETE.value,
                    finished_utc,
                    BatchStatus.RUNNING.value,
                ),
            ).rowcount
            for row in active_attempts:
                self._insert_event(
                    connection,
                    row["batch_id"],
                    int(row["attempt_id"]),
                    "attempt_recovered_incomplete",
                    {"from": row["status"], "to": AttemptStatus.INCOMPLETE.value},
                    monotonic_ns=ended_monotonic_ns,
                )
            for row in active_batches:
                self._insert_event(
                    connection,
                    row["batch_id"],
                    None,
                    "batch_recovered_incomplete",
                    {"from": row["status"], "to": BatchStatus.INCOMPLETE.value},
                    monotonic_ns=ended_monotonic_ns,
                )
        return {"batches": int(batches), "attempts": int(attempts)}

    def _create_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS schema_info(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS batches(
            batch_id TEXT PRIMARY KEY,
            recipe_id TEXT NOT NULL,
            recipe_sha256 TEXT NOT NULL,
            recipe_json TEXT NOT NULL,
            engineering_only INTEGER NOT NULL,
            operator TEXT NOT NULL,
            output_dir TEXT NOT NULL,
            status TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            started_utc TEXT,
            finished_utc TEXT,
            notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS devices(
            batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
            serial_number TEXT NOT NULL,
            slot INTEGER NOT NULL CHECK(slot BETWEEN 1 AND 4),
            endpoint_ip TEXT NOT NULL DEFAULT '',
            endpoint_port INTEGER,
            hardware_type TEXT NOT NULL DEFAULT '',
            device_uid TEXT NOT NULL DEFAULT '',
            mac_address TEXT NOT NULL DEFAULT '',
            mac_source INTEGER,
            status TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_utc TEXT NOT NULL,
            PRIMARY KEY(batch_id, serial_number),
            UNIQUE(batch_id, slot)
        );
        CREATE TABLE IF NOT EXISTS fixture_actions(
            action_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
            action_type TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence_level TEXT NOT NULL,
            participant_ids_json TEXT NOT NULL,
            command_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            started_monotonic_ns INTEGER,
            ended_monotonic_ns INTEGER,
            created_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts(
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
            serial_number TEXT NOT NULL,
            test_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            status TEXT NOT NULL,
            phase TEXT,
            fixture_action_id TEXT REFERENCES fixture_actions(action_id),
            started_monotonic_ns INTEGER,
            ended_monotonic_ns INTEGER,
            created_utc TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            manual_override TEXT,
            override_reason TEXT,
            FOREIGN KEY(batch_id, serial_number)
                REFERENCES devices(batch_id, serial_number) ON DELETE CASCADE,
            UNIQUE(batch_id, serial_number, test_id, ordinal)
        );
        CREATE TABLE IF NOT EXISTS metrics(
            metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            value_json TEXT NOT NULL,
            unit TEXT NOT NULL,
            verdict TEXT NOT NULL,
            algorithm_version TEXT NOT NULL,
            sample_count INTEGER,
            evidence_level TEXT NOT NULL,
            created_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events(
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
            attempt_id INTEGER REFERENCES attempts(attempt_id) ON DELETE CASCADE,
            monotonic_ns INTEGER NOT NULL,
            wall_time_utc TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS report_snapshots(
            batch_id TEXT NOT NULL,
            serial_number TEXT NOT NULL,
            report_id TEXT NOT NULL,
            automatic_verdict TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            snapshot_sha256 TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            PRIMARY KEY(batch_id, serial_number),
            FOREIGN KEY(batch_id, serial_number)
                REFERENCES devices(batch_id, serial_number) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS report_artifacts(
            batch_id TEXT NOT NULL,
            serial_number TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL,
            final_verdict TEXT NOT NULL,
            document_path TEXT NOT NULL DEFAULT '',
            document_sha256 TEXT NOT NULL DEFAULT '',
            evidence_path TEXT NOT NULL DEFAULT '',
            evidence_sha256 TEXT NOT NULL DEFAULT '',
            manifest_path TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_utc TEXT NOT NULL,
            PRIMARY KEY(batch_id, serial_number, version),
            FOREIGN KEY(batch_id, serial_number)
                REFERENCES report_snapshots(batch_id, serial_number) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS report_reviews(
            batch_id TEXT NOT NULL,
            serial_number TEXT NOT NULL,
            source_version INTEGER NOT NULL,
            automatic_verdict TEXT NOT NULL,
            final_verdict TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            reason TEXT NOT NULL,
            reviewed_utc TEXT NOT NULL,
            PRIMARY KEY(batch_id, serial_number),
            FOREIGN KEY(batch_id, serial_number)
                REFERENCES report_snapshots(batch_id, serial_number) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_attempts_batch ON attempts(batch_id);
        CREATE INDEX IF NOT EXISTS idx_events_batch ON events(batch_id, event_id);
        """
        with self._lock, self._connection:
            self._connection.executescript(schema)
            existing = self._connection.execute(
                "SELECT value FROM schema_info WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    "INSERT INTO schema_info(key, value) VALUES ('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
                existing = {"value": str(_SCHEMA_VERSION)}
            elif int(existing["value"]) == 1:
                self._connection.execute(
                    "ALTER TABLE devices ADD COLUMN device_uid TEXT NOT NULL DEFAULT ''"
                )
                self._connection.execute(
                    "ALTER TABLE devices ADD COLUMN mac_address TEXT NOT NULL DEFAULT ''"
                )
                self._connection.execute(
                    "ALTER TABLE devices ADD COLUMN mac_source INTEGER"
                )
                self._connection.execute(
                    "UPDATE schema_info SET value = '2' WHERE key = 'schema_version'"
                )
                existing = {"value": "2"}
            if int(existing["value"]) == 2:
                self._connection.execute(
                    "UPDATE schema_info SET value = ? WHERE key = 'schema_version'",
                    (str(_SCHEMA_VERSION),),
                )
            elif int(existing["value"]) != _SCHEMA_VERSION:
                raise ResultStoreError(
                    f"unsupported result-store schema: {existing['value']}"
                )
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_batch_uid
                ON devices(batch_id, device_uid) WHERE device_uid <> ''
                """
            )
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_batch_mac
                ON devices(batch_id, mac_address) WHERE mac_address <> ''
                """
            )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        attempt_id: Optional[int],
        event_type: str,
        payload: Mapping[str, Any],
        *,
        monotonic_ns: Optional[int] = None,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO events(
                batch_id, attempt_id, monotonic_ns, wall_time_utc,
                event_type, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                attempt_id,
                time.monotonic_ns() if monotonic_ns is None else int(monotonic_ns),
                _utc_now(),
                _required_text(event_type, "event_type", 128),
                _json(payload),
            ),
        )
        return int(cursor.lastrowid)

    def _fetchone(
        self, sql: str, parameters: Sequence[Any] = ()
    ) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, tuple(parameters)).fetchone()

    def _fetchall(
        self, sql: str, parameters: Sequence[Any] = ()
    ) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._connection.execute(sql, tuple(parameters)).fetchall())


def _required_text(value: Any, name: str, maximum: int) -> str:
    text = str(value).strip()
    if not text:
        raise ResultStoreError(f"{name} is required")
    if len(text) > maximum:
        raise ResultStoreError(f"{name} exceeds {maximum} characters")
    return text


def _normalize_device_uid(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if len(text) != 24 or any(character not in "0123456789ABCDEF" for character in text):
        raise ResultStoreError("device_uid must contain exactly 24 hexadecimal characters")
    return text


def _normalize_mac_address(value: Any) -> str:
    text = str(value or "").strip().replace("-", ":").upper()
    if not text:
        return ""
    parts = text.split(":")
    if len(parts) != 6 or any(
        len(part) != 2 or any(character not in "0123456789ABCDEF" for character in part)
        for part in parts
    ):
        raise ResultStoreError("mac_address must use XX:XX:XX:XX:XX:XX format")
    return ":".join(parts)


def _normalize_mac_source(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    source = int(value)
    if not (0 <= source <= 255):
        raise ResultStoreError("mac_source must be between 0 and 255")
    return source


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ResultStoreError(f"value is not JSON serializable: {exc}") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for key in (
        "recipe_json",
        "metadata_json",
        "result_json",
        "command_json",
        "participant_ids_json",
        "payload_json",
        "value_json",
        "snapshot_json",
    ):
        if key in value and value[key] not in (None, ""):
            value[key] = json.loads(value[key])
    if "engineering_only" in value:
        value["engineering_only"] = bool(value["engineering_only"])
    return value


def _update_fields(
    connection: sqlite3.Connection,
    table: str,
    key_name: str,
    key_value: Any,
    fields: Mapping[str, Any],
) -> None:
    assignments = ", ".join(f"{name} = ?" for name in fields)
    connection.execute(
        f"UPDATE {table} SET {assignments} WHERE {key_name} = ?",
        (*fields.values(), key_value),
    )
