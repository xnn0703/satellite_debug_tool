"""Verify the fixed Lingjing platform-service payload without importing the app."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


EXPECTED_SCHEMA = "satellite.lingjing-platform-service"
EXPECTED_PLAT_SHA256 = (
    "eaead18830af2ce2799c73f47d7197954eb26b02f8d95c616949203ec190d761"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(root: Path) -> None:
    manifest = json.loads((root / "service-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != EXPECTED_SCHEMA or manifest.get("schema_version") != 1:
        raise RuntimeError("platform service manifest schema is invalid")
    if manifest.get("entrypoint") != "ProConvert.exe":
        raise RuntimeError("platform service entrypoint is invalid")
    for record in manifest.get("files", []):
        relative = Path(str(record["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"unsafe platform service path: {relative}")
        path = root / relative
        if path.stat().st_size != int(record["size"]):
            raise RuntimeError(f"platform service size differs: {relative}")
        if _sha256(path) != str(record["sha256"]):
            raise RuntimeError(f"platform service hash differs: {relative}")
    plat_hash = _sha256(root / "配置" / "plat.xml")
    if plat_hash != EXPECTED_PLAT_SHA256:
        raise RuntimeError("the bundled plat.xml is not the approved original file")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_platform_service_assets.py <service-root>")
    service_root = Path(sys.argv[1])
    verify(service_root)
    print(f"Platform service assets OK: {service_root}")
