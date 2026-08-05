# M18 Customer workspace acceptance

> Date: 2026-08-05
>
> Status: Software verification complete; AFD01 hardware and Windows acceptance pending
>
> Plan: `doc/M18_customer_workspace_plan.md`

## A. Product and role boundary

- [x] Customer and engineering workspaces share one device session and raw data path.
- [x] Switching workspace does not reconnect, clear stores or alter device state.
- [x] Customer workspace never exposes arbitrary parameters, channel masks or raw diagnostic controls.
- [x] Engineering unlock restores all existing Live/Playback/Log/Device capabilities.
- [x] AFD01 is the only M18 customer implementation and delivery target.

## B. Customer live overview

- [x] Model, serial, connection, mode, phase, lock, navigation and GNSS state are visible.
- [x] Beam angles, attitude, 3D model, longitude, latitude, altitude and SNR are visible.
- [x] SNR current value and history update without changing chart layout.
- [x] Converter, TX array and RX array status/temperature/voltage/version are visible when reported.
- [x] GNSS detail reuses the current sky-plot and signal-level dialog.
- [x] Unsupported values show `-`; stale values are muted; only explicit faults are red.

## C. AFD01 service protocol

- [x] Identity, capabilities, fast state, slow state and component health decode independently.
- [x] Optional fields use validity bits and never overload zero as unavailable.
- [ ] Fast state supports smooth 3D/beam display at the declared rate.
- [x] Slow/component data has timestamps and stale detection in host tests.
- [x] Legacy Debug v2 remains compatible and is adapted only from authoritative roles.

## D. RF control

- [x] Manual fields remain disabled until manual-mode readback succeeds.
- [x] RX/TX frequency and polarization apply as one transaction.
- [x] Device ranges and linked/independent polarization capability drive the controls.
- [x] TX enable requires explicit confirmation and is never changed on reconnect.
- [x] Every result is matched by request ID and includes actual applied values.
- [x] Timeout, rejection and stale readback leave the UI in a safe, truthful state.

## E. Recording and playback

- [x] Customer recording requests `support_full` before reporting that full recording is active.
- [x] SDB v3 stores raw records, host timestamps, identity/profile metadata and gap counters.
- [x] Recording stop restores the previous device stream profile.
- [x] A dropped record or unsupported capture mode is visible in file metadata and UI.
- [x] Customer playback mirrors the live overview with play/pause/seek/speed and no controls.
- [x] Engineering playback of the same file exposes all recorded diagnostics.
- [x] SDB v2 remains readable without rewriting the source file.

## F. Maintenance and OTA

- [x] Customer maintenance shows authoritative AFD01 identity and component information.
- [x] Customer OTA rejects unsigned, corrupt, wrong-product and wrong-hardware packages in host tests.
- [x] Engineering parameter/Debug OTA behavior remains available.
- [ ] Approved production AFD01 Ed25519 public key is provisioned and signed OTA passes hardware acceptance.

## G. Automated and platform verification

- [x] `PYTHONPATH=. pytest satellite_debug_tool/tests -q` is green with no new warnings (`690 passed`, four existing deprecation warnings).
- [x] AFD01 app debug and release builds succeed without new compiler warnings.
- [x] AFD01 service codec has host golden-vector tests shared with the upper PC.
- [x] 1024x600 and 1280x800 customer views have no clipping or overlap in Chinese and English in offscreen checks.
- [x] macOS native 3D visual, strict code-signing, packaged-resource and startup checks pass.
- [ ] Windows 125%/150% DPI and native package checks pass.
- [ ] Hardware verifies stale states, control readback, TX safety, full capture and OTA rejection paths.

## Passing rule

Host tests, firmware builds and the macOS package complete the current software verification. M18 is release-accepted only after the remaining AFD01 hardware items and Windows native/DPI checks are closed.
