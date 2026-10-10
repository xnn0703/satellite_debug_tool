"""Immutable update handoff from the main-window owner to updater.exe."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence
from uuid import uuid4

from satellite_debug_tool.updater.status import write_update_status


@dataclass(frozen=True)
class UpdateLaunchRequest:
    target_version: str
    main_pid: int
    install_dir: Path
    updater_executable: Path
    volumes: tuple[Path, ...]
    transaction_dir: Path
    work_dir: Path
    log_path: Path
    status_path: Path

    def command(self) -> list[str]:
        return [
            str(self.updater_executable),
            "--pid",
            str(self.main_pid),
            "--install-dir",
            str(self.install_dir),
            "--workdir",
            str(self.work_dir),
            "--log",
            str(self.log_path),
            "--status",
            str(self.status_path),
            "--target-version",
            self.target_version,
            "--volumes",
            *(str(path) for path in self.volumes),
        ]


def prepare_update_launch_request(
    *,
    target_version: str,
    install_dir: Path,
    updater_executable: Path,
    volumes: Sequence[Path],
    transaction_dir: Path,
    main_pid: int | None = None,
) -> UpdateLaunchRequest:
    normalized_volumes = tuple(Path(path).resolve() for path in volumes)
    if not normalized_volumes:
        raise ValueError("update handoff requires at least one volume")
    missing = [str(path) for path in normalized_volumes if not path.is_file()]
    if missing:
        raise ValueError("update volume is missing: " + ", ".join(missing))
    executable = Path(updater_executable).resolve()
    if not executable.is_file():
        raise ValueError(f"updater executable is missing: {executable}")
    install = Path(install_dir).resolve()
    if not install.is_dir():
        raise ValueError(f"install directory is missing: {install}")

    root = Path(transaction_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    work_dir = root / f"work-{uuid4().hex}"
    request = UpdateLaunchRequest(
        target_version=str(target_version),
        main_pid=os.getpid() if main_pid is None else int(main_pid),
        install_dir=install,
        updater_executable=executable,
        volumes=normalized_volumes,
        transaction_dir=root,
        work_dir=work_dir,
        log_path=root / "updater.log",
        status_path=root / "update-status.json",
    )
    write_update_status(
        request.status_path,
        "prepared",
        target_version=request.target_version,
        install_dir=str(request.install_dir),
        log_path=str(request.log_path),
        volumes=[str(path) for path in request.volumes],
    )
    return request


def launch_updater(request: UpdateLaunchRequest) -> None:
    """Start updater after the main window completed its owned shutdown."""

    command = request.command()
    if sys.platform.startswith("win"):
        subprocess.Popen(
            command,
            creationflags=0x00000010,  # CREATE_NEW_CONSOLE
            close_fds=True,
        )
    else:
        subprocess.Popen(command, start_new_session=True, close_fds=True)


__all__ = [
    "UpdateLaunchRequest",
    "launch_updater",
    "prepare_update_launch_request",
]
