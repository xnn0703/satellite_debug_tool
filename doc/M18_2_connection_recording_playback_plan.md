# M18.2 Plan: Persistent device wait, armed recording and customer chart playback

## Document information

| Item | Value |
|------|-------|
| Date | 2026-08-06 |
| Status | Software implementation and host verification complete; AFD01 hardware acceptance pending |
| Upper-PC baseline | `da4aed4` plus M18.1/M18.2 worktree (`719 passed`, 4 existing warnings) |
| Scope | AFD01 customer workflow and upper-PC implementation only |
| Acceptance | `doc/M18_2_connection_recording_playback_acceptance.md` |
| Development log | `doc/M18_2_connection_recording_playback_dev_log.md` |

## 1. Audited current behavior

The existing UDP transport reports `connected` immediately after binding the local socket. Customer UI therefore shows an online-looking connection before any valid device frame has arrived.

The dynamic Debug handshake keeps requesting missing definitions, but customer product-service subscription stops after three one-second attempts. If AFD01 powers up after that window, the customer service has no persistent discovery request. The existing automatic detection identifies a device/profile after frames arrive; it is not a durable pre-connect or reconnect state machine.

Customer recording currently requires an already connected device and an already received `capture_profile` capability. Clicking Record before power-up is rejected instead of remembering the operator's intent.

Customer playback currently embeds the complete customer Overview and animates it with play/pause/seek. Engineering Playback instead presents full-file curves with range inspection. The new requirement changes customer playback to the latter interaction model while retaining a strict customer-only field set.

## 2. Connection state model

Separate transport intent from verified device presence:

- `DISCONNECTED`: no socket and no discovery intent.
- `WAITING`: UDP socket is bound and persistent discovery is active, but no recent valid device frame has been decoded.
- `ONLINE`: at least one valid frame has arrived and product telemetry remains recent.
- `RECONNECTING`: a previously online device has stopped reporting; the socket remains open and discovery resumes automatically.

`LiveView.is_connected()` remains the transport/session guard used by engineering and Device views. A new explicit device-presence query and signal drive customer status, controls and recording readiness. This avoids changing the existing worker-sharing contract.

Clicking Connect before power-up enters `WAITING`, locks the configured IP/ports and changes the button to Cancel. A manual Cancel/Disconnect clears the connection intent and closes the worker. Loss of device traffic does not close the worker.

## 3. Persistent AFD01 discovery and reconnect

- Keep the existing `UdpWorker` socket implementation; no firmware or protocol change is required.
- While `WAITING` or `RECONNECTING`, send a product-service `SUBSCRIBE` probe once per second for the first 10 seconds, then once every 3 seconds until a valid AFD01 response arrives.
- Continue the existing Debug profile handshake independently for engineering compatibility. Do not use profile readiness as the customer online condition.
- Treat only successfully decoded protocol records as device activity. A locally bound socket or an arbitrary UDP datagram is not proof that the device is online.
- Mark the device absent after 3 seconds without a valid frame while product telemetry was active. Clear stale customer runtime values, keep cached profile definitions, and return to persistent discovery.
- On the first valid frame after power-up or reboot, enter `ONLINE`, restart product subscription until product service is available, and run the normal identity/capability flow.
- Customer status text distinguishes Waiting for device, Online and Reconnecting. RF and maintenance operations remain disabled unless the device is verified online and their capabilities are current.

## 4. Armed customer recording

Add a customer recording state machine:

- `IDLE`: no recording intent.
- `ARMED`: the operator selected a path before the device was online; no file is created yet.
- `PREPARING`: the device is online and `SET_CAPTURE_PROFILE=support_full` is awaiting its exact response.
- `ACTIVE`: the response was successful and the full SDB v3 recorder is running.
- `RESTORING`: recording stopped and `customer_live` is being restored.

Clicking Record in `WAITING` or `RECONNECTING` opens the save dialog once and arms the operation. The button becomes Cancel recording. When authoritative AFD01 capabilities arrive, the existing exact request-ID negotiation starts automatically.

The file path is selected before negotiation, but the recorder starts only inside the exact successful capture-profile response handler. This removes the current post-ACK file-dialog delay while preserving the rule that customer full recording is never reported active without confirmation. The queued worker signal ordering ensures the recorder is active before subsequent full-support frames are processed.

Cancel while `ARMED` creates no file. Cancel/failure while `PREPARING` clears the intent and sends the idempotent customer-profile restore when needed. A device reboot while armed or active keeps the UDP wait intent; an active recording records the outage/gap and resumes when frames return. A manual Disconnect stops or cancels recording explicitly.

## 5. Customer chart playback

Replace the embedded Overview playback with a customer-specific chart workspace based on the proven engineering `GroupedChartWidget` and `TimeRangeControl`.

The full recording is still imported, but only these stable numeric product fields are projected into a synthetic customer profile and chart data store:

| Group | Fields |
|------|--------|
| Attitude | Roll, Pitch, Yaw |
| Beam | Beam azimuth, Beam elevation/off-axis |
| Signal | SNR |
| Position | Longitude, Latitude, Altitude |

The projection prefers product-service fast/slow records. For SDB v2 or older support recordings it falls back only to authoritative profile roles already accepted by `LegacyV2Projector`; unknown or unsupported fields remain absent.

The customer playback page contains Open/Clear, file identity and quality, duration, time-range selection, grouped/single-chart controls and the existing GNSS details button. It loads the complete file into an isolated unbounded store and shows full-file curves like Engineering Playback. It does not expose raw engineering channels, events, parameter data, RF control or Device controls.

The previous play/pause/speed/whole-Overview animation is removed from customer playback. Engineering Playback remains unchanged and continues to expose every recorded channel and event from the same SDB file.

## 6. One-screen responsive customer overview

The customer Overview must show every routine field without vertical scrolling on the supported display matrix. Adaptation is driven by the actual customer viewport in Qt logical pixels, not only the physical monitor resolution, so resizing a window and Windows DPI scaling produce the same predictable result.

Use one responsive grid:

1. Connection and identity bar.
2. A compact information band split into a state section and a runtime-data section.
3. Main visualization band with only the 3D attitude and 2D beam-direction panels. Their own footers are the authoritative attitude and beam readouts; no duplicate 3x3 metric panel is shown.
4. Full-width SNR readout/chart band.
5. A final compact component strip with converter, TX array and RX array arranged horizontally in one row.

The state section contains connection, control mode, tracking, lock, combined-navigation, GNSS fix, TX state and Modem online state. The adjacent runtime-data section contains longitude, latitude, altitude, RX/TX RF frequency, RX/TX local oscillator and one satellite summary. GEO displays satellite longitude; TLE/LEO displays the satellite catalog number or name when available. Unsupported fields remain `—`.

TX state is the actual gated PA output, not merely Trace's desired command. A TX-array Offline component forces the customer-facing TX state to Unavailable even when connected to an old firmware that temporarily reports a contradictory PA value. The firmware interlock plan is `target/afd01/application/app/transceiver/plan_tx_array_interlock.md` in the terminal repository.

TX state and RX/TX RF frequency reuse `SERVICE_FAST_STATE` and `SERVICE_SLOW_STATE`. A new optional `SERVICE_LINK_DETAIL(0x27)` record carries Modem online, RX/TX LO and structured satellite information. It uses the existing Debug v2 envelope and schema-1 validation. Old upper PCs ignore the unknown command as `RawFrame`; new upper PCs keep the fields unavailable when connected to old firmware or ESA01. Existing fixed-length product records are not extended.

The main visualization remains a balanced two-column layout at all supported resolutions; drawing-surface minimums change by density class so the 3D model and beam plot remain useful.

Define three discrete density classes from the available viewport height:

- Regular: generous drawing surfaces and spacing for large displays, with a 240 px SNR band and 180 px minimum plot canvas.
- Compact: reduced panel margins and drawing surfaces, with a 175 px SNR band and 126 px minimum plot canvas.
- Dense: compact status and position rows, smaller 3D/beam canvases, a 122 px SNR band with an 86 px minimum plot canvas, and the shortest accepted component strip for 1024x600-class displays.

Do not continuously scale fonts with resolution. Each density class uses fixed readable typography while reducing whitespace and graphics first. Text must not be elided when it carries a value or fault state.

The existing scroll container may remain only as an emergency fallback below the documented minimum resolution. At 1024x600, 1280x800 and 1920x1080, in Chinese and English, both horizontal and vertical scrollbar ranges must be zero. Customer chart Playback also fits its toolbar and charts without whole-page scrolling.

The interim M18.1 SNR correction remains: the standalone section no longer consumes arbitrary spare height. M18.2 replaces that temporary fixed band with the final responsive bottom-grid sizing.

## 6.1 Engineering workspace parity

Customer mode shows no Operation or Engineering mode label: it is the default product experience and does not need to announce itself. Engineering unlock changes the complete workspace, not merely the currently visible page. Once unlocked, the center navigation must match the pre-customer engineering console and expose only `Live / Playback / Log / Device`; the customer Overview, RF control, customer Playback and Maintenance pages must not appear in that navigation.

The four engineering views continue sharing the existing `LiveView` worker and stores, and the selected engineering tab remains persistent through `ui.active_tab_id`. There is no visible mode-exit label; after the one-time confirmation, `Ctrl+Shift+E` toggles between the customer and engineering workspaces. Switching either direction must not reconnect the worker, recreate a view, clear data, or change recording/OTA state.

## 7. Implementation order

1. Add the one-screen responsive Overview grid and density classes.
2. Add connection-phase and device-activity state without changing `UdpWorker` or protocol bytes.
3. Add persistent subscribe/reconnect scheduling and customer status/enable gating.
4. Refactor customer recording path selection and implement the armed state machine.
5. Add the fixed customer playback projection and synthetic profile.
6. Rebuild Customer Playback around `GroupedChartWidget`, `TimeRangeControl`, quality and GNSS.
7. Restore the four-tab engineering navigation and workspace toggle boundary.
8. Add the optional AFD01 link-detail product record and its decoder/store coverage.
9. Update Chinese translations, user documentation and development log.
10. Run focused layout/state-machine tests, full pytest, AFD01 builds and customer visual checks.

All nine software steps are implemented. The implementation keeps the existing UDP worker and protocol bytes, adds explicit presence and recording state models, projects playback into a synthetic nine-channel customer profile, and restores the pre-customer engineering navigation. Hardware acceptance remains separate from host completion.

## 8. Verification

- Use a fake UDP device that starts after Connect to prove automatic transition from `WAITING` to `ONLINE` without another click.
- Stop and restart the fake device to prove `ONLINE -> RECONNECTING -> ONLINE` while preserving the socket and operator intent.
- Verify persistent discovery is rate bounded and stops when product service is active.
- Arm recording before device startup, then verify one exact capture-profile request, SDB v3 creation after ACK, complete raw frame capture and customer-profile restore.
- Verify cancel-before-online creates no file and unsupported capture capability produces an explicit failure.
- Load the same SDB in customer and engineering playback. Customer charts contain only the nine approved fields; engineering playback still contains all channels.
- Verify SDB v2/v3, u32 uptime rollover, GNSS popup, quality summary, time windows and empty/unsupported fields.
- Run Chinese and English checks at 1024x600, 1280x800 and 1920x1080 in light and dark themes; require zero Overview horizontal and vertical scroll range.
- Repeat Windows checks at 125% and 150% DPI using Qt logical viewport dimensions.
- Run `PYTHONPATH=. pytest satellite_debug_tool/tests -q` with no new warning class.

## 9. Exclusions

- No change to the existing 0x20~0x26 record layouts; AFD01 only adds optional `SERVICE_LINK_DETAIL(0x27)`.
- No ESA01 customer workflow adaptation.
- No broadcast subnet discovery or automatic IP scanning; the operator still configures the target IP and ports.
- No change to engineering Debug, Device OTA, parameter management or engineering recording behavior.
- No automatic commit, push, tag or release.
