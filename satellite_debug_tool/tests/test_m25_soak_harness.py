"""M25 long-soak orchestration must close device ownership before offline QA."""

from __future__ import annotations

from scripts.m25_multi_device_soak import _SoakRun


def test_capture_restore_precedes_long_sdb_validation() -> None:
    run = object.__new__(_SoakRun)
    events: list[str] = []
    run.setup_error = ""
    run.cleanup_errors = []
    run._setup_sessions = lambda: events.append("setup")
    run._negotiate_capture = lambda target: events.append(
        "capture-full" if target else "capture-restore"
    )
    run._start_recorders = lambda: events.append("recorders-start")
    run._run_measurement = lambda: events.append("measurement")
    run._stop_recorders = lambda: events.append("recorders-stop")
    run._validate_sdb_files = lambda: events.append("offline-sdb-validation")
    run._cleanup = lambda: events.append("cleanup")
    run._build_report = lambda: {"events": tuple(events)}

    report = run.execute()

    assert report["events"] == (
        "setup",
        "capture-full",
        "recorders-start",
        "measurement",
        "recorders-stop",
        "capture-restore",
        "offline-sdb-validation",
        "cleanup",
    )
