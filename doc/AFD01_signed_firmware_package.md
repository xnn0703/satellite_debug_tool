# AFD01 signed customer firmware package

## Scope

Customer Maintenance accepts only `.sfpkg` archives. Engineering Device OTA
continues to accept raw `.bin` images for controlled development use.

## Trust boundary

- `satellite_debug_tool/resources/firmware_signing_keys.json` is the immutable
  public-key trust store bundled by PyInstaller.
- A release artifact must contain at least one approved AFD01 release public key.
- Private keys must never be committed, copied into the application, or stored in
  a customer package.
- The current repository intentionally ships an empty trust store until the
  release owner provisions an approved public key.

## Package format

The ZIP-compatible `.sfpkg` archive contains exactly:

1. `manifest.json`
2. one firmware image named by `manifest.firmware`
3. `signature.ed25519`

The Ed25519 signature covers the canonical manifest with a fixed domain prefix.
The manifest binds the image by byte size and SHA-256. The host additionally
checks product, connected hardware, current firmware and signed version policy.

Supported policies are:

- `upgrade_only`: target must be newer.
- `allow_same`: target may be the same version or newer.
- `allow_downgrade`: the signed package explicitly authorizes rollback.

## Release command

Generate an Ed25519 key with an approved secret-management process, then build a
package from the repository root:

```bash
python3 tools/build_signed_firmware_package.py \
  path/to/afd01_application.bin \
  release/afd01-v0.0.131.sfpkg \
  --private-key /secure/path/afd01-release-key.pem \
  --key-id afd01-release-2026 \
  --version 0.0.131 \
  --version-policy upgrade_only
```

The command prints a public trust-store entry. Review and add that public entry
to `firmware_signing_keys.json`, rebuild the application, and retain the private
key only in the approved signing environment.

## Release acceptance

Before customer release, verify the packaged application accepts a valid package
and rejects unsigned, modified, wrong-product, wrong-hardware and disallowed-
version packages. Source-level pytest alone does not close this acceptance item.
