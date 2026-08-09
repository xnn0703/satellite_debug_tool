# M18.1 Plan: AFD01 customer overview visualization

## Document information

| Item | Value |
|------|-------|
| Date | 2026-08-05 |
| Status | Software implementation complete; AFD01 hardware acceptance pending |
| Upper-PC baseline | `da4aed4`, M18 full suite `690 passed, 4 existing warnings` |
| AFD01 baseline | `593444b0`, app debug/release builds passed |
| Scope | AFD01 customer overview presentation; upper-PC implementation only |
| Acceptance | `doc/M18_1_customer_overview_visual_acceptance.md` |
| Development log | `doc/M18_1_customer_overview_visual_dev_log.md` |

## 1. Goal

Restructure the customer overview around the information operators read most often: physical attitude, beam direction, tracking angles, position and SNR. Keep the engineering views and protocol diagnostics unchanged.

This milestone also closes the preceding timestamp request by presenting the existing device-uptime timestamp in seconds. Calendar/UTC time is not required.

## 2. Wide layout

The main visualization band becomes three columns:

1. 3D attitude: the existing AFD01 model and beam vector, with a larger Roll/Pitch/Yaw readout below the scene.
2. 2D beam indicator: a square polar plot with the device at the center. Azimuth is 0 degrees at the top and increases clockwise; radius is beam elevation/off-axis angle. The current beam is a radial vector with an endpoint marker.
3. Key values: a stable 3x3 grid ordered as:
   - Row 1: Roll, Pitch, Yaw.
   - Row 2: Beam elevation/off-axis, Beam azimuth, RX/TX polarization.
   - Row 3: Longitude, Latitude, Altitude.

The 2D indicator uses the current AFD01 product values `beam_az_deg` and `beam_el_deg`. The latter comes from `ant_el`, whose current device range is 0 to 90 degrees and represents the angular distance from array boresight/zenith for this view.

At narrower widths the 3D and 2D visualizations remain side by side, while the 3x3 values move below them. This prevents compressed text or hidden controls at 1024-wide windows. No horizontal scrollbar is introduced.

## 3. Beam and polarization presentation

- Draw 30, 60 and 90 degree concentric rings, cardinal azimuth labels and a theme-aware beam vector.
- A valid sample draws the vector and endpoint. A stale sample remains visible in a muted style. Unsupported or non-finite values show an unavailable state and never draw a misleading zero-angle vector.
- Normalize azimuth modulo 360 degrees. Clamp only the drawing radius at 90 degrees; retain the original value in the numeric readout.
- The footer below the polar plot shows beam elevation/off-axis, azimuth and polarization with larger values.
- Current AFD01 circular polarization is rendered explicitly from product enums. When RX and TX differ, both are shown, for example `RX left circular / TX right circular`.
- Arbitrary linear polarization angle is not present in schema 1. The formatter and view model will accept a future numeric angle, but M18.1 will not infer one from Vertical/Horizontal or Debug names.

## 4. SNR presentation

The signal band becomes two columns inside one section:

- Left: a stable-width SNR readout with a large monospace value and availability styling.
- Right: a 5-minute history chart.

SNR is removed from the 3x3 key-value grid. The numeric readout and chart always use the same `OperationalSnapshot.snr_db` source and stale state.

## 5. Device uptime axis

Keep the existing telemetry `timestamp_ms` as the authoritative unsigned device-uptime millisecond counter. No UTC reference or new protocol command is added.

- Convert the device timestamp to seconds for the X axis and show the actual uptime interval, for example `1230..1530 s`, rather than normalizing the newest sample to zero.
- Retain only the newest 300 seconds for the customer SNR view. Capacity must cover the declared maximum 20 Hz product-service rate.
- Unwrap the u32 millisecond counter across its 49.7-day rollover so a continuously connected device retains a monotonically increasing second axis.
- Clear the unwrap anchor and SNR history on disconnect/session reset so a reboot cannot be mistaken for rollover.
- Customer playback uses the timestamps recorded in the product frames and renders the same uptime-second axis.

## 6. Implementation boundary

- Add a standalone `BeamPolarWidget` implemented with `QPainter`; no new dependency or OpenGL context is required.
- Add a customer-only emphasis API to `AttitudeWidget` rather than globally enlarging engineering views.
- Extend the product store with a pure wrap-safe uptime un-wrapper and a 300-second SNR history window.
- Keep engineering Live, existing 3D geometry, RF controls, recording data and SDB format behavior unchanged.
- Do not modify the AFD01 repository. Its current unrelated modem/tracking worktree changes remain untouched.

## 7. Verification

- Unit-test polar geometry at 0/90/180/270 degrees, ring scaling, stale/unsupported behavior and non-finite input.
- Test exact metric order, SNR separation, circular-polarization text and future linear-angle formatting.
- Test uptime-second conversion, 300-second pruning, u32 wrap and reboot/session reset.
- Verify customer live and customer playback use identical beam/SNR presentation.
- Run Chinese and English visual checks at 1024x600, 1280x800 and 1920x1080 in light and dark themes.
- Run `PYTHONPATH=. pytest satellite_debug_tool/tests -q`.
- Hardware-check beam direction conventions against shell-reported `ant_az/ant_el`, uptime-axis continuity, stale rendering and at least 5 minutes of live SNR updates.

## 8. Exclusions

- No ESA01 customer view or firmware adaptation.
- No arbitrary linear-polarization control or protocol field until the device provides an authoritative angle.
- No change to the existing monotonic timestamp semantics.
- No UTC/calendar-time synchronization or product-protocol extension.
- No automatic commit, push, tag or release.
