#!/usr/bin/env python3
"""Build one AES-GCM customer OTA package from an application BIN."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from satellite_debug_tool.core.product import customer_ota_product_policy
from satellite_debug_tool.core.security import (
    FirmwarePackageError,
    create_firmware_package_bytes,
    load_bundled_encryption_keys,
    verify_firmware_package,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path, help="product application .bin")
    parser.add_argument("output", type=Path, help="output .sfpkg")
    parser.add_argument("--version", required=True, help="firmware semantic version")
    parser.add_argument("--product", default="AFD01")
    parser.add_argument(
        "--version-policy",
        choices=("upgrade_only", "allow_same", "allow_downgrade"),
        default="upgrade_only",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.output.suffix.lower() != ".sfpkg":
        raise SystemExit("output filename must end with .sfpkg")
    policy = customer_ota_product_policy(args.product)
    if policy is None or policy.customer_ota_product is None:
        raise SystemExit(f"unregistered customer OTA product: {args.product!r}")
    if args.output.resolve() == args.firmware.resolve():
        raise SystemExit("output package must not overwrite the firmware")
    try:
        keys = load_bundled_encryption_keys()
    except FirmwarePackageError as exc:
        raise SystemExit(exc.detail) from exc
    if len(keys) != 1:
        raise SystemExit("firmware key store must contain exactly one active key")
    encryption_key = next(iter(keys.values()))
    try:
        firmware = args.firmware.read_bytes()
    except OSError as exc:
        raise SystemExit(f"failed to read firmware: {exc}") from exc
    try:
        manifest, encrypted = create_firmware_package_bytes(
            firmware,
            firmware_name=args.firmware.name,
            product=policy.customer_ota_product,
            hardware_types=(policy.hardware_type,),
            version=args.version,
            version_policy=args.version_policy,
            encryption_key=encryption_key,
        )
    except FirmwarePackageError as exc:
        raise SystemExit(exc.detail) from exc

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary_handle = tempfile.NamedTemporaryFile(
        prefix=f".{args.output.name}.",
        suffix=".tmp",
        dir=args.output.parent,
        delete=False,
    )
    temporary = Path(temporary_handle.name)
    temporary_handle.close()
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr("firmware.aesgcm", encrypted)
        package = verify_firmware_package(
            temporary,
            expected_product=policy.customer_ota_product,
            expected_hardware=policy.hardware_type,
            current_version=None,
            encryption_keys=keys,
        )
        if package.firmware != firmware:
            raise SystemExit("self-verification returned different firmware bytes")
        temporary.replace(args.output)
    except FirmwarePackageError as exc:
        raise SystemExit(exc.detail) from exc
    finally:
        temporary.unlink(missing_ok=True)

    print(f"created: {args.output}")
    print(f"algorithm: {package.algorithm}")
    print(f"key_id: {package.key_id}")
    print(f"firmware_sha256: {package.firmware_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
