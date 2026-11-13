"""从已构建载荷生成不可变候选；只写本地，不签名或推进正式渠道。"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from satellite_debug_tool.edition import Edition, create_build_identity
from satellite_debug_tool.package_names import WINDOWS_EXECUTABLE, release_archive_name
from satellite_debug_tool.updater.install_platform import identity_path, installation_platform
from satellite_debug_tool.updater.payload import build_manifest, create_delta, read_manifest, sha256_file, verify_tree, write_manifest
from satellite_debug_tool.versioning import parse_version


def source_commit(root: Path = ROOT, *, clean: bool = True) -> str:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if clean and subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip():
        raise ValueError("release source requires a clean checkout")
    return commit


def load_identity(payload: Path, expected_commit: str, expected_version: str) -> dict:
    paths = [path for path in payload.rglob("build_identity.json") if not path.is_symlink()]
    expected_path = identity_path(payload, installation_platform(payload))
    if len(paths) != 1 or paths[0] != expected_path:
        raise ValueError(
            f"release payload requires one build identity at {expected_path.relative_to(payload)}"
        )
    identity = json.loads(expected_path.read_text(encoding="utf-8"))
    canonical = create_build_identity(**{key: identity[key] for key in
        ("edition", "version", "platform", "architecture", "build_id")})
    if identity != canonical or identity["build_id"] != expected_commit:
        raise ValueError("payload identity differs from the exact source commit")
    parse_version(expected_version.removeprefix("v"), complete=True)
    if identity["version"] != expected_version.removeprefix("v"):
        raise ValueError("payload identity differs from the requested release version")
    return identity


def package(payload: Path, destination: Path, *, commit: str, version: str,
            base_root: Path | None = None, base_manifest: Path | None = None,
            signature: str = "unsigned") -> Path:
    """调用方先完成平台签名，随后本函数哈希最终文件树并归档。"""
    import py7zr

    payload = payload.resolve(strict=True)
    destination = destination.resolve()
    if payload == destination or payload in destination.parents:
        raise ValueError("candidate output must be outside the application payload")
    identity = load_identity(payload, commit, version)
    if identity["platform"] == "win" and signature not in ("unsigned", "authenticode-verified"):
        raise ValueError("Windows signature evidence is invalid")
    if identity["platform"] == "mac" and signature not in ("adhoc-verified", "developer-id-verified"):
        raise ValueError("macOS signature verification is required")
    if bool(base_root) != bool(base_manifest):
        raise ValueError("delta requires both baseline tree and its manifest")
    if base_root and (identity["edition"] != "ops" or identity["platform"] != "win"):
        raise ValueError("file delta currently supports Windows operations edition")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("candidate output already exists; upload the existing receipt to retry")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".candidate-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        windows = identity["platform"] == "win"
        if windows:
            name = release_archive_name(edition=identity["edition"], platform=identity["platform"],
                architecture=identity["architecture"], version=identity["version"], extension="7z")
            tree = build_manifest(payload, identity)
            write_manifest(staging / "payload-manifest.json", tree)
            with py7zr.SevenZipFile(staging / name, "w") as output:
                for path in sorted(payload.rglob("*")):
                    if path.is_file():
                        output.write(path, path.relative_to(payload).as_posix())
            from scripts.verify_update_archive import verify_archive
            verify_archive(staging / name, edition=identity["edition"], version=version)
            with tempfile.TemporaryDirectory(prefix=".verify-package-", dir=destination.parent) as expanded:
                with py7zr.SevenZipFile(staging / name, "r") as archive:
                    archive.extractall(path=expanded)
                verify_tree(Path(expanded), tree)
            if base_root:
                create_delta(base_root, payload, staging / "delta.zip", read_manifest(base_manifest), tree)
        else:
            name = release_archive_name(edition=identity["edition"], platform=identity["platform"],
                architecture=identity["architecture"], version=identity["version"], extension="zip")
            subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(payload),
                            str(staging / name)], check=True)
            with zipfile.ZipFile(staging / name) as archive:
                if archive.testzip() is not None:
                    raise ValueError("candidate ZIP CRC verification failed")
            tree = build_manifest(payload, identity)
            write_manifest(staging / "payload-manifest.json", tree)
        artifacts = [{"name": path.name, "size": path.stat().st_size, "sha256": sha256_file(path)}
                     for path in sorted(staging.iterdir())]
        receipt = {"schema": "satellite.release-candidate/v1", "identity": identity,
                   "source_commit": commit, "system_signature": signature, "artifacts": artifacts,
                   "environment": {"python": sys.version, "host": platform.platform(),
                       "packages": sorted([{"name": item.metadata["Name"], "version": item.version}
                           for item in importlib.metadata.distributions()], key=lambda item: item["name"].lower())}}
        (staging / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        if destination.exists():
            destination.rmdir()
        Path(temporary).rename(destination)
    return destination / "receipt.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--base-root", type=Path)
    parser.add_argument("--base-manifest", type=Path)
    parser.add_argument("--signature", choices=("unsigned", "authenticode-verified", "adhoc-verified", "developer-id-verified"), default="unsigned")
    args = parser.parse_args()
    if source_commit() != args.source_commit:
        raise SystemExit("release checkout differs from the requested commit")
    if args.signature == "authenticode-verified":
        for name in (WINDOWS_EXECUTABLE, "updater.exe"):
            path = args.payload / name
            if path.exists():
                subprocess.run(["signtool", "verify", "/pa", str(path)], check=True)
    if args.signature in ("adhoc-verified", "developer-id-verified"):
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(args.payload)], check=True)
        detail = subprocess.check_output(["codesign", "-dv", "--verbose=4", str(args.payload)], stderr=subprocess.STDOUT, text=True)
        if args.signature == "developer-id-verified" and "Authority=Developer ID Application:" not in detail:
            raise SystemExit("Developer ID signature evidence is required")
    print(package(args.payload, args.output, commit=args.source_commit, version=args.version,
                  base_root=args.base_root, base_manifest=args.base_manifest, signature=args.signature))


if __name__ == "__main__":
    main()
