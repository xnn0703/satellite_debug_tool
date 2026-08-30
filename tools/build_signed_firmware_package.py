#!/usr/bin/env python3
"""Build an authenticated customer OTA package for a registered product.

The private key is supplied explicitly and is never copied into the package or
repository. The printed trust-store entry contains only the raw public key.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from satellite_debug_tool.core.security import build_signed_manifest_message
from satellite_debug_tool.core.product import customer_ota_product_policy
from satellite_debug_tool.core.security import (
    FirmwarePackageError,
    validate_firmware_manifest_fields,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path, help="product application .bin")
    parser.add_argument("output", type=Path, help="output .sfpkg")
    parser.add_argument("--private-key", type=Path, required=True, help="Ed25519 private key in PEM")
    parser.add_argument("--key-id", required=True, help="key ID already provisioned in the app trust store")
    parser.add_argument("--version", required=True, help="firmware semantic version")
    parser.add_argument("--product", default="AFD01")
    parser.add_argument(
        "--version-policy",
        choices=("upgrade_only", "allow_same", "allow_downgrade"),
        default="upgrade_only",
    )
    parser.add_argument("--key-label", default="Product release key")
    return parser


def main() -> int:
    args = _parser().parse_args()
    output = args.output
    if output.suffix.lower() != ".sfpkg":
        raise SystemExit("output filename must end with .sfpkg")
    policy = customer_ota_product_policy(args.product)
    if policy is None or policy.customer_ota_product is None:
        raise SystemExit(f"unregistered customer OTA product: {args.product!r}")
    output_identity = output.resolve()
    if output_identity in {args.firmware.resolve(), args.private_key.resolve()}:
        raise SystemExit("output package must not overwrite the firmware or private key")
    firmware = args.firmware.read_bytes()
    private = serialization.load_pem_private_key(args.private_key.read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise SystemExit("--private-key must contain an Ed25519 private key")

    firmware_name = Path(args.firmware.name).name
    manifest = {
        "schema_version": 1,
        "product": policy.customer_ota_product,
        "hardware_types": [policy.hardware_type],
        "version": str(args.version).strip(),
        "version_policy": str(args.version_policy),
        "firmware": firmware_name,
        "firmware_size": len(firmware),
        "firmware_sha256": hashlib.sha256(firmware).hexdigest(),
        "key_id": str(args.key_id).strip(),
    }
    try:
        validate_firmware_manifest_fields(manifest)
    except FirmwarePackageError as exc:
        raise SystemExit(exc.detail) from exc
    signature = private.sign(build_signed_manifest_message(manifest))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_handle = tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
        delete=False,
    )
    temporary = Path(temporary_handle.name)
    temporary_handle.close()
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            archive.writestr(firmware_name, firmware)
            archive.writestr("signature.ed25519", signature)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)

    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    trust_entry = {
        "key_id": manifest["key_id"],
        "label": str(args.key_label),
        "public_key_base64": base64.b64encode(public_raw).decode("ascii"),
    }
    print(f"created: {output}")
    print(f"firmware_sha256: {manifest['firmware_sha256']}")
    print("trust_store_entry:")
    print(json.dumps(trust_entry, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
