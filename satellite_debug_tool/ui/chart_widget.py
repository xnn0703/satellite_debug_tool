from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import Qt
import pyqtgraph as pg


COLORS = [
    "#E74C3C",
    "#3498DB",
    "#2ECC71",
    "#F39C12",
    "#9B59B6",
    "#1ABC9C",
    "#E91E63",
    "#00BCD4",
    "#8BC34A",
    "#FF5722",
    "#607D8B",
    "#673AB7",
]


class ChartWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._plot_items = {}
        self._curves = {}
        self._time_window = 10.0
        self._max_points = 1000
        self._auto_time_range = False
        self._min_ts = None
        self._max_ts = None
        self._is_dark = True
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._plot_widget = pg.PlotWidget()
        self._set_theme_colors()
        self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self._plot_widget.setLabel("bottom", "Time", units="s")
        self._plot_widget.setLabel("left", "Value")
        layout.addWidget(self._plot_widget)

    def _set_theme_colors(self):
        if self._is_dark:
            self._plot_widget.setBackground("#1E1E1E")
            self._plot_widget.getAxis("bottom").setPen("#CCCCCC")
            self._plot_widget.getAxis("left").setPen("#CCCCCC")
            self._plot_widget.getAxis("bottom").setTextPen("#CCCCCC")
            self._plot_widget.getAxis("left").setTextPen("#CCCCCC")
        else:
            self._plot_widget.setBackground("#FFFFFF")
            self._plot_widget.getAxis("bottom").setPen("#333333")
            self._plot_widget.getAxis("left").setPen("#333333")
            self._plot_widget.getAxis("bottom").setTextPen("#333333")
            self._plot_widget.getAxis("left").setTextPen("#333333")

    def set_dark_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self._set_theme_colors()

    def set_channels(self, names: list) -> None:
        existing = set(self._plot_items.keys())
        new = set(names)
        for name in existing - new:
            self._remove_channel(name)
        for i, name in enumerate(names):
            if name not in self._plot_items:
                self._add_channel(name, COLORS[i % len(COLORS)])

    def _add_channel(self, name: str, color: str) -> None:
        plot_item = pg.PlotDataItem(pen=pg.mkPen(color=color, width=1.5))
        self._plot_widget.addItem(plot_item)
        self._plot_items[name] = plot_item

    def _remove_channel(self, name: str) -> None:
        if name in self._plot_items:
            self._plot_widget.removeItem(self._plot_items[name])
            del self._plot_items[name]

    def update_data(self, timestamp: float, values: dict) -> None:
        if self._auto_time_range:
            if self._min_ts is None or timestamp < self._min_ts:
                self._min_ts = timestamp
            if self._max_ts is None or timestamp > self._max_ts:
                self._max_ts = timestamp
        for name, value in values.items():
            if name not in self._plot_items:
                continue
            curve = self._plot_items[name]
            x = curve.xData
            y = curve.yData
            times = list(x) if x is not None and len(x) > 0 else []
            vals = list(y) if y is not None and len(y) > 0 else []
            if not self._auto_time_range and len(times) >= self._max_points:
                times = times[-self._max_points + 1 :]
                vals = vals[-self._max_points + 1 :]
            times.append(timestamp)
            vals.append(value)
            curve.setData(times, vals)
        if (
            self._auto_time_range
            and self._min_ts is not None
            and self._max_ts is not None
        ):
            self._plot_widget.setXRange(self._min_ts, self._max_ts)

    def set_time_window(self, seconds: float) -> None:
        self._time_window = seconds
        if not self._auto_time_range:
            self._plot_widget.setXRange(-seconds, 0)

    def set_auto_time_range(self, enabled: bool) -> None:
        self._auto_time_range = enabled
        if enabled:
            self._min_ts = None
            self._max_ts = None

    def set_time_range(self, min_ts: float, max_ts: float) -> None:
        self._min_ts = min_ts
        self._max_ts = max_ts
        self._plot_widget.setXRange(min_ts, max_ts)

    def clear(self) -> None:
        for name in list(self._plot_items.keys()):
            self._plot_widget.removeItem(self._plot_items[name])
        self._plot_items.clear()
        self._min_ts = None
        self._max_ts = None
        self._auto_time_range = False
