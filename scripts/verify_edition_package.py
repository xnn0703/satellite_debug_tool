"""Verify the frozen build identity and operations-module boundary."""

import argparse
import ast
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.edition_policy import USER_SHELL_MODULES, operations_modules
from satellite_debug_tool.updater.install_platform import identity_path, installation_platform

FORBIDDEN = ("PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "satellite_debug_tool.ui.map_widget", "satellite_debug_tool.ui.main_window", "satellite_debug_tool.ui.production_workspace",
             "satellite_debug_tool.ui.device_view", "satellite_debug_tool.ui.customer_iperf_view",
             "satellite_debug_tool.core.iperf_test", "satellite_debug_tool.core.external_power_monitor",
             "satellite_debug_tool.core.motion_platform",
             "satellite_debug_tool.core.production.reporting", "satellite_debug_tool.core.production.fixture_runtime")


def verify(root, edition, toc):
    forbidden_modules = FORBIDDEN + operations_modules(Path(__file__).resolve().parents[1])
    identities = [p for p in root.rglob("build_identity.json") if not p.is_symlink()]
    expected_identity = identity_path(root, installation_platform(root))
    if len(identities) != 1 or identities[0] != expected_identity:
        raise ValueError(
            f"frozen payload requires one build identity at {expected_identity.relative_to(root)}"
        )
    data = json.loads(expected_identity.read_text())
    if data["edition"] != edition or not data["version"]:
        raise ValueError("build identity differs from the requested edition")
    if edition == "user":
        forbidden_files = [path for path in root.rglob("*")
                           if path.name in ("updater", "updater.exe", "release.config.json")]
        if forbidden_files:
            raise ValueError(f"user payload includes application update files: {forbidden_files}")
    if toc:
        data = ast.literal_eval(toc.read_text())
        names = []

        def walk(value):
            if isinstance(value, (list, tuple)):
                if len(value) == 3 and isinstance(value[0], str):
                    names.append(value[0])
                for item in value:
                    walk(item)
        walk(data)
        if edition == "user":
            forbidden = [name for name in names if "WebEngine" in name or any(
                name.replace("/", ".") == item or name.replace("/", ".").startswith(item + ".") for item in forbidden_modules)]
            if forbidden:
                raise ValueError(f"user payload includes operations modules: {forbidden}")
            missing = USER_SHELL_MODULES.difference(names)
            if missing:
                raise ValueError(f"user payload requires Shell modules: {sorted(missing)}")
        print(f"{edition}: {len(names)} frozen TOC entries; identity and module boundary PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--edition", required=True, choices=("user", "ops"))
    parser.add_argument("--toc", type=Path)
    args = parser.parse_args()
    verify(args.root, args.edition, args.toc)
