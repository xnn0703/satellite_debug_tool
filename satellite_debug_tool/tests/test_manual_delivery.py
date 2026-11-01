"""人工完整包交付保持原字节、唯一身份和独立 user 边界。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import zipfile

import py7zr
import pytest

from scripts.publish_manual_delivery import publish_manual_delivery
from satellite_debug_tool.edition import create_build_identity
from satellite_debug_tool.package_names import release_archive_name

COMMIT = "a" * 40


def candidate(tmp_path: Path, platform="mac"):
    root, output = tmp_path / "incoming", tmp_path / "downloads"
    root.mkdir()
    output.mkdir()
    identity = create_build_identity(edition="user", version="1.9.0", platform=platform,
                                     architecture="arm64" if platform == "mac" else "x86_64", build_id=COMMIT)
    name = release_archive_name(edition="user", platform=platform, architecture=identity["architecture"],
                                version="1.9.0", extension="zip" if platform == "mac" else "7z")
    if platform == "mac":
        with zipfile.ZipFile(root / name, "w") as archive:
            archive.writestr("SatelliteTerminalManagerUser.app/Contents/Resources/satellite_debug_tool/build_identity.json",
                             json.dumps(identity))
            archive.writestr("SatelliteTerminalManagerUser.app/Contents/MacOS/SatelliteTerminalManager", b"application")
    else:
        tree = tmp_path / "payload"
        path = tree / "_internal/satellite_debug_tool/build_identity.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(identity))
        (tree / "SatelliteTerminalManager.exe").write_bytes(b"application")
        with py7zr.SevenZipFile(root / name, "w") as archive:
            for path in tree.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(tree).as_posix())
    data = (root / name).read_bytes()
    receipt = {"schema": "satellite.release-candidate/v1", "identity": identity, "source_commit": COMMIT,
               "artifacts": [{"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]}
    save(root, receipt)
    return root, output, receipt


def save(root, receipt):
    (root / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")


def publish(root, output, **kwargs):
    return publish_manual_delivery(root, output_root=output, expected_commit=kwargs.get("commit", COMMIT),
                                   public_base_url=kwargs.get("url", "https://example.com/downloads/"))


def update_digest(root, receipt):
    row = receipt["artifacts"][0]
    data = (root / row["name"]).read_bytes()
    row.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    save(root, receipt)


@pytest.mark.parametrize("platform", ["mac", "win"])
def test_verified_complete_user_bytes_are_published_and_reused(tmp_path, platform):
    root, output, receipt = candidate(tmp_path, platform)
    first = publish(root, output)
    target = Path(first["path"])
    assert target.read_bytes() == (root / receipt["artifacts"][0]["name"]).read_bytes()
    assert first["url"].endswith("/v1.9.0/" + target.name)
    assert first["identity"] == receipt["identity"]
    assert first["state"] == "published"
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o755
    assert publish(root, output)["state"] == "reused"
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("change", ["ops", "commit", "app"])
def test_wrong_authoritative_identity_is_rejected(tmp_path, change):
    root, output, receipt = candidate(tmp_path)
    if change == "ops":
        receipt["identity"]["edition"] = "ops"
    elif change == "app":
        receipt["identity"]["app_id"] = "other"
    else:
        receipt["source_commit"] = "b" * 40
    save(root, receipt)
    with pytest.raises(ValueError, match="identity"):
        publish(root, output)
    assert list(output.iterdir()) == []


def test_candidate_digest_and_safe_artifact_name_are_required(tmp_path):
    root, output, receipt = candidate(tmp_path)
    (root / receipt["artifacts"][0]["name"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="differs"):
        publish(root, output)
    receipt["artifacts"][0]["name"] = "../outside.zip"
    save(root, receipt)
    with pytest.raises(ValueError, match="basename"):
        publish(root, output)


def test_mac_archive_identity_and_crc_are_verified_before_publication(tmp_path):
    root, output, receipt = candidate(tmp_path)
    path = root / receipt["artifacts"][0]["name"]
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("unsafe/../outside", b"payload")
    update_digest(root, receipt)
    with pytest.raises(ValueError, match="unsafe"):
        publish(root, output)
    assert list(output.iterdir()) == []


def test_mac_receipt_build_id_must_match_actual_archive(tmp_path):
    root, output, receipt = candidate(tmp_path)
    receipt["identity"]["build_id"] = receipt["source_commit"] = "b" * 40
    save(root, receipt)
    with pytest.raises(ValueError, match="archive build identity"):
        publish(root, output, commit="b" * 40)


def test_corrupt_mac_crc_is_rejected_even_with_matching_receipt_hash(tmp_path):
    root, output, receipt = candidate(tmp_path)
    path = root / receipt["artifacts"][0]["name"]
    data = bytearray(path.read_bytes())
    offset = data.index(b"application")
    data[offset] ^= 1
    path.write_bytes(data)
    update_digest(root, receipt)
    with pytest.raises(ValueError, match="CRC"):
        publish(root, output)
    assert list(output.iterdir()) == []


def test_existing_different_public_bytes_are_preserved(tmp_path):
    root, output, receipt = candidate(tmp_path)
    directory = output / "v1.9.0"
    directory.mkdir()
    target = directory / receipt["artifacts"][0]["name"]
    target.write_bytes(b"previous")
    with pytest.raises(ValueError, match="different bytes"):
        publish(root, output)
    assert target.read_bytes() == b"previous"
    assert list(directory.iterdir()) == [target]


def test_symlink_output_parent_and_candidate_artifact_are_rejected(tmp_path):
    root, output, receipt = candidate(tmp_path)
    link = tmp_path / "linked-output"
    try:
        link.symlink_to(output, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires platform permissions")
    with pytest.raises(ValueError, match="real directories"):
        publish(root, link)
    path = root / receipt["artifacts"][0]["name"]
    actual = tmp_path / "actual.zip"
    path.rename(actual)
    path.symlink_to(actual)
    with pytest.raises(ValueError, match="regular file"):
        publish(root, output)


def test_https_and_existing_output_root_are_required(tmp_path):
    root, output, _ = candidate(tmp_path)
    with pytest.raises(ValueError, match="HTTPS"):
        publish(root, output, url="http://example.com/downloads/")
    with pytest.raises(FileNotFoundError):
        publish(root, output / "missing")


def test_cli_preserves_fixed_server_roots(tmp_path, monkeypatch):
    from scripts.publish_manual_delivery import main
    monkeypatch.setattr("sys.argv", ["publish_manual_delivery.py", "--candidate", str(tmp_path),
        "--expected-commit", COMMIT, "--output-root", str(tmp_path),
        "--public-base-url", "https://example.com/downloads/"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
