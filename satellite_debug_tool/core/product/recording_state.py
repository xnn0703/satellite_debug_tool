"""Customer support-recording workflow states."""

from __future__ import annotations

from enum import Enum


class CustomerRecordingState(str, Enum):
    IDLE = "idle"
    ARMED = "armed"
    PREPARING = "preparing"
    ACTIVE = "active"
    RESTORING = "restoring"
