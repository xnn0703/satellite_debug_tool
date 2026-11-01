"""真实签名元数据 + 本地 SFTP/HTTPS 传输替身验证离线签名晋升。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import urllib.parse

import pytest
from securesystemslib.signer import CryptoSigner
from tuf.api.exceptions import DownloadHTTPError
from tuf.ngclient.fetcher import FetcherInterface

from scripts.promote_release import PUBLIC_ROOT, STATE_ROOT, _ensure_directory, commit_bundle, fetch_candidate, prepare_bundle
from scripts.upload_release import CANDIDATE_ROOT, ensure_directory as ensure_candidate_directory
from satellite_debug_tool.edition import create_build_identity
from satellite_debug_tool.release_config import DEFAULTS
from satellite_debug_tool.updater.checker import ReleaseChecker
from satellite_debug_tool.updater.payload import canonical_bytes
from satellite_debug_tool.updater.publishing import initialize_repository, promotion_lock, refresh_online_metadata


class Sftp:
    def __init__(self, root):
        self.root = root
        self.prefetches = []
        self.moves = []
        self.fail_timestamp = False
        (root / PUBLIC_ROOT.lstrip("/")).mkdir(parents=True)
        (root / STATE_ROOT.lstrip("/")).mkdir(parents=True)

    def path(self, value):
        return self.root / value.lstrip("/")

    def lstat(self, value):
        return self.path(value).lstat()

    def mkdir(self, value, mode):
        self.path(value).mkdir(mode=mode)

    def open(self, value, mode):
        stream = self.path(value).open("xb" if mode == "wx" else mode)
        stream.stat = lambda: os.fstat(stream.fileno())
        stream.prefetch = lambda **options: self.prefetches.append((value, options))
        stream.set_pipelined = lambda enabled: None
        return stream

    def remove(self, value):
        self.path(value).unlink()

    def rename(self, source, destination):
        if self.path(destination).exists():
            raise FileExistsError(destination)
        self.path(source).rename(self.path(destination))

    def posix_rename(self, source, destination):
        if self.fail_timestamp and destination.endswith("/timestamp.json"):
            raise OSError("injected timestamp interruption")
        self.path(source).replace(self.path(destination))
        self.moves.append(destination)


class Fetcher(FetcherInterface):
    def __init__(self, sftp):
        self.sftp = sftp
        self.calls = []
        self.reject = ""

    def _fetch(self, url):
        self.calls.append(url)
        if self.reject and self.reject in url:
            raise DownloadHTTPError("injected missing public target", 404)
        path = self.sftp.root / "srv/satellite-updates/public" / urllib.parse.urlsplit(url).path.lstrip("/")
        if not path.exists():
            raise DownloadHTTPError("missing", 404)
        yield path.read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="POSIX publication directory permission contract")
def test_public_directories_allow_nginx_traversal_and_private_state_keeps_its_mode(tmp_path):
    sftp = Sftp(tmp_path / "remote")
    previous_umask = os.umask(0o022)
    try:
        public = PUBLIC_ROOT + "/satellite-terminal-tool/targets/packages"
        _ensure_directory(sftp, public, PUBLIC_ROOT)
        for name in ("satellite-terminal-tool", "satellite-terminal-tool/targets", "satellite-terminal-tool/targets/packages"):
            assert stat.S_IMODE(sftp.path(PUBLIC_ROOT + "/" + name).stat().st_mode) == 0o755
        sftp.path(STATE_ROOT).rmdir()
        _ensure_directory(sftp, STATE_ROOT, STATE_ROOT)
        assert stat.S_IMODE(sftp.path(STATE_ROOT).stat().st_mode) == 0o750
        sftp.path(CANDIDATE_ROOT).mkdir(mode=0o750)
        ensure_candidate_directory(sftp, CANDIDATE_ROOT + "/candidate")
        assert stat.S_IMODE(sftp.path(CANDIDATE_ROOT + "/candidate").stat().st_mode) == 0o750
        metadata = PUBLIC_ROOT + "/satellite-terminal-tool/metadata"
        sftp.path(metadata).mkdir(mode=0o750)
        sftp.path(metadata).chmod(0o2775)
        _ensure_directory(sftp, metadata, PUBLIC_ROOT)
        assert stat.S_IMODE(sftp.path(metadata).stat().st_mode) == 0o2775
    finally:
        os.umask(previous_umask)


def candidate(path, version="2.0.0", commit="a" * 40):
    path.mkdir()
    identity = create_build_identity(edition="ops", version=version, platform="win", architecture="x86_64", build_id=commit)
    (path / "complete.7z").write_bytes(b"packaging verified separately" * 100)
    tree = {"schema": "satellite.update-tree/v1", "identity": {**identity, "platform": "windows"},
            "files": [{"path": "app.exe", "size": 3, "sha256": hashlib.sha256(b"exe").hexdigest(), "kind": "file"}]}
    (path / "payload-manifest.json").write_bytes(canonical_bytes(tree))
    files = [{"name": p.name, "size": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in path.iterdir()]
    (path / "receipt.json").write_bytes(canonical_bytes({"schema": "satellite.release-candidate/v1", "identity": identity,
        "source_commit": commit, "system_signature": "unsigned", "artifacts": files, "environment": {}}))
    return path


@pytest.fixture
def deployment(tmp_path):
    keys = {role: CryptoSigner.generate_ed25519() for role in ("root", "targets", "snapshot", "timestamp")}
    local = tmp_path / "signing-repository"
    root = initialize_repository(local, keys, app_id="satellite-terminal-tool", repo_id="test-repo")
    sftp = Sftp(tmp_path / "remote")
    fetcher = Fetcher(sftp)
    url = "https://60.205.157.141/v1/apps/satellite-terminal-tool/"
    def prepare(name, *, initial=True, version="2.0.0", commit="a" * 40):
        return prepare_bundle(sftp, local, candidate(tmp_path / (name + "-candidate"), version, commit),
            tmp_path / name, keys, expected_commit=commit, min_os="10.0.0", public_base_url=url, initial=initial)
    def check():
        config = {**DEFAULTS, "repo_id": "test-repo", "trusted_root": root, "metadata_base_url": url + "metadata/", "target_base_url": url + "targets/"}
        return ReleaseChecker(config, cache_dir=tmp_path / "client", platform="win", architecture="x86_64", os_version="10.0.1", fetcher=fetcher).fetch_latest()
    return tmp_path, local, keys, sftp, fetcher, prepare, check


def test_prepare_keeps_remote_and_local_channel_then_commit_publishes_timestamp_last(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    previous = (local / "metadata/timestamp.json").read_bytes()
    manifest = prepare("bundle")
    assert (local / "metadata/timestamp.json").read_bytes() == previous
    assert not sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool/metadata/timestamp.json").exists()
    result = commit_bundle(sftp, manifest, fetcher=fetcher)
    assert result["state"] == "published"
    assert sftp.moves[-1].endswith("/timestamp.json")
    assert check().tag_name == "2.0.0"
    assert (local / "metadata/timestamp.json").read_bytes() == sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool/metadata/timestamp.json").read_bytes()
    assert commit_bundle(sftp, manifest, fetcher=fetcher)["reused"]
    public = sftp.path(PUBLIC_ROOT)
    assert not list(public.rglob("*.pem"))
    assert not list(public.rglob("publication.json"))
    assert not list(public.rglob("last-publish.json"))
    assert not list(public.rglob("publish.lock"))


def test_readback_failure_keeps_old_channel_and_same_bundle_retries(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    first = prepare("first")
    commit_bundle(sftp, first, fetcher=fetcher)
    second = prepare("second", initial=False, version="2.1.0", commit="b" * 40)
    old = sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool/metadata/timestamp.json").read_bytes()
    fetcher.reject = "complete.7z"
    with pytest.raises(DownloadHTTPError):
        commit_bundle(sftp, second, fetcher=fetcher)
    assert sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool/metadata/timestamp.json").read_bytes() == old
    fetcher.reject = ""
    assert check().tag_name == "2.0.0"
    commit_bundle(sftp, second, fetcher=fetcher)
    assert check().tag_name == "2.1.0"


def test_interrupted_timestamp_commit_reuses_uploaded_files(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    manifest = prepare("bundle")
    sftp.fail_timestamp = True
    with pytest.raises(OSError, match="interruption"):
        commit_bundle(sftp, manifest, fetcher=fetcher)
    assert not sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool/metadata/timestamp.json").exists()
    sftp.fail_timestamp = False
    commit_bundle(sftp, manifest, fetcher=fetcher)
    assert check().tag_name == "2.0.0"


def test_conflicting_bundle_is_rejected_after_first_commit(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    first, second = prepare("first"), prepare("second", version="2.1.0", commit="b" * 40)
    commit_bundle(sftp, first, fetcher=fetcher)
    with pytest.raises(ValueError, match="channel changed"):
        commit_bundle(sftp, second, fetcher=fetcher)
    assert check().tag_name == "2.0.0"


def test_prepare_absorbs_server_timestamp_and_snapshot_renewal(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    first = prepare("first")
    commit_bundle(sftp, first, fetcher=fetcher)
    server = sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool")
    refresh_online_metadata(server, {k: keys[k] for k in ("snapshot", "timestamp")}, refresh_snapshot=True)
    second = prepare("second", initial=False, version="2.1.0", commit="b" * 40)
    commit_bundle(sftp, second, fetcher=fetcher)
    assert check().tag_name == "2.1.0"


def test_lock_recovery_requires_same_bundle_and_explicit_intent(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    manifest = prepare("bundle")
    lock = sftp.path(STATE_ROOT + "/satellite-terminal-tool.publication.lock")
    identity = hashlib.sha256(manifest.read_bytes()).hexdigest()
    lock.write_bytes(canonical_bytes({"schema": "satellite.publication-lock/v1", "owner": identity}))
    with pytest.raises(FileExistsError):
        commit_bundle(sftp, manifest, fetcher=fetcher)
    with pytest.raises(FileExistsError):
        with promotion_lock(lock):
            pass
    commit_bundle(sftp, manifest, fetcher=fetcher, recover_lock=True)
    assert check().tag_name == "2.0.0"


def test_changed_bundle_and_remote_symlink_rejected(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    manifest = prepare("bundle")
    next((manifest.parent / "repository/targets").rglob("*.7z")).write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed after signing"):
        commit_bundle(sftp, manifest, fetcher=fetcher)
    clean = prepare("clean")
    sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool").symlink_to(local, target_is_directory=True)
    with pytest.raises(ValueError, match="real directories"):
        commit_bundle(sftp, clean, fetcher=fetcher)


def test_remote_windows_candidate_can_be_fetched_then_prepared(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    remote = CANDIDATE_ROOT + "/release/ops"
    sftp.path(remote).parent.mkdir(parents=True)
    candidate(sftp.path(remote))
    downloaded = fetch_candidate(sftp, remote, tmp / "downloaded")
    rows = json.loads(downloaded.read_text())["artifacts"]
    assert sftp.prefetches == [
        (remote + "/" + row["name"], {"file_size": row["size"], "max_concurrent_requests": 32})
        for row in rows
    ]
    # 本地候选上传使用的固定 owner 文件不属于发行字节，也不会进入 publication。
    (downloaded.parent / ".upload-local-lock").touch()
    bundle = prepare_bundle(sftp, local, downloaded.parent, tmp / "bundle", keys,
        expected_commit="a" * 40, min_os="10.0.0", initial=True,
        public_base_url="https://60.205.157.141/v1/apps/satellite-terminal-tool/")
    commit_bundle(sftp, bundle, fetcher=fetcher)
    assert check().tag_name == "2.0.0"
    assert not list((bundle.parent / "repository").rglob(".upload-local-lock"))


def test_remote_candidate_tamper_is_rejected_before_local_commit(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    remote = CANDIDATE_ROOT + "/release/ops"
    sftp.path(remote).parent.mkdir(parents=True)
    source = candidate(sftp.path(remote))
    (source / "complete.7z").write_bytes(b"tamper")
    with pytest.raises(ValueError, match="differs"):
        fetch_candidate(sftp, remote, tmp / "downloaded")
    assert not (tmp / "downloaded").exists()


def test_remote_candidate_prefetch_failure_preserves_fresh_output(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    remote = CANDIDATE_ROOT + "/release/ops"
    sftp.path(remote).parent.mkdir(parents=True)
    candidate(sftp.path(remote))
    original_open = sftp.open

    def failed_prefetch(**options):
        raise ConnectionError("prefetch interrupted")

    def interrupted_open(path, mode):
        stream = original_open(path, mode)
        stream.prefetch = failed_prefetch
        return stream

    sftp.open = interrupted_open
    with pytest.raises(ConnectionError, match="prefetch interrupted"):
        fetch_candidate(sftp, remote, tmp / "downloaded")
    assert not (tmp / "downloaded").exists()
    assert not list(tmp.glob(".candidate-fetch-*"))


def test_publication_ledger_rejects_remote_timestamp_rollback(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    commit_bundle(sftp, prepare("first"), fetcher=fetcher)
    timestamp = sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool/metadata/timestamp.json")
    previous = timestamp.read_bytes()
    commit_bundle(sftp, prepare("second", initial=False, version="2.1.0", commit="b" * 40), fetcher=fetcher)
    timestamp.write_bytes(previous)
    with pytest.raises(ValueError, match="precedes the local publication ledger"):
        prepare("third", initial=False, version="2.2.0", commit="c" * 40)


def test_remote_metadata_signature_is_verified_before_preparation(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    commit_bundle(sftp, prepare("first"), fetcher=fetcher)
    timestamp = sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool/metadata/timestamp.json")
    data = json.loads(timestamp.read_bytes())
    data["signed"]["version"] += 1
    timestamp.write_text(json.dumps(data))
    with pytest.raises(Exception, match="signature|signed"):
        prepare("second", initial=False, version="2.1.0", commit="b" * 40)
