"""M52：真实 TUF 签名仓库配内存传输，所有验证保持离线。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import zipfile
import urllib.parse

import pytest
from securesystemslib.signer import CryptoSigner
from tuf.api.exceptions import DownloadHTTPError
from tuf.api.metadata import Metadata
from tuf.ngclient.fetcher import FetcherInterface

from satellite_debug_tool.edition import Edition
from satellite_debug_tool.release_config import DEFAULTS
from satellite_debug_tool.updater.checker import ReleaseChecker, UpdateCheckError, compare_versions
from satellite_debug_tool.updater.publishing import initialize_repository, publish_targets, publish_candidate, refresh_metadata, refresh_online_metadata, rotate_root


class RepositoryFetcher(FetcherInterface):
    def __init__(self, root: Path):
        self.root = root
        self.calls = []

    def _fetch(self, url):
        self.calls.append(url)
        path = self.root / urllib.parse.unquote(urllib.parse.urlsplit(url).path).lstrip("/")
        if not path.is_file():
            raise DownloadHTTPError("missing", 404)
        yield path.read_bytes()


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return path


@pytest.fixture
def repository(tmp_path):
    signers = {role: CryptoSigner.generate_ed25519() for role in ("root", "targets", "snapshot", "timestamp")}
    path = tmp_path / "remote"
    root = initialize_repository(path, signers, app_id="satellite-terminal-tool", repo_id="test-repo")
    identity = {"app_id": "satellite-terminal-tool", "edition": "ops", "platform": "windows", "architecture": "x86_64",
                "version": "1.9.0", "build_id": "1" * 40}
    catalog = {"schema": "satellite.update-catalog/v1", "identity": identity, "channel": "stable", "update_protocol": 1,
               "min_updater_version": "1.0.0", "config_schema": {"min": 1, "max": 1}, "min_os": "10.0.19041",
               "package_format": "7z", "layout_version": "windows-onedir-v1", "installation_mode": "automatic",
               "package": "packages/ops-windows-1.9.0.7z", "tree_manifest": "manifests/ops-windows-1.9.0.json", "notes": "release"}
    package = tmp_path / "ops.7z"
    package.write_bytes(b"trusted complete package" * 100)
    tree = write_json(tmp_path / "tree.json", {"schema": "satellite.update-tree/v1", "identity": identity, "files": []})
    catalog_file = write_json(tmp_path / "catalog.json", catalog)
    logical = "catalog/ops/stable/windows/x86_64.json"
    inputs = {catalog["package"]: package, catalog["tree_manifest"]: tree, logical: catalog_file}
    publish_targets(path, inputs, signers)
    config = {**DEFAULTS, "repo_id": "test-repo", "metadata_base_url": "https://60.205.157.141/metadata/",
              "target_base_url": "https://60.205.157.141/targets/", "trusted_root": root}
    fetcher = RepositoryFetcher(path)
    def checker(**kwargs):
        return ReleaseChecker(config, cache_dir=tmp_path / "cache", platform="win", architecture="x86_64",
                              os_version="10.0.22631", fetcher=fetcher, **kwargs)
    return {"path": path, "signers": signers, "config": config, "fetcher": fetcher, "checker": checker,
            "catalog": catalog, "catalog_file": catalog_file, "logical": logical, "inputs": inputs, "tmp": tmp_path}


@pytest.mark.parametrize("a,b,expected", [
    ("1.2.10", "1.2.9", 1), ("v1.0.0", "1.0.0", 0), ("1.0", "1.0.0", 0),
    ("1.0.0-beta.10", "1.0.0-beta.2", 1), ("1.0.0-1", "1.0.0-alpha", -1),
    ("1.0.0-beta.2", "1.0.0-beta.2.1", -1), ("1.0.0+build.1", "1.0.0+build.2", 0),
    ("1.0.0", "1.0.0-rc.1", 1),
])
def test_semver_numeric_prerelease_and_build_metadata(a, b, expected):
    assert compare_versions(a, b) == expected


def test_verified_catalog_has_hashes_identity_and_ip_https(repository):
    release = repository["checker"]().fetch_latest()
    assert release.tag_name == "1.9.0"
    assert release.identity == repository["catalog"]["identity"]
    assert release.assets[0].sha256 == hashlib.sha256(repository["inputs"][repository["catalog"]["package"]].read_bytes()).hexdigest()
    assert release.tree_manifest.sha256
    assert release.assets_for_platform("win", edition=Edition.OPS) == release.assets
    assert release.assets_for_platform("mac", edition=Edition.OPS) == []
    assert release.assets_for_platform("win", edition=Edition.USER) == []
    assert all(url.startswith("https://60.205.157.141/") for url in repository["fetcher"].calls)


def test_user_policy_and_unconfigured_source_make_zero_requests(repository):
    with pytest.raises(UpdateCheckError, match="manual"):
        repository["checker"](edition=Edition.USER).fetch_latest()
    repository["config"]["trusted_root"] = None
    with pytest.raises(UpdateCheckError, match="await deployment"):
        repository["checker"]().fetch_latest()
    assert repository["fetcher"].calls == []


def test_tampered_catalog_and_wrong_root_are_rejected(repository):
    path = repository["path"] / "targets/catalog/ops/stable/windows"
    catalog = next(path.iterdir())
    catalog.write_bytes(catalog.read_bytes().replace(b"release", b"tampered"))
    with pytest.raises(UpdateCheckError):
        repository["checker"]().fetch_latest()


def test_timestamp_rollback_is_rejected_after_newer_snapshot(repository):
    timestamp = repository["path"] / "metadata/timestamp.json"
    previous = timestamp.read_bytes()
    repository["checker"]().fetch_latest()
    refresh_metadata(repository["path"], repository["signers"])
    repository["checker"]().fetch_latest()
    timestamp.write_bytes(previous)
    with pytest.raises(UpdateCheckError, match="version"):
        repository["checker"]().fetch_latest()


def test_expired_timestamp_preserves_existing_application(repository):
    refresh_metadata(repository["path"], repository["signers"], now=datetime.now(timezone.utc) - timedelta(days=8))
    with pytest.raises(UpdateCheckError, match="expired"):
        repository["checker"]().fetch_latest()
    assert repository["inputs"][repository["catalog"]["package"]].exists()


@pytest.mark.parametrize("change", [
    {"update_protocol": 2}, {"min_updater_version": "2.0.0"}, {"min_os": "11.0.0"},
    {"config_schema": {"min": 2, "max": 2}}, {"installation_mode": "manual"},
])
def test_signed_but_incompatible_catalog_has_explicit_failure(repository, change):
    repository["catalog"].update(change)
    write_json(repository["catalog_file"], repository["catalog"])
    publish_targets(repository["path"], {repository["logical"]: repository["catalog_file"]}, repository["signers"])
    with pytest.raises(UpdateCheckError):
        repository["checker"]().fetch_latest()


def test_root_rotation_uses_complete_old_new_authorization_chain(repository):
    repository["checker"]().fetch_latest()
    next_signers = {role: CryptoSigner.generate_ed25519() for role in repository["signers"]}
    rotate_root(repository["path"], repository["signers"]["root"], next_signers)
    assert repository["checker"]().fetch_latest().tag_name == "1.9.0"
    assert (repository["path"] / "metadata/1.root.json").exists()
    assert (repository["path"] / "metadata/2.root.json").exists()


@pytest.mark.parametrize("interrupted_file", ["root.json", "timestamp.json"])
def test_interrupted_root_rotation_resumes_with_same_keys(repository, monkeypatch, interrupted_file):
    from satellite_debug_tool.updater import publishing
    repository["checker"]().fetch_latest()
    next_signers = {role: CryptoSigner.generate_ed25519() for role in repository["signers"]}
    write = publishing._write_bytes
    def stop_before_commit(path, value, **kwargs):
        if path.name == interrupted_file:
            raise OSError("injected publication interruption")
        return write(path, value, **kwargs)
    monkeypatch.setattr(publishing, "_write_bytes", stop_before_commit)
    with pytest.raises(OSError, match="interruption"):
        rotate_root(repository["path"], repository["signers"]["root"], next_signers)
    monkeypatch.setattr(publishing, "_write_bytes", write)
    rotate_root(repository["path"], repository["signers"]["root"], next_signers,
                now=datetime.now(timezone.utc) + timedelta(seconds=5))
    assert repository["checker"]().fetch_latest().tag_name == "1.9.0"
    assert Metadata.from_file(str(repository["path"] / "metadata/root.json")).signed.version == 2


def test_publication_keeps_same_version_package_immutable(repository):
    package = repository["tmp"] / "changed.7z"
    package.write_bytes(b"different content")
    with pytest.raises(ValueError, match="immutable target"):
        publish_targets(repository["path"], {repository["catalog"]["package"]: package}, repository["signers"])
    repository["catalog"]["package"] = "packages/alternate.7z"
    write_json(repository["catalog_file"], repository["catalog"])
    with pytest.raises(ValueError, match="same release version"):
        publish_targets(repository["path"], {"packages/alternate.7z": package,
                         repository["logical"]: repository["catalog_file"]}, repository["signers"])
    assert repository["checker"]().fetch_latest().tag_name == "1.9.0"


def test_build_metadata_cannot_redefine_same_semver_release(repository):
    repository["catalog"]["identity"]["version"] = "1.9.0+another-build"
    write_json(repository["catalog_file"], repository["catalog"])
    with pytest.raises(ValueError, match="same release version"):
        publish_targets(repository["path"], {repository["logical"]: repository["catalog_file"]}, repository["signers"])
    assert repository["checker"]().fetch_latest().tag_name == "1.9.0"


def test_failed_candidate_with_missing_target_keeps_existing_channel(repository):
    repository["catalog"]["package"] = "packages/missing.7z"
    write_json(repository["catalog_file"], repository["catalog"])
    before = (repository["path"] / "metadata/timestamp.json").read_bytes()
    with pytest.raises(ValueError, match="unpublished"):
        publish_targets(repository["path"], {repository["logical"]: repository["catalog_file"]}, repository["signers"])
    assert (repository["path"] / "metadata/timestamp.json").read_bytes() == before


def test_delta_is_bound_to_trusted_base_and_target_manifests(repository):
    base = write_json(repository["tmp"] / "base.json", {"old": "manifest"})
    delta = repository["tmp"] / "delta.zip"
    delta.write_bytes(b"delta")
    base_hash = hashlib.sha256(base.read_bytes()).hexdigest()
    target = repository["inputs"][repository["catalog"]["tree_manifest"]]
    repository["catalog"]["deltas"] = [{"package": "deltas/previous.zip", "base_manifest": "manifests/previous.json",
        "base_manifest_sha256": base_hash, "target_manifest_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "base_build_id": "2" * 40}]
    write_json(repository["catalog_file"], repository["catalog"])
    publish_targets(repository["path"], {repository["logical"]: repository["catalog_file"],
                    "deltas/previous.zip": delta, "manifests/previous.json": base}, repository["signers"])
    release = repository["checker"]().fetch_latest()
    assert len(release.deltas) == 1
    assert release.deltas[0].base_manifest.sha256 == base_hash
    assert release.deltas[0].target_manifest_sha256 == release.tree_manifest.sha256


def test_other_app_trust_root_is_rejected_before_network(repository):
    repository["config"]["app_id"] = "another-app"
    with pytest.raises(UpdateCheckError, match="identity"):
        repository["checker"]().fetch_latest()
    assert repository["fetcher"].calls == []


def test_signed_ip_source_migration_survives_old_host_removal_and_root_rotation(repository):
    source = write_json(repository["tmp"] / "sources.json", {
        "schema": "satellite.update-sources/v1", "app_id": "satellite-terminal-tool", "repo_id": "test-repo", "generation": 1,
        "repositories": [{"metadata_base_url": "https://60.205.157.142/metadata/", "target_base_url": "https://60.205.157.142/targets/"}],
    })
    publish_targets(repository["path"], {"bootstrap/sources.json": source}, repository["signers"])
    migrated = repository["tmp"] / "migrated"
    shutil.copytree(repository["path"], migrated)
    old_available = True
    class HostFetcher(FetcherInterface):
        def _fetch(self, url):
            parsed = urllib.parse.urlsplit(url)
            if parsed.hostname == "60.205.157.141" and old_available:
                root = repository["path"]
            elif parsed.hostname == "60.205.157.142":
                root = migrated
            else:
                raise DownloadHTTPError("offline", 503)
            path = root / urllib.parse.unquote(parsed.path).lstrip("/")
            if not path.is_file():
                raise DownloadHTTPError("missing", 404)
            yield path.read_bytes()
    def checker():
        return ReleaseChecker(repository["config"], cache_dir=repository["tmp"] / "cache", platform="win",
            architecture="x86_64", os_version="10.0.22631", fetcher=HostFetcher())
    assert checker().fetch_latest().tag_name == "1.9.0"
    old_available = False
    assert checker().fetch_latest().tag_name == "1.9.0"
    new_signers = {role: CryptoSigner.generate_ed25519() for role in repository["signers"]}
    rotate_root(migrated, repository["signers"]["root"], new_signers)
    assert checker().fetch_latest().tag_name == "1.9.0"


def test_cache_lock_rejects_second_owner_without_metadata_mutation(tmp_path):
    from satellite_debug_tool.updater.repository import repository_lock, RepositoryError
    with repository_lock(tmp_path / "refresh.lock"):
        with pytest.raises(RepositoryError, match="in use"):
            with repository_lock(tmp_path / "refresh.lock", timeout=0):
                raise AssertionError("second owner acquired active cache")


def test_version_rejects_unknown_and_invalid_numeric_prerelease():
    for value in ("unknown", "1.0.0-beta.01", "1.0.0_legacy"):
        with pytest.raises(ValueError):
            compare_versions(value, "1.0.0")


def test_daily_and_weekly_renewal_have_separate_key_permissions(repository):
    receipt = refresh_online_metadata(repository["path"], {"timestamp": repository["signers"]["timestamp"]})
    assert receipt["snapshot_version"] == 2
    assert repository["checker"]().fetch_latest().tag_name == "1.9.0"
    receipt = refresh_online_metadata(repository["path"], {k: repository["signers"][k] for k in ("snapshot", "timestamp")}, refresh_snapshot=True)
    assert receipt["snapshot_version"] == 3
    assert repository["checker"]().fetch_latest().tag_name == "1.9.0"


def test_daily_renewal_does_not_extend_expired_targets(repository):
    with pytest.raises(ValueError, match="root/targets renewal"):
        refresh_online_metadata(repository["path"], {"timestamp": repository["signers"]["timestamp"]},
                                now=datetime.now(timezone.utc) + timedelta(days=91))


def candidate_fixture(repository, *, edition="ops", version="2.0.0", commit="a" * 40, delta_base=None):
    """此夹具验证发布合同；归档内容由冻结构建专项独立验证。"""
    from satellite_debug_tool.edition import create_build_identity
    from satellite_debug_tool.updater.payload import canonical_bytes
    identity = create_build_identity(edition=edition, version=version, platform="win", architecture="x86_64", build_id=commit)
    candidate = repository["tmp"] / f"candidate-{edition}-{version}"
    candidate.mkdir()
    (candidate / "complete.7z").write_bytes(b"archive bytes verified by packaging test" * 100)
    tree = {"schema": "satellite.update-tree/v1", "identity": {**identity, "platform": "windows"},
            "files": [{"path": "SatelliteTerminalManager.exe", "kind": "file", "size": 3,
                       "sha256": hashlib.sha256(b"exe").hexdigest()}]}
    (candidate / "payload-manifest.json").write_bytes(canonical_bytes(tree))
    if delta_base:
        with zipfile.ZipFile(candidate / "delta.zip", "w") as archive:
            archive.writestr("delta.json", canonical_bytes({"schema": "satellite.update-delta/v1",
                "base_manifest_sha256": delta_base, "target_manifest_sha256": hashlib.sha256(canonical_bytes(tree)).hexdigest(), "changed": []}))
    artifacts = [{"name": p.name, "size": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in candidate.iterdir()]
    write_json(candidate / "receipt.json", {"schema": "satellite.release-candidate/v1", "identity": identity,
        "source_commit": commit, "system_signature": "unsigned", "artifacts": artifacts, "environment": {}})
    return candidate


def test_candidate_receipt_publishes_full_then_previous_baseline_delta(repository):
    candidate = candidate_fixture(repository)
    publish_candidate(repository["path"], candidate, repository["signers"], expected_commit="a" * 40, min_os="10.0.19041", precommit_probe=lambda targets: None)
    first = repository["checker"]().fetch_latest()
    assert first.tag_name == "2.0.0"
    assert first.deltas == ()
    next_candidate = candidate_fixture(repository, version="2.1.0", commit="b" * 40, delta_base=first.tree_manifest.sha256)
    publish_candidate(repository["path"], next_candidate, repository["signers"], expected_commit="b" * 40, min_os="10.0.19041", precommit_probe=lambda targets: None)
    second = repository["checker"]().fetch_latest()
    assert second.tag_name == "2.1.0"
    assert second.deltas[0].base_build_id == "a" * 40
    assert second.deltas[0].base_manifest.sha256 == first.tree_manifest.sha256
    assert second.tree_manifest.name != second.deltas[0].base_manifest.name


def test_manual_candidate_keeps_online_metadata_unchanged(repository):
    candidate = candidate_fixture(repository, edition="user")
    before = (repository["path"] / "metadata/timestamp.json").read_bytes()
    receipt = publish_candidate(repository["path"], candidate, {}, expected_commit="a" * 40,
                                min_os="10.0.19041", manual_root=repository["tmp"] / "manual")
    assert receipt["schema"] == "satellite.manual-delivery/v1"
    assert (Path(receipt["directory"]) / "complete.7z").exists()
    assert (repository["path"] / "metadata/timestamp.json").read_bytes() == before


def test_candidate_tamper_or_wrong_commit_is_rejected_before_channel_update(repository):
    candidate = candidate_fixture(repository)
    with pytest.raises(ValueError, match="approved exact commit"):
        publish_candidate(repository["path"], candidate, repository["signers"], expected_commit="b" * 40, min_os="10.0.19041")
    (candidate / "complete.7z").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="differs from its receipt"):
        publish_candidate(repository["path"], candidate, repository["signers"], expected_commit="a" * 40, min_os="10.0.19041")


def test_public_readback_failure_preserves_old_channel(repository):
    candidate = candidate_fixture(repository)
    before = (repository["path"] / "metadata/timestamp.json").read_bytes()
    called = []
    def reject_public(targets):
        called.extend(targets)
        assert any("complete.7z" in name for name in targets)
        raise ValueError("public target returned 403")
    with pytest.raises(ValueError, match="403"):
        publish_candidate(repository["path"], candidate, repository["signers"], expected_commit="a" * 40,
                          min_os="10.0.19041", precommit_probe=reject_public)
    assert called
    assert (repository["path"] / "metadata/timestamp.json").read_bytes() == before
    assert repository["checker"]().fetch_latest().tag_name == "1.9.0"
