# M18 Customer workspace development log

## 2026-08-05: implementation start

### Clean baseline

- Upper PC: commit `60eef54`; clean worktree; `655 passed, 4 warnings in 37.20s`.
- The four warnings are the existing `datetime.utcnow()` deprecation warnings.
- AFD01: commit `7b5c5f8d`; clean worktree; `./build.sh afd01 app debug` passed.
- AFD01 debug image baseline: FLASH 661456 B (84.11%), RAM 412624 B (78.70%).
- ESA01: commit `43be02e`; clean worktree; customer adaptation deferred outside M18.

### Audited current behavior

- Live recording writes received bytes before protocol decoding, so one raw source can feed both presentation policies.
- SDB v2 embeds one Profile snapshot but has no per-record host timestamp, session identity or capture-quality metadata.
- Current Playback loads all records into stores immediately; it is an analysis view rather than a time-driven replay engine.
- AFD01 Debug Profile already exposes attitude, beam, SNR, position and tracking data.
- AFD01 array drivers cache temperature, voltage and MCU version, but the customer protocol does not expose the complete snapshot.
- AFD01 Debug meta uses a development serial placeholder rather than a factory identity.
- ESA01 was reviewed but removed from the M18 implementation and acceptance scope.

### Progress

- [x] Existing uncommitted work committed separately in all three repositories.
- [x] M18 plan, acceptance and development log created.
- [x] Product model and workspace shell.
- [x] Customer live overview.
- [x] AFD01 product service protocol and telemetry.
- [x] Guarded RF control.
- [x] SDB v3 and role-based playback.
- [x] Customer maintenance and signed OTA verifier/tooling.
- [x] Software review, automated tests, firmware builds and macOS package verification.
- [ ] AFD01 hardware and Windows native acceptance.

## 2026-08-05: AFD01-only implementation

### Customer workspace

- The application now starts in a customer workspace with Overview, RF control, Playback and Maintenance pages.
- Engineering diagnostics remain in the same process and share the existing Live worker and stores. They are hidden until the operator confirms the session-only `Ctrl+Shift+E` unlock.
- Customer UDP connection subscribes to the AFD01 product service without enabling dynamic engineering Debug data.
- Overview renders stable product identity, tracking/navigation/GNSS state, attitude, beam, SNR history, 3D model, position and component health. Unsupported values remain unavailable rather than being inferred.

### AFD01 product service

- Added service command range `0x20..0x26` within the existing Debug v2 envelope. Identity, capabilities, fast state, slow state and component health are independent records with explicit validity bits.
- Service control uses `request_id + operation`, typed result codes and applied readback. RF and TX commands require confirmed manual mode.
- AFD01 exposes only left/right circular polarization. Product values `2/3` are explicitly converted to internal array values `0/1`; capabilities advertise mask `0x0C`.
- Position validity now requires a usable `GPS_FIX` and a position update no older than 3000 ms, preventing historical coordinates from appearing as a current solution.
- Product service polling is independent of Debug ON/OFF. `support_full` only controls the dynamic engineering stream used by support recording.

### Recording and playback

- SDB v3 stores host-timestamped RX chunks, outgoing controls, metadata, gap markers and a final quality summary while preserving SDB v2 import compatibility.
- Customer recording starts only after exact `SET_CAPTURE_PROFILE=support_full` confirmation. Stop restores `customer_live`, then restores the Debug state that was confirmed before recording.
- Recorder finalization is owned by the writer thread. A full stop queue no longer blocks the UI; any forced drop is counted and marks the capture incomplete.
- Customer playback uses an independent product store and timed replay controls. The engineering playback path still sees all recorded Debug records.

### Customer maintenance

- Customer mode accepts only strict `.sfpkg` archives authenticated by Ed25519. Product, hardware, version policy, image size and SHA-256 are bound by the signed manifest.
- Added signing CLI and package specification. A temporary-key CLI smoke test created the exact three-entry package and the temporary private key was removed afterward.
- The bundled trust store is intentionally empty until the release owner provisions the approved production AFD01 public key. Consequently customer OTA remains disabled in a production-like build until that public key is supplied.

### Final software verification

- Full upper-PC suite: `690 passed, 4 warnings in 43.91s`. The four warnings are the existing `datetime.utcnow()` deprecations; M18 added no new warning class.
- Translation catalog: 548 Chinese messages, all finished; TS validation, QM compilation and i18n tests passed.
- AFD01 `app debug` and `app release` builds passed. `arm-none-eabi-size` reports debug `text=663676, data=2700, bss=459392` and release `text=611032, data=2700, bss=458880`.
- Sixteen Chinese/English offscreen screenshots at 1024x600 and 1280x800 were inspected. A native macOS packaged-app check confirmed the AFD01 STL renders nonblank and correctly framed.
- The final macOS app passed strict deep code-sign verification, retained only the required QtWebEngine helper app, and contains AFD01/ESA01 model assets, TS/QM translations, Leaflet/map resources, the trust store and the one-file arm64 updater. The packaged app remained running for more than eight seconds in the final startup smoke.
- Final archive: `release/SatelliteDebugTool-macOS-arm64.zip`, SHA-256 `14bd1495a25b447f26a17ce91c9c276e5c946fdede92d10330967ec651fc0356`.
- Product subscription now starts immediately after connection and does not wait for the dynamic Debug handshake. Product identity can select the AFD01 3D model before an engineering profile is ready; both paths have regression tests.

### Open acceptance boundaries

- AFD01 bench validation is still required for service update timing/stale behavior, RF and polarization readback, TX safety, array/converter telemetry, full-support capture continuity and signed OTA rejection/install/reboot behavior.
- The bundled production trust store has no approved key, so customer OTA remains disabled until the release owner supplies the AFD01 Ed25519 public key. No production private key is generated or stored in this repository.
- The current serial is derived from the MCU UID and is not a factory-assigned customer SN. Converter firmware version is reported as unsupported because no authoritative source was found in the audited AFD01 driver.
- Windows 125%/150% DPI and native package checks remain open. ESA01 customer adaptation remains outside M18.
