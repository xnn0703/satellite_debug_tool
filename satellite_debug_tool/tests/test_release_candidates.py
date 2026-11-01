"""发行候选边界：精确 tag、最终文件树、SFTP 断点和不可变字节。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts.release_artifacts import load_identity, package
from scripts.resolve_release import resolve
from scripts.upload_release import CANDIDATE_ROOT, candidate_directory, ensure_directory, upload_candidate, upload_file
from satellite_debug_tool.edition import Edition, create_build_identity


def payload(tmp_path, edition="ops"):
    root = tmp_path / "payload"
    root.mkdir()
    (root / "SatelliteTerminalManager.exe").write_bytes(b"application")
    if edition == "ops":
        (root / "updater.exe").write_bytes(b"updater")
    identity = root / "_internal/satellite_debug_tool/build_identity.json"
    identity.parent.mkdir(parents=True)
    identity.write_text(json.dumps(create_build_identity(edition=Edition(edition), version="1.8.1",
        platform="win", architecture="AMD64", build_id="a" * 40)))
    return root


@pytest.mark.parametrize("edition", ["user", "ops"])
def test_package_freezes_final_bytes_and_refuses_rebuild(tmp_path, edition):
    root = payload(tmp_path, edition)
    receipt = package(root, tmp_path / "candidate", commit="a" * 40, version="v1.8.1")
    data = json.loads(receipt.read_text())
    assert data["identity"]["build_id"] == data["source_commit"] == "a" * 40
    assert data["system_signature"] == "unsigned"
    assert {row["name"].endswith(".7z") for row in data["artifacts"]} == {False, True}
    assert next(row["name"] for row in data["artifacts"] if row["name"].endswith(".7z")) == (
        f"satellite_terminal_manager-{edition}-windows-x86_64-v1.8.1.7z")
    for row in data["artifacts"]:
        content = (receipt.parent / row["name"]).read_bytes()
        assert (len(content), hashlib.sha256(content).hexdigest()) == (row["size"], row["sha256"])
    manifest = json.loads((receipt.parent / "payload-manifest.json").read_text())
    assert manifest["identity"]["platform"] == "windows"
    assert {row["path"] for row in manifest["files"]} == {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    saved = receipt.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        package(root, receipt.parent, commit="a" * 40, version="1.8.1")
    assert receipt.read_bytes() == saved


def test_wrong_source_identity_fails_before_candidate_creation(tmp_path):
    root = payload(tmp_path)
    with pytest.raises(ValueError, match="source commit"):
        package(root, tmp_path / "candidate", commit="b" * 40, version="1.8.1")
    assert not (tmp_path / "candidate").exists()


def test_optional_windows_ops_delta_binds_exact_baseline_and_target_trees(tmp_path):
    import zipfile
    from satellite_debug_tool.updater.payload import build_manifest, manifest_sha256, write_manifest
    old_parent = tmp_path / "old"
    old_parent.mkdir()
    old_root = payload(old_parent)
    old_identity = load_identity(old_root, "a" * 40, "1.8.1")
    baseline = build_manifest(old_root, old_identity)
    baseline_path = tmp_path / "baseline.json"
    write_manifest(baseline_path, baseline)
    new_parent = tmp_path / "new"
    new_parent.mkdir()
    new_root = payload(new_parent)
    identity = new_root / "_internal/satellite_debug_tool/build_identity.json"
    data = json.loads(identity.read_text())
    identity.write_text(json.dumps({**data, "version": "1.8.2", "build_id": "b" * 40}))
    (new_root / "SatelliteTerminalManager.exe").write_bytes(b"new application")
    receipt = package(new_root, tmp_path / "candidate", commit="b" * 40, version="1.8.2",
                      base_root=old_root, base_manifest=baseline_path)
    target = json.loads((receipt.parent / "payload-manifest.json").read_text())
    with zipfile.ZipFile(receipt.parent / "delta.zip") as archive:
        descriptor = json.loads(archive.read("delta.json"))
    assert descriptor["base_manifest_sha256"] == manifest_sha256(baseline)
    assert descriptor["target_manifest_sha256"] == manifest_sha256(target)
    assert "updater.exe" not in descriptor["changed"]
    assert {row["name"] for row in json.loads(receipt.read_text())["artifacts"]} == {
        "payload-manifest.json", "delta.zip", "satellite_terminal_manager-ops-windows-x86_64-v1.8.2.7z"}


def test_payload_changed_after_manifest_is_rejected_before_candidate_commit(tmp_path, monkeypatch):
    from scripts import release_artifacts
    root = payload(tmp_path)
    original = release_artifacts.build_manifest
    def changing_manifest(*arguments, **keywords):
        tree = original(*arguments, **keywords)
        (root / "SatelliteTerminalManager.exe").write_bytes(b"modified-after-tree-hash")
        return tree
    monkeypatch.setattr(release_artifacts, "build_manifest", changing_manifest)
    with pytest.raises(ValueError, match="differ|match|tree"):
        package(root, tmp_path / "candidate", commit="a" * 40, version="1.8.1")
    assert not (tmp_path / "candidate").exists()


def test_release_tag_resolves_its_commit_instead_of_the_current_branch(tmp_path):
    def git(*arguments):
        return subprocess.check_output(["git", *arguments], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    source = tmp_path / "satellite_debug_tool/__init__.py"
    source.parent.mkdir()
    source.write_text('__version__ = "1.8.0"\n')
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "old")
    previous = git("rev-parse", "HEAD")
    git("tag", "v1.8.0")
    source.write_text('__version__ = "1.8.1"\n')
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "new")
    assert resolve("v1.8.0", tmp_path) == {"tag": "v1.8.0", "commit": previous}
    assert previous != git("rev-parse", "HEAD")
    git("tag", "v9.0.0")
    with pytest.raises(ValueError, match="source version"):
        resolve("v9.0.0", tmp_path)
    with pytest.raises(ValueError, match="version tag"):
        resolve("v1.8.0;echo injected", tmp_path)


class LocalSftp:
    """使用本地文件操作验证纯 SFTP 流程，保留 lstat/排他 rename 语义。"""
    def __init__(self, root):
        self.root = root
        (root / "srv/satellite-updates/incoming").mkdir(parents=True)

    def path(self, value):
        return self.root / value.lstrip("/")

    def lstat(self, value):
        return self.path(value).lstat()

    def mkdir(self, value, mode):
        self.path(value).mkdir(mode=mode)

    def open(self, value, mode):
        return self.path(value).open("xb" if mode == "wx" else mode)

    def remove(self, value):
        self.path(value).unlink()

    def rename(self, source, destination):
        if self.path(destination).exists():
            raise FileExistsError(destination)
        self.path(source).rename(self.path(destination))


def test_upload_resumes_verifies_and_reuses_immutable_bytes(tmp_path):
    sftp = LocalSftp(tmp_path / "remote")
    local = tmp_path / "content.7z"
    content = b"payload" * 300000
    local.write_bytes(content)
    directory = CANDIDATE_ROOT + "/release"
    ensure_directory(sftp, directory)
    remote = directory + "/content.7z"
    sftp.path(remote + ".part").write_bytes(content[:123456])
    expected = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    assert upload_file(sftp, local, remote, **expected) == "resumed"
    assert sftp.path(remote).read_bytes() == content
    assert upload_file(sftp, local, remote, **expected) == "reused"
    sftp.path(remote).write_bytes(b"changed")
    with pytest.raises(ValueError, match="different bytes"):
        upload_file(sftp, local, remote, **expected)
    assert sftp.path(remote).read_bytes() == b"changed"


def test_wrong_partial_bytes_and_symlink_directory_are_rejected(tmp_path):
    sftp = LocalSftp(tmp_path / "remote")
    local = tmp_path / "content.7z"
    local.write_bytes(b"correct")
    remote = CANDIDATE_ROOT + "/content.7z"
    sftp.path(remote + ".part").write_bytes(b"bad")
    with pytest.raises(ValueError, match="partial bytes"):
        upload_file(sftp, local, remote, size=7, sha256=hashlib.sha256(b"correct").hexdigest())
    sftp.path(CANDIDATE_ROOT + "/alias").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="real directories"):
        ensure_directory(sftp, CANDIDATE_ROOT + "/alias/output")
    with pytest.raises(ValueError, match="fixed candidate root"):
        ensure_directory(sftp, "/var/www/release")


def test_upload_receipt_commits_after_every_artifact_and_preflights_all_files(tmp_path):
    receipt = package(payload(tmp_path), tmp_path / "candidate", commit="a" * 40, version="1.8.1")
    data = json.loads(receipt.read_text())
    sftp = LocalSftp(tmp_path / "remote")
    target = candidate_directory("release-1", data["identity"])
    archive = next(receipt.parent.glob("*.7z"))
    archive.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="differs from its receipt"):
        upload_candidate(sftp, receipt, "release-1")
    assert not sftp.path(target).exists()
    with pytest.raises(ValueError, match="component"):
        candidate_directory("../escape", data["identity"])


def test_candidate_upload_is_complete_idempotent_and_has_one_owner(tmp_path):
    receipt = package(payload(tmp_path), tmp_path / "candidate", commit="a" * 40, version="1.8.1")
    sftp = LocalSftp(tmp_path / "remote")
    proof = upload_candidate(sftp, receipt, "release-1")
    assert proof["state"] == "candidate-uploaded"
    assert set(proof["files"].values()) == {"uploaded"}
    assert sftp.path(proof["directory"] + "/receipt.json").read_bytes() == receipt.read_bytes()
    assert not sftp.path(proof["directory"] + "/.upload-lock").exists()
    assert set(upload_candidate(sftp, receipt, "release-1")["files"].values()) == {"reused"}
    sftp.path(proof["directory"] + "/.upload-lock").write_bytes(b"active-owner")
    with pytest.raises(FileExistsError):
        upload_candidate(sftp, receipt, "release-1")
    assert sftp.path(proof["directory"] + "/.upload-lock").read_bytes() == b"active-owner"


def test_candidate_retry_recovers_interrupted_upload_using_same_receipt(tmp_path):
    receipt = package(payload(tmp_path), tmp_path / "candidate", commit="a" * 40, version="1.8.1")
    sftp = LocalSftp(tmp_path / "remote")
    original_open = sftp.open
    original_remove = sftp.remove

    class InterruptedWriter:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def seek(self, value):
            self.stream.seek(value)
        def write(self, value):
            self.stream.write(value[:17])
            raise ConnectionError("connection interrupted")

    def interrupted_open(path, mode):
        output = original_open(path, mode)
        return InterruptedWriter(output) if path.endswith(".part") and mode == "wx" else output

    def disconnected_remove(path):
        raise ConnectionError("connection unavailable during cleanup")

    sftp.open = interrupted_open
    sftp.remove = disconnected_remove
    with pytest.raises(ConnectionError):
        upload_candidate(sftp, receipt, "release-1")
    sftp.open = original_open
    sftp.remove = original_remove
    proof = upload_candidate(sftp, receipt, "release-1")
    assert "resumed" in proof["files"].values()
    assert proof["state"] == "candidate-uploaded"
    assert not sftp.path(proof["directory"] + "/.upload-lock").exists()


def test_upload_cli_requires_pinned_host_key_and_explicit_identity(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from scripts import upload_release

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"host": "updates.example.test", "port": 22,
                                 "username": "satellite_upload", "candidate_root": CANDIDATE_ROOT}))
    known = tmp_path / "known_hosts"
    known.write_text("pinned-host-key")
    key = tmp_path / "key"
    key.write_text("test-key")
    observed = {}
    class RejectPolicy:
        pass
    class Client:
        def load_host_keys(self, path):
            observed["known_hosts"] = path
        def set_missing_host_key_policy(self, policy):
            observed["policy"] = policy
        def connect(self, *args, **kwargs):
            observed["connection"] = kwargs
            raise ValueError("host key differs from pinned identity")
        def close(self):
            observed["closed"] = True
    monkeypatch.setitem(sys.modules, "paramiko", SimpleNamespace(SSHClient=Client, RejectPolicy=RejectPolicy))
    monkeypatch.setattr(sys, "argv", ["upload_release", "--config", str(config), "--receipt", str(tmp_path / "receipt.json"),
        "--release-id", "release-1", "--known-hosts", str(known), "--identity-file", str(key)])
    with pytest.raises(ValueError, match="pinned identity"):
        upload_release.main()
    assert observed["known_hosts"] == str(known)
    assert isinstance(observed["policy"], RejectPolicy)
    assert observed["connection"]["look_for_keys"] is False
    assert observed["connection"]["allow_agent"] is False
    assert observed["connection"]["key_filename"] == str(key)
    assert observed["closed"] is True
