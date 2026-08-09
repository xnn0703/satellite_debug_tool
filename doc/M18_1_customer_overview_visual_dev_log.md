# M18.1 Customer overview visualization development log

## 2026-08-05: planning baseline

- Upper PC baseline: `da4aed4`; worktree was clean before these planning documents.
- M18 verification baseline: `690 passed, 4 existing datetime deprecation warnings`.
- AFD01 baseline: `593444b0`; prior app debug/release builds passed.
- The AFD01 worktree currently contains separate modem/tracking/startup work. It is outside M18.1 and must be preserved.
- ESA01 customer adaptation remains deferred.

## Audited behavior

- Customer SNR history already stores `SERVICE_FAST_STATE.timestamp`, an AFD01 uptime millisecond value, and normalizes the newest sample to zero for the `-60..0 s` axis.
- AFD01 GNSS input calibrates both `sys_time` and the LSE-backed RTC. The UTC-to-uptime reference is not currently exposed through the product service.
- AFD01 `ant_az` is registered over 0 to 360 degrees. `ant_el` is registered over 0 to 90 degrees and is the array zenith/off-axis coordinate required by the proposed polar view.
- Schema 1 reports independent RX/TX polarization enums but no arbitrary linear-polarization angle.

## 2026-08-05: requirement refinement

- SNR history changes from 60 seconds to 5 minutes.
- The X axis uses the existing device-uptime timestamp converted to seconds; it does not use UTC/calendar time.
- No AFD01 firmware or product-protocol change is required. The upper PC owns u32 rollover handling and session reset.

## Progress

- [x] Source, protocol and layout audit.
- [x] Plan and acceptance criteria drafted.
- [x] User confirmation.
- [x] Beam polar widget and responsive overview layout.
- [x] SNR emphasis and polarization formatting.
- [x] Five-minute SNR history and device-uptime second axis.
- [x] Automated tests and visual QA.
- [ ] Hardware acceptance.

## 2026-08-05: implementation

- Added a standalone `BeamPolarWidget` using `QPainter`. Its pure geometry maps 0 degrees to the top, increases clockwise and uses 30/60/90-degree off-axis rings.
- Reworked the overview into a three-column wide layout and a compact two-row layout. The metrics now follow attitude, beam/polarization and position ordering; SNR is no longer duplicated there.
- Added a customer-only emphasized readout mode to `AttitudeWidget`; engineering views keep their existing type scale.
- Added explicit RX/TX polarization formatting. Current circular enums remain authoritative, while a future numeric linear angle has a formatter without adding or inferring a schema-1 field.
- Added a dedicated large SNR readout beside a 300-second chart.
- Added `U32UptimeUnwrapper` and changed product and legacy customer histories to actual device-uptime seconds. Session clear resets the unwrap anchor, and customer playback uses the same rollover handling.
- Updated Chinese translations and rebuilt the QM catalog. Translation validation reports 557 complete messages and no unfinished entry.

## 2026-08-05: software verification

- Focused product, overview, playback, polarization, polar-geometry and timestamp tests passed.
- Full suite: `706 passed, 4 warnings in 43.87s`. The warnings are the pre-existing `datetime.utcnow()` deprecations; M18.1 adds no warning class.
- UI-level SNR verification confirmed a real uptime window of `1230.0..1530.0 s`, not `-300..0 s`.
- Exact offscreen matrix covered Chinese and English, light and dark themes, and 1024x600, 1280x800 and 1920x1080. All 12 combinations used the intended compact/wide mode and reported zero horizontal-scroll range.
- High-contrast beam and overview rendering were checked separately. One native macOS capture confirmed the bundled AFD01 3D model and new beam view render together. Later synthetic captures that repeatedly recreated OpenGL contexts emitted pyqtgraph errors, so those repetitions are not counted as 3D renderer acceptance; M18.1 did not change the renderer or model-loading path.
- `scripts/update_translations.py check` and `git diff --check` passed.
- The AFD01 firmware repository was not modified; its pre-existing modem/tracking/startup work remains intact.

## Remaining hardware acceptance

- Compare the polar vector against live AFD01 `ant_az/ant_el` values to confirm the physical clockwise convention.
- Observe at least five continuous minutes and a reconnect/reboot to confirm uptime continuity and session reset on hardware.

## 2026-08-05: proportion follow-up

- Hardware feedback showed the SNR section expanding too far vertically when the page had spare height.
- The section now uses a fixed vertical policy with a 188 to 220 px range, while the 3D, beam and key-value band receives the remaining space.
- Added a focused height regression test; the overview test file reports `8 passed`.

## 2026-08-06: M18.2 responsive supersession

- The interim 188 to 220 px SNR-only band was superseded by the final M18.2 layout: a dedicated full-width SNR band at 240/175/122 px for Regular/Compact/Dense, followed by a separate compact component strip.

## 2026-08-07: 3D default beam headroom

- Field feedback showed the blue pointing trail touching the top toolbar in the default view. The old camera looked at the device origin, while normal GEO pointing occupies the upper hemisphere.
- The default and Reset camera now use the same look-at center at `Z=0.9`, placing the device lower and the skyward beam/trail inside the drawing area. Beam geometry, length and manual camera interaction are unchanged.
- Added a regression that mutates the camera and verifies Reset restores the elevated target, distance, elevation and azimuth.
- At supported widths, 3D attitude, 2D beam and the 3x3 values remain in one row. The full 12-case language/theme/resolution matrix reports no whole-page scrolling.
