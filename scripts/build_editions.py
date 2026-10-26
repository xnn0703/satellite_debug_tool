"""Build user/ops payloads through the project spec and verify each artifact."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def run(*arguments, env=None):
    subprocess.run(arguments, cwd=ROOT, env=env, check=True)


def build(edition: str) -> None:
    started = time.perf_counter()
    env = dict(os.environ, SATELLITE_BUILD_EDITION=edition)
    output = ROOT / "dist" / edition
    work = ROOT / "build" / edition
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(output),
        "--workpath", str(work), "satellite_debug_tool.spec", env=env)
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(output),
        "--workpath", str(work / "updater"), "updater.spec", env=env)
    mac = sys.platform == "darwin"
    app_name = "SatelliteTerminalToolUser" if edition == "user" else "SatelliteDebugTool"
    payload = output / (f"{app_name}.app" if mac else ("SatelliteTerminalToolUser" if edition == "user" else "SatelliteDebugTool"))
    updater = "updater" if mac else "updater.exe"
    shutil.copy2(output / updater, payload / "Contents/MacOS/updater" if mac else payload / updater)
    run(sys.executable, "scripts/verify_model_assets.py", str(payload))
    run(sys.executable, "scripts/verify_edition_package.py", str(payload), "--edition", edition,
        "--toc", str(work / "satellite_debug_tool" / "Analysis-00.toc"))
    if mac:
        run("codesign", "--force", "--deep", "--sign", "-", str(payload))
        run("codesign", "--verify", "--deep", "--strict", str(payload))
        run("xattr", "-cr", str(payload))
    else:
        docs = ["user_edition_guide.md"] if edition == "user" else ["WINDOWS_QUICK_START.md", "user_manual.md", "user_manual_en.md", "production_automation_operator_guide.md"]
        for name in docs:
            shutil.copy2(ROOT / "doc" / name, payload / name)
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    archive = release / f"SatelliteDebugTool-{edition}-{'macOS' if mac else 'Windows'}-{platform.machine()}.zip"
    archive.unlink(missing_ok=True)
    if mac:
        run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(payload), str(archive))
    else:
        shutil.make_archive(str(archive.with_suffix("")), "zip", output, payload.name)
    metrics = {"edition": edition, "payload": str(payload), "archive": str(archive),
               "archive_bytes": archive.stat().st_size, "build_seconds": round(time.perf_counter() - started, 2),
               "payload_bytes": sum(p.stat().st_size for p in payload.rglob("*") if p.is_file() and not p.is_symlink())}
    (release / f"build-{edition}.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics), flush=True)


def main():
    if sys.platform not in ("darwin", "win32"):
        raise SystemExit("Edition packages currently support macOS and Windows")
    parser = argparse.ArgumentParser()
    parser.add_argument("edition", choices=("user", "ops", "both"), default="ops", nargs="?")
    args = parser.parse_args()
    run(sys.executable, "scripts/update_translations.py", "check")
    for edition in ("user", "ops") if args.edition == "both" else (args.edition,):
        build(edition)


if __name__ == "__main__":
    main()
