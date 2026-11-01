"""安装事务测试构造真实文件树和归档，不连接更新服务器。"""
from pathlib import Path
import json

from satellite_debug_tool.updater.handoff import prepare_update_launch_request, sha256_file
from satellite_debug_tool.updater.payload import build_manifest, write_manifest


def make_payload(path: Path, version: str = "1.9.1", *, edition: str = "ops") -> dict:
    path.mkdir(parents=True)
    identity = {"app_id": "satellite-terminal-tool", "edition": edition, "version": version,
                "platform": "win", "architecture": "x86_64", "build_id": version.replace(".", "") * 12 + "a"}
    target = path / "_internal/satellite_debug_tool/build_identity.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(identity), encoding="utf-8")
    (path / "SatelliteTerminalManager.exe").write_bytes(("executable-" + version).encode())
    (path / "updater.exe").write_bytes(("updater-" + version).encode())
    (path / "_internal/runtime.dll").write_bytes(b"same-runtime")
    return build_manifest(path, identity)


def archive_payload(root: Path, destination: Path) -> Path:
    import py7zr
    with py7zr.SevenZipFile(destination, "w") as archive:
        archive.writeall(root, arcname=".")
    return destination


def make_request(root: Path, *, prepared: bool = False):
    install = root / "install"
    make_payload(install, "1.9.0")
    payload = root / "new"
    tree = make_payload(payload)
    manifest = root / "target.json"
    write_manifest(manifest, tree)
    archive = archive_payload(payload, root / "complete.7z")
    settings = root / "configuration"
    settings.mkdir()
    (settings / "settings.json").write_text('{"schema":1,"value":"old"}', encoding="utf-8")
    request = prepare_update_launch_request(
        target_version="1.9.1", install_dir=install, updater_executable=install / "updater.exe",
        volumes=[archive], transaction_dir=root / "maintenance", main_pid=12345,
        package_sha256=sha256_file(archive), tree_manifest=manifest,
        tree_manifest_sha256=sha256_file(manifest), settings_directory=settings,
        prepared_payload=payload if prepared else None,
    )
    return request
