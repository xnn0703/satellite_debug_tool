from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

from satellite_debug_tool.core.production.platform_service import (
    LingjingPlatformServiceController,
    PlatformServiceState,
    PlatformServiceSnapshot,
    SERVICE_SCHEMA,
    SERVICE_VERSION,
    UdpListener,
    bundled_platform_service_root,
    deploy_platform_service,
    export_platform_service_diagnostics,
    load_service_manifest,
    validate_service_tree,
)


ORIGINAL_PLAT_SHA256 = (
    "eaead18830af2ce2799c73f47d7197954eb26b02f8d95c616949203ec190d761"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _service_resource(root: Path) -> Path:
    root.mkdir(parents=True)
    files = {
        "ProConvert.exe": b"vendor-program",
        "配置/plat.xml": b"authoritative-platform-parameters",
    }
    records = []
    for relative, payload in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        records.append(
            {
                "path": relative,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    manifest = {
        "schema": SERVICE_SCHEMA,
        "schema_version": 1,
        "service_version": SERVICE_VERSION,
        "entrypoint": "ProConvert.exe",
        "platform": "windows",
        "architecture": "anycpu",
        "files": records,
    }
    (root / "service-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return root


class _FakeProcess:
    def __init__(self, pid: int = 4102) -> None:
        self.pid = pid
        self.exit_code = None

    def poll(self):
        return self.exit_code


def test_bundled_service_manifest_keeps_original_platform_parameters() -> None:
    root = bundled_platform_service_root()
    manifest = load_service_manifest(root)
    validate_service_tree(root, manifest)

    assert manifest["service_version"] == "2604"
    assert manifest["entrypoint"] == "ProConvert.exe"
    assert _sha256(root / "配置" / "plat.xml") == ORIGINAL_PLAT_SHA256


def test_deploy_service_repairs_a_changed_runtime_file(tmp_path: Path) -> None:
    resource = _service_resource(tmp_path / "resource")
    runtime = tmp_path / "runtime" / "2604"

    assert deploy_platform_service(resource, runtime) == runtime
    (runtime / "ProConvert.exe").write_bytes(b"changed")
    assert deploy_platform_service(resource, runtime) == runtime
    assert (runtime / "ProConvert.exe").read_bytes() == b"vendor-program"
    assert (runtime / "log").is_dir()


def test_non_windows_controller_uses_remote_service_without_launching(
    tmp_path: Path,
) -> None:
    launches = []
    controller = LingjingPlatformServiceController(
        resource_root=_service_resource(tmp_path / "resource"),
        runtime_root=tmp_path / "runtime",
        platform_name="darwin",
        process_launcher=lambda exe, cwd: launches.append((exe, cwd)),
    )

    snapshot = controller.ensure_running()

    assert snapshot.state == PlatformServiceState.REMOTE_REQUIRED
    assert launches == []


def test_windows_controller_requires_local_service_address(
    tmp_path: Path,
) -> None:
    launches = []
    controller = LingjingPlatformServiceController(
        resource_root=_service_resource(tmp_path / "resource"),
        runtime_root=tmp_path / "runtime",
        platform_name="win32",
        local_ip_provider=lambda: {"192.168.15.49"},
        listener_provider=lambda: (),
        process_launcher=lambda exe, cwd: launches.append((exe, cwd)),
    )

    snapshot = controller.ensure_running()

    assert snapshot.state == PlatformServiceState.FAILED
    assert "192.168.15.101" in snapshot.details
    assert launches == []


def test_windows_controller_launches_once_and_confirms_its_udp_listener(
    tmp_path: Path,
) -> None:
    process = _FakeProcess()
    launches = []
    listeners = []

    def launch(executable: Path, working_directory: Path):
        launches.append((executable, working_directory))
        return process

    controller = LingjingPlatformServiceController(
        resource_root=_service_resource(tmp_path / "resource"),
        runtime_root=tmp_path / "runtime" / "2604",
        platform_name="win32",
        local_ip_provider=lambda: {"192.168.15.101"},
        listener_provider=lambda: tuple(listeners),
        process_launcher=launch,
    )

    assert controller.ensure_running().state == PlatformServiceState.STARTING
    assert len(launches) == 1
    assert launches[0][0].name == "ProConvert.exe"
    assert launches[0][1] == tmp_path / "runtime" / "2604"

    listeners.append(UdpListener("0.0.0.0", 9800, process.pid))
    controller._poll()

    assert controller.snapshot.state == PlatformServiceState.LISTENING
    assert controller.snapshot.ready
    assert controller.ensure_running().state == PlatformServiceState.LISTENING
    assert len(launches) == 1


def test_windows_controller_rejects_unknown_udp_owner(
    tmp_path: Path,
) -> None:
    controller = LingjingPlatformServiceController(
        resource_root=_service_resource(tmp_path / "resource"),
        runtime_root=tmp_path / "runtime",
        platform_name="win32",
        local_ip_provider=lambda: {"192.168.15.101"},
        listener_provider=lambda: (UdpListener("0.0.0.0", 9800, None),),
    )

    snapshot = controller.ensure_running()

    assert snapshot.state == PlatformServiceState.FAILED
    assert "unknown process" in snapshot.details


def test_windows_controller_reuses_matching_existing_vendor_service(
    tmp_path: Path,
) -> None:
    resource = _service_resource(tmp_path / "resource")
    launches = []
    controller = LingjingPlatformServiceController(
        resource_root=resource,
        runtime_root=tmp_path / "runtime",
        platform_name="win32",
        local_ip_provider=lambda: {"192.168.15.101"},
        listener_provider=lambda: (UdpListener("0.0.0.0", 9800, 731),),
        process_executable_provider=lambda _pid: resource / "ProConvert.exe",
        process_launcher=lambda exe, cwd: launches.append((exe, cwd)),
    )

    snapshot = controller.ensure_running()

    assert snapshot.state == PlatformServiceState.EXTERNAL_LISTENING
    assert snapshot.pid == 731
    assert not snapshot.owned
    assert launches == []


def test_windows_controller_reports_vendor_process_exit(
    tmp_path: Path,
) -> None:
    process = _FakeProcess()
    controller = LingjingPlatformServiceController(
        resource_root=_service_resource(tmp_path / "resource"),
        runtime_root=tmp_path / "runtime",
        platform_name="win32",
        local_ip_provider=lambda: {"192.168.15.101"},
        listener_provider=lambda: (),
        process_launcher=lambda _exe, _cwd: process,
    )
    assert controller.ensure_running().state == PlatformServiceState.STARTING

    process.exit_code = 9
    controller._poll()

    assert controller.snapshot.state == PlatformServiceState.EXITED
    assert "code 9" in controller.snapshot.details


def test_diagnostic_export_collects_vendor_application_and_network_evidence(
    tmp_path: Path,
) -> None:
    resource = _service_resource(tmp_path / "resource")
    runtime = deploy_platform_service(resource, tmp_path / "runtime" / "2604")
    (runtime / "log" / "log.txt").write_text("vendor-event\n", encoding="utf-8")
    events = runtime.parent / "platform_service_events.jsonl"
    events.write_text('{"state":"listening"}\n', encoding="utf-8")
    app_log = tmp_path / "logs" / "application.log"
    app_log.parent.mkdir()
    app_log.write_text("application-event\n", encoding="utf-8")
    destination = tmp_path / "platform-diagnostics.zip"

    result = export_platform_service_diagnostics(
        destination,
        snapshot=PlatformServiceSnapshot(
            PlatformServiceState.LISTENING,
            "UDP 9800 is listening",
            pid=4102,
            owned=True,
            runtime_dir=runtime,
        ),
        resource_root=resource,
        runtime_root=runtime,
        listeners=(UdpListener("0.0.0.0", 9800, 4102),),
        local_addresses={"192.168.15.101"},
        application_log_path=app_log,
    )

    assert result == destination
    with zipfile.ZipFile(result, "r") as archive:
        names = set(archive.namelist())
        assert "bundle_manifest.json" in names
        assert "service/config/plat.xml" in names
        assert "service/vendor_log/log.txt" in names
        assert "application/platform_service_events.jsonl" in names
        assert "application/application.log" in names
        assert "network/network.json" in names
        manifest = json.loads(archive.read("bundle_manifest.json"))
        network = json.loads(archive.read("network/network.json"))

    assert manifest["state"] == "listening"
    assert manifest["pid"] == 4102
    assert all(record["sha256"] for record in manifest["included_files"])
    assert network["local_ipv4_addresses"] == ["192.168.15.101"]
    assert network["udp_listeners"] == [
        {"host": "0.0.0.0", "port": 9800, "pid": 4102}
    ]
