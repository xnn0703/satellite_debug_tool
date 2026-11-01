"""在指定干净源码 SHA 构建 Mac 候选并上传；普通 build_macos.sh 继续只写本地。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.release_artifacts import source_commit
from scripts.upload_release import component
from satellite_debug_tool.package_names import edition_directory


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--edition", choices=("user", "ops", "both"), default="both")
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--identity-file", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("Mac releases require the local macOS build host")
    if source_commit() != args.source_commit:
        raise SystemExit("release requires the requested exact clean source SHA")
    component(args.release_id)
    env = dict(os.environ, SATELLITE_BUILD_COMMIT=args.source_commit, SATELLITE_UPDATE_CHECK="0")

    def run(*arguments):
        subprocess.run([sys.executable, *arguments], cwd=ROOT, env=env, check=True)

    editions = ("user", "ops") if args.edition == "both" else (args.edition,)
    existing = {edition: ROOT / "release/candidates" / args.source_commit / "macos" / edition / "receipt.json"
                for edition in editions}
    if any(not receipt.is_file() for receipt in existing.values()):
        run("-m", "pip", "install", "-r", "requirements/build-macos.txt")
        run("-m", "pytest", "satellite_debug_tool/tests", "-q")
        run("scripts/update_translations.py", "check")
    for edition, receipt in existing.items():
        folder = edition_directory(edition)
        payload = ROOT / "dist" / edition / f"{folder}.app"
        if not receipt.is_file():
            run("scripts/build_editions.py", edition)
            run("scripts/check_frozen_application.py", "--edition", edition, "--version", args.version)
            run("scripts/release_artifacts.py", "--payload", str(payload), "--output", str(receipt.parent),
                "--source-commit", args.source_commit, "--version", args.version, "--signature", "adhoc-verified")
        else:
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            if saved["source_commit"] != args.source_commit or saved["identity"]["version"] != args.version.removeprefix("v"):
                raise ValueError("existing candidate differs from the requested release")
        deploy_python = ROOT / ".venv-deploy/bin/python"
        if not deploy_python.is_file():
            run("-m", "venv", str(deploy_python.parents[1]))
        subprocess.run([str(deploy_python), "-m", "pip", "install", "-r", "requirements/deploy.txt"],
                       cwd=ROOT, check=True)
        subprocess.run([str(deploy_python), "scripts/upload_release.py", "--config", str(args.config),
            "--receipt", str(receipt), "--release-id", args.release_id, "--known-hosts", str(args.known_hosts),
            "--identity-file", str(args.identity_file)], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
