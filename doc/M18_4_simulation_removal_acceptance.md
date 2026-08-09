# M18.4 Simulation removal acceptance

> Status: software acceptance complete; hardware verification pending
>
> Plan: `doc/M18_4_simulation_removal_plan.md`

## Upper PC

- [x] Live has no simulation control, panel, state or dynamic import.
- [x] No simulation package, mock loopback tool, standalone device simulator or
  simulator PyInstaller spec remains.
- [x] macOS and Windows packaging scripts create only the main application and
  updater artifacts.
- [x] Current customer/operator/build documents no longer describe a simulator.
- [x] Simulation-only tests and generated package-manifest entries are removed.
- [x] `PYTHONPATH=. pytest satellite_debug_tool/tests -q` passes (`672 passed`).
- [x] Translation update/check passes with no stale source (556 messages).

## AFD01

- [x] UDP modem transport source and header are removed from the firmware tree.
- [x] `modem` Shell usage has no simulation subcommand.
- [x] No `SimMode`/host/port parameter is exposed or read.
- [x] The deployed app parameter tail remains offset-compatible; old raw bytes
  cannot affect normal modem startup.
- [x] AFD01 app debug and release builds pass.

## Cross-repository

- [x] `git diff --check` passes in both repositories.
- [x] Existing real-device recording, playback, product-service and OTA tests
  remain green.
- [ ] Hardware start-up verification remains pending until the new app is
  flashed to a device.
