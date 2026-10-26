"""M30 main-window-owned updater handoff regressions."""

from __future__ import annotations

import json

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.updater.handoff import prepare_update_launch_request
from satellite_debug_tool.ui.main_window import MainWindow


def _request(tmp_path):
    install = tmp_path / "SatelliteDebugTool"
    install.mkdir(parents=True)
    updater = install / "updater.exe"
    updater.write_bytes(b"PE-updater")
    volume = tmp_path / "satellite_debug_tool-win-v2.0.0.7z"
    volume.write_bytes(b"archive")
    return prepare_update_launch_request(
        target_version="v2.0.0",
        install_dir=install,
        updater_executable=updater,
        volumes=[volume],
        transaction_dir=tmp_path / "updates" / "v2.0.0",
        main_pid=1234,
    )


def test_handoff_uses_persistent_transaction_paths(tmp_path) -> None:
    request = _request(tmp_path)

    assert request.log_path == request.transaction_dir / "updater.log"
    assert request.status_path == request.transaction_dir / "update-status.json"
    status = json.loads(request.status_path.read_text(encoding="utf-8"))
    assert status["state"] == "prepared"
    assert status["target_version"] == "v2.0.0"
    command = request.command()
    assert command[command.index("--status") + 1] == str(request.status_path)
    assert command[command.index("--target-version") + 1] == "v2.0.0"


def test_main_window_launches_updater_after_owned_shutdown(
    qapplication_session,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    launched = []
    monkeypatch.setattr(
        "satellite_debug_tool.ui.application_window_actions.launch_updater",
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
        "satellite_debug_tool.ui.application_window_actions.launch_updater",
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
        "satellite_debug_tool.ui.application_window_actions.launch_updater",
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
