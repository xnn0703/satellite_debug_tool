"""启动真实冻结程序，并将 windowed 程序的阶段/异常证据回传 CI。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--edition", choices=("user", "ops"), required=True)
    parser.add_argument("--version", default="")
    args = parser.parse_args()
    version = args.version.removeprefix("v") or re.search(
        r'__version__\s*=\s*"([^"]+)"', (ROOT / "satellite_debug_tool/__init__.py").read_text(encoding="utf-8")
    ).group(1)
    folder = "SatelliteTerminalToolUser" if args.edition == "user" else "SatelliteDebugTool"
    exe = ROOT / "dist" / args.edition / folder / "SatelliteDebugTool.exe"
    result = ROOT / "release" / f"smoke-{args.edition}.json"
    result.parent.mkdir(exist_ok=True)
    env = dict(os.environ, SATELLITE_UPDATE_CHECK="0")
    with result.with_suffix(".stdout.log").open("w") as stdout, result.with_suffix(".stderr.log").open("w") as stderr:
        process = subprocess.Popen([str(exe), "--smoke-test", f"--smoke-output={result}"], env=env, stdout=stdout, stderr=stderr)
        try:
            code = process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            code = 124
    for path in sorted(result.parent.glob(f"smoke-{args.edition}.*")):
        print(f"{path.name}:\n{path.read_text(encoding='utf-8', errors='replace')}", flush=True)
    if code != 0:
        raise SystemExit(f"Frozen {args.edition} smoke failed with exit code {code}")
    proof = json.loads(result.read_text(encoding="utf-8"))
    if env.get("QT_QPA_PLATFORM") and proof.get("qpa_platform") != env["QT_QPA_PLATFORM"]:
        raise SystemExit("Frozen smoke used a different Qt platform")
    if (proof.get("edition"), proof.get("version"), proof.get("overview_panels"), proof.get("shell_dialog")) != (args.edition, version, "PASS", "PASS"):
        raise SystemExit("Frozen smoke evidence differs from the release contract")


if __name__ == "__main__":
    main()
