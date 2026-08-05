# M18 Plan: Customer workspace and product service protocol

## Document information

| Item | Value |
|------|-------|
| Date | 2026-08-05 |
| Status | Software implementation complete; AFD01 hardware acceptance pending |
| Upper-PC baseline | `60eef54`, `655 passed, 4 warnings` |
| AFD01 baseline | `7b5c5f8d`, app debug build passed |
| ESA01 baseline | `43be02e`, customer adaptation deferred |
| Acceptance | `doc/M18_customer_workspace_acceptance.md` |
| Development log | `doc/M18_customer_workspace_dev_log.md` |

## 1. Product boundary

The application changes from an internal-only debug tool to one package with two presentation policies:

- Customer workspace: stable product semantics, routine monitoring, guarded controls, recording, playback and OTA.
- Engineering workspace: the existing dynamic Debug Profile, all channels, events, parameters, logs and diagnostic tools.
- Both workspaces share the same communication session, decoder and raw recording path. Switching workspace must not reconnect the device or clear data.

AFD01 is the only implementation and delivery target for M18. ESA01 customer projection, fields and controls are deferred until there is a concrete delivery need. Existing ESA01 engineering behavior must not be deliberately removed, but it is not part of M18 acceptance.

## 2. Customer information architecture

The main navigation is:

1. Devices
2. Live overview
3. RF control
4. Playback
5. Maintenance
6. Engineering diagnostics, visible only after service unlock

The live overview contains:

- Product identity: model, serial number, IP and connection state.
- Control mode, tracking phase, lock result, combined-navigation state and GNSS fix.
- Beam angles, roll/pitch/yaw, 3D device model, longitude/latitude/altitude.
- Current SNR and a focused SNR history chart.
- Converter, TX array and RX array status, temperature, voltage and version.
- GNSS detail opens the existing sky-plot and signal-level dialog.

Tracking semantics are separated into `control_mode`, `tracking_phase` and `locked`. Combined navigation is normalized to `unavailable / initializing / aligning / ready / degraded / fault`; customer UI never exposes internal/external INS source selection.

## 3. Customer product model

Introduce a stable host-side product model independent of raw Debug names:

- `DeviceIdentity`: model, serial, main firmware, boot firmware and protocol versions.
- `OperationalSnapshot`: mode, phase, lock, navigation, GNSS, attitude, beam, position, SNR and TX state.
- `ComponentHealth`: converter, TX array and RX array values with validity and age.
- `RfCapabilities`: frequency limits, polarization mode, TX support and supported commands.
- `ControlResult`: request ID, result code, requested values, applied values and rejection detail.

Every field carries one of `valid / stale / unsupported`. The UI renders unsupported as `-`, stale as muted, and red only for an explicit device fault. Zero is always a real value, never an unavailable sentinel.

AFD01 receives an authoritative adapter. No ESA01-specific customer adapter or fallback mapping is added in M18.

## 4. Lightweight product service protocol

Keep the existing `AA 55 0D ... EE` frame envelope and reserve a product-service command range. Customer UI does not depend on dynamic Debug definitions.

- `SERVICE_IDENTITY`: on connection and on change.
- `SERVICE_FAST_STATE`: attitude, beam, SNR, mode, phase, lock and navigation at 10-20 Hz.
- `SERVICE_SLOW_STATE`: position, GNSS summary, TX readback and runtime at 1-2 Hz.
- `SERVICE_COMPONENT_HEALTH`: component values at 1 Hz or on change.
- `SERVICE_CAPABILITIES`: optional fields, ranges and command support.
- `SERVICE_CONTROL_REQUEST/RESPONSE`: typed transactions with request IDs and applied readback.
- `SERVICE_CONTROL_REQUEST/RESPONSE.SET_CAPTURE_PROFILE`: switch between customer-live and full-support capture profiles.

Dynamic Debug v2/v3 remains available for engineering diagnostics and full-support recording. Legacy Debug v2 is adapted to partial customer display, but is not used to infer unsupported component information.

## 5. Guarded RF control

- Auto/manual is a stable segmented control.
- Frequency and polarization controls are enabled only after manual mode is confirmed by readback.
- RX/TX frequency and polarization are applied atomically.
- The service model supports independent RX/TX polarization; a device capability may collapse them into one linked control.
- TX enable is a separate guarded operation with explicit confirmation.
- Reconnect never changes TX state. The UI reads and displays actual state before enabling control.
- Every command has an exact response context and final applied values. Generic uncorrelated `OK` is not accepted.

## 6. Recording and role-based playback

One recording contains the full support evidence; customer and engineering modes only change presentation.

- Starting customer recording first requests the AFD01 `support_full` capture profile and waits for confirmation.
- The recorder stores every received raw frame, product/profile snapshots, control operations, gap markers and capture-quality counters.
- Stopping recording restores the previous stream profile.
- Customer playback mirrors the live overview and adds play, pause, seek and speed controls. It is read-only.
- Engineering playback keeps the current full-channel, state, event and GNSS analysis views.

Add SDB v3 with record-level host receive timestamps, device/session identity, metadata snapshots and completeness information. SDB v2 remains import-compatible.

UI filtering is not a confidentiality boundary: a full SDB contains engineering data. If raw support data later becomes confidential, encrypted support-bundle export is a separate security milestone.

## 7. OTA and maintenance

- Customer OTA accepts only a signed package whose product and hardware identity match the connected AFD01.
- Customer mode follows package version policy and never exposes arbitrary parameter editing.
- Engineering mode retains existing parameter and debug OTA functions, including development rollback policy.

## 8. Implementation order

1. Product model, customer workspace shell and legacy-v2 projection tests.
2. Live overview, component table, SNR chart and GNSS dialog reuse.
3. AFD01 product-service frames, identity/component telemetry and host decoder.
4. Guarded RF control with exact ACK and applied-value readback.
5. SDB v3, capture negotiation and customer timeline playback.
6. Customer maintenance and signed OTA package verification.
7. Full upper-PC tests, AFD01 debug/release builds, simulator, visual QA and hardware acceptance.

## 9. Explicit exclusions

- No ESA01 customer projection, firmware fields, controls, recording negotiation or acceptance work.
- No removal of existing engineering Debug v2 behavior.
- No claim that host tests replace AFD01 hardware acceptance.
- No automatic commit, tag or release during implementation.

## 10. Delivery status

The AFD01 upper-PC implementation, product-service firmware, automated tests, debug/release firmware builds and macOS package verification are complete. Customer product subscription is independent of the dynamic Debug handshake, so an unavailable engineering profile does not prevent the customer overview from receiving product records.

M18 is not yet a hardware-accepted release. The remaining work is AFD01 bench verification of telemetry timing, RF/TX control readback, capture continuity and signed OTA; Windows 125%/150% DPI and native package checks; and provisioning of the approved production Ed25519 public key. The current UID-derived identifier is a stable hardware identity, not a factory-assigned customer serial number. Converter firmware version remains `unsupported` until an authoritative device source is defined.
