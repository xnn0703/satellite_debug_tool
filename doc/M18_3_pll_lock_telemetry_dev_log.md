# M18.3 AFD01 PLL lock telemetry development log

## 2026-08-07: source audit and design

- `conv show` reads `clock_lock`, `tx_pll_lock` and `rx_pll_lock` from
  `conv_info`; `transceiver_get_product_status()` already provides the shared
  thread-safe product snapshot boundary but did not export those fields.
- Dynamic Debug exposes only aggregate `PLL_LOCKED = clock && tx && rx`.
  Customer display must not reverse-engineer individual paths from this value.
- Existing M18.2 used optional `SERVICE_LINK_DETAIL (0x27)` specifically to
  avoid extending fixed product records. M18.3 follows the same forward-
  compatible pattern with optional `0x28`.
- The runtime data band is intentionally kept as a 4x2 grid. The lock triplet
  is a compact header summary so the minimum customer viewport remains a
  one-page layout.

## 2026-08-07: implementation and software verification

- AFD01 now publishes optional `SERVICE_RF_LOCK_STATUS (0x28)` with the
  hardware-derived clock, TX and RX lock bits. Existing `0x20..0x27` product
  record layouts are unchanged, and the service identity advertises version 4.
- The upper PC decodes the optional record into three independently stale-aware
  `OperationalSnapshot` values. Missing data stays unavailable; it is never
  inferred from aggregate Debug `PLL_LOCKED`.
- Customer Overview renders `PLL lock: CLK / TX / RX` in the Runtime data
  header. All locked is green, any unlocked valid path is amber, and stale or
  unsupported data remains neutral. Light and dark 1024x600 host renders were
  checked for clipping.
- Verification: `PYTHONPATH=. pytest satellite_debug_tool/tests -q` completed
  with `727 passed` (four existing updater `datetime.utcnow()` deprecation
  warnings); translation validation reports `579 messages`; AFD01 debug and
  release builds, `format.sh check`, and both repository `git diff --check`
  commands passed.
- Hardware remains pending: compare one same-boot customer display with
  `conv show`, then observe or inject an individual unlock.
