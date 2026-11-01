"""下载准备的增量选择、一次完整包回退和人工安装边界。"""
import hashlib
import json
from pathlib import Path

import pytest

from satellite_debug_tool.updater.checker import Asset, DeltaRelease, LatestRelease
from satellite_debug_tool.updater.downloader import DownloadError
from satellite_debug_tool.updater.payload import build_manifest, canonical_bytes, create_delta, verify_tree
from satellite_debug_tool.updater.preparation import prepare_download


class FilesDownloader:
    def __init__(self, files, failure=None):
        self.files = files
        self.failure = failure
        self.calls = []

    def download_all(self, assets, dest, **kwargs):
        item = assets[0]
        self.calls.append(item.name)
        if item.name == self.failure:
            raise DownloadError("download_hash_mismatch", "corrupt delta")
        path = dest / item.name
        path.write_bytes(self.files[item.name])
        return [path]


@pytest.fixture
def release(tmp_path):
    base_dir, new_dir = tmp_path / "base", tmp_path / "new"
    base_identity = dict(app_id="satellite-terminal-tool", edition="ops", platform="windows",
                         architecture="x86_64", version="1.0.0", build_id="old")
    target_identity = dict(base_identity, version="2.0.0", build_id="new")
    for directory, identity in ((base_dir, base_identity), (new_dir, target_identity)):
        (directory / "_internal/satellite_debug_tool").mkdir(parents=True)
        (directory / "_internal/satellite_debug_tool/build_identity.json").write_text(json.dumps(identity))
        (directory / "app.exe").write_bytes(identity["build_id"].encode())
        (directory / "_internal/Qt.dll").write_bytes(b"unchanged")
    base, target = build_manifest(base_dir, base_identity), build_manifest(new_dir, target_identity)
    delta_path = tmp_path / "delta.zip"
    create_delta(base_dir, new_dir, delta_path, base, target)
    files = {"base.json": canonical_bytes(base), "target.json": canonical_bytes(target),
             "delta.zip": delta_path.read_bytes(), "full.7z": b"full-package"}

    def item(name):
        data = files[name]
        return Asset(name, "https://example.invalid/" + name, len(data), hashlib.sha256(data).hexdigest(), name)

    old_manifest, new_manifest = item("base.json"), item("target.json")
    latest = LatestRelease("2.0.0", assets=[item("full.7z")], identity=target_identity,
        tree_manifest=new_manifest, installation_mode="automatic",
        deltas=(DeltaRelease(item("delta.zip"), old_manifest, old_manifest.sha256, new_manifest.sha256, "old"),))
    return latest, files, base_dir, target


def test_matching_base_uses_delta_and_constructs_complete_tree(release, tmp_path):
    latest, files, old, target = release
    downloader = FilesDownloader(files)
    result = prepare_download(latest, tmp_path / "download", install_dir=old, downloader=downloader)
    assert result.asset.name == "delta.zip"
    assert "full.7z" not in downloader.calls
    verify_tree(result.prepared_payload, target)


def test_changed_base_uses_complete_package(release, tmp_path):
    latest, files, old, _ = release
    (old / "app.exe").write_bytes(b"modified")
    downloader = FilesDownloader(files)
    result = prepare_download(latest, tmp_path / "download", install_dir=old, downloader=downloader)
    assert result.asset.name == "full.7z"
    assert "delta.zip" not in downloader.calls
    assert result.prepared_payload is None


def test_bad_delta_falls_back_once_to_same_target(release, tmp_path):
    latest, files, old, _ = release
    downloader = FilesDownloader(files, failure="delta.zip")
    statuses = []
    result = prepare_download(latest, tmp_path / "download", install_dir=old,
                              downloader=downloader, on_status=statuses.append)
    assert result.asset == latest.assets[0]
    assert downloader.calls.count("full.7z") == 1
    assert any("Incremental preparation failed" in s for s in statuses)


def test_older_build_skips_delta(release, tmp_path):
    latest, files, old, _ = release
    identity_path = old / "_internal/satellite_debug_tool/build_identity.json"
    data = json.loads(identity_path.read_text())
    data["build_id"] = "older"
    identity_path.write_text(json.dumps(data))
    downloader = FilesDownloader(files)
    result = prepare_download(latest, tmp_path / "download", install_dir=old, downloader=downloader)
    assert result.asset.name == "full.7z"
    assert "base.json" not in downloader.calls


@pytest.mark.parametrize("invalid", [[], None, 3, "old"])
def test_invalid_installed_identity_uses_complete_package(release, tmp_path, invalid):
    latest, files, old, _ = release
    (old / "_internal/satellite_debug_tool/build_identity.json").write_text(json.dumps(invalid))
    downloader = FilesDownloader(files)
    result = prepare_download(latest, tmp_path / "download", install_dir=old, downloader=downloader)
    assert result.asset.name == "full.7z"
    assert "delta.zip" not in downloader.calls


def test_manual_release_downloads_only_complete_package(release, tmp_path):
    latest, files, _, _ = release
    latest.installation_mode = "manual"
    downloader = FilesDownloader(files)
    result = prepare_download(latest, tmp_path / "download", downloader=downloader)
    assert result.installation_mode == "manual"
    assert downloader.calls == ["full.7z"]


def test_trusted_manifest_failure_stops_before_any_package(release, tmp_path):
    latest, files, old, _ = release
    downloader = FilesDownloader(files, failure="target.json")
    with pytest.raises(DownloadError):
        prepare_download(latest, tmp_path / "download", install_dir=old, downloader=downloader)
    assert downloader.calls == ["target.json"]
