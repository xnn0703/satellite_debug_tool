# M18.4 Simulation removal development log

## 2026-08-07: scope audit

- The upper PC had two distinct simulation surfaces: an embedded
  `MockModem`/Live panel and a standalone DEBUG-v2 device generator. Both are
  removed, along with their packaging and test paths.
- AFD01 had a separate UDP modem transport selected by four persisted
  parameters and exposed through `modem sim_modem`. It is a functional device
  feature, not merely a host test fixture, so it must be removed with the
  upper-PC simulation surface.
- The AFD01 parameter block is raw persistent storage. Removing the old fields
  outright would shift later app values when an app-only image reads existing
  FRAM. The implementation therefore keeps anonymous same-size compatibility
  storage while removing every reader and public parameter entry.

## 2026-08-07: implementation and software verification

- Removed the Live toolbar action, embedded simulation panel, dynamic runtime
  import and `MockModem` lifecycle. Deleted the simulation package, standalone
  generator, PyInstaller spec and simulation-only tests.
- Removed simulator references from packaging, active operator/build
  documentation and the generated package source manifest. Packaging now emits
  only the main application and updater artifacts.
- Updated the translation workflow to purge obsolete entries, then regenerated
  the catalog: 556 finished messages and no stale simulation source.
- `PYTHONPATH=. pytest satellite_debug_tool/tests -q` passed with `672 passed`.
  The four existing `datetime.utcnow()` deprecation warnings are unrelated.
- AFD01 deletes the UDP modem transport and `modem sim_modem`; startup always
  selects the physical modem driver. Four public simulation parameters were
  removed from the table while a 21-byte anonymous compatibility field keeps
  every later persisted app field at its deployed offset.
- `./build.sh afd01 app debug` and `./build.sh afd01 app release` passed.
  The touched modem and parameter files pass strict clang-format checking. The
  repository-wide format command still reports unrelated pre-existing
  locate/INS files, which were not modified by this work.
- No device has yet been flashed for a real startup, recording/playback and OTA
  smoke test; that is the remaining acceptance boundary.
