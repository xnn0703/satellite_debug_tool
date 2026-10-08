"""Independent MS-6222 ownership and evidence tests."""

from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from satellite_debug_tool.core.production import (
    Ms6222Conclusion,
    Ms6222ControlLease,
    Ms6222DebugError,
    Ms6222FrameEnvelope,
    Ms6222FrameType,
    Ms6222SessionRecorder,
    Ms6222StreamParser,
    Ms6222WorkerStatistics,
    Ms6222ParserStatistics,
)
from satellite_debug_tool.tests.test_ms6222_protocol import (
    _gnss_frame,
    _ins_frame,
    _rawimu_frame,
)


def test_ms6222_lease_has_one_affirmative_owner() -> None:
    lease = Ms6222ControlLease()
    handle = lease.acquire("MS-6222 diagnostics")
    assert lease.held_by(handle)
    assert lease.owner == "MS-6222 diagnostics"
    with pytest.raises(Ms6222DebugError, match="already controlled"):
        lease.acquire("production batch")
    lease.release(handle)
    batch = lease.acquire("production batch")
    assert lease.held_by(batch)


def test_ms6222_session_records_all_frame_types_and_hashes(tmp_path: Path) -> None:
    recorder = Ms6222SessionRecorder(
        port="/dev/tty.test",
        operator="operator-a",
        root=tmp_path,
        session_id="MS-SESSION-1",
    )
    session_dir = recorder.start()
    parser = Ms6222StreamParser()
    frames = parser.feed(
        _ins_frame() + _gnss_frame(v2=True) + _rawimu_frame(),
        host_monotonic_ns=100,
        host_wall_time_ns=200,
    )
    for frame in frames:
        recorder.record_frame(frame)
    recorder.record_frame(
        Ms6222FrameEnvelope(
            frame_type=Ms6222FrameType.UNKNOWN,
            raw=b"noise",
            host_monotonic_ns=300,
            host_wall_time_ns=400,
            crc_valid=False,
            protocol_version=0,
            record=None,
            error="noise",
        )
    )
    recorder.record_event("note", "independent reference capture")
    result = recorder.complete(
        conclusion=Ms6222Conclusion.PASS,
        notes="host evidence only",
        statistics=None,
    )

    assert result.status == "complete"
    assert result.conclusion == Ms6222Conclusion.PASS
    summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    assert summary["result_class"] == "ENGINEERING_ONLY"
    assert summary["counts"] == {
        "events": 1,
        "invalid": 1,
        "raw": 4,
        "statistics": 0,
        "valid": 3,
    }
    assert manifest["status"] == "complete"
    assert set(manifest["files"]) == {
        "configuration.json",
        "events.jsonl",
        "parsed_frames.csv",
        "raw_frames.jsonl",
        "statistics.jsonl",
        "summary.json",
    }


def test_ms6222_session_abort_keeps_incomplete_evidence(tmp_path: Path) -> None:
    recorder = Ms6222SessionRecorder(
        port="COM7",
        root=tmp_path,
        session_id="MS-SESSION-ABORT",
    )
    session_dir = recorder.start()
    result = recorder.abort(reason="serial disconnected", statistics=None)
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result.status == "incomplete"
    assert manifest["status"] == "incomplete"
    assert manifest["conclusion"] == "INCONCLUSIVE"


def test_ms6222_page_records_without_motion_profile_or_batch(
    qapplication_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.ui.ms6222_debug_workspace import Ms6222DebugWorkspace

    monkeypatch.setenv("HOME", str(tmp_path))
    page = Ms6222DebugWorkspace(
        Settings(),
        Ms6222ControlLease(),
        session_root=tmp_path / "sessions",
    )
    page._port.addItem("/dev/tty.test")
    page._port.setCurrentText("/dev/tty.test")
    page._connected = True
    page._start_capture()
    assert page.session_active
    assert page._session_path is not None

    envelope = Ms6222StreamParser().feed(
        _ins_frame(),
        host_monotonic_ns=100,
        host_wall_time_ns=200,
    )[0]
    page._on_frame(envelope)
    page._on_statistics(
        Ms6222WorkerStatistics(
            parser=Ms6222ParserStatistics(valid_frames=1, ins_frames=1),
            connected=True,
            elapsed_s=1.0,
            ins_rate_hz=1.0,
            gnss_rate_hz=0.0,
            rawimu_rate_hz=0.0,
            valid_ratio=1.0,
        )
    )
    assert page._valid_data_confirmed
    assert "-2.5000" in page._ins_values[0][1].text()
    page._finish_capture()
    deadline = time.monotonic() + 3.0
    while page._finalizer is not None and time.monotonic() < deadline:
        qapplication_session.processEvents()
        time.sleep(0.005)
    assert page._finalizer is None
    assert (page._session_path / "summary.json").is_file()
    page.close()
    page.deleteLater()
    qapplication_session.processEvents()


def test_production_workspace_has_lazy_motion_ms6222_and_power_pages(
    qapplication_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    settings.set("production.discovery_cidr", "127.0.0.1/32")
    workspace = ProductionWorkspace(settings)
    assert workspace._subpages.count() == 4
    assert workspace._fixture_debug is None
    assert workspace._ms6222_debug is None
    assert workspace._power_debug is None

    workspace.activate_view()
    workspace._switch_subpage(1)
    assert workspace._fixture_debug is not None
    assert not workspace._fixture_debug._motion_only
    assert workspace._fixture_debug._reference_lease is workspace._ms6222_control_lease
    assert not workspace._fixture_debug._serial_connect.isHidden()
    assert not workspace._fixture_debug._qualification_group.isHidden()
    workspace._switch_subpage(2)
    assert workspace._ms6222_debug is not None
    assert workspace._subpages.currentIndex() == 2
    workspace.close()
    workspace.deleteLater()
    qapplication_session.processEvents()


def test_ms6222_capture_without_valid_data_becomes_incomplete(
    qapplication_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.ui.ms6222_debug_workspace import Ms6222DebugWorkspace

    monkeypatch.setenv("HOME", str(tmp_path))
    page = Ms6222DebugWorkspace(
        Settings(),
        Ms6222ControlLease(),
        session_root=tmp_path / "sessions-timeout",
    )
    page._port.setCurrentText("COM9")
    page._connected = True
    page._start_capture()
    page._capture_started_monotonic_ns = time.monotonic_ns() - 6_000_000_000
    page._on_statistics(
        Ms6222WorkerStatistics(
            parser=Ms6222ParserStatistics(),
            connected=True,
            elapsed_s=6.0,
            ins_rate_hz=0.0,
            gnss_rate_hz=0.0,
            rawimu_rate_hz=0.0,
            valid_ratio=0.0,
        )
    )
    deadline = time.monotonic() + 3.0
    while page._finalizer is not None and time.monotonic() < deadline:
        qapplication_session.processEvents()
        time.sleep(0.005)
    summary = json.loads((page._session_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "incomplete"
    assert "MS-6222" in summary["notes"]
    page.close()
    page.deleteLater()
    qapplication_session.processEvents()
