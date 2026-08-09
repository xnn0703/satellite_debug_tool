# M18.2 Connection, recording and customer playback development log

## 2026-08-05: audit and planning

- Upper-PC HEAD is `da4aed4`; the worktree contains the uncommitted M18.1 implementation and must be preserved.
- M18.1 full-suite baseline is `706 passed, 4 existing datetime deprecation warnings`.
- `UdpWorker.connect()` binds a local UDP socket, starts its thread and immediately emits `connected`; this is transport readiness, not device presence.
- `CustomerOverviewView` currently uses `LiveView.is_connected()` for both button state and the customer Online label, so the two meanings are conflated.
- `Handshake` retries missing dynamic definitions, but `LiveView._retry_product_subscription()` stops product discovery after three attempts.
- Customer recording currently rejects all clicks before transport and capabilities are ready; no armed intent exists.
- Customer playback currently embeds `CustomerOverviewView` and animates it. The accepted requirement is now a restricted chart view similar to Engineering Playback.
- Engineering mode is unlocked for the current process with `Ctrl+Shift+E` and an explicit confirmation dialog. The unlock is intentionally not persisted.
- The customer Overview is currently hosted by a resizable `QScrollArea`, but its stacked main, SNR and component rows have a minimum height larger than the usable viewport on smaller displays. The new requirement is one-screen presentation at all supported resolutions.

## 2026-08-05: M18.1 proportion correction

- Changed the live customer SNR section from a stretch participant to a fixed vertical policy with a 188 to 220 px range.
- Added a focused regression assertion; `test_customer_overview_view.py` reports `8 passed`.

## 2026-08-06: one-screen requirement refinement

- Customer routine data must not require vertical scrolling.
- The final layout will place SNR and component health side by side, keep the three main visual/value columns in one row, and select Regular/Compact/Dense spacing from the actual logical viewport height.
- The acceptance matrix now requires zero horizontal and vertical scroll range at 1024x600, 1280x800 and 1920x1080, including Chinese/English and Windows DPI scaling.

## Progress

- [x] Current connection, discovery, recording and playback paths audited.
- [x] M18.2 plan and acceptance criteria drafted.
- [x] User confirmation.
- [x] One-screen responsive Overview implementation.
- [x] Persistent wait/reconnect implementation.
- [x] Armed recording implementation.
- [x] Customer chart playback implementation.
- [x] Translation and documentation update.
- [x] Full automated and visual verification.
- [x] Engineering workspace parity correction and regression verification.
- [x] Customer navigation simplification and duplicate-metric layout correction.
- [ ] AFD01 hardware acceptance.

## 2026-08-06: engineering workspace regression audit

- The four original engineering views still exist in the shared `QTabWidget`, but its native tab bar is hidden.
- The customer integration replaced the original four top navigation pills with only Operation and Engineering pills. After unlock there is therefore no control that can select Playback, Log or Device.
- The correction will restore the original four engineering pills only while the engineering workspace is active. Customer pages remain in the separate customer stack, and Return to Operation is a standalone toolbar command.

## 2026-08-06: implementation

- Added explicit `DISCONNECTED / WAITING / ONLINE / RECONNECTING` device-presence phases without changing the UDP worker contract. A valid decoded record is required for Online.
- Product subscription now probes at 1 second for the first ten attempts and at 3 seconds thereafter until service is available. A three-second telemetry loss clears stale customer runtime data and resumes discovery without closing the socket.
- RF and maintenance pages now gate operations on verified device presence rather than local socket state.
- Added `IDLE / ARMED / PREPARING / ACTIVE / RESTORING` customer recording states. The path is selected once, the file is created only after the exact full-capture response, and manual disconnect clears every pending state.
- Active SDB v3 recording writes device-offline/device-online metadata and renegotiates `support_full` after reconnect.
- Replaced animated Overview playback with a static customer curve workspace. A synthetic profile exposes only nine approved fields; product service wins over legacy Debug fallback, which uses profile roles.
- Customer playback uses the unwrapped u32 device-uptime axis, supports SDB v2/v3, full-file ranges, quality summary and the existing GNSS details popup.
- Final Overview density heights are 205 px (Regular), 160 px (Compact) and 108 px (Dense) for the side-by-side SNR/component band.

## 2026-08-06: software verification

- Focused customer, recording, connection, playback, chart and i18n regression: `62 passed`; additional recording-state tests cover unsupported capability and reconnect metadata/resume.
- Full suite: `718 passed, 4 warnings in 54.26s`. All warnings are the pre-existing `datetime.utcnow()` deprecations.
- Translation catalog: 571 finished entries, no unfinished, empty, stale or placeholder mismatch.
- Offscreen matrix covered 1024x600, 1280x800 and 1920x1080 in Chinese/English and light/dark themes. All 12 Overview cases reported horizontal and vertical scroll ranges `0,0`.
- Customer Playback at 1024x600 kept its complete toolbar visible; grouped chart scrolling remains internal to the chart, matching Engineering Playback.
- `git diff --check` passed. Native Windows 125%/150% DPI and AFD01 late-power/reboot behavior remain hardware/platform acceptance items.

## 2026-08-06: engineering workspace parity correction

- The center toolbar now switches its navigation set with the workspace. Customer mode keeps Operation/Engineering entry controls; engineering mode exposes exactly Live, Playback, Log and Device in the original order.
- Customer pages are no longer represented inside engineering navigation. Return to Operation is an independent right-side command.
- After one-time confirmation, `Ctrl+Shift+E` toggles in both directions while preserving the selected engineering tab and all existing view instances.
- Added a real MainWindow regression covering customer visibility, all four engineering tab selections, view identity preservation, shortcut toggling, explicit exit and runtime Chinese/English labels.
- Full suite: `719 passed, 4 warnings in 60.65s`; translation catalog: 573 finished entries. Offscreen visual checks passed at 1024x600 English and 1280x800 Chinese.

## 2026-08-06: customer navigation and layout refinement

- Customer mode must not display an Operation/Engineering selector. Engineering mode is entered and exited with the already confirmed `Ctrl+Shift+E` shortcut and shows only its four page tabs.
- The 3x3 value panel duplicates attitude and beam values already shown below their visualizations. It will be removed; longitude, latitude and altitude move to a compact position row.
- SNR becomes a full-width row. Converter, TX array and RX array health move to a single horizontal strip at the absolute bottom.

## 2026-08-06: compact state/data band and AFD01 field audit

- The existing product service already carries TX enable in `SERVICE_FAST_STATE` and RX/TX RF frequency in `SERVICE_SLOW_STATE`; these values do not need another wire representation.
- Current product records do not carry Modem online, RX/TX LO or satellite identity. Extending the fixed-length slow record would break its exact decoder, so the implementation uses optional `SERVICE_LINK_DETAIL(0x27)` instead.
- AFD01 sources are authoritative: Modem online comes from `modem_device_get_status`, LO and GEO longitude come from the latest `modem_sat` snapshot, and TLE catalog number comes from columns 3-7 of TLE line 1. No value is inferred in the UI.
- Old upper PCs treat 0x27 as `RawFrame`; new upper PCs show `—` when 0x27 is absent. ESA01 remains outside this firmware change.

## 2026-08-06: compact state/data band implementation

- Removed the duplicated 3x3 readout panel. The main row now contains only the shared 3D attitude view and the 2D beam-direction view; their local footers remain the attitude and beam readouts.
- Rebuilt the top information band as two compact 4x2 grids. Status contains connection, control, tracking, lock, combined navigation, GNSS, TX and Modem. Runtime data contains longitude, latitude, altitude, RX/TX RF, RX/TX LO and satellite summary.
- Added optional `SERVICE_LINK_DETAIL(0x27)` decoding and `ProductServiceStore` projection. AFD01 publishes Modem presence, current local oscillators, GEO longitude or TLE catalog number at the slow-report rate. Missing records remain unavailable instead of falling back to guessed values.
- Converted component health to one bottom horizontal strip and kept SNR as an independent full-width band. The final dense 1024x600 layout uses a 122 px SNR band and a 44 px component strip.
- The SNR plot now retains a rolling 300-second device-uptime window, disables SI-prefix conversion on the seconds axis and applies a minimum 4 dB y-range for a single or flat sample.
- Customer mode has no visible mode selector. Engineering mode exposes only Live, Playback, Log and Device; the confirmed `Ctrl+Shift+E` shortcut toggles both directions.
- Focused protocol, customer-view and i18n regression passed. Offscreen 1024x600 Chinese inspection shows every routine field without page scrolling; full-suite results are recorded below after final verification.
- AFD01 app Debug and Release builds passed with the optional link-detail record. ESA01 firmware was not changed.
- Final upper-PC suite: `722 passed, 4 warnings in 58.29s`; all four warnings are the existing `datetime.utcnow()` deprecations.
- Translation validation compiled all 576 entries with no unfinished, empty, stale or placeholder mismatch.
- Final offscreen matrix covered Chinese/English, dark/light and 1024x600, 1280x800, 1920x1080. All 12 customer Overview cases reported horizontal and vertical scroll ranges `0,0`; density selection was Dense, Compact and Regular respectively.
- Native Windows 125%/150% DPI remains a platform acceptance item; live AFD01 link-detail field validation is recorded below.

## 2026-08-06: live link-detail verification and SNR height refinement

- Captured live AFD01 UDP traffic on port 4004 and decoded nine valid `SERVICE_LINK_DETAIL(0x27)` records with zero framing, CRC or decoder errors. The current payload reports Modem online, RX LO 18250 MHz, TX LO 28050 MHz and GEO longitude 125.00 degrees.
- Inspected the running customer Overview after reconnect. It displays `Modem: Online`, `RX LO: 18250.00 MHz`, `TX LO: 28050.00 MHz` and `Satellite: GEO 125.00°E`, confirming the firmware-to-decoder-to-store-to-UI path.
- Increased the SNR band by density rather than scaling typography continuously: Regular is 240 px with a 180 px plot, Compact is 175 px with a 126 px plot, and Dense is 122 px with an 86 px plot. The component strip heights remain unchanged.
- A large-to-minimum window resize exposed an ordering issue: the scroll area measured the page before `CustomerOverviewView` changed from Regular to Dense. `_ViewportFitScrollArea` now performs one queued follow-up fit after child resize events settle; a 1920x1080 to 1024x600 regression guards against retaining the old minimum height.
- Focused customer-layout regression passed with `13 passed`. The final full suite remains `722 passed, 4 warnings`; the warnings are the existing `datetime.utcnow()` deprecations.
- The final sequential resize matrix covered Chinese/English, light/dark and 1920x1080, 1280x800, 1024x600 in one window. All 12 cases reported horizontal and vertical scroll ranges `0,0`; measured SNR plot heights were 202-205 px, 143-146 px and 92-95 px respectively.

## 2026-08-06: TX-array health semantic correction

- Field screenshot exposed that product `TX array Online` and `TX On` were not end-to-end transmission evidence. The former originates from KA256 communication state, while the latter originates from the transceiver PA GPIO.
- The immediate correction keeps the current product-service wire format, starts KA256 health as unverified/Offline, gates PA on fresh TX-array health, and makes the customer UI render TX Unavailable if a reported TX array is Offline. It deliberately does not invent an "uninstalled" state without an explicit hardware-presence source.

## 2026-08-06: TX-array health host verification

- Added a Customer Overview regression where the legacy product stream claims `tx_enabled=1` while the component stream says TX array Offline. The view now renders `TX: Unavailable` with warning styling instead of green On.
- Focused customer/product tests passed: `23 passed`.
- Full upper-PC suite passed: `723 passed, 4 warnings in 84.15s`. All warnings remain the existing `datetime.utcnow()` deprecations.
- The paired AFD01 Debug and Release app builds passed. Hardware acceptance remains separate: it must show an Offline TX array, actual PA disabled and IOT503 `Power: 0` on the no-array bench unit.
