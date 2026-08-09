# M18.2 Connection, recording and customer playback acceptance

> Date: 2026-08-05
>
> Status: Software acceptance passed; AFD01 hardware acceptance pending
>
> Plan: `doc/M18_2_connection_recording_playback_plan.md`

## A. Connection intent and device presence

- [x] Connect can be pressed before AFD01 powers up and enters a visible Waiting for device state.
- [x] AFD01 power-up transitions to Online without another operator action.
- [x] Local UDP bind alone never presents the device as Online.
- [x] Loss of valid device records transitions to Reconnecting without closing the socket.
- [x] AFD01 reboot returns to Online automatically and refreshes product identity/capabilities.
- [x] Manual Cancel/Disconnect is the only routine action that clears the connection intent.

## B. Discovery behavior

- [x] Product subscription continues beyond the current three-attempt window with the documented bounded rate.
- [x] Only valid decoded frames refresh device presence.
- [x] Customer discovery and the engineering Debug handshake remain independent.
- [x] RF and maintenance operations stay disabled while the device is Waiting or Reconnecting.
- [x] No `UdpWorker`, AFD01 firmware or protocol-byte change is required.

## C. Armed recording

- [x] Record can be selected before AFD01 powers up and shows an Armed state.
- [x] The selected file is not created before the device supports and confirms full capture.
- [x] Exactly one `SET_CAPTURE_PROFILE=support_full` transaction is active at a time.
- [x] SDB v3 starts immediately after the exact successful response, without a second file dialog.
- [x] Cancel while Armed creates no file; timeout/rejection is visible and leaves no false Active state.
- [x] Active recording captures reconnect gaps and resumes when the same session returns.
- [x] Stop restores `customer_live`; manual disconnect safely cancels or stops every recording state.

## D. Customer playback

- [x] Customer Playback is a chart workspace, not an animated copy of the complete Overview page.
- [x] Only Roll, Pitch, Yaw, beam azimuth, beam elevation/off-axis, SNR, longitude, latitude and altitude are charted.
- [x] Product-service records are preferred; legacy files use only authoritative profile-role fallback.
- [x] Open/Clear, identity, quality, duration, time range, single/grouped chart and GNSS controls work.
- [x] Unsupported customer fields stay absent and raw engineering fields cannot be selected.
- [x] Engineering Playback of the same SDB remains full-channel and unchanged.
- [x] SDB v2/v3 and u32 device-uptime rollover remain correct.

## E. One-screen customer overview

- [x] 3D attitude and 2D beam are the only main visualization columns; no duplicate 3x3 metric panel remains.
- [x] The top information band is split into compact state and runtime-data sections.
- [x] State shows connection, control, tracking, lock, combined navigation, GNSS, TX and Modem online.
- [x] Customer UI never renders green TX On while the TX array is Offline; the paired AFD01 gate is built and awaits hardware validation.
- [x] Runtime data shows longitude, latitude, altitude, RX/TX RF, RX/TX LO and satellite information.
- [x] GEO uses satellite longitude; TLE/LEO uses catalog number or name; unavailable ESA01/old-firmware fields show `—`.
- [x] SNR readout and chart occupy one full-width row; Regular/Compact/Dense use 240/175/122 px bands so larger screens expose more signal detail without making the minimum layout scroll.
- [x] Converter, TX array and RX array are arranged horizontally in one compact strip at the bottom.
- [x] Regular, Compact and Dense layouts are selected from the actual viewport height in Qt logical pixels.
- [x] Values and fault text retain readable fixed typography; adaptation reduces whitespace and graphics before text.
- [x] Overview horizontal and vertical scrollbar ranges are both zero at 1024x600, 1280x800 and 1920x1080.
- [x] Resizing directly from a Regular-height window to 1024x600 converges to Dense without retaining a scrollbar calculated from the old density.
- [x] The same matrix passes in Chinese/English and light/dark themes without overlap or clipping.
- [ ] Windows 125% and 150% DPI use the correct density class and still show all routine data.

## F. Regression and delivery boundary

- [x] Full upper-PC pytest passes with no new warning class.
- [x] Chinese and English views have no clipping, overlap or required page scrolling at 1024x600, 1280x800 and 1920x1080.
- [ ] AFD01 hardware verifies late power-up, reboot reconnect and armed full-support recording.
- [x] AFD01 app debug/release builds pass after the optional 0x27 product-service addition; ESA01 remains untouched.

## G. Engineering workspace parity

- [x] Unlocking Engineering switches to the engineering workspace and hides the customer workspace.
- [x] Engineering navigation exposes exactly Live, Playback, Log and Device in the pre-customer order.
- [x] Every engineering navigation item switches to its existing view without recreating the shared session.
- [x] Customer mode shows no Operation/Engineering mode selector or label.
- [x] Engineering mode shows no mode label or exit label, only the four engineering tabs.
- [x] After one-time unlock, each `Ctrl+Shift+E` activation toggles between Operation and Engineering without another prompt.
- [x] Runtime Chinese/English switching updates both workspace and engineering navigation labels.

## Passing rule

Host tests and visual checks complete software verification. Late power-up, device reboot and full-support recording remain hardware acceptance items until demonstrated on AFD01.
