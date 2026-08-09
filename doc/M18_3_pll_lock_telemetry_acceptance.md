# M18.3 AFD01 PLL lock telemetry acceptance

> Status: software acceptance complete; hardware acceptance pending
>
> Plan: `doc/M18_3_pll_lock_telemetry_plan.md`

## Software acceptance

- [x] `SERVICE_RF_LOCK_STATUS (0x28)` is independently decodable and remains
  optional for old firmware.
- [x] Bits 0/1/2 map exactly to clock, TX PLL and RX PLL.
- [x] `OperationalSnapshot` retains valid/stale/unsupported state for each path.
- [x] Runtime data shows the compact `CLK / TX / RX` summary without adding a
  third data-grid row.
- [x] All-lock and individual-unlock states are visually distinct.
- [x] English and Simplified Chinese translations are complete.
- [x] `PYTHONPATH=. pytest satellite_debug_tool/tests -q` passes.
- [x] `scripts/update_translations.py check` passes.
- [x] `git diff --check` passes.

## Firmware acceptance

- [x] AFD01 debug build passes.
- [x] AFD01 release build passes.
- [x] Existing 0x20..0x27 product records retain their schema-1 layouts.

## Hardware acceptance

- [ ] Live customer Overview matches `conv show` for `clk-tx-rx` on the same
  boot.
- [ ] A single lost PLL is visible as the matching unlocked item within the
  subscribed fast-report interval.
- [ ] A missing/old 0x28 record displays `—`, not a false healthy state.
