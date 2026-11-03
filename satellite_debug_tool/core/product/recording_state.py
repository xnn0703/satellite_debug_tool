"""Customer support-recording workflow states."""

from __future__ import annotations

from enum import Enum


class CustomerRecordingState(str, Enum):
    IDLE = "idle"
    ACTIVE = "active"
    RESTORING = "restoring"


class CustomerCaptureStage(str, Enum):
    """录制期间设备采集档位的独立事实。"""

    IDLE = "idle"
    WAITING_DEVICE = "waiting_device"
    NEGOTIATING_FULL = "negotiating_full"
    FULL = "full"
    PASSIVE = "passive"
    RESTORING = "restoring"
