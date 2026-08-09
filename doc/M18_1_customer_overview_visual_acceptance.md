# M18.1 Customer overview visualization acceptance

> Date: 2026-08-05
>
> Status: Software acceptance passed; AFD01 hardware acceptance pending
>
> Plan: `doc/M18_1_customer_overview_visual_plan.md`

## A. Layout and responsive behavior

- [x] Wide layout shows 3D attitude, 2D beam indicator and 3x3 key values as three distinct columns.
- [x] Supported widths keep 3D, 2D beam and key values in one row; an emergency sub-760 px fallback may move values below.
- [x] 3D Roll/Pitch/Yaw readout is larger than the engineering default and remains inside the 3D region.
- [x] Default and Reset 3D camera framing keep normal upward beam trajectories clear of the top toolbar.
- [x] Key-value rows are attitude, beam/polarization and position in that exact order.
- [x] SNR no longer appears in the key-value grid.
- [x] 1024x600, 1280x800 and 1920x1080 have no overlap, clipping or unexpected layout shift.

## B. 2D beam indicator

- [x] Center represents the device and concentric rings represent 30/60/90 degree off-axis angles.
- [x] Azimuth convention is 0 degrees up and clockwise positive, with correct 90/180/270 placement.
- [x] Beam vector length and endpoint match the reported off-axis angle.
- [x] Stale data is muted; unsupported/non-finite data does not draw a false zero vector.
- [x] Light, dark and high-contrast themes remain readable.

## C. Polarization and values

- [x] Circular polarization displays explicit left/right circular text.
- [x] Different RX/TX polarization values are both visible.
- [x] Future numeric linear-polarization angle has a defined formatter and test without inventing a schema-1 value.
- [x] Longitude and latitude retain six decimals; other angles retain two decimals.

## D. SNR

- [x] A large SNR readout appears to the left of the 5-minute chart.
- [x] Numeric SNR and history use the same source and availability state.
- [x] Missing and stale SNR states are clear without treating zero as unavailable.
- [x] The final M18.2 responsive SNR band uses fixed 240/175/122 px density heights, preserving more chart detail on large screens without forcing page scrolling on the minimum layout.

## E. Device uptime

- [x] The X axis displays actual seconds since device boot rather than `-300..0 s`.
- [x] The visible window retains the newest 300 seconds at product-service rates up to 20 Hz.
- [x] U32 millisecond rollover unwraps to a continuous uptime-second axis.
- [x] Disconnect/session reset clears history and unwrap state so rebooted timestamps start a new series.
- [x] Customer playback reproduces the recorded uptime-second axis.

## F. Regression and delivery boundary

- [x] Full upper-PC pytest passes with no new warning class.
- [x] Engineering Live/Playback/Log/Device behavior remains unchanged.
- [x] The AFD01 repository and its unrelated worktree changes remain untouched.
- [ ] AFD01 hardware verifies beam orientation and uptime-axis continuity.

## Passing rule

Host tests and visual checks complete software verification. Uptime continuity and physical beam-direction acceptance require AFD01 hardware evidence.
