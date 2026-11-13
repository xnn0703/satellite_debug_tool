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
from tuf.api.metadata import Metadata
from tuf.ngclient.fetcher import FetcherInterface

from scripts.promote_release import (
    PUBLIC_ROOT, STATE_ROOT, VERIFY_REQUEST_ROOT, VERIFY_RESULT_ROOT,
    _ensure_directory, _remote_lock, commit_bundle, fetch_candidate, prepare_bundle,
)
from scripts.upload_release import CANDIDATE_ROOT, ensure_directory as ensure_candidate_directory
from scripts.verify_publication_server import process_requests
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
        self.read_paths = []
        self.write_paths = []
        self.fail_timestamp = False
        self.corrupt_before_verification = False
        (root / PUBLIC_ROOT.lstrip("/")).mkdir(parents=True)
        (root / STATE_ROOT.lstrip("/")).mkdir(parents=True)
        (root / CANDIDATE_ROOT.lstrip("/")).mkdir(parents=True)
        (root / VERIFY_REQUEST_ROOT.lstrip("/")).mkdir(parents=True)
        (root / VERIFY_RESULT_ROOT.lstrip("/")).mkdir(parents=True)

    def path(self, value):
        return self.root / value.lstrip("/")

    def lstat(self, value):
        return self.path(value).lstat()

    def mkdir(self, value, mode):
        self.path(value).mkdir(mode=mode)

    def open(self, value, mode):
        if mode == "rb":
            self.read_paths.append(value)
        else:
            self.write_paths.append(value)
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
        if destination.startswith(VERIFY_REQUEST_ROOT + "/"):
            if self.corrupt_before_verification:
                package = next(self.path(PUBLIC_ROOT).rglob("*.7z"))
                package.write_bytes(package.read_bytes() + b"tamper")
            process_requests(
                self.path(VERIFY_REQUEST_ROOT),
                self.path(VERIFY_RESULT_ROOT),
                self.path(PUBLIC_ROOT),
                incoming_root=self.path(CANDIDATE_ROOT),
            )


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

    def fetch_range(self, url, start, end):
        return b"".join(self._fetch(url))[start:end + 1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX publication directory permission contract")
def test_public_directories_allow_nginx_traversal_and_private_state_keeps_its_mode(tmp_path):
    sftp = Sftp(tmp_path / "remote")
    previous_umask = os.umask(0o022)
    try:
        public = PUBLIC_ROOT + "/satellite-terminal-tool/targets/packages"
        _ensure_directory(sftp, public, PUBLIC_ROOT)
        for name in ("satellite-terminal-tool", "satellite-terminal-tool/targets", "satellite-terminal-tool/targets/packages"):
            assert stat.S_IMODE(sftp.path(PUBLIC_ROOT + "/" + name).stat().st_mode) == 0o755
        shutil.rmtree(sftp.path(STATE_ROOT + "/verification"))
        sftp.path(STATE_ROOT).rmdir()
        _ensure_directory(sftp, STATE_ROOT, STATE_ROOT)
        assert stat.S_IMODE(sftp.path(STATE_ROOT).stat().st_mode) == 0o750
        sftp.path(CANDIDATE_ROOT).chmod(0o750)
        ensure_candidate_directory(sftp, CANDIDATE_ROOT + "/candidate")
        assert stat.S_IMODE(sftp.path(CANDIDATE_ROOT + "/candidate").stat().st_mode) == 0o750
        metadata = PUBLIC_ROOT + "/satellite-terminal-tool/metadata"
        sftp.path(metadata).mkdir(mode=0o750)
        sftp.path(metadata).chmod(0o2775)
        _ensure_directory(sftp, metadata, PUBLIC_ROOT)
        assert stat.S_IMODE(sftp.path(metadata).stat().st_mode) == 0o2775
    finally:
        os.umask(previous_umask)


def candidate(path, version="2.0.0", commit="a" * 40, *, platform="win"):
    path.mkdir()
    identity = create_build_identity(edition="ops", version=version, platform=platform,
        architecture="x86_64" if platform == "win" else "arm64", build_id=commit)
    (path / ("complete.7z" if platform == "win" else "complete.zip")).write_bytes(b"packaging verified separately" * 100)
    tree = {"schema": "satellite.update-tree/v1", "identity": {**identity, "platform": "windows" if platform == "win" else "macos"},
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


def test_two_platform_candidates_publish_one_snapshot_and_both_fresh_catalogs(deployment):
    """两个平台消费同一签名账本；中间分支永远不进入公开文件集。"""
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    prior = (local / "metadata/timestamp.json").read_bytes()
    windows = candidate(tmp / "windows")
    macos = candidate(tmp / "macos", platform="mac")
    url = "https://60.205.157.141/v1/apps/satellite-terminal-tool/"
    manifest = prepare_bundle(sftp, local, [(windows, "10.0.0"), (macos, "14.0")],
        tmp / "combined", keys, expected_commit="a" * 40, public_base_url=url, initial=True)
    document = json.loads(manifest.read_bytes())
    files = {row["path"] for row in document["files"]}
    assert len([p for p in files if p.endswith(".snapshot.json")]) == 1
    assert len([p for p in files if p.endswith(".targets.json")]) == 1
    assert (local / "metadata/timestamp.json").read_bytes() == prior
    assert len(document["publish_receipts"]) == 2
    # 同版本中间元数据可能已被历史失败事务占用；最终文件集应完全避开。
    intermediate = manifest.parent / "repository/metadata/2.targets.json"
    assert intermediate.is_file() and "metadata/2.targets.json" not in files
    remote_metadata = sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool/metadata")
    remote_metadata.mkdir(parents=True)
    (remote_metadata / intermediate.name).write_bytes(b"other unpublished branch")
    commit_bundle(sftp, manifest, fetcher=fetcher)
    root = Metadata.from_file(str(local / "metadata/root.json")).to_dict()
    config = {**DEFAULTS, "repo_id": "test-repo", "trusted_root": root,
              "metadata_base_url": url + "metadata/", "target_base_url": url + "targets/"}
    for platform, architecture, os_version in (("win", "x86_64", "10.0.1"), ("mac", "arm64", "14.0")):
        latest = ReleaseChecker(config, cache_dir=tmp / ("fresh-" + platform), platform=platform,
            architecture=architecture, os_version=os_version, fetcher=fetcher).fetch_latest()
        assert latest.tag_name == "2.0.0"
        assert latest.identity["build_id"] == "a" * 40
        assert latest.installation_mode == "automatic"
    assert sum(path.endswith("/timestamp.json") for path in sftp.moves) == 1
    assert commit_bundle(sftp, manifest, fetcher=fetcher)["reused"]
    assert sum(path.endswith("/timestamp.json") for path in sftp.moves) == 1


def test_candidate_packages_are_staged_on_server_without_second_sftp_upload(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    source = candidate(tmp / "local-candidate")
    receipt = source / "receipt.json"
    receipt.write_text(json.dumps(json.loads(receipt.read_bytes()), indent=2), encoding="utf-8")
    remote = CANDIDATE_ROOT + "/" + "a" * 40 + "/satellite-terminal-tool/ops/win/x86_64/" + "a" * 40
    sftp.path(remote).parent.mkdir(parents=True)
    shutil.copytree(source, sftp.path(remote))
    manifest = prepare_bundle(sftp, local, source, tmp / "server-stage", keys,
        expected_commit="a" * 40, min_os="10.0.0", initial=True,
        public_base_url="https://60.205.157.141/v1/apps/satellite-terminal-tool/")

    commit_bundle(sftp, manifest, fetcher=fetcher, candidates=[(source, remote)])

    published = next(sftp.path(PUBLIC_ROOT).rglob("*.7z"))
    incoming = sftp.path(remote) / "complete.7z"
    assert published.read_bytes() == incoming.read_bytes()
    assert published.stat().st_ino != incoming.stat().st_ino
    assert not any(path.endswith(".7z") or ".7z.upload-" in path for path in sftp.write_paths)
    assert not list(sftp.path(PUBLIC_ROOT).rglob("*.7z.upload-*"))
    assert check().tag_name == "2.0.0"


def test_server_stage_rejects_changed_candidate_and_keeps_old_timestamp(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    source = candidate(tmp / "local-candidate")
    remote = CANDIDATE_ROOT + "/" + "a" * 40 + "/satellite-terminal-tool/ops/win/x86_64/" + "a" * 40
    sftp.path(remote).parent.mkdir(parents=True)
    shutil.copytree(source, sftp.path(remote))
    manifest = prepare_bundle(sftp, local, source, tmp / "server-stage", keys,
        expected_commit="a" * 40, min_os="10.0.0", initial=True,
        public_base_url="https://60.205.157.141/v1/apps/satellite-terminal-tool/")
    package = sftp.path(remote) / "complete.7z"
    package.write_bytes(b"!" + package.read_bytes()[1:])

    with pytest.raises(ValueError, match="server publication verification failed"):
        commit_bundle(sftp, manifest, fetcher=fetcher, candidates=[(source, remote)])

    assert not sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool/metadata/timestamp.json").exists()


def test_server_stage_rejects_symlinked_incoming_root(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    source = candidate(tmp / "local-candidate")
    remote = CANDIDATE_ROOT + "/" + "a" * 40 + "/satellite-terminal-tool/ops/win/x86_64/" + "a" * 40
    sftp.path(remote).parent.mkdir(parents=True)
    shutil.copytree(source, sftp.path(remote))
    incoming = sftp.path(CANDIDATE_ROOT)
    real_incoming = incoming.with_name("incoming-real")
    incoming.rename(real_incoming)
    incoming.symlink_to(real_incoming, target_is_directory=True)
    manifest = prepare_bundle(sftp, local, source, tmp / "server-stage", keys,
        expected_commit="a" * 40, min_os="10.0.0", initial=True,
        public_base_url="https://60.205.157.141/v1/apps/satellite-terminal-tool/")

    with pytest.raises(ValueError, match="server publication verification failed"):
        commit_bundle(sftp, manifest, fetcher=fetcher, candidates=[(source, remote)])

    assert not sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool/metadata/timestamp.json").exists()


@pytest.mark.parametrize("second_version,second_platform", [("2.1.0", "mac"), ("2.0.0", "win")])
def test_grouped_candidates_require_one_version_and_unique_platform(deployment, second_version, second_platform):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    sources = [(candidate(tmp / "first"), "10.0.0"),
               (candidate(tmp / "second", second_version, platform=second_platform), "14.0")]
    with pytest.raises(ValueError, match="one release version|unique platform"):
        prepare_bundle(sftp, local, sources, tmp / "group", keys, expected_commit="a" * 40,
            public_base_url="https://example.test/v1/apps/satellite-terminal-tool/", initial=True)
    assert not sftp.moves


def test_existing_package_digest_is_verified_on_server_without_sftp_readback(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    manifest = prepare("bundle")
    fetcher.reject = "complete.7z"
    with pytest.raises(DownloadHTTPError):
        commit_bundle(sftp, manifest, fetcher=fetcher)
    assert not any(path.endswith(".7z") for path in sftp.read_paths)
    package = next(sftp.path(PUBLIC_ROOT).rglob("*.7z"))
    original = package.read_bytes()
    package.write_bytes(b"!" + original[1:])
    fetcher.reject = ""
    sftp.read_paths.clear()
    with pytest.raises(ValueError, match="server publication verification failed"):
        commit_bundle(sftp, manifest, fetcher=fetcher)
    assert not any(path.endswith(".7z") for path in sftp.read_paths)
    assert not sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool/metadata/timestamp.json").exists()
    package.write_bytes(original)
    assert commit_bundle(sftp, manifest, fetcher=fetcher)["state"] == "published"
    assert check().tag_name == "2.0.0"


def test_range_fetcher_uses_shared_trusted_https_opener(monkeypatch):
    import io
    from types import SimpleNamespace
    from scripts import promote_release

    calls = []
    def open_response(request, *, timeout):
        calls.append((request.full_url, request.get_header("Range"), timeout))
        response = io.BytesIO(b"data")
        response.status = 206
        response.headers = {"Content-Range": "bytes 0-3/4"}
        return response

    monkeypatch.setattr(promote_release, "build_https_opener", lambda: SimpleNamespace(open=open_response))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("implicit CA opener used"))
    assert promote_release.PublicRangeFetcher().fetch_range("https://example.test/file", 0, 3) == b"data"
    assert calls == [("https://example.test/file", "bytes=0-3", 10.0)]


def test_prepare_cli_validates_candidate_os_pairing_before_connect(monkeypatch, tmp_path):
    from scripts import promote_release

    monkeypatch.setattr(promote_release, "_connect", lambda _args: pytest.fail("connected before input validation"))
    with pytest.raises(SystemExit) as error:
        promote_release.main(["prepare", "--config", "config", "--known-hosts", "known", "--identity-file", "identity",
            "--repository", str(tmp_path), "--key-dir", "keys", "--candidate", "windows", "--candidate", "macos",
            "--min-os", "10.0", "--output", "bundle", "--expected-commit", "a" * 40,
            "--public-base-url", "https://example.test/"])
    assert error.value.code == 2


def test_orchestrator_serialized_resume_keeps_both_signed_platform_catalogs(deployment, monkeypatch):
    """真实签名+真实编排字段经过 JSON 重载，提交仍只有一个事务。"""
    from scripts import release

    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    commit_bundle(sftp, prepare("baseline", version="1.0.0", commit="b" * 40), fetcher=fetcher)
    source = tmp / "source"
    (source / "doc").mkdir(parents=True)
    (source / "doc/v2.0.0_release_notes.md").write_text("release")
    state = tmp / "runs"
    windows = state / "v2.0.0/candidates/windows-ops"
    windows.parent.mkdir(parents=True)
    candidate(windows)
    macos = source / ("release/candidates/" + "a" * 40 + "/macos/ops")
    macos.parent.mkdir(parents=True)
    candidate(macos, platform="mac")
    monkeypatch.setattr(release, "inspect_preflight", lambda *_args: {"source_commit": "a" * 40, "tag": "v2.0.0"})
    operations = []

    def runner(arguments, **_options):
        command = arguments[2]
        operations.append(command)
        def values(flag):
            return [arguments[i + 1] for i, value in enumerate(arguments) if value == flag]
        if command == "prepare":
            manifest = prepare_bundle(sftp, local,
                [(Path(path), minimum) for path, minimum in zip(values("--candidate"), values("--min-os"))],
                Path(values("--output")[0]), keys, expected_commit=values("--expected-commit")[0],
                public_base_url=values("--public-base-url")[0])
            return json.dumps({"state": "prepared", "bundle": str(manifest)})
        assert command == "commit"
        return json.dumps(commit_bundle(sftp, Path(values("--bundle")[0]), fetcher=fetcher))

    first = release.ReleaseOrchestrator(root=source, state_root=state, version="2.0.0", runner=runner)
    first._prepare_publication()
    confirmed = first.journal.data["evidence"]["publication_sha256"]
    resumed = release.ReleaseOrchestrator(root=source, state_root=state, version="2.0.0", runner=runner,
                                          confirm_stable=confirmed)
    resumed._stable_confirmation()
    resumed._commit_publication()
    assert operations == ["prepare", "commit"]
    assert sum(path.endswith("/timestamp.json") for path in sftp.moves) == 2  # baseline + whole release
    url = "https://60.205.157.141/v1/apps/satellite-terminal-tool/"
    config = {**DEFAULTS, "repo_id": "test-repo", "trusted_root": Metadata.from_file(str(local / "metadata/root.json")).to_dict(),
              "metadata_base_url": url + "metadata/", "target_base_url": url + "targets/"}
    for platform, architecture, os_version in (("win", "x86_64", "10.0.19041"), ("mac", "arm64", "14.0")):
        latest = ReleaseChecker(config, cache_dir=tmp / ("final-" + platform), platform=platform,
            architecture=architecture, os_version=os_version, fetcher=fetcher).fetch_latest()
        assert latest.tag_name == "2.0.0"
        assert latest.identity["build_id"] == "a" * 40


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


def test_server_digest_failure_keeps_timestamp_unpublished(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    manifest = prepare("bundle")
    sftp.corrupt_before_verification = True
    with pytest.raises(ValueError, match="server publication verification failed"):
        commit_bundle(sftp, manifest, fetcher=fetcher)
    assert not sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool/metadata/timestamp.json").exists()


def test_server_archives_a_request_when_verified_result_is_reused(deployment):
    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    commit_bundle(sftp, prepare("bundle"), fetcher=fetcher)
    requests = sftp.path(VERIFY_REQUEST_ROOT)
    archive = sftp.path(STATE_ROOT + "/verification/archive")
    assert list(requests.glob("*.json"))
    process_requests(requests, sftp.path(VERIFY_RESULT_ROOT), sftp.path(PUBLIC_ROOT), archive)
    assert not list(requests.glob("*.json"))
    assert list(archive.glob("*.json"))


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


def test_disconnected_lock_cleanup_preserves_the_primary_failure(tmp_path):
    sftp = Sftp(tmp_path / "remote")
    lock = STATE_ROOT + "/publication.lock"
    with pytest.raises(RuntimeError, match="primary publication failure"):
        with _remote_lock(sftp, lock, "a" * 64, recover=False):
            sftp.lstat = lambda _path: (_ for _ in ()).throw(ConnectionError("connection closed"))
            raise RuntimeError("primary publication failure")
    assert sftp.path(lock).is_file()


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


@pytest.mark.parametrize("corrupt_public_range", [False, True])
def test_cli_uses_bounded_https_ranges_and_keeps_one_sftp_owner(
        deployment, monkeypatch, corrupt_public_range):
    """ECS 完整校验后只做有界 Range，分段不一致不提交 timestamp。"""
    import sys
    from types import SimpleNamespace
    from scripts import promote_release

    tmp, local, keys, sftp, fetcher, prepare, check = deployment
    manifest = prepare("idle-bundle")
    events = []

    class Transport:
        def __init__(self):
            self.authenticated = False
            self.active = True
            self.now = self.last_activity = self.interval = self.heartbeats = 0

        def set_keepalive(self, interval):
            assert self.authenticated
            self.interval = interval

        def advance(self, seconds):
            # 传输替身模拟 120 秒协议空闲期限；不改变生产 SSH/SFTP 超时。
            for _ in range(seconds):
                self.now += 1
                if self.interval and self.now - self.last_activity >= self.interval:
                    self.last_activity = self.now
                    self.heartbeats += 1
                if self.now - self.last_activity >= 120:
                    self.active = False
                    break

        def activity(self):
            if not self.active:
                raise ConnectionError("publication control connection expired while HTTPS was active")
            self.last_activity = self.now

    transport = Transport()

    class Session:
        def __enter__(self):
            transport.activity()
            return self

        def __exit__(self, *_args):
            events.append("sftp-closed")

        def get_channel(self):
            return SimpleNamespace(settimeout=lambda seconds: events.append(("sftp-timeout", seconds)))

        def __getattr__(self, name):
            operation = getattr(sftp, name)
            def call(*args, **kwargs):
                transport.activity()
                return operation(*args, **kwargs)
            return call

    class Client:
        def load_host_keys(self, _path):
            events.append("host-keys-loaded")

        def set_missing_host_key_policy(self, _policy):
            events.append("host-key-policy-set")

        def connect(self, *_args, **_kwargs):
            transport.authenticated = True
            events.append("authenticated")

        def get_transport(self):
            return transport

        def open_sftp(self):
            events.append("sftp-opened")
            return Session()

        def close(self):
            transport.active = False
            events.append("ssh-closed")

    class RangeProbe(Fetcher):
        def __init__(self, target):
            super().__init__(target)
            self.ranges = []

        def fetch_range(self, url, start, end):
            self.ranges.append((url, start, end))
            block = super().fetch_range(url, start, end)
            if corrupt_public_range and url.endswith("complete.7z"):
                block = block[:-1] + b"!"
            return block

    monkeypatch.setitem(sys.modules, "paramiko", SimpleNamespace(SSHClient=Client, RejectPolicy=object))
    probe = RangeProbe(sftp)
    monkeypatch.setattr(promote_release, "PublicRangeFetcher", lambda *_args: probe)
    config = tmp / "connection.json"
    config.write_text(json.dumps({"host": "example.test", "username": "publisher",
                                  "public_root": PUBLIC_ROOT, "state_root": STATE_ROOT}))
    known_hosts, identity = tmp / "known-hosts", tmp / "identity"
    known_hosts.touch()
    identity.touch()
    arguments = ["commit", "--config", str(config), "--known-hosts", str(known_hosts),
                 "--identity-file", str(identity), "--bundle", str(manifest)]
    if corrupt_public_range:
        with pytest.raises(ValueError, match="public publication range differs"):
            promote_release.main(arguments)
        assert not sftp.path(PUBLIC_ROOT + "/satellite-terminal-tool/metadata/timestamp.json").exists()
    else:
        promote_release.main(arguments)
        assert sftp.moves[-1].endswith("/timestamp.json")
        assert check().tag_name == "2.0.0"
    assert probe.ranges
    assert all(end - start + 1 <= 4096 for _url, start, end in probe.ranges)
    assert transport.heartbeats == 0
    assert events.count("authenticated") == 1
    assert events[-2:] == ["sftp-closed", "ssh-closed"]
    assert not sftp.path(STATE_ROOT + "/satellite-terminal-tool.publication.lock").exists()
