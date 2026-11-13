"""M57：macOS app 自动安装的发行、载荷与交接合同。"""
from __future__ import annotations

import json
from pathlib import Path
import plistlib
import subprocess
import shutil

import pytest

from satellite_debug_tool.edition import Edition, create_build_identity, validate_payload_identity
from satellite_debug_tool.updater.applier import Applier, PayloadContract
from satellite_debug_tool.updater.handoff import prepare_update_launch_request, sha256_file
from satellite_debug_tool.updater.install_platform import identity_path
from satellite_debug_tool.updater.main import execute_update
from satellite_debug_tool.updater.payload import build_manifest, verify_tree, write_manifest


pytestmark = pytest.mark.skipif(__import__("sys").platform != "darwin", reason="requires macOS codesign and ditto")


def _app(root: Path, *, version: str = "2.0.0", build: str = "a" * 40) -> Path:
    app = root / "SatelliteTerminalManagerOps.app"
    macos = app / "Contents/MacOS"
    identity = app / "Contents/Resources/satellite_debug_tool/build_identity.json"
    identity.parent.mkdir(parents=True)
    macos.mkdir(parents=True)
    plist = {
        "CFBundleExecutable": "SatelliteTerminalManagerOps",
        "CFBundleIdentifier": "com.satellite-debug-tool.desktop",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
    }
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(plist))
    for name in ("SatelliteTerminalManagerOps", "updater"):
        path = macos / name
        shutil.copyfile("/usr/bin/true", path)
        path.chmod(0o755)
    identity.write_text(json.dumps(create_build_identity(
        edition=Edition.OPS, version=version, platform="mac", architecture="arm64", build_id=build)))
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True, exist_ok=True)
    (resources / "data.bin").write_bytes(b"resource")
    (resources / "data-current").symlink_to("data.bin")
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    return app


def test_macos_identity_uses_the_real_pyinstaller_resources_layout(tmp_path):
    app = _app(tmp_path)
    assert identity_path(app) == app / "Contents/Resources/satellite_debug_tool/build_identity.json"
    assert identity_path(app).is_file()
    assert not (app / "Contents/MacOS/_internal/satellite_debug_tool/build_identity.json").exists()
    validate_payload_identity(app, Edition.OPS, "2.0.0")


def _archive(app: Path, destination: Path) -> Path:
    subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(destination)], check=True)
    return destination


def test_macos_tree_preserves_safe_bundle_links_and_identity(tmp_path):
    app = _app(tmp_path)
    identity = json.loads(next(app.rglob("build_identity.json")).read_text())
    tree = build_manifest(app, identity)
    assert tree["identity"]["platform"] == "macos"
    assert any(row["kind"] == "symlink" for row in tree["files"])
    verify_tree(app, tree)
    validate_payload_identity(app, Edition.OPS, "2.0.0")


def test_macos_zip_preparation_freezes_verified_app_before_shutdown(tmp_path):
    installed = _app(tmp_path / "installed", version="1.9.1", build="1" * 40)
    candidate = _app(tmp_path / "candidate", version="2.0.0", build="2" * 40)
    identity = json.loads(next(candidate.rglob("build_identity.json")).read_text())
    manifest = tmp_path / "tree.json"
    write_manifest(manifest, build_manifest(candidate, identity))
    package = _archive(candidate, tmp_path / "candidate.zip")
    request = prepare_update_launch_request(
        target_version="2.0.0", install_dir=installed,
        updater_executable=installed / "Contents/MacOS/updater", volumes=[package],
        transaction_dir=tmp_path / "transactions", package_sha256=sha256_file(package),
        tree_manifest=manifest, tree_manifest_sha256=sha256_file(manifest), main_pid=1234,
    )
    assert request.prepared_payload is not None
    assert request.prepared_payload.suffix == ".app"
    verify_tree(request.prepared_payload, json.loads(manifest.read_text()))
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(request.prepared_payload)], check=True)


def test_macos_zip_rejects_link_escape(tmp_path):
    import zipfile
    package = tmp_path / "escape.zip"
    with zipfile.ZipFile(package, "w") as archive:
        info = zipfile.ZipInfo("Application.app/Contents/escape")
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, "../../../../outside")
    with pytest.raises(Exception, match="escapes"):
        Applier().extract_zip_to(package, tmp_path / "expanded")


def _request(tmp_path: Path):
    installed = _app(tmp_path / "installed", version="1.9.1", build="1" * 40)
    candidate = _app(tmp_path / "candidate", version="2.0.0", build="2" * 40)
    identity = json.loads(next(candidate.rglob("build_identity.json")).read_text())
    manifest = tmp_path / "tree.json"
    write_manifest(manifest, build_manifest(candidate, identity))
    package = _archive(candidate, tmp_path / "candidate.zip")
    request = prepare_update_launch_request(
        target_version="2.0.0", install_dir=installed,
        updater_executable=installed / "Contents/MacOS/updater", volumes=[package],
        transaction_dir=tmp_path / "transactions", package_sha256=sha256_file(package),
        tree_manifest=manifest, tree_manifest_sha256=sha256_file(manifest), main_pid=99999999,
    )
    return installed, request


def test_macos_transaction_swaps_verified_app_and_commits(tmp_path, monkeypatch):
    from satellite_debug_tool.updater import main
    installed, request = _request(tmp_path)
    class Trial:
        pid = __import__("os").getpid()
        def poll(self): return None
    monkeypatch.setattr(main, "wait_for_pid", lambda *_: True)
    monkeypatch.setattr(main, "require_installation_idle", lambda *_: None)
    monkeypatch.setattr(main, "register_login_recovery", lambda *_: None)
    monkeypatch.setattr(main, "remove_login_recovery", lambda *_: None)
    monkeypatch.setattr(main, "_start_trial", lambda *_: Trial())
    monkeypatch.setattr(main, "_wait_for_health", lambda *_: {})
    assert execute_update(request) == 0
    assert plistlib.loads((installed / "Contents/Info.plist").read_bytes())["CFBundleShortVersionString"] == "2.0.0"


def test_macos_post_swap_signature_failure_restores_previous_app(tmp_path, monkeypatch):
    from satellite_debug_tool.updater import main
    installed, request = _request(tmp_path)
    restarted = []
    monkeypatch.setattr(main, "wait_for_pid", lambda *_: True)
    monkeypatch.setattr(main, "require_installation_idle", lambda *_: None)
    monkeypatch.setattr(main, "register_login_recovery", lambda *_: None)
    monkeypatch.setattr(main, "remove_login_recovery", lambda *_: None)
    monkeypatch.setattr(main, "_start_recovered_application", lambda path: restarted.append(path))
    monkeypatch.setattr(main, "validate_macos_bundle", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad signature")))
    assert execute_update(request) == 3
    assert plistlib.loads((installed / "Contents/Info.plist").read_bytes())["CFBundleShortVersionString"] == "1.9.1"
    status = json.loads(request.status_path.read_text())
    assert status["recovery"] == "rolled_back"
    assert status["application_restart"] == "started"
    assert restarted == [installed]


def test_macos_recovered_app_restart_failure_does_not_erase_rollback_fact(tmp_path, monkeypatch):
    from satellite_debug_tool.updater import main
    installed, request = _request(tmp_path)
    monkeypatch.setattr(main, "wait_for_pid", lambda *_: True)
    monkeypatch.setattr(main, "require_installation_idle", lambda *_: None)
    monkeypatch.setattr(main, "register_login_recovery", lambda *_: None)
    monkeypatch.setattr(main, "remove_login_recovery", lambda *_: None)
    monkeypatch.setattr(main, "_start_recovered_application",
                        lambda *_: (_ for _ in ()).throw(OSError("restart refused")))
    monkeypatch.setattr(main, "validate_macos_bundle",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad signature")))
    assert execute_update(request) == 3
    assert plistlib.loads((installed / "Contents/Info.plist").read_bytes())["CFBundleShortVersionString"] == "1.9.1"
    status = json.loads(request.status_path.read_text())
    assert status["recovery"] == "rolled_back"
    assert status["application_restart"] == "failed"
    assert status["application_restart_detail"] == "restart refused"


def test_macos_login_recovery_entry_is_transaction_bound_and_removed(tmp_path, monkeypatch):
    from satellite_debug_tool.updater.recovery import register_login_recovery, remove_login_recovery, recovery_name
    monkeypatch.setenv("HOME", str(tmp_path))
    install = tmp_path / "Applications/SatelliteTerminalManagerOps.app"
    install.parent.mkdir()
    runtime = tmp_path / "transaction/updater"
    runtime.parent.mkdir()
    runtime.write_bytes(b"updater")
    journal = tmp_path / "transaction/journal.json"
    journal.write_text("{}")
    register_login_recovery(runtime, journal, install)
    path = tmp_path / "Library/LaunchAgents" / (recovery_name(install) + ".plist")
    data = plistlib.loads(path.read_bytes())
    assert data["ProgramArguments"] == [str(runtime.resolve()), "--recover", str(journal.resolve())]
    assert data["RunAtLoad"] is True
    remove_login_recovery(install)
    assert not path.exists()
