"""Helpers for device-uptime timestamps carried in unsigned 32-bit fields."""

from __future__ import annotations

from typing import Iterable, Optional

import numpy as np


U32_MODULUS = 1 << 32
U32_HALF_RANGE = 1 << 31


class U32UptimeUnwrapper:
    """Expand a forward-moving u32 millisecond counter into a monotonic integer.

    A backward jump smaller than half the u32 domain is an old/out-of-order
    sample, not a rollover. It is rejected without moving the unwrap anchor.
    Session owners must call :meth:`reset` when the device reconnects.
    """

    def __init__(self) -> None:
        self._last_raw: Optional[int] = None
        self._last_unwrapped: Optional[int] = None

    def reset(self) -> None:
        self._last_raw = None
        self._last_unwrapped = None

    def add(self, timestamp_ms: int) -> Optional[int]:
        raw = int(timestamp_ms) & (U32_MODULUS - 1)
        if self._last_raw is None or self._last_unwrapped is None:
            self._last_raw = raw
            self._last_unwrapped = raw
            return raw

        delta = (raw - self._last_raw) & (U32_MODULUS - 1)
        if delta >= U32_HALF_RANGE:
            return None

        self._last_raw = raw
        self._last_unwrapped += delta
        return self._last_unwrapped


def unwrap_u32_series(timestamps_ms: Iterable[int]) -> tuple[np.ndarray, np.ndarray]:
    """Return unwrapped timestamps and indices of accepted forward samples."""

    unwrapper = U32UptimeUnwrapper()
    unwrapped: list[int] = []
    accepted: list[int] = []
    for index, timestamp in enumerate(timestamps_ms):
        value = unwrapper.add(int(timestamp))
        if value is None:
            continue
        unwrapped.append(value)
        accepted.append(index)
    return (
        np.asarray(unwrapped, dtype=np.float64),
        np.asarray(accepted, dtype=np.int64),
    )
