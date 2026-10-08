"""Managed lifecycle for the vendor Lingjing motion-platform service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable, Optional
import uuid
import zipfile

import psutil
from PySide6.QtCore import QObject, QTimer, Signal

from satellite_debug_tool import __version__
from satellite_debug_tool.core.application_logging import default_application_log_path


SERVICE_SCHEMA = "satellite.lingjing-platform-service"
SERVICE_VERSION = "2604"
SERVICE_HOST = "192.168.15.101"
SERVICE_PORT = 9800


class PlatformServiceError(RuntimeError):
    pass


class PlatformServiceState(str, Enum):
    REMOTE_REQUIRED = "remote_required"
    STOPPED = "stopped"
    STARTING = "starting"
    LISTENING = "listening"
    EXTERNAL_LISTENING = "external_listening"
    EXITED = "exited"
    FAILED = "failed"


@dataclass(frozen=True)
class PlatformServiceSnapshot:
    state: PlatformServiceState
    details: str = ""
    pid: Optional[int] = None
    owned: bool = False
    runtime_dir: Optional[Path] = None

    @property
    def ready(self) -> bool:
        return self.state in {
            PlatformServiceState.LISTENING,
            PlatformServiceState.EXTERNAL_LISTENING,
        }


@dataclass(frozen=True)
class UdpListener:
    host: str
    port: int
    pid: Optional[int]


def bundled_platform_service_root() -> Path:
    relative = Path(
        "satellite_debug_tool/resources/vendor/lingjing_platform_service"
    ) / SERVICE_VERSION
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / relative
    return Path(__file__).resolve().parents[2] / "resources/vendor/lingjing_platform_service" / SERVICE_VERSION


def default_platform_service_runtime_root() -> Path:
    return (
        Path.home()
        / ".satellite_debug_tool"
        / "vendor_services"
        / "lingjing-a6"
        / SERVICE_VERSION
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_service_manifest(resource_root: Path) -> dict:
    manifest_path = Path(resource_root) / "service-manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformServiceError(f"platform service manifest is invalid: {exc}") from exc
    if payload.get("schema") != SERVICE_SCHEMA or payload.get("schema_version") != 1:
        raise PlatformServiceError("platform service manifest schema is unsupported")
    if payload.get("service_version") != SERVICE_VERSION:
        raise PlatformServiceError("platform service version does not match the application")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise PlatformServiceError("platform service manifest has no files")
    return payload


def validate_service_tree(root: Path, manifest: dict) -> None:
    root = Path(root).resolve()
    for record in manifest["files"]:
        relative = Path(str(record.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise PlatformServiceError("platform service manifest contains an unsafe path")
        path = (root / relative).resolve()
        if root not in path.parents:
            raise PlatformServiceError("platform service manifest escapes its resource root")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise PlatformServiceError(f"platform service file is missing: {relative}") from exc
        if size != int(record.get("size", -1)):
            raise PlatformServiceError(f"platform service file size differs: {relative}")
        if _sha256(path) != str(record.get("sha256", "")):
            raise PlatformServiceError(f"platform service file hash differs: {relative}")


def deploy_platform_service(resource_root: Path, runtime_root: Path) -> Path:
    resource_root = Path(resource_root)
    runtime_root = Path(runtime_root)
    manifest = load_service_manifest(resource_root)
    validate_service_tree(resource_root, manifest)
    if runtime_root.exists():
        try:
            validate_service_tree(runtime_root, manifest)
            return runtime_root
        except PlatformServiceError:
            shutil.rmtree(runtime_root)
    runtime_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{runtime_root.name}-",
            dir=str(runtime_root.parent),
        )
    )
    try:
        for record in manifest["files"]:
            relative = Path(record["path"])
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resource_root / relative, destination)
        shutil.copy2(resource_root / "service-manifest.json", staging / "service-manifest.json")
        (staging / "log").mkdir(exist_ok=True)
        validate_service_tree(staging, manifest)
        os.replace(staging, runtime_root)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return runtime_root


def _local_ipv4_addresses() -> set[str]:
    addresses: set[str] = set()
    for records in psutil.net_if_addrs().values():
        for record in records:
            if record.family.name == "AF_INET":
                addresses.add(str(record.address))
    return addresses


def _udp_listeners() -> tuple[UdpListener, ...]:
    listeners: list[UdpListener] = []
    for connection in psutil.net_connections(kind="udp"):
        if not connection.laddr:
            continue
        listeners.append(
            UdpListener(
                host=str(connection.laddr.ip),
                port=int(connection.laddr.port),
                pid=connection.pid,
            )
        )
    return tuple(listeners)


def _process_executable(pid: int) -> Path:
    return Path(psutil.Process(pid).exe())


def _default_process_launcher(executable: Path, working_directory: Path):
    kwargs: dict = {
        "cwd": str(working_directory),
        "close_fds": True,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform.startswith("win"):
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 2  # SW_SHOWMINIMIZED
        kwargs["startupinfo"] = startup
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen([str(executable)], **kwargs)


class LingjingPlatformServiceController(QObject):
    """Deploy, launch and observe the vendor Windows service as one owner."""

    snapshot_changed = Signal(object)

    def __init__(
        self,
        *,
        resource_root: Optional[Path] = None,
        runtime_root: Optional[Path] = None,
        platform_name: Optional[str] = None,
        local_ip_provider: Callable[[], set[str]] = _local_ipv4_addresses,
        listener_provider: Callable[[], Iterable[UdpListener]] = _udp_listeners,
        process_executable_provider: Callable[[int], Path] = _process_executable,
        process_launcher: Callable[[Path, Path], object] = _default_process_launcher,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._resource_root = Path(resource_root or bundled_platform_service_root())
        self._runtime_root = Path(runtime_root or default_platform_service_runtime_root())
        self._platform_name = str(platform_name or sys.platform)
        self._local_ip_provider = local_ip_provider
        self._listener_provider = listener_provider
        self._process_executable_provider = process_executable_provider
        self._process_launcher = process_launcher
        self._process = None
        self._owned_pid: Optional[int] = None
        self._started_monotonic_ms = 0
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._poll)
        initial = (
            PlatformServiceState.STOPPED
            if self._platform_name.startswith("win")
            else PlatformServiceState.REMOTE_REQUIRED
        )
        self._snapshot = PlatformServiceSnapshot(initial)

    @property
    def snapshot(self) -> PlatformServiceSnapshot:
        return self._snapshot

    def ensure_running(self) -> PlatformServiceSnapshot:
        if not self._platform_name.startswith("win"):
            return self._publish(
                PlatformServiceState.REMOTE_REQUIRED,
                "run the platform service on Windows 192.168.15.101",
            )
        listeners = self._matching_listeners()
        if listeners:
            if self._owned_pid is not None and any(
                item.pid == self._owned_pid for item in listeners
            ):
                return self._publish(
                    PlatformServiceState.LISTENING,
                    f"UDP {SERVICE_PORT} is listening",
                    pid=self._owned_pid,
                    owned=True,
                    runtime_dir=self._runtime_root,
                )
            try:
                supported = self._supported_existing_listener(listeners)
            except PlatformServiceError as exc:
                return self._publish(PlatformServiceState.FAILED, str(exc))
            if supported is not None:
                return self._publish(
                    PlatformServiceState.EXTERNAL_LISTENING,
                    "supported platform service is already listening",
                    pid=supported.pid,
                    owned=False,
                )
            return self._publish(
                PlatformServiceState.FAILED,
                f"UDP {SERVICE_PORT} is occupied by an unknown process",
            )
        if self._process is not None and self._process.poll() is None:
            return self._publish(
                PlatformServiceState.FAILED,
                f"platform service PID {self._owned_pid} is running without UDP {SERVICE_PORT}",
                pid=self._owned_pid,
                owned=True,
                runtime_dir=self._runtime_root,
            )
        if SERVICE_HOST not in self._local_ip_provider():
            return self._publish(
                PlatformServiceState.FAILED,
                f"this Windows workstation does not own IPv4 {SERVICE_HOST}",
            )
        try:
            runtime_dir = deploy_platform_service(self._resource_root, self._runtime_root)
            process = self._process_launcher(runtime_dir / "ProConvert.exe", runtime_dir)
            pid = int(process.pid)
        except (OSError, PlatformServiceError, ValueError) as exc:
            return self._publish(PlatformServiceState.FAILED, str(exc))
        self._process = process
        self._owned_pid = pid
        self._started_monotonic_ms = int(time.monotonic() * 1000)
        self._timer.start()
        return self._publish(
            PlatformServiceState.STARTING,
            f"waiting for UDP {SERVICE_PORT}",
            pid=pid,
            owned=True,
            runtime_dir=runtime_dir,
        )

    def _matching_listeners(self) -> tuple[UdpListener, ...]:
        return tuple(
            item
            for item in self._listener_provider()
            if item.port == SERVICE_PORT and item.host in {SERVICE_HOST, "0.0.0.0", "::"}
        )

    def _supported_existing_listener(
        self,
        listeners: Iterable[UdpListener],
    ) -> Optional[UdpListener]:
        expected = load_service_manifest(self._resource_root)
        expected_exe = next(
            record["sha256"]
            for record in expected["files"]
            if record["path"] == expected["entrypoint"]
        )
        for listener in listeners:
            if listener.pid is None:
                continue
            try:
                executable = Path(self._process_executable_provider(listener.pid))
                if executable.name.lower() != "proconvert.exe":
                    continue
                if _sha256(executable) == expected_exe:
                    return listener
            except (OSError, psutil.Error):
                continue
        return None

    def _poll(self) -> None:
        process = self._process
        if process is None or self._owned_pid is None:
            self._timer.stop()
            return
        exit_code = process.poll()
        if exit_code is not None:
            self._timer.stop()
            self._publish(
                PlatformServiceState.EXITED,
                f"platform service exited with code {exit_code}",
                pid=self._owned_pid,
                owned=True,
                runtime_dir=self._runtime_root,
            )
            return
        if any(item.pid == self._owned_pid for item in self._matching_listeners()):
            self._timer.stop()
            self._publish(
                PlatformServiceState.LISTENING,
                f"UDP {SERVICE_PORT} is listening",
                pid=self._owned_pid,
                owned=True,
                runtime_dir=self._runtime_root,
            )
            return
        now_ms = int(time.monotonic() * 1000)
        if now_ms - self._started_monotonic_ms >= 15000:
            self._timer.stop()
            self._publish(
                PlatformServiceState.FAILED,
                f"platform service did not listen on UDP {SERVICE_PORT} within 15 seconds",
                pid=self._owned_pid,
                owned=True,
                runtime_dir=self._runtime_root,
            )

    def shutdown(self) -> bool:
        self._timer.stop()
        process = self._process
        if process is None or process.poll() is not None:
            return True
        # Do not force-kill motion control. Windows validation must prove a
        # vendor-supported graceful close before the application owns shutdown.
        self._publish(
            self._snapshot.state,
            "application exited; managed service remains running",
            pid=self._owned_pid,
            owned=True,
            runtime_dir=self._runtime_root,
        )
        return True

    def export_diagnostics(self, destination: Path) -> Path:
        return export_platform_service_diagnostics(
            destination,
            snapshot=self._snapshot,
            resource_root=self._resource_root,
            runtime_root=self._runtime_root,
            listeners=tuple(self._listener_provider()),
            local_addresses=self._local_ip_provider(),
        )

    def _publish(
        self,
        state: PlatformServiceState,
        details: str = "",
        *,
        pid: Optional[int] = None,
        owned: bool = False,
        runtime_dir: Optional[Path] = None,
    ) -> PlatformServiceSnapshot:
        self._snapshot = PlatformServiceSnapshot(
            state=state,
            details=str(details),
            pid=pid,
            owned=owned,
            runtime_dir=runtime_dir,
        )
        self._append_event(self._snapshot)
        self.snapshot_changed.emit(self._snapshot)
        return self._snapshot

    def _append_event(self, snapshot: PlatformServiceSnapshot) -> None:
        try:
            event_path = self._runtime_root.parent / "platform_service_events.jsonl"
            event_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "state": snapshot.state.value,
                "details": snapshot.details,
                "pid": snapshot.pid,
                "owned": snapshot.owned,
                "runtime_dir": str(snapshot.runtime_dir or ""),
                "event_id": uuid.uuid4().hex,
            }
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _command_snapshot(command: list[str]) -> dict:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"command": command, "error": str(exc)}
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def export_platform_service_diagnostics(
    destination: Path,
    *,
    snapshot: PlatformServiceSnapshot,
    resource_root: Path,
    runtime_root: Path,
    listeners: Iterable[UdpListener],
    local_addresses: Iterable[str],
    application_log_path: Optional[Path] = None,
) -> Path:
    destination = Path(destination)
    if destination.suffix.lower() != ".zip":
        destination = destination.with_suffix(".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    source_root = runtime_root if (runtime_root / "service-manifest.json").is_file() else resource_root
    missing: list[str] = []
    included: list[dict] = []

    def add_file(archive: zipfile.ZipFile, source: Path, arcname: str) -> None:
        if not source.is_file():
            missing.append(arcname)
            return
        try:
            payload = source.read_bytes()
        except OSError:
            missing.append(arcname)
            return
        archive.writestr(arcname, payload)
        included.append(
            {
                "path": arcname,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )

    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            add_file(archive, source_root / "service-manifest.json", "service/service-manifest.json")
            for name in ("appset.xml", "plat.xml", "platset.xml", "DCS.xml", "XPlane.xml", "陀螺仪.xml"):
                add_file(archive, source_root / "配置" / name, f"service/config/{name}")
            log_dir = runtime_root / "log"
            vendor_logs = sorted(log_dir.glob("log*.txt")) + sorted(log_dir.glob("posdata*.txt"))
            if vendor_logs:
                for path in vendor_logs:
                    add_file(archive, path, f"service/vendor_log/{path.name}")
            else:
                missing.append("service/vendor_log/*.txt")
            event_path = runtime_root.parent / "platform_service_events.jsonl"
            add_file(archive, event_path, "application/platform_service_events.jsonl")
            app_log = Path(application_log_path or default_application_log_path())
            app_candidates = [app_log] + [
                app_log.with_name(f"{app_log.name}.{index}") for index in range(1, 4)
            ]
            found_app_log = False
            for path in app_candidates:
                if path.is_file():
                    found_app_log = True
                    add_file(archive, path, f"application/{path.name}")
            if not found_app_log:
                missing.append("application/application.log*")
            network = {
                "local_ipv4_addresses": sorted(str(item) for item in local_addresses),
                "udp_listeners": [
                    {"host": item.host, "port": item.port, "pid": item.pid}
                    for item in listeners
                    if item.port == SERVICE_PORT
                ],
                "commands": (
                    [
                        _command_snapshot(["ipconfig", "/all"]),
                        _command_snapshot(["route", "print", "-4"]),
                        _command_snapshot(["netstat", "-ano", "-p", "udp"]),
                    ]
                    if sys.platform.startswith("win")
                    else []
                ),
            }
            network_payload = json.dumps(network, ensure_ascii=False, indent=2).encode("utf-8")
            archive.writestr("network/network.json", network_payload)
            included.append(
                {
                    "path": "network/network.json",
                    "size": len(network_payload),
                    "sha256": hashlib.sha256(network_payload).hexdigest(),
                }
            )
            summary = {
                "schema": "satellite.platform-service-diagnostics",
                "schema_version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "software_version": __version__,
                "service_version": SERVICE_VERSION,
                "service_host": SERVICE_HOST,
                "service_port": SERVICE_PORT,
                "state": snapshot.state.value,
                "details": snapshot.details,
                "pid": snapshot.pid,
                "owned": snapshot.owned,
                "runtime_dir": str(snapshot.runtime_dir or runtime_root),
                "included_files": sorted(included, key=lambda item: item["path"]),
                "missing_files": sorted(set(missing)),
            }
            archive.writestr(
                "bundle_manifest.json",
                json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


__all__ = [
    "LingjingPlatformServiceController",
    "PlatformServiceError",
    "PlatformServiceSnapshot",
    "PlatformServiceState",
    "SERVICE_HOST",
    "SERVICE_PORT",
    "SERVICE_VERSION",
    "UdpListener",
    "bundled_platform_service_root",
    "default_platform_service_runtime_root",
    "deploy_platform_service",
    "export_platform_service_diagnostics",
    "load_service_manifest",
    "validate_service_tree",
]
