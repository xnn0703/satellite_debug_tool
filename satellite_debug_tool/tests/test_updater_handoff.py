"""M30 main-window-owned updater handoff regressions."""

from __future__ import annotations

import json
from threading import Event

import pytest

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.updater.handoff import prepare_update_launch_request, load_update_launch_request, sha256_file
from satellite_debug_tool.ui.main_window import MainWindow


def _request(tmp_path, **options):
    install = tmp_path / "SatelliteTerminalManagerOps"
    install.mkdir(parents=True)
    updater = install / "updater.exe"
    updater.write_bytes(b"PE-updater")
    volume = tmp_path / "satellite_terminal_manager-ops-windows-x86_64-v2.0.0.7z"
    volume.write_bytes(b"archive")
    manifest = tmp_path / "tree.json"
    manifest.write_text('{"schema":"test"}')
    return prepare_update_launch_request(
        target_version="v2.0.0",
        install_dir=install,
        updater_executable=updater,
        volumes=[volume],
        transaction_dir=tmp_path / "updates" / "v2.0.0",
        main_pid=1234,
        package_sha256=sha256_file(volume),
        tree_manifest=manifest,
        tree_manifest_sha256=sha256_file(manifest),
        **options,
    )


def test_handoff_hash_cancellation_observed_between_chunks(tmp_path):
    from satellite_debug_tool.updater.downloader import DownloadCancelled
    archive = tmp_path / "payload.7z"
    archive.write_bytes(b"x" * (3 * 1024 * 1024))
    class CancelAfterFirstChunk:
        calls = 0
        def is_set(self):
            self.calls += 1
            return self.calls == 2
    with pytest.raises(DownloadCancelled):
        sha256_file(archive, CancelAfterFirstChunk())


@pytest.mark.parametrize("cancel", [False, True])
def test_handoff_partial_preparation_cleans_only_its_transaction(tmp_path, monkeypatch, cancel):
    from satellite_debug_tool.updater import handoff
    from satellite_debug_tool.updater.downloader import DownloadCancelled
    event = Event()
    earlier = tmp_path / "updates" / "v2.0.0" / "earlier"
    earlier.mkdir(parents=True)
    (earlier / "evidence.json").write_text("existing")
    def copy(source, target, cancel_event):
        target.write_bytes(b"partial")
        if cancel:
            cancel_event.set()
            handoff._check_cancel(cancel_event)
        raise OSError("copy failed")
    monkeypatch.setattr(handoff, "_copy_file", copy)
    with pytest.raises(DownloadCancelled if cancel else OSError):
        _request(tmp_path, cancel_event=event)
    assert list(earlier.parent.iterdir()) == [earlier]
    assert (earlier / "evidence.json").read_text() == "existing"
    assert (tmp_path / "SatelliteTerminalManagerOps/updater.exe").read_bytes() == b"PE-updater"


def test_handoff_uses_persistent_transaction_paths(tmp_path) -> None:
    request = _request(tmp_path)

    assert request.log_path == request.transaction_dir / "updater.log"
    assert request.status_path == request.transaction_dir / "update-status.json"
    status = json.loads(request.status_path.read_text(encoding="utf-8"))
    assert status["state"] == "prepared"
    assert status["target_version"] == "v2.0.0"
    command = request.command()
    assert command == [str(request.updater_executable), "--request", str(request.request_path)]
    assert load_update_launch_request(request.request_path) == request
    assert request.install_dir not in request.updater_executable.parents
    assert request.updater_executable.read_bytes() == b"PE-updater"


def test_updater_launch_uses_maintenance_cwd(tmp_path, monkeypatch):
    from satellite_debug_tool.updater import handoff
    request = _request(tmp_path)
    monkeypatch.chdir(request.install_dir)
    calls = []
    monkeypatch.setattr(handoff.subprocess, "Popen", lambda command, **options: calls.append((command, options)))
    handoff.launch_updater(request)
    assert calls[0][1]["cwd"] == str(request.transaction_dir)


def test_main_window_launches_updater_after_owned_shutdown(
    qapplication_session,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    launched = []
    monkeypatch.setattr(
        "satellite_debug_tool.updater.handoff.launch_updater",
        launched.append,
    )
    window = MainWindow(settings=Settings())
    request = _request(tmp_path / "handoff")
    window._pending_update_request = request

    assert window.close()
    assert launched == [request]


def test_shutdown_rejection_clears_request_without_launching_updater(
    qapplication_session,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    launched = []
    monkeypatch.setattr(
        "satellite_debug_tool.updater.handoff.launch_updater",
        launched.append,
    )

    class RejectingProduction:
        @staticmethod
        def confirm_shutdown() -> bool:
            return False

    window = MainWindow(settings=Settings())
    request = _request(tmp_path / "rejected")
    window._pending_update_request = request
    window._production = RejectingProduction()

    assert not window.close()
    assert launched == []
    assert window._pending_update_request is None
    status = json.loads(request.status_path.read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["stage"] == "main_shutdown"

    window._production = None
    assert window.close()


def test_transport_shutdown_failure_prevents_updater_launch(
    qapplication_session,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    launched = []
    monkeypatch.setattr(
        "satellite_debug_tool.updater.handoff.launch_updater",
        launched.append,
    )
    window = MainWindow(settings=Settings())
    request = _request(tmp_path / "transport-failure")
    window._pending_update_request = request
    monkeypatch.setattr(window._live, "shutdown", lambda: False)
    monkeypatch.setattr(
        "satellite_debug_tool.ui.main_window.QMessageBox.warning",
        lambda *args, **kwargs: None,
    )

    assert not window.close()
    assert launched == []
    assert window._pending_update_request is None
    status = json.loads(request.status_path.read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["stage"] == "main_shutdown"

    monkeypatch.setattr(window._live, "shutdown", lambda: True)
    assert window.close()
