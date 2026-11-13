"""M67 ECS 本机原子生成四份完整包、索引和只读回执。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import zipfile
from datetime import datetime, timedelta, timezone

import py7zr
import pytest
from securesystemslib.signer import CryptoSigner
from tuf.api.metadata import Metadata

from scripts.publish_release_downloads_server import (
    INDEX_SCHEMA, REQUEST_SCHEMA, load_request, process_requests, publish_release,
)
from satellite_debug_tool.edition import create_build_identity
from satellite_debug_tool.package_names import edition_directory, release_archive_name
from satellite_debug_tool.updater.payload import build_manifest, canonical_bytes, write_manifest
from satellite_debug_tool.updater.publishing import initialize_repository, publish_candidate


COMMIT = "a" * 40
VERSION = "2.0.0"


def candidate(root: Path, *, edition: str, platform: str) -> Path:
    root.mkdir(parents=True)
    architecture = "x86_64" if platform == "win" else "arm64"
    identity = create_build_identity(edition=edition, version=VERSION, platform=platform,
                                     architecture=architecture, build_id=COMMIT)
    payload = root.parent / (root.name + "-payload")
    if platform == "win":
        payload.mkdir()
        (payload / "SatelliteTerminalManager.exe").write_bytes(b"application")
        if edition == "ops":
            (payload / "updater.exe").write_bytes(b"updater")
        identity_path = payload / "_internal/satellite_debug_tool/build_identity.json"
        identity_path.parent.mkdir(parents=True)
        identity_path.write_text(json.dumps(identity))
        archive_name = release_archive_name(edition=edition, platform=platform, architecture=architecture,
                                            version=VERSION, extension="7z")
        with py7zr.SevenZipFile(root / archive_name, "w") as archive:
            for path in payload.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(payload).as_posix())
    else:
        app = payload / (edition_directory(edition) + ".app")
        executable = app / "Contents/MacOS/SatelliteTerminalManager"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"application")
        if edition == "ops":
            (executable.parent / "updater").write_bytes(b"updater")
        identity_path = app / "Contents/MacOS/_internal/satellite_debug_tool/build_identity.json"
        identity_path.parent.mkdir(parents=True)
        identity_path.write_text(json.dumps(identity))
        resource_identity = app / "Contents/Resources/satellite_debug_tool/build_identity.json"
        resource_identity.parent.mkdir(parents=True)
        resource_identity.write_text(json.dumps(identity))
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": "SatelliteTerminalManager",
            "CFBundleIdentifier": "com.satellite-debug-tool.desktop",
            "CFBundleShortVersionString": VERSION,
        }))
        archive_name = release_archive_name(edition=edition, platform=platform, architecture=architecture,
                                            version=VERSION, extension="zip")
        with zipfile.ZipFile(root / archive_name, "w") as archive:
            for path in app.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(payload).as_posix())
        payload = app
    write_manifest(root / "payload-manifest.json", build_manifest(payload, identity))
    artifacts = [{"name": path.name, "size": path.stat().st_size,
                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in sorted(root.iterdir())]
    receipt = {"schema": "satellite.release-candidate/v1", "identity": identity,
               "source_commit": COMMIT, "system_signature": "unsigned",
               "artifacts": artifacts, "environment": {}}
    (root / "receipt.json").write_bytes(canonical_bytes(receipt))
    return root


@pytest.fixture
def release_tree(tmp_path):
    incoming = tmp_path / "incoming"
    downloads = tmp_path / "downloads"
    incoming.mkdir()
    downloads.mkdir()
    relative = Path(COMMIT) / "satellite-terminal-tool"
    win_user = candidate(incoming / relative / "user/win/x86_64" / COMMIT, edition="user", platform="win")
    mac_user = candidate(incoming / relative / "user/mac/arm64" / COMMIT, edition="user", platform="mac")
    win_ops = candidate(tmp_path / "windows-ops", edition="ops", platform="win")
    mac_ops = candidate(tmp_path / "macos-ops", edition="ops", platform="mac")
    repository = tmp_path / "repository"
    signers = {role: CryptoSigner.generate_ed25519() for role in ("root", "targets", "snapshot", "timestamp")}
    initialize_repository(repository, signers, app_id="satellite-terminal-tool", repo_id="test-repo")
    initial_targets = (repository / "metadata/targets.json").read_bytes()
    publish_candidate(repository, win_ops, signers, expected_commit=COMMIT, min_os="10.0.0",
                      publication_mode="prepare")
    publish_candidate(repository, mac_ops, signers, expected_commit=COMMIT, min_os="14.0",
                      publication_mode="prepare")
    # 真实 SFTP 晋升只推进 timestamp 和编号文件；未编号 alias 仍是初始空目录。
    (repository / "metadata/targets.json").write_bytes(initial_targets)
    def receipt_sha(path: Path) -> str:
        return hashlib.sha256((path / "receipt.json").read_bytes()).hexdigest()
    base = {"schema": REQUEST_SCHEMA, "app_id": "satellite-terminal-tool", "version": VERSION,
            "source_commit": COMMIT, "public_base_url": "https://example.test/downloads/",
            "users": {
                "windows_x86_64": {"candidate": (relative / "user/win/x86_64" / COMMIT).as_posix(),
                                    "receipt_sha256": receipt_sha(win_user)},
                "macos_arm64": {"candidate": (relative / "user/mac/arm64" / COMMIT).as_posix(),
                                "receipt_sha256": receipt_sha(mac_user)}},
            "ops": {
                "windows_x86_64": {"catalog": "catalog/ops/stable/windows/x86_64.json"},
                "macos_arm64": {"catalog": "catalog/ops/stable/macos/arm64.json"}}}
    request = {**base, "request_id": hashlib.sha256(canonical_bytes(base)).hexdigest()}
    return incoming, downloads, repository, request


def test_four_verified_packages_and_indexes_are_published_atomically(release_tree):
    incoming, downloads, repository, request = release_tree
    first = publish_release(request, incoming_root=incoming, repository=repository, downloads_root=downloads)
    assert first["state"] == "published"
    directory = downloads / "v2.0.0"
    assert {path.name for path in directory.iterdir()} == {
        "satellite_terminal_manager-windows-x86_64-v2.0.0.7z",
        "satellite_terminal_manager-ops-windows-x86_64-v2.0.0.7z",
        "satellite_terminal_manager-macos-arm64-v2.0.0.zip",
        "satellite_terminal_manager-ops-macos-arm64-v2.0.0.zip",
        "release.json", "SHA256SUMS.txt"}
    index = json.loads((directory / "release.json").read_bytes())
    assert index["schema"] == INDEX_SCHEMA
    assert index["source_commit"] == COMMIT
    assert len(index["artifacts"]) == 4
    sums = (directory / "SHA256SUMS.txt").read_text()
    assert all(row["sha256"] in sums and row["name"] in sums for row in index["artifacts"])
    assert publish_release(request, incoming_root=incoming, repository=repository,
                           downloads_root=downloads)["state"] == "reused"


def test_current_signed_chain_succeeds_with_alias_removed_and_reads_timestamp_once(release_tree, monkeypatch):
    from scripts import publish_release_downloads_server as server

    incoming, downloads, repository, request = release_tree
    (repository / "metadata/targets.json").unlink()
    (repository / "metadata/snapshot.json").unlink()
    reads = []
    original = server._metadata_bytes

    def read(path, name):
        reads.append(name)
        return original(path, name)

    monkeypatch.setattr(server, "_metadata_bytes", read)
    result = publish_release(request, incoming_root=incoming, repository=repository, downloads_root=downloads)
    assert result["state"] == "published"
    assert len(result["artifacts"]) == 4
    assert reads.count("timestamp.json") == 1
    assert "targets.json" not in reads and "snapshot.json" not in reads
    assert len(reads) == 4


@pytest.mark.parametrize("role", ["snapshot", "targets"])
def test_changed_referenced_metadata_rejects_publication(release_tree, role):
    incoming, downloads, repository, request = release_tree
    metadata = repository / "metadata"
    timestamp = Metadata.from_file(str(metadata / "timestamp.json"))
    snapshot_path = metadata / f"{timestamp.signed.snapshot_meta.version}.snapshot.json"
    snapshot = Metadata.from_file(str(snapshot_path))
    path = snapshot_path if role == "snapshot" else metadata / f"{snapshot.signed.meta['targets.json'].version}.targets.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(Exception, match="length|hash"):
        publish_release(request, incoming_root=incoming, repository=repository, downloads_root=downloads)
    assert not list(downloads.iterdir())


def test_expired_signed_metadata_rejects_publication(release_tree, monkeypatch):
    from scripts import publish_release_downloads_server as server

    incoming, downloads, repository, request = release_tree
    class FutureClock:
        @staticmethod
        def now(_timezone):
            return datetime.now(timezone.utc) + timedelta(days=400)

    monkeypatch.setattr(server, "datetime", FutureClock)
    with pytest.raises(ValueError, match="expired"):
        publish_release(request, incoming_root=incoming, repository=repository, downloads_root=downloads)
    assert not list(downloads.iterdir())


def test_timestamp_signature_is_verified_before_targets_are_used(release_tree):
    incoming, downloads, repository, request = release_tree
    path = repository / "metadata/timestamp.json"
    document = json.loads(path.read_bytes())
    document["signed"]["version"] += 1
    path.write_text(json.dumps(document))
    with pytest.raises(Exception, match="signed|signature"):
        publish_release(request, incoming_root=incoming, repository=repository, downloads_root=downloads)
    assert not list(downloads.iterdir())


def test_private_candidate_is_copied_without_changing_source_permissions(release_tree):
    incoming, downloads, repository, request = release_tree
    candidate_path = incoming / request["users"]["windows_x86_64"]["candidate"]
    receipt = json.loads((candidate_path / "receipt.json").read_bytes())
    archive_row = next(row for row in receipt["artifacts"] if row["name"].endswith(".7z"))
    source = candidate_path / archive_row["name"]
    source_bytes = source.read_bytes()
    source.chmod(0o600)
    publish_release(request, incoming_root=incoming, repository=repository, downloads_root=downloads)
    target = downloads / "v2.0.0" / archive_row["name"]
    assert source.read_bytes() == source_bytes
    if os.name != "nt":
        assert source.stat().st_mode & 0o777 == 0o600
        assert target.stat().st_mode & 0o777 == 0o644
    assert source.stat().st_ino != target.stat().st_ino


@pytest.mark.skipif(os.name == "nt", reason="ECS POSIX publication permissions")
def test_service_private_umask_keeps_published_downloads_publicly_readable(release_tree):
    incoming, downloads, repository, request = release_tree
    previous = os.umask(0o027)
    try:
        publish_release(request, incoming_root=incoming, repository=repository, downloads_root=downloads)
        directory = downloads / "v2.0.0"
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755
        assert all(path.stat().st_mode & stat.S_IROTH for path in directory.iterdir())
        directory.chmod(0o750)
        assert publish_release(request, incoming_root=incoming, repository=repository,
                               downloads_root=downloads)["state"] == "reused"
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    finally:
        os.umask(previous)


def test_request_is_canonical_and_binds_receipts(release_tree):
    _, _, _, request = release_tree
    assert load_request(canonical_bytes(request)) == request
    changed = dict(request)
    changed["users"] = {**request["users"], "windows_x86_64": {
        **request["users"]["windows_x86_64"], "receipt_sha256": "f" * 64}}
    with pytest.raises(ValueError, match="receipt"):
        publish_release(changed, incoming_root=release_tree[0], repository=release_tree[2],
                        downloads_root=release_tree[1])
    changed = {**request, "version": "2.0.1"}
    with pytest.raises(ValueError, match="id differs"):
        load_request(canonical_bytes(changed))


def test_existing_different_download_directory_is_preserved(release_tree):
    incoming, downloads, repository, request = release_tree
    directory = downloads / "v2.0.0"
    directory.mkdir()
    (directory / "previous").write_text("preserve")
    with pytest.raises(ValueError, match="different bytes"):
        publish_release(request, incoming_root=incoming, repository=repository, downloads_root=downloads)
    assert (directory / "previous").read_text() == "preserve"


def test_request_service_writes_result_and_archives_input(release_tree, tmp_path):
    incoming, downloads, repository, request = release_tree
    requests, results, archive = tmp_path / "requests", tmp_path / "results", tmp_path / "archive"
    requests.mkdir()
    results.mkdir()
    archive.mkdir()
    path = requests / (request["request_id"] + ".json")
    path.write_bytes(canonical_bytes(request))
    outcomes = process_requests(requests, results, archive, incoming_root=incoming,
                                repository=repository, downloads_root=downloads)
    assert outcomes[0]["state"] == "published"
    assert not path.exists()
    assert (archive / path.name).is_file()
    assert json.loads((results / path.name).read_bytes())["request_id"] == request["request_id"]


def test_malformed_request_is_archived_verbatim_and_returns_failure(release_tree, tmp_path):
    incoming, downloads, repository, _ = release_tree
    requests, results, archive = tmp_path / "requests", tmp_path / "results", tmp_path / "archive"
    requests.mkdir()
    results.mkdir()
    archive.mkdir()
    raw = b"{not-json"
    path = requests / "invalid.json"
    path.write_bytes(raw)
    outcomes = process_requests(requests, results, archive, incoming_root=incoming,
                                repository=repository, downloads_root=downloads)
    assert outcomes[0]["state"] == "failed"
    archived = archive / (outcomes[0]["request_id"] + ".json")
    assert archived.read_bytes() == raw
    assert not path.exists()


def test_candidate_symlink_is_rejected_without_publishing(release_tree, tmp_path):
    incoming, downloads, repository, request = release_tree
    source = incoming / request["users"]["windows_x86_64"]["candidate"]
    moved = tmp_path / "real-candidate"
    source.rename(moved)
    source.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="real directories"):
        publish_release(request, incoming_root=incoming, repository=repository,
                        downloads_root=downloads)
    assert not (downloads / "v2.0.0").exists()


def test_systemd_service_has_offline_and_narrow_write_contract():
    root = Path(__file__).resolve().parents[2]
    unit = (root / "deploy/updates/satellite-updates-publish-downloads.service").read_text()
    assert "RestrictAddressFamilies=AF_UNIX" in unit
    assert "ReadOnlyPaths=/srv/satellite-updates/incoming" in unit
    assert "ReadWritePaths=/srv/satellite-updates/public/downloads" in unit
    assert "/srv/satellite-updates/receipts/downloads/requests" in unit
    assert "/srv/satellite-updates/receipts/downloads/results" in unit
    assert "/srv/satellite-updates/receipts/downloads/archive" in unit
