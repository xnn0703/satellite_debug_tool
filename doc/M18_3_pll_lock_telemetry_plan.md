# M18.3 Plan: AFD01 PLL lock telemetry

## Goal

Expose the real AFD01 `conv show` lock triplet in the customer Overview without
changing existing fixed-length product-service records or making the customer
UI infer RF health from an aggregate Debug state.

## Source and semantics

`transceiver` updates three hardware-derived values in every lock poll:

- `clock_lock`: ADF4002 reference/clock PLL lock.
- `tx_pll_lock`: ADMV4530 transmit-converter PLL lock.
- `rx_pll_lock`: LMX2594 receive LO PLL lock.

The existing dynamic `PLL_LOCKED` state is only `clock && tx && rx`. It is
insufficient for a customer display because it cannot identify the failed
path. The new display therefore consumes the three values directly.

## Protocol

- Reserve optional `SERVICE_RF_LOCK_STATUS (0x28)` in the existing Debug v2
  envelope. It is emitted with the configured fast product-service cadence.
- Keep schema `1` and all `0x20..0x27` payload layouts unchanged.
- Payload is `schema u8, timestamp_ms u32, valid_mask u32, lock_mask u8`.
  Bits `0/1/2` mean clock, TX and RX respectively in both masks.
- The AFD01 service protocol identity changes from `3` to `4` to advertise
  the optional record. Old upper PCs ignore unknown `0x28` frames; new upper
  PCs show `—` when the record or a valid bit is absent.

## Customer UI

- Keep the existing 4x2 runtime-data grid intact, so 1024x600 does not gain a
  third row or page scrolling.
- Add a compact `PLL lock` summary at the right side of the Runtime data title:
  `CLK / TX / RX` with an individual locked or unlocked mark.
- All valid and locked is normal/green. Any valid unlocked path is a warning.
  Partial or stale data remains explicitly stale; no missing path is assumed
  locked or unlocked.

## Verification

1. Protocol golden vectors cover decoding, legacy product-record compatibility
   and all three valid-mask paths.
2. Product-store tests cover valid, stale and unsupported propagation.
3. Customer Overview tests cover compact wording and warning/neutral styling.
4. Run full upper-PC pytest, translation validation and AFD01 debug/release
   builds.
5. Hardware acceptance: compare the three displayed values with one same-boot
   `conv show`; force or observe one lock loss and verify the matching path
   changes without masking the other two.

## Non-goals

- No ESA01 customer telemetry work.
- No change to Debug `PLL_LOCKED`, TX interlocks, RF frequency control or
  recording format.
- No inference from a missing optional product-service record.
