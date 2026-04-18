"""
GroupedChartWidget — profile 驱动的分组曲线图。

与旧 ChartWidget 的关键差异：
- 按 `channel_def.group_id` 把曲线分到多个 PlotItem 垂直叠放，每组独立 Y 轴
- 共享 X 轴（pyqtgraph `setXLink`）
- 数据源改为 pull（`refresh(data_store)`）：每帧从 ChannelBuffer 拿 ndarray
  整批 setData，避免 Python list 反复拼接
- 事件竖线：`add_event_marker(ts_ms, level)` 为所有子图添加半透明竖线
- 时间窗口：set_time_window(seconds) / set_auto_range(True) 两种模式
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from satellite_debug_tool.core.data import DataStore
from satellite_debug_tool.core.data.data_store import channel_key
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import ChannelDefEntry


# 每个 group_id 选一套视觉友好的曲线配色
_GROUP_PALETTES = {
    0: ["#E74C3C", "#E67E22", "#F1C40F"],                     # 姿态（暖色）
    1: ["#3498DB", "#2980B9", "#1ABC9C", "#16A085"],          # 指向/角度（蓝绿）
    2: ["#2ECC71", "#27AE60"],                                # 信号（绿）
    3: ["#9B59B6", "#8E44AD", "#E91E63"],                     # PID/误差（紫红）
    4: ["#F39C12", "#D35400"],                                # GPS（橙）
}
_DEFAULT_PALETTE = ["#CCCCCC", "#888888"]

# 与 EventLog level 对应的事件竖线颜色
_EVENT_LEVEL_COLORS = {
    0: "#808080",   # DEBUG
    1: "#4EC9B0",   # INFO
    2: "#DCDCAA",   # WARN
    3: "#F14C4C",   # ERROR
}

# 每个子图最小高度（像素）
_MIN_SUBPLOT_HEIGHT = 120


def _group_title(group_id: int) -> str:
    return {
        0: "姿态（Attitude）",
        1: "指向（Pointing）",
        2: "信号（Signal）",
        3: "PID / 误差",
        4: "位置（GPS）",
    }.get(group_id, f"Group {group_id}")


class GroupedChartWidget(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        # 默认 120s 窗口；配合 ChannelBuffer=30000 容量，足够保留最近几分钟历史
        self._time_window = 120.0
        self._auto_range = False
        self._is_dark = True
        self._current_hw: Optional[str] = None
        self._profile: Optional[ProfileStore] = None

        # group_id -> PlotItem
        self._plots: Dict[int, pg.PlotItem] = {}
        # channel_id -> (group_id, PlotDataItem, ChannelDefEntry)
        self._curves: Dict[int, tuple] = {}
        # 事件竖线缓存（最多保留 200 条，避免无限增长）
        self._event_lines: List[tuple] = []   # (ts_ms, [InfiniteLine...])
        self._max_event_lines = 200

        # pyqtgraph 全局默认：抗锯齿 + 黑背景
        pg.setConfigOptions(antialias=True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._gl = pg.GraphicsLayoutWidget()
        layout.addWidget(self._gl)
        self._apply_theme()

        self._empty = pg.LabelItem("等待设备握手…", color="#666666", size="10pt")
        self._gl.addItem(self._empty)

    # ---- 公共 API ----

    def set_dark_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self._apply_theme()

    def set_profile_store(self, profile: ProfileStore) -> None:
        """绑定 profile store；profile_changed 时自动重建子图。"""
        if self._profile is profile:
            return
        if self._profile is not None:
            try:
                self._profile.profile_changed.disconnect(self._on_profile_changed)
            except (TypeError, RuntimeError):
                pass
        self._profile = profile
        profile.profile_changed.connect(self._on_profile_changed)

    def set_hw_type(self, hw_type: Optional[str]) -> None:
        if hw_type == self._current_hw:
            return
        self._current_hw = hw_type
        self._rebuild()

    def set_time_window(self, seconds: float) -> None:
        self._time_window = max(1.0, float(seconds))
        self._auto_range = False

    def set_auto_range(self, enabled: bool) -> None:
        self._auto_range = bool(enabled)

    def refresh(self, data_store: DataStore) -> None:
        """按 profile 拉取 ChannelBuffer 的最新全量数据整批刷新。"""
        if not self._curves:
            return

        latest_x = None
        for channel_id, (group_id, curve, _entry) in self._curves.items():
            buf = data_store.get_channel(channel_key(channel_id))
            if buf is None:
                continue
            xs = buf.get_times()
            ys = buf.get_values()
            if xs.size == 0:
                continue
            # ChannelBuffer 存的是设备时间戳（ms）；X 轴按秒显示
            xs_sec = xs / 1000.0
            curve.setData(xs_sec, ys)
            if latest_x is None or xs_sec[-1] > latest_x:
                latest_x = float(xs_sec[-1])

        if latest_x is None:
            return
        if not self._auto_range:
            # 任一子图 setXRange 即同步（已 setXLink）
            first_plot = next(iter(self._plots.values()), None)
            if first_plot is not None:
                first_plot.setXRange(latest_x - self._time_window, latest_x, padding=0)

    def add_event_marker(self, timestamp_ms: int, level: int) -> None:
        """在所有子图上叠一条半透明竖线（接 EventLog.event_added）。"""
        if not self._plots:
            return
        color = _EVENT_LEVEL_COLORS.get(level, "#CCCCCC")
        pen = pg.mkPen(color=color, width=1, style=Qt.DashLine)
        lines: List[pg.InfiniteLine] = []
        for plot in self._plots.values():
            line = pg.InfiniteLine(pos=timestamp_ms / 1000.0, angle=90, pen=pen)
            line.setZValue(-1)   # 放曲线底下
            plot.addItem(line)
            lines.append(line)
        self._event_lines.append((timestamp_ms, lines))
        # 环形：超过上限丢最旧
        while len(self._event_lines) > self._max_event_lines:
            _, old_lines = self._event_lines.pop(0)
            for ln, plot in zip(old_lines, self._plots.values()):
                plot.removeItem(ln)

    def clear_event_markers(self) -> None:
        for _, lines in self._event_lines:
            for ln, plot in zip(lines, self._plots.values()):
                plot.removeItem(ln)
        self._event_lines.clear()

    def clear(self) -> None:
        for channel_id, (_gid, curve, _entry) in self._curves.items():
            curve.clear()
        self.clear_event_markers()

    # ---- 旧 API 兼容（MainWindow 暂未重构时保留） ----

    def set_channels(self, names: List[str]) -> None:
        """兼容旧 API；新架构按 profile 决定曲线集合，此方法为 no-op。"""
        return

    def set_time_range(self, min_ts: float, max_ts: float) -> None:
        first = next(iter(self._plots.values()), None)
        if first is not None:
            first.setXRange(min_ts, max_ts, padding=0)

    # ---- 响应 profile 变化 ----

    def _on_profile_changed(self, hw_type: str) -> None:
        if self._current_hw is None:
            self._current_hw = hw_type
        if hw_type == self._current_hw:
            self._rebuild()

    # ---- 重建 ----

    def _apply_theme(self) -> None:
        bg = "#1E1E1E" if self._is_dark else "#FFFFFF"
        self._gl.setBackground(bg)

    def _clear_plots(self) -> None:
        self._gl.clear()
        self._plots.clear()
        self._curves.clear()
        self._event_lines.clear()

    def _rebuild(self) -> None:
        self._clear_plots()

        if self._current_hw is None or self._profile is None:
            self._empty = pg.LabelItem("等待设备握手…", color="#666666", size="10pt")
            self._gl.addItem(self._empty)
            return

        channels = self._profile.get_channels(self._current_hw)
        if not channels:
            self._empty = pg.LabelItem(
                f"[{self._current_hw}] 暂无通道", color="#666666", size="10pt",
            )
            self._gl.addItem(self._empty)
            return

        # 按 group_id 聚合
        groups: Dict[int, List[ChannelDefEntry]] = {}
        for ch in channels:
            groups.setdefault(ch.group_id, []).append(ch)

        axis_text_color = "#CCCCCC" if self._is_dark else "#333333"
        grid_alpha = 0.25
        first_plot: Optional[pg.PlotItem] = None

        for row_idx, group_id in enumerate(sorted(groups.keys())):
            group_channels = groups[group_id]
            plot: pg.PlotItem = self._gl.addPlot(row=row_idx, col=0, title=_group_title(group_id))
            plot.setMinimumHeight(_MIN_SUBPLOT_HEIGHT)
            plot.setLabel("left", "Value")
            plot.showGrid(x=True, y=True, alpha=grid_alpha)
            plot.getAxis("left").setTextPen(axis_text_color)
            plot.getAxis("bottom").setTextPen(axis_text_color)
            plot.setDownsampling(mode="peak", auto=True)
            plot.setClipToView(True)
            plot.addLegend(offset=(4, 4))

            if first_plot is None:
                first_plot = plot
            else:
                plot.setXLink(first_plot)

            palette = _GROUP_PALETTES.get(group_id, _DEFAULT_PALETTE)
            for i, ch in enumerate(group_channels):
                color = palette[i % len(palette)]
                curve = plot.plot(
                    [], [], pen=pg.mkPen(color=color, width=1.5), name=ch.name
                )
                self._curves[ch.channel_id] = (group_id, curve, ch)

            # 建议 Y 轴范围：取组内 display_min/max 聚合
            y_mins = [c.display_min for c in group_channels]
            y_maxs = [c.display_max for c in group_channels]
            if y_mins and y_maxs:
                plot.setYRange(min(y_mins), max(y_maxs), padding=0.05)

            self._plots[group_id] = plot

        if first_plot is not None:
            first_plot.setLabel("bottom", "Time", units="s")
