"""文件级增量必须合成与完整发行相同的目标树，且原安装始终保留。"""
import copy
import json
from pathlib import Path
import zipfile

import pytest

from satellite_debug_tool.updater.payload import (
    PayloadError, apply_delta, build_manifest, create_delta, manifest_sha256,
    validate_manifest, verify_tree, write_manifest, read_manifest,
)


def identity(build="old", version="1.0.0"):
    return dict(app_id="satellite-terminal-tool", edition="ops", platform="windows",
                architecture="x86_64", version=version, build_id=build)


def tree(directory, files):
    directory.mkdir()
    for name, data in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return directory


@pytest.fixture
def pair(tmp_path):
    old = tree(tmp_path / "old", {"app.exe": b"old-code", "_internal/Qt.dll": b"library", "removed": b"old"})
    new = tree(tmp_path / "new", {"app.exe": b"new-code", "_internal/Qt.dll": b"library", "added/data": b"new"})
    base = build_manifest(old, identity())
    target = build_manifest(new, identity("new", "2.0.0"))
    archive = tmp_path / "delta.zip"
    create_delta(old, new, archive, base, target)
    return old, new, base, target, archive


def test_delta_reuses_library_adds_changes_and_removes_old_files(pair, tmp_path):
    old, new, base, target, archive = pair
    with zipfile.ZipFile(archive) as zf:
        assert set(zf.namelist()) == {"delta.json", "files/app.exe", "files/added/data"}
    result = apply_delta(old, archive, base, target, tmp_path / "prepared")
    verify_tree(result, target)
    verify_tree(old, base)
    assert not (result / "removed").exists()
    assert (old / "removed").is_file()


def test_modified_baseline_is_rejected_before_destination_created(pair, tmp_path):
    old, _, base, target, archive = pair
    (old / "app.exe").write_bytes(b"modified")
    with pytest.raises(PayloadError):
        apply_delta(old, archive, base, target, tmp_path / "prepared")
    assert not (tmp_path / "prepared").exists()


def test_same_version_different_build_and_manifest_are_rejected(pair, tmp_path):
    old, _, base, target, archive = pair
    other = copy.deepcopy(base)
    other["identity"]["build_id"] = "another-build"
    with pytest.raises(PayloadError, match="baseline or target"):
        apply_delta(old, archive, other, target, tmp_path / "prepared")


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/escape", "dir\\escape", "NUL.txt", "name."])
def test_manifest_rejects_escaping_and_windows_special_paths(pair, name):
    bad = copy.deepcopy(pair[3])
    bad["files"][0]["path"] = name
    with pytest.raises(PayloadError):
        validate_manifest(bad)


def test_casefold_and_file_directory_collision(pair):
    for name in ("APP.EXE", "app.exe/child"):
        bad = copy.deepcopy(pair[3])
        row = dict(bad["files"][0], path=name)
        bad["files"].append(row)
        with pytest.raises(PayloadError):
            validate_manifest(bad)


def test_extra_archive_member_is_rejected_before_writing(pair, tmp_path):
    old, _, base, target, archive = pair
    with zipfile.ZipFile(archive, "a") as zf:
        zf.writestr("files/../../escape", b"bad")
    with pytest.raises(PayloadError):
        apply_delta(old, archive, base, target, tmp_path / "prepared")
    assert not (tmp_path / "prepared").exists()


def test_wrong_changed_file_hash_cleans_prepared_directory(pair, tmp_path):
    old, _, base, target, archive = pair
    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(corrupt, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, b"BAD-code" if name == "files/app.exe" else src.read(name))
    with pytest.raises(PayloadError, match="trusted manifest"):
        apply_delta(old, corrupt, base, target, tmp_path / "prepared")
    assert not (tmp_path / "prepared").exists()
    verify_tree(old, base)


def test_manifest_round_trip_hash_is_canonical(pair, tmp_path):
    path = tmp_path / "tree.json"
    write_manifest(path, pair[3])
    import hashlib
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest_sha256(read_manifest(path))


def test_links_and_extra_local_files_are_rejected(pair):
    old, _, base, _, _ = pair
    (old / "link").symlink_to(old / "app.exe")
    with pytest.raises(PayloadError, match="link"):
        verify_tree(old, base)


def test_cross_edition_delta_is_rejected(pair, tmp_path):
    old, new, base, target, _ = pair
    target = copy.deepcopy(target)
    target["identity"]["edition"] = "user"
    with pytest.raises(PayloadError):
        create_delta(old, new, tmp_path / "bad.zip", base, target)
