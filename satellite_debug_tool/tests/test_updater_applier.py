"""同盘准备、可信目录树及可恢复 rename 的故障回归。"""
from pathlib import Path
import time

import pytest

from satellite_debug_tool.tests._updater_helpers import archive_payload, make_payload
from satellite_debug_tool.updater.applier import Applier, ApplyError, PayloadContract
from satellite_debug_tool.updater.recovery import InstallJournal, recover_installation

CONTRACT = PayloadContract("SatelliteTerminalManager.exe", "updater.exe", edition="ops", target_version="1.9.1")


def prepare(tmp_path):
    install = tmp_path / "installed"
    make_payload(install, "1.9.0")
    source = tmp_path / "download" / "payload"
    manifest = make_payload(source)
    token = "a" * 32
    stage = Applier().prepare_payload(install, source, token, tree_manifest=manifest, contract=CONTRACT)
    maintenance = tmp_path / "maintenance"
    maintenance.mkdir()
    updater = maintenance / "updater.exe"
    updater.write_bytes(b"independent-updater")
    journal = InstallJournal.create(maintenance / "journal.json", transaction_id=token,
        install_dir=install, staging_dir=stage, updater_executable=updater, target_version="1.9.1")
    journal.save("recovery_ready")
    return install, source, stage, journal


def test_real_archive_and_unique_payload(tmp_path):
    tree = tmp_path / "source" / "nested" / "payload"
    make_payload(tree)
    archive = archive_payload(tmp_path / "source", tmp_path / "release.7z")
    staged = Applier().extract_to(archive, tmp_path / "extracted")
    assert Applier().resolve_payload_root(staged, CONTRACT) == staged / "nested" / "payload"


@pytest.mark.parametrize("content,code", [(b"bad", "apply_invalid_archive"), (None, "apply_archive_missing")])
def test_invalid_archive(tmp_path, content, code):
    archive = tmp_path / "release.7z"
    if content is not None:
        archive.write_bytes(content)
    with pytest.raises(ApplyError) as exc:
        Applier().extract_to(archive, tmp_path / "extracted")
    assert exc.value.code == code


def test_extraction_budget_rejected_before_writing_payload(tmp_path):
    root = tmp_path / "source"
    make_payload(root)
    archive = archive_payload(root, tmp_path / "release.7z")
    with pytest.raises(ApplyError, match="budget"):
        Applier().extract_to(archive, tmp_path / "extracted", max_expanded_bytes=1)
    assert not (tmp_path / "extracted").exists()


@pytest.mark.parametrize("member", ["../outside", "/absolute", "C:/outside", "CON", "dir/file."])
def test_archive_paths_rejected_before_extract(tmp_path, monkeypatch, member):
    import py7zr
    from types import SimpleNamespace
    archive = tmp_path / "release.7z"
    archive.write_bytes(b"\x37\x7a\xbc\xaf\x27\x1c")
    class FakeArchive:
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def list(self):
            return [SimpleNamespace(filename=member, is_directory=False, is_file=True,
                                    is_symlink=False, uncompressed=1)]
        def extractall(self, **_kwargs):
            pytest.fail("unsafe archive reached extraction")
    monkeypatch.setattr(py7zr, "SevenZipFile", FakeArchive)
    with pytest.raises(ApplyError):
        Applier().extract_to(archive, tmp_path / "extracted")
    assert not (tmp_path / "outside").exists()


def test_heartbeat_during_slow_extraction(tmp_path, monkeypatch):
    import py7zr
    root = tmp_path / "source"
    make_payload(root)
    archive = archive_payload(root, tmp_path / "release.7z")
    extract = py7zr.SevenZipFile.extractall
    def slow(self, *args, **kwargs):
        time.sleep(0.04)
        return extract(self, *args, **kwargs)
    monkeypatch.setattr(py7zr.SevenZipFile, "extractall", slow)
    reported = []
    Applier(lambda stage, value: reported.append((stage, value)),
            heartbeat_interval=0.01).extract_to(archive, tmp_path / "extracted")
    assert any(stage == "extracting" and value > 0 for stage, value in reported)


def test_ambiguous_payload_rejected(tmp_path):
    make_payload(tmp_path / "first")
    make_payload(tmp_path / "second")
    with pytest.raises(ApplyError) as exc:
        Applier().resolve_payload_root(tmp_path, CONTRACT)
    assert exc.value.code == "apply_payload_ambiguous"


def test_partial_cross_volume_copy_preserves_original(tmp_path, monkeypatch):
    """旧版先移动旧目录再 shutil.move，部分复制留下 install 后无法回退。"""
    import shutil
    install = tmp_path / "installed"
    make_payload(install, "1.9.0")
    source = tmp_path / "download" / "payload"
    manifest = make_payload(source)
    def partial_copy(_source, destination):
        destination.mkdir()
        (destination / "SatelliteTerminalManager.exe").write_bytes(b"partial")
        raise OSError("disk full during cross-volume copy")
    monkeypatch.setattr(shutil, "copytree", partial_copy)
    with pytest.raises(ApplyError, match="disk full"):
        Applier().prepare_payload(install, source, "b" * 32, tree_manifest=manifest, contract=CONTRACT)
    assert (install / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"
    assert not list(tmp_path.glob("installed.bak.*"))
    assert not list(tmp_path.glob(".installed.staging.*"))


def test_verified_staging_is_beside_installation(tmp_path):
    install, source, staging, journal = prepare(tmp_path)
    assert staging.parent == install.parent
    assert staging.stat().st_dev == install.stat().st_dev
    result = Applier().install_prepared(journal)
    assert result.backup_dir.is_dir()
    assert (install / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.1"
    assert (result.backup_dir / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"
    assert source.is_dir()


def test_new_directory_rename_failure_restores_original(tmp_path, monkeypatch):
    install, _, staging, journal = prepare(tmp_path)
    rename = Path.rename
    def fail_new(self, target):
        if self == staging:
            raise OSError("rename refused")
        return rename(self, target)
    monkeypatch.setattr(Path, "rename", fail_new)
    with pytest.raises(ApplyError) as exc:
        Applier().install_prepared(journal)
    assert exc.value.code == "apply_swap_failed_restored"
    assert (install / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"
    assert journal.data["state"] == "rolled_back"


@pytest.mark.parametrize("cut_state", ["old_backed_up", "switched"])
def test_power_loss_between_renames_recovers_from_persistent_journal(tmp_path, monkeypatch, cut_state):
    install, _, _, journal = prepare(tmp_path)
    save = journal.save
    class PowerLoss(BaseException): pass
    def power_loss(state, **facts):
        if state == cut_state:
            raise PowerLoss()
        save(state, **facts)
    monkeypatch.setattr(journal, "save", power_loss)
    with pytest.raises(PowerLoss):
        Applier().install_prepared(journal)
    if cut_state == "old_backed_up":
        assert not install.exists()
    assert recover_installation(journal.path) == "rolled_back"
    assert (install / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"
    assert recover_installation(journal.path) == "rolled_back"


def test_swap_requires_registered_recovery_state(tmp_path):
    install, _, _, journal = prepare(tmp_path)
    journal.save("prepared")
    with pytest.raises(ApplyError, match="recovery"):
        Applier().install_prepared(journal)
    assert (install / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"


def test_manifest_identity_and_packaged_identity_must_agree(tmp_path):
    import json
    from satellite_debug_tool.updater.payload import build_manifest
    install = tmp_path / "installed"
    make_payload(install, "1.9.0")
    source = tmp_path / "payload"
    manifest = make_payload(source)
    identity_path = source / "_internal/satellite_debug_tool/build_identity.json"
    identity = json.loads(identity_path.read_text())
    identity["architecture"] = "arm64"
    identity_path.write_text(json.dumps(identity))
    # 完整树摘要全部正确，但发行元数据与内嵌身份冲突，仍需拒绝。
    tree = build_manifest(source, manifest["identity"])
    with pytest.raises(ApplyError, match="architecture"):
        Applier().prepare_payload(install, source, "a" * 32, tree_manifest=tree, contract=CONTRACT)
    assert (install / "SatelliteTerminalManager.exe").read_bytes() == b"executable-1.9.0"
