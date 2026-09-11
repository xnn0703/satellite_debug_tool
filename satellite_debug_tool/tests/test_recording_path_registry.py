"""M25 recording path ownership and DataRecorder handoff contracts."""

from __future__ import annotations

import os
from pathlib import Path
import threading

import pytest

from satellite_debug_tool.io.data_recorder import DataRecorder
from satellite_debug_tool.io.recording_path_registry import (
    RecordingPathError,
    RecordingPathRegistry,
    canonical_recording_key,
)


def test_exact_reservation_never_overwrites_existing_file(tmp_path) -> None:
    target = tmp_path / "evidence.sdb"
    target.write_bytes(b"existing-evidence")
    registry = RecordingPathRegistry()

    with pytest.raises(FileExistsError):
        registry.reserve_exact(target)

    assert target.read_bytes() == b"existing-evidence"
    assert registry.active_keys == frozenset()


def test_equivalent_parent_and_unicode_name_share_one_process_key(tmp_path) -> None:
    directory = tmp_path / "records"
    directory.mkdir()
    decomposed = "cafe\u0301.sdb"
    composed = "caf\u00e9.sdb"
    registry = RecordingPathRegistry()
    first = registry.reserve_exact(directory / "child" / ".." / decomposed)

    with pytest.raises(RecordingPathError):
        registry.reserve_exact(directory / composed)

    assert canonical_recording_key(first.path) == canonical_recording_key(
        directory / composed
    )
    first.discard_failed_file()
    assert registry.active_keys == frozenset()


def test_unique_reservation_uses_deterministic_suffix(tmp_path) -> None:
    base = tmp_path / "support.sdb"
    base.write_bytes(b"old")
    (tmp_path / "support_001.sdb").write_bytes(b"old-1")
    reservation = RecordingPathRegistry().reserve_unique(base)

    assert reservation.path.name == "support_002.sdb"
    reservation.discard_failed_file()


def test_recorder_adopts_reservation_and_releases_path_after_stop(tmp_path) -> None:
    registry = RecordingPathRegistry()
    target = tmp_path / "recording.sdb"
    reservation = registry.reserve_exact(target)
    recorder = DataRecorder(target)

    assert recorder.start(reservation)
    assert canonical_recording_key(target) in registry.active_keys
    assert recorder.write_frame(b"frame")
    assert recorder.stop()
    assert registry.active_keys == frozenset()
    assert target.exists()


def test_stop_timeout_can_be_retried_until_finalize_releases_path(
    tmp_path,
    monkeypatch,
) -> None:
    registry = RecordingPathRegistry()
    target = tmp_path / "retry-stop.sdb"
    reservation = registry.reserve_exact(target)
    recorder = DataRecorder(target)
    finalize_entered = threading.Event()
    allow_finalize = threading.Event()
    original_finalize = recorder._finalize_file

    def delayed_finalize() -> None:
        finalize_entered.set()
        allow_finalize.wait(timeout=1.0)
        original_finalize()

    monkeypatch.setattr(recorder, "_finalize_file", delayed_finalize)
    monkeypatch.setattr(
        "satellite_debug_tool.io.data_recorder._STOP_TIMEOUT_SEC",
        0.01,
    )

    assert recorder.start(reservation)
    assert recorder.write_frame(b"frame")
    assert not recorder.stop()
    assert finalize_entered.wait(timeout=0.2)
    assert canonical_recording_key(target) in registry.active_keys

    allow_finalize.set()
    assert recorder.stop()
    assert recorder.stop()
    assert registry.active_keys == frozenset()


def test_header_failure_deletes_only_reserved_inode(tmp_path) -> None:
    target = tmp_path / "bad-header.sdb"
    registry = RecordingPathRegistry()
    reservation = registry.reserve_exact(target)
    recorder = DataRecorder(target, profile_dict={"bad": object()})

    assert not recorder.start(reservation)
    assert not target.exists()
    assert registry.active_keys == frozenset()


def test_default_start_refuses_existing_path_without_changing_bytes(tmp_path) -> None:
    target = tmp_path / "same.sdb"
    original = b"existing"
    target.write_bytes(original)

    assert not DataRecorder(target).start()
    assert target.read_bytes() == original


def test_reservation_context_closes_fd_and_releases_lease(tmp_path) -> None:
    registry = RecordingPathRegistry()
    target = tmp_path / "unhanded.sdb"
    with registry.reserve_exact(target) as reservation:
        fd = reservation._fd
        assert fd is not None
    assert registry.active_keys == frozenset()
    with pytest.raises(OSError):
        os.fstat(fd)


def test_thread_start_failure_removes_partial_normal_file(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "thread-failed.sdb"

    def fail_start(_thread):
        raise RuntimeError("injected thread start failure")

    monkeypatch.setattr("threading.Thread.start", fail_start)
    recorder = DataRecorder(target)

    assert not recorder.start()
    assert not target.exists()


def test_reusing_transferred_reservation_cannot_unlink_active_recording(
    tmp_path,
) -> None:
    registry = RecordingPathRegistry()
    target = tmp_path / "active.sdb"
    reservation = registry.reserve_exact(target)
    first = DataRecorder(target)
    second = DataRecorder(target)

    assert first.start(reservation)
    assert not second.start(reservation)
    assert target.exists()
    assert first.write_frame(b"still-owned")
    assert first.stop()
    assert target.exists()
