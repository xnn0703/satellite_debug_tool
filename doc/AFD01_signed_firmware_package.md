# AFD01 family signed customer firmware package

## Scope

Customer Maintenance accepts only `.sfpkg` archives. Engineering Device OTA
continues to accept raw `.bin` images for controlled development use.

## Trust boundary

- `satellite_debug_tool/resources/firmware_signing_keys.json` is the immutable
  public-key trust store bundled by PyInstaller.
- A release artifact must contain at least one approved product release public key.
- Private keys must never be committed, copied into the application, or stored in
  a customer package.
- The current repository intentionally ships an empty trust store until the
  release owner provisions an approved public key.
- Customer selection is bound to one immutable verified-package token. Selecting
  an Engineering raw image invalidates that customer confirmation, and an active
  transfer cannot replace its bytes, filename, hash, target version, or device scope.

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

Package versions use `MAJOR.MINOR.PATCH`, optionally followed by a SemVer
prerelease (`-beta.2`) or the deployed underscore spelling (`_beta`), and
optional `+build` metadata. Each numeric component is bounded to nine digits
and the complete version is bounded to 128 characters. Device comparison also
accepts the deployed wire forms `V0.0.130 beta <git-hex>.d`, `V1.0.7.c` and
`afd01-1.3.2`. Git/build identity does not affect ordering; explicit `alpha`,
`beta` and `rc` stages do.

The firmware member is one platform-independent basename. The signer and
verifier both reject path separators, dot/dotdot, control characters, Windows
forbidden characters and reserved device names, and trailing spaces or dots.

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

AFD01C uses an independent signed product and hardware identity. Build its package
explicitly; the signing tool derives the one registered hardware identity from
`--product` and rejects unregistered product names:

```bash
python3 tools/build_signed_firmware_package.py \
  path/to/afd01c_application.bin \
  release/afd01c-v0.0.131.sfpkg \
  --private-key /secure/path/afd-family-release-key.pem \
  --key-id afd-family-release-2026 \
  --product AFD01C \
  --version 0.0.131 \
  --version-policy upgrade_only
```

The customer UI requires exact `AFD01/afd01` or `AFD01C/afd01c` identity
matching, a registered Product Service version, and a non-placeholder production
serial number or MCU UID. When Debug META and Product Service both report firmware,
their semantic versions must agree before selection. When both report a production
serial number, those serials must also agree. AFD01 and AFD01C packages are never
interchangeable.

`OTA_END=VERIFIED` proves transfer verification, not the running application.
The UI reports completion only after every immutable identity fact captured before
the transfer is reported again, and every captured Debug/Product firmware source is
reported again, agrees semantically, and matches the signed target version. A
different or missing returned version, conflicting firmware sources, a
changed/unavailable identity, or a same-version package without independent
image-application evidence is shown as unconfirmed rather than as a successful
update. A same-endpoint connection epoch may continue the reboot wait; a different
endpoint cancels the transaction.

## Release acceptance

Before customer release, verify the packaged application accepts a valid package
and rejects unsigned, modified, wrong-product, wrong-hardware and disallowed-
version packages, including both directions of AFD01/AFD01C cross-selection.
Source-level pytest alone does not close this acceptance item.
