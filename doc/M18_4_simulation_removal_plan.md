# M18.4 Plan: Remove simulation capability

## Goal

Remove the unused simulation capability from the desktop tool and AFD01
firmware. The product remains a real-device engineering and customer service
tool: live connection, recording, playback, GNSS, 3D presentation, Device/OTA,
and product-service telemetry remain unchanged.

## Upper-PC scope

- Remove the Live toolbar simulation action, `SimulationPanelWidget`,
  `MockModem` lifecycle and all simulation-only signal handlers.
- Remove `core/simulation`, `tools/fake_device.py`,
  `tools/device_simulator.py`, `device_simulator.spec`, their tests, and their
  setuptools/package-manifest entries.
- Stop producing a standalone `DeviceSimulator` artifact from macOS and
  Windows packaging scripts.
- Remove simulation-only text from the current README, operator manuals,
  build instructions and active feature definitions. Feature-specific
  simulation plans and acceptance documents are deleted; historical milestone
  logs may retain factual past-tense references.

## Firmware scope

- Remove the UDP modem transport source and public header.
- Make the AFD01 modem start only the selected physical modem device.
- Remove `modem sim_modem ...` Shell commands and the simulated-online branch
  used by modem characterization.
- Remove the four user-visible simulation parameters from the parameter table
  and parameter reference.
- Preserve the former 21-byte app-tail storage as unnamed compatibility bytes.
  This keeps subsequent persistent fields at their deployed offsets, so an
  app-only OTA neither corrupts `nav_*_source` nor resets the selected KA256 or
  modem model. The retained bytes have no parameter key, Shell command or
  runtime reader.

## Non-goals

- Do not remove real DEBUG v2 framing, the SDB recorder/importer, the normal
  device simulator-independent protocol test infrastructure, GNSS windows or
  customer 3D rendering.
- Do not change bootloader layout or device-control protocol behavior.
- Do not rewrite historical delivery logs merely to erase prior work.

## Verification

1. A source scan has no active UI, runtime, tool, package or firmware code
   reference to the removed feature.
2. Full upper-PC pytest passes after deleting simulation-only test modules.
3. Translation catalog has no stale simulation source and its QM is current.
4. AFD01 app debug and release builds pass; `format.sh check` passes.
5. Normal AFD01 startup reaches the selected physical modem path, while legacy
   raw parameter bytes cannot activate an alternate transport.
