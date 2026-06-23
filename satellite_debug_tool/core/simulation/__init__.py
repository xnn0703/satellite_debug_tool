"""模拟对星仿真模块 — 桌面端卫星通信链路闭环仿真。"""

from .presets import BAND_PRESETS, SATELLITE_PRESETS

# 延迟导入避免启动时崩溃
def __getattr__(name):
    if name == "MockModem":
        from .mock_modem import MockModem
        return MockModem
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "BAND_PRESETS",
    "SATELLITE_PRESETS",
    "MockModem",
]
