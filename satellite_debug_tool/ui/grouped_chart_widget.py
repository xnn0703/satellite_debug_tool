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
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.data import DataStore
from satellite_debug_tool.core.data.data_store import channel_key
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import ChannelDefEntry
from satellite_debug_tool.ui import styles as S


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

# stacked 模式下每个子图固定目标高度（像素）。
# - 小于可视区：容器自适应不触发滚动
# - 大于可视区：容器总高度累加触发 QScrollArea 滚动
_STACKED_SUBPLOT_HEIGHT = 220

# X 窗口"滚屏阈值"：新数据超出当前 xmax 超过这个秒数才扩展 viewbox，
# 避免每帧 setXRange 触发 sigRangeChanged → 刻度/网格重绘导致肉眼闪烁。
_X_SCROLL_STEP_SEC = 1.0

# 合并模式下 12 条曲线的颜色序列（高区分度）
_COMBINED_PALETTE = [
    "#E74C3C", "#3498DB", "#2ECC71", "#F39C12",
    "#9B59B6", "#1ABC9C", "#E91E63", "#00BCD4",
    "#8BC34A", "#FF5722", "#607D8B", "#673AB7",
    "#CDDC39", "#FFC107", "#795548", "#03A9F4",
]


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
        # 默认 "combined"：所有通道叠到一张大图看全貌；
        # "stacked"：按 group_id 纵向分子图（适合多量纲对比）
        self._mode = "combined"

        # group_id (combined 模式用 0) -> PlotItem
        self._plots: Dict[int, pg.PlotItem] = {}
        # channel_id -> (group_id, PlotDataItem, ChannelDefEntry)
        self._curves: Dict[int, tuple] = {}
        # 事件竖线缓存（最多保留 200 条，避免无限增长）
        self._event_lines: List[tuple] = []   # (ts_ms, [InfiniteLine...])
        self._max_event_lines = 200

        # 相对时间原点（第一帧数据的 ms 时间戳）。X 轴显示 = (ts_ms - origin)/1000，
        # 避免设备启动已运行几千秒时 pyqtgraph 把单位自动切到 "ks" 导致刻度跳变
        self._x_origin_ms: Optional[float] = None
        # 当前 X 窗口右边界（秒，相对时间）；仅在真正扩窗时更新
        self._x_view_max: float = 0.0

        # pyqtgraph 全局默认：抗锯齿 + 黑背景
        pg.setConfigOptions(antialias=True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # 顶部 toolbar：单图 / 分组 切换 + 自动/跟随 开关
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 2, 4, 2)
        toolbar.setSpacing(4)
        self._btn_combined = QPushButton("单图")
        self._btn_combined.setCheckable(True)
        self._btn_combined.setChecked(True)
        self._btn_combined.clicked.connect(lambda: self.set_mode("combined"))
        self._btn_stacked = QPushButton("分组")
        self._btn_stacked.setCheckable(True)
        self._btn_stacked.clicked.connect(lambda: self.set_mode("stacked"))
        for b in (self._btn_combined, self._btn_stacked):
            b.setFixedHeight(24)
        toolbar.addWidget(self._btn_combined)
        toolbar.addWidget(self._btn_stacked)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        # Chart 容器放进 QScrollArea：stacked 模式下子图多时可上下滚动
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._gl = pg.GraphicsLayoutWidget()
        self._scroll.setWidget(self._gl)
        layout.addWidget(self._scroll, 1)
        self._apply_theme()

        self._empty = pg.LabelItem("等待设备握手…", color="#666666", size="10pt")
        self._gl.addItem(self._empty)

    # ---- 公共 API ----

    def set_dark_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self._apply_theme()
        self._apply_button_theme()
        # 已有子图的轴色也要同步；简化做法：重建
        if self._current_hw is not None and self._curves:
            self._rebuild()

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

    def set_mode(self, mode: str) -> None:
        """mode = "combined" (所有通道叠一张大图) 或 "stacked" (按 group_id 分子图)。"""
        if mode not in ("combined", "stacked"):
            return
        self._mode = mode
        self._btn_combined.setChecked(mode == "combined")
        self._btn_stacked.setChecked(mode == "stacked")
        self._rebuild()

    def refresh(self, data_store: DataStore) -> None:
        """按 profile 拉取 ChannelBuffer 的最新全量数据整批刷新。

        防闪烁要点：
        - 只 setData，不动 autoRange（构建时已一次性固定 Y 范围）
        - X 窗口仅在新数据超出右边界 `_X_SCROLL_STEP_SEC` 秒时才扩窗，
          而不是每帧都 setXRange，避免 sigRangeChanged 级联重绘
        - 时间戳用"相对启动时刻"的秒数，避免 pyqtgraph 把时间单位自动
          切到 ks/Ms 造成整体刻度跳变
        """
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
            if self._x_origin_ms is None:
                self._x_origin_ms = float(xs[0])
            xs_sec = (xs - self._x_origin_ms) / 1000.0
            # connect='all' + skipFiniteCheck 略快；数据是有序的 ms 不存在 NaN
            curve.setData(xs_sec, ys)
            if latest_x is None or xs_sec[-1] > latest_x:
                latest_x = float(xs_sec[-1])

        if latest_x is None or self._auto_range:
            return

        # 只在真的需要扩窗时才 setXRange。阈值 1s：
        # - 数据 100Hz 刷新，每 200ms refresh 产生 0.2s 新数据
        # - 连续 5 次 refresh 才触发一次 setXRange → 肉眼不易察觉跳动
        if latest_x <= self._x_view_max:
            return
        new_xmax = latest_x + _X_SCROLL_STEP_SEC   # 多扩一小段，下次不用立刻再扩
        new_xmin = new_xmax - self._time_window
        self._x_view_max = new_xmax
        first_plot = next(iter(self._plots.values()), None)
        if first_plot is not None:
            first_plot.setXRange(new_xmin, new_xmax, padding=0)

    def add_event_marker(self, timestamp_ms: int, level: int) -> None:
        """在所有子图上叠一条半透明竖线（接 EventLog.event_added）。"""
        if not self._plots:
            return
        # 未收到首帧数据时还没有时间原点，拿当前时间戳做原点占位
        if self._x_origin_ms is None:
            self._x_origin_ms = float(timestamp_ms)
        x_sec = (float(timestamp_ms) - self._x_origin_ms) / 1000.0

        color = _EVENT_LEVEL_COLORS.get(level, "#CCCCCC")
        pen = pg.mkPen(color=color, width=1, style=Qt.DashLine)
        lines: List[pg.InfiniteLine] = []
        for plot in self._plots.values():
            line = pg.InfiniteLine(pos=x_sec, angle=90, pen=pen)
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
        # 时间原点/窗口回到未初始化状态，下次新数据重新对齐
        self._x_origin_ms = None
        self._x_view_max = 0.0

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
        if hw_type != self._current_hw or self._profile is None:
            return
        # 二次保险：即便 ProfileStore 错误地多发了一次 profile_changed，
        # 也只有在 channels 组成/属性真变化时才 rebuild，避免清空曲线历史
        new_sig = tuple(
            (c.channel_id, c.name, c.unit, c.group_id, c.display_min, c.display_max)
            for c in self._profile.get_channels(hw_type)
        )
        if getattr(self, "_channels_signature", None) == new_sig and self._curves:
            return
        self._channels_signature = new_sig
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
        # 切换 mode / profile 时丢弃旧的 X 原点，避免新图沿用旧时间轴
        self._x_origin_ms = None
        self._x_view_max = 0.0

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

        self._apply_button_theme()
        if self._mode == "combined":
            self._rebuild_combined(channels)
        else:
            self._rebuild_stacked(channels)

    def _plot_axis_color(self) -> str:
        return "#CCCCCC" if self._is_dark else "#333333"

    def _apply_button_theme(self) -> None:
        p = S.palette(self._is_dark)
        btn_style = (
            f"QPushButton {{ background-color: {p['input_bg']}; color: {p['text']}; "
            f"border: 1px solid {p['input_border']}; border-radius: 3px; padding: 2px 10px; }}"
            f"QPushButton:hover {{ background-color: {p['card_alt']}; }}"
            f"QPushButton:checked {{ background-color: #0E639C; color: white; "
            f"border-color: #0E639C; font-weight: 600; }}"
        )
        self._btn_combined.setStyleSheet(btn_style)
        self._btn_stacked.setStyleSheet(btn_style)

    def _rebuild_combined(self, channels: List[ChannelDefEntry]) -> None:
        """所有通道叠一张大图，共用 Y 轴。Y 范围取所有通道 display_min/max 包络，
        不开 autoRange 避免每帧 Y 轴回弹造成整图闪烁。"""
        # 让容器高度跟滚动区一致（combined 模式只有一张图，撑满可视区即可）
        self._gl.setMinimumHeight(0)

        plot: pg.PlotItem = self._gl.addPlot(row=0, col=0, title="全部通道")
        plot.setLabel("left", "Value")
        plot.setLabel("bottom", "Time", units="s")
        plot.showGrid(x=True, y=True, alpha=0.25)
        plot.getAxis("left").setTextPen(self._plot_axis_color())
        plot.getAxis("bottom").setTextPen(self._plot_axis_color())
        plot.setDownsampling(mode="peak", auto=True)
        plot.setClipToView(True)
        plot.addLegend(offset=(10, 10))
        # 默认关交互自动范围：只有用户手动拖动时才允许
        plot.enableAutoRange(x=False, y=False)

        for i, ch in enumerate(channels):
            color = _COMBINED_PALETTE[i % len(_COMBINED_PALETTE)]
            label = f"{ch.name} ({ch.unit})" if ch.unit else ch.name
            curve = plot.plot(
                [], [], pen=pg.mkPen(color=color, width=1.5), name=label
            )
            self._curves[ch.channel_id] = (0, curve, ch)

        # Y 范围 = 所有通道 display_min/max 的包络，留 5% padding
        y_mins = [c.display_min for c in channels]
        y_maxs = [c.display_max for c in channels]
        if y_mins and y_maxs:
            plot.setYRange(min(y_mins), max(y_maxs), padding=0.05)
        # 初始 X 范围：0 ~ time_window，新数据到来后 refresh() 滚窗
        plot.setXRange(0.0, self._time_window, padding=0)
        self._x_view_max = self._time_window
        self._plots[0] = plot

    def _rebuild_stacked(self, channels: List[ChannelDefEntry]) -> None:
        """按 group_id 分子图，各自独立 Y 轴，共享 X 轴。
        总高度 = 子图数 × _STACKED_SUBPLOT_HEIGHT，超过可视区由 QScrollArea 滚动。"""
        groups: Dict[int, List[ChannelDefEntry]] = {}
        for ch in channels:
            groups.setdefault(ch.group_id, []).append(ch)

        n_groups = len(groups)
        # 给 GraphicsLayoutWidget 一个明确的最小总高度，触发 QScrollArea 滚动条
        self._gl.setMinimumHeight(n_groups * _STACKED_SUBPLOT_HEIGHT)

        axis_text_color = self._plot_axis_color()
        first_plot: Optional[pg.PlotItem] = None

        for row_idx, group_id in enumerate(sorted(groups.keys())):
            group_channels = groups[group_id]
            plot: pg.PlotItem = self._gl.addPlot(row=row_idx, col=0, title=_group_title(group_id))
            plot.setMinimumHeight(_STACKED_SUBPLOT_HEIGHT - 20)  # 留一些 layout 余量
            plot.setLabel("left", "Value")
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.getAxis("left").setTextPen(axis_text_color)
            plot.getAxis("bottom").setTextPen(axis_text_color)
            plot.setDownsampling(mode="peak", auto=True)
            plot.setClipToView(True)
            plot.addLegend(offset=(4, 4))
            plot.enableAutoRange(x=False, y=False)

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

            # 固定 Y 范围（组内 display_min/max 包络）
            y_mins = [c.display_min for c in group_channels]
            y_maxs = [c.display_max for c in group_channels]
            if y_mins and y_maxs:
                plot.setYRange(min(y_mins), max(y_maxs), padding=0.05)

            self._plots[group_id] = plot

        if first_plot is not None:
            first_plot.setLabel("bottom", "Time", units="s")
            # 初始 X 范围同 combined
            first_plot.setXRange(0.0, self._time_window, padding=0)
            self._x_view_max = self._time_window
