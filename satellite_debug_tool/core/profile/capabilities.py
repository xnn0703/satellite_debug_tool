"""设备能力声明的三态领域模型。"""

from __future__ import annotations

from enum import Enum


class CapabilitySupport(Enum):
    """设备对一项 capability 的明确声明状态。"""

    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
