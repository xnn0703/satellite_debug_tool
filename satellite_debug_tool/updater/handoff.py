"""主窗口完成资源收尾后，把不可变安装请求交给持久 updater 副本。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from threading import Event
from typing import Sequence
from uuid import uuid4

from satellite_debug_tool.edition import Edition
from satellite_debug_tool.updater.status import write_atomic_json, write_update_status


REQUEST_SCHEMA = "satellite.update-request/v1"


def _check_cancel(event: Event | None) -> None:
    if event is not None and event.is_set():
        from satellite_debug_tool.updater.downloader import DownloadCancelled
        raise DownloadCancelled()


def sha256_file(path: Path, cancel_event: Event | None = None) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            _check_cancel(cancel_event)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(source: Path, target: Path, cancel_event: Event | None) -> None:
    with source.open("rb") as reader, target.open("xb") as writer:
        while True:
            _check_cancel(cancel_event)
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    shutil.copystat(source, target)


def _digest(value: str) -> str:
    if re.fullmatch(r"[a-f0-9]{64}", str(value)) is None:
        raise ValueError("update request requires an SHA-256 digest")
    return str(value)


def _outside(path: Path, install: Path) -> bool:
    return path != install and install not in path.parents


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
    transaction_id: str
    request_path: Path
    package_sha256: str
    tree_manifest: Path
    tree_manifest_sha256: str
    prepared_payload: Path | None = None
    settings_directory: Path | None = None
    edition: Edition = Edition.OPS

    def command(self) -> list[str]:
        return [str(self.updater_executable), "--request", str(self.request_path)]

    def to_json(self) -> dict:
        data = asdict(self)
        for key, value in tuple(data.items()):
            if isinstance(value, Path):
                data[key] = str(value)
        data["volumes"] = [str(path) for path in self.volumes]
        data["edition"] = self.edition.value
        return {"schema": REQUEST_SCHEMA, **data}


def _validate_request(request: UpdateLaunchRequest) -> None:
    if request.edition is not Edition.OPS:
        raise ValueError("user edition uses manual application installation")
    if re.fullmatch(r"[a-f0-9]{32}", request.transaction_id) is None:
        raise ValueError("update transaction identity is invalid")
    if len(request.volumes) != 1 or request.main_pid <= 0 or not request.target_version:
        raise ValueError("update request requires one complete package and an application identity")
    _digest(request.package_sha256)
    _digest(request.tree_manifest_sha256)
    for path in (request.transaction_dir, request.updater_executable, request.request_path,
                 request.tree_manifest, request.status_path, request.log_path):
        if not _outside(path, request.install_dir):
            raise ValueError("update maintenance files must be outside the installation")
    for path in (request.updater_executable, request.request_path, request.tree_manifest,
                 request.status_path, request.log_path, request.work_dir):
        if request.transaction_dir not in path.parents:
            raise ValueError("update transaction path is outside its maintenance directory")
    if request.prepared_payload is not None and not _outside(request.prepared_payload, request.install_dir):
        raise ValueError("prepared payload must be separate from the installed application")
    if request.settings_directory is not None and not _outside(request.settings_directory, request.install_dir):
        raise ValueError("application settings must be outside the installation")


def load_update_launch_request(path: Path) -> UpdateLaunchRequest:
    source = Path(path).resolve()
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.pop("schema", None) != REQUEST_SCHEMA:
        raise ValueError("unsupported update request schema")
    paths = ("install_dir", "updater_executable", "transaction_dir", "work_dir", "log_path",
             "status_path", "request_path", "tree_manifest", "prepared_payload", "settings_directory")
    for key in paths:
        if data.get(key) is not None:
            data[key] = Path(data[key]).resolve()
    data["volumes"] = tuple(Path(item).resolve() for item in data["volumes"])
    data["edition"] = Edition(data["edition"])
    request = UpdateLaunchRequest(**data)
    if request.request_path != source:
        raise ValueError("update request path differs from its recorded identity")
    _validate_request(request)
    return request


def prepare_update_launch_request(
    *,
    target_version: str,
    install_dir: Path,
    updater_executable: Path,
    volumes: Sequence[Path],
    transaction_dir: Path,
    package_sha256: str,
    tree_manifest: Path,
    tree_manifest_sha256: str,
    prepared_payload: Path | None = None,
    settings_directory: Path | None = None,
    main_pid: int | None = None,
    edition: Edition = Edition.OPS,
    cancel_event: Event | None = None,
) -> UpdateLaunchRequest:
    """冻结可信输入并复制 updater，普通下载目录不会成为安装目录。"""
    if Edition(edition) is not Edition.OPS:
        raise ValueError("user edition uses manual application installation")
    normalized_volumes = tuple(Path(path).resolve() for path in volumes)
    if len(normalized_volumes) != 1 or not normalized_volumes[0].is_file():
        raise ValueError("update handoff requires one complete downloaded package")
    if sha256_file(normalized_volumes[0], cancel_event) != _digest(package_sha256):
        raise ValueError("downloaded update package digest differs from the trusted target")
    manifest = Path(tree_manifest).resolve()
    if sha256_file(manifest, cancel_event) != _digest(tree_manifest_sha256):
        raise ValueError("update file manifest digest differs from the trusted target")
    executable = Path(updater_executable).resolve()
    if not executable.is_file():
        raise ValueError(f"updater executable is missing: {executable}")
    install = Path(install_dir).resolve()
    if not install.is_dir():
        raise ValueError(f"install directory is missing: {install}")
    from satellite_debug_tool.updater.install_platform import installation_platform
    platform = installation_platform(install)
    if platform == "macos":
        from satellite_debug_tool.updater.install_platform import validate_macos_bundle
        validate_macos_bundle(install)
    permission_probe = install.parent / (".satellite-update-permission-" + uuid4().hex)
    try:
        permission_probe.mkdir()
        permission_probe.rmdir()
    except OSError as exc:
        raise ValueError(f"application directory is not writable for automatic update: {install.parent}") from exc

    transaction_id = uuid4().hex
    root = Path(transaction_dir).resolve() / transaction_id
    if not _outside(root, install):
        raise ValueError("update maintenance directory must be outside the installation")
    root.mkdir(parents=True, exist_ok=False)
    try:
        _check_cancel(cancel_event)
        runtime = root / "runtime"
        runtime.mkdir()
        copied_executable = runtime / executable.name
        _copy_file(executable, copied_executable, cancel_event)
        if sha256_file(copied_executable, cancel_event) != sha256_file(executable, cancel_event):
            raise ValueError("persistent updater copy verification failed")
        if platform == "macos":
            subprocess.run(["codesign", "--verify", "--strict", str(copied_executable)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        frozen_manifest = root / "target-tree.json"
        _copy_file(manifest, frozen_manifest, cancel_event)
        if sha256_file(frozen_manifest, cancel_event) != tree_manifest_sha256:
            raise ValueError("persistent target manifest verification failed")
        frozen_payload = None if prepared_payload is None else Path(prepared_payload).resolve()
        if platform == "macos" and frozen_payload is None:
            from satellite_debug_tool.updater.applier import Applier, PayloadContract
            from satellite_debug_tool.updater.install_platform import main_executable
            from satellite_debug_tool.updater.payload import read_manifest
            manifest_data = read_manifest(frozen_manifest)
            applier = Applier()
            extracted = applier.extract_zip_to(normalized_volumes[0], root / "verified-payload",
                max_expanded_bytes=sum(int(item["size"]) if item["kind"] == "file"
                                       else len(item["target"].encode("utf-8"))
                                       for item in manifest_data["files"]))
            contract = PayloadContract(main_executable(install).name, "updater", edition="ops",
                                       target_version=str(target_version), platform="macos")
            frozen_payload = applier.resolve_payload_root(extracted, contract)
            from satellite_debug_tool.updater.payload import verify_tree
            verify_tree(frozen_payload, manifest_data, cancel_event)
        request = UpdateLaunchRequest(
            target_version=str(target_version),
            main_pid=os.getpid() if main_pid is None else int(main_pid),
            install_dir=install,
            updater_executable=copied_executable,
            volumes=normalized_volumes,
            transaction_dir=root,
            work_dir=root / "work",
            log_path=root / "updater.log",
            status_path=root / "update-status.json",
            transaction_id=transaction_id,
            request_path=root / "request.json",
            package_sha256=package_sha256,
            tree_manifest=frozen_manifest,
            tree_manifest_sha256=tree_manifest_sha256,
            prepared_payload=frozen_payload,
            settings_directory=(Path.home() / ".satellite_debug_tool" if settings_directory is None
                                else Path(settings_directory).resolve()),
            edition=Edition(edition),
        )
        _validate_request(request)
        _check_cancel(cancel_event)
        write_atomic_json(request.request_path, request.to_json())
        write_update_status(request.status_path, "prepared", transaction_id=transaction_id,
                            target_version=request.target_version, install_dir=str(install),
                            log_path=str(request.log_path))
        _check_cancel(cancel_event)
        return request
    except Exception:
        shutil.rmtree(root)
        raise


def launch_updater(request: UpdateLaunchRequest) -> None:
    """仅在窗口所属资源完成收尾后调用。"""
    frozen = load_update_launch_request(request.request_path)
    if frozen != request:
        raise ValueError("update request changed after it was prepared")
    # Windows 将当前目录作为占用句柄；维护进程不能继承安装目录。
    options = {"close_fds": True, "cwd": str(request.transaction_dir)}
    if sys.platform.startswith("win"):
        options["creationflags"] = 0x00000010
    else:
        options["start_new_session"] = True
    subprocess.Popen(request.command(), **options)


__all__ = ["UpdateLaunchRequest", "launch_updater", "prepare_update_launch_request",
           "load_update_launch_request", "sha256_file"]
