"""内部 INS 航向在 Live 3D 中的选择合同。"""

from __future__ import annotations

from typing import Optional


INTERNAL_INS_YAW_UNAVAILABLE = 0
INTERNAL_INS_YAW_RELATIVE = 1
INTERNAL_INS_YAW_ABSOLUTE = 2

YAW_DISPLAY_LEGACY = "legacy"
YAW_DISPLAY_UNAVAILABLE = "unavailable"
YAW_DISPLAY_RELATIVE = "relative"
YAW_DISPLAY_ABSOLUTE = "absolute"


def select_attitude_yaw_channel(
    *,
    reference_state_defined: bool,
    reference_value: Optional[int],
    business_yaw_channel: str,
    internal_yaw_channel: str,
) -> tuple[str, str]:
    """返回 ``(channel_key, display_reference)``。

    旧固件没有 reference state 时保持历史行为。新固件必须显式声明参考类型；
    ``RELATIVE`` 只影响 3D 诊断显示，不会替代业务 ``yaw`` 的绝对航向语义。
    """
    if not reference_state_defined:
        return business_yaw_channel, YAW_DISPLAY_LEGACY
    if reference_value == INTERNAL_INS_YAW_RELATIVE:
        return internal_yaw_channel, YAW_DISPLAY_RELATIVE
    if reference_value == INTERNAL_INS_YAW_ABSOLUTE:
        channel = business_yaw_channel or internal_yaw_channel
        return channel, YAW_DISPLAY_ABSOLUTE
    return "", YAW_DISPLAY_UNAVAILABLE

