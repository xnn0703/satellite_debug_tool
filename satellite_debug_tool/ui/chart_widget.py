"""
旧 ChartWidget 在 M3 被 ``GroupedChartWidget`` 取代，此文件仅保留通用
曲线配色调色盘 ``COLORS`` 给 channel 选择面板复用。

2026-06：调色盘对齐 Mission Console 信号色板（styles.SIGNAL_PALETTE），
让 ChannelPanel 的色块与曲线视觉语言一致。
"""

from satellite_debug_tool.ui.styles import SIGNAL_PALETTE

# 通道选择面板色块用；与 grouped_chart 曲线同源
COLORS = list(SIGNAL_PALETTE)
