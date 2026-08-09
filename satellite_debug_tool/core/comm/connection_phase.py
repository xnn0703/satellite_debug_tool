"""Device presence state layered over a locally ready transport."""

from __future__ import annotations

from enum import Enum


class DeviceConnectionPhase(str, Enum):
    DISCONNECTED = "disconnected"
    WAITING = "waiting"
    ONLINE = "online"
    RECONNECTING = "reconnecting"
