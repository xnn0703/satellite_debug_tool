"""LogView —— WindTerm Log 导入解析 Tab（M7-S6）。

读取下位机控制台 log（``track_debug_print_table_header:`` + ``track_table_row_bynav:``），
自动按表头分列、剔除非数字列，绘制为曲线。

独立于 LiveView / PlaybackView：
- 独立 DataStore（无界、max_channels=128 足以容纳 68 列实测样例）
- 独立 ProfileStore（虚拟 hw_type="windterm_log"，每列一个 ChannelDefEntry）

X 轴时间为占位（行号 × 100ms），下位机将来吐 ms 时间戳后只需更新
``WindTermLogParser._DEFAULT_ROW_TO_MS``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.data import DataStore
from satellite_debug_tool.core.log_parser import WindTermLogParser, WindTermLogResult
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import ChannelSample, DataReport
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget
from satellite_debug_tool.ui.time_range_control import TimeRangeControl


_VIRTUAL_HW_TYPE = "windterm_log"

GPS_LAT_CHANNEL_NAME = "gps_lat"
GPS_LON_CHANNEL_NAME = "gps_lon"


def _infer_group_id(name: str) -> int:
    """按列名前缀映射到 GroupedChartWidget 已有的 group 配色（仅启发式）。

    - ins/imu/姿态 → 0（暖色）
    - gps/位置     → 4
    - snr/信号     → 2
    - bias/陀螺    → 3
    - 其他         → 2（信号配色复用）
    """
    n = name.lower()
    if n.startswith("ins") or n.startswith("i") and any(
        n.startswith(p) for p in ("iyaw", "ipitch", "iroll", "iy_", "ip_", "ir_",
                                  "in_", "ie_", "iu_", "inspvax")
    ):
        return 0
    if n.startswith("gps") or n in ("gpos", "gvel", "gcog", "gcall", "gsz", "gsd"):
        return 4
    if "snr" in n or "scan" in n or "loss" in n:
        return 2
    if n.startswith("b") and len(n) <= 3:   # bgx/bgy/bgz/bax/bay/baz
        return 3
    return 2


def _build_virtual_profile_dict(result: WindTermLogResult) -> dict:
    """从 parser 结果构造一份 schema_version=1 的 profile dict，喂给 ProfileStore。

    每列 ChannelDefEntry：
    - channel_id = 列索引
    - data_type / flags = 0
    - group_id = 启发式映射
    - display_min / display_max = 该列数据 min/max ± 5% padding（防止全 0 列退化）
    """
    channels = []
    for i, col in enumerate(result.columns):
        col_data = result.data[:, i] if result.data.size else None
        if col_data is None or col_data.size == 0:
            dmin, dmax = -1.0, 1.0
        else:
            cmin = float(col_data.min())
            cmax = float(col_data.max())
            if cmax - cmin < 1e-6:
                # 全相同：给个 ±1 的视窗，避免 setYRange 退化
                dmin, dmax = cmin - 1.0, cmax + 1.0
            else:
                pad = (cmax - cmin) * 0.05
                dmin, dmax = cmin - pad, cmax + pad
        channels.append({
            "channel_id": i,
            "data_type": 0,
            "group_id": _infer_group_id(col),
            "flags": 0,
            "name": col,
            "unit": "",
            "display_min": dmin,
            "display_max": dmax,
        })
    return {
        "schema_version": 1,
        "hw_type": _VIRTUAL_HW_TYPE,
        "channel_table_ver": 1,
        "state_table_ver": 0,
        "event_table_ver": 0,
        "channels": channels,
        "states": [],
        "events": [],
        "meta": None,
    }


class LogView(QWidget):
    status_message = Signal(str, int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme = "dark"

        # 独立 store；max_channels 调大以容纳实测 68 列
        self._data_store = DataStore(max_channels=128, buffer_capacity=None)
        self._profile_store = ProfileStore(cache=None)

        self._current_file: Optional[Path] = None
        self._total_sec: float = 0.0
        self._loaded_rows: int = 0
        self._loaded_cols: int = 0
        self._skipped_rows: int = 0
        self._dropped_cols: list[str] = []

        # M8: 地图浮窗（懒加载）
        self._map_widget = None
        self._map_dock: Optional[QDockWidget] = None
        self._gps_lat_id: Optional[int] = None
        self._gps_lon_id: Optional[int] = None
        # 缓存第一帧时间戳（log 路径用占位时间戳，应为 0）
        self._first_ts_ms: float = 0.0

        self._setup_ui()
        self._apply_theme()

    # ============================ UI ============================

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(3)

        # 顶部一行：Open + 文件名 + 行/列统计 + range
        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(4, 2, 4, 2)
        top_layout.setSpacing(6)

        self._open_btn = QPushButton("Open .log")
        self._open_btn.setFixedSize(100, 28)
        self._open_btn.setToolTip(
            "导入 WindTerm 控制台 log（track_debug_print_table_header / track_table_row_bynav）"
        )
        self._open_btn.clicked.connect(self._on_open_clicked)
        top_layout.addWidget(self._open_btn)

        self._file_label = QLabel("（未加载文件）")
        self._file_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        top_layout.addWidget(self._file_label)

        self._stats_label = QLabel("行数: — · 列数: —")
        self._stats_label.setFixedWidth(180)
        top_layout.addWidget(self._stats_label)

        self._range_ctl = TimeRangeControl()
        self._range_ctl.range_changed.connect(self._on_range_changed)
        top_layout.addWidget(self._range_ctl)

        # M8: 地图按钮
        self._map_btn = QPushButton("地图")
        self._map_btn.setFixedSize(60, 28)
        self._map_btn.setEnabled(False)
        self._map_btn.setToolTip(
            "打开/关闭离线地图浮窗（需要 log 中含 gps_lat / gps_lon 列）"
        )
        self._map_btn.clicked.connect(self._toggle_map)
        top_layout.addWidget(self._map_btn)

        root.addWidget(top)

        # 第二行：X 轴占位说明（小字）
        hint = QLabel("X 轴时间：行号 × 100ms（占位，下位机吐 ms 时间戳后更新）")
        hint.setStyleSheet("font-size: 10px;")
        self._hint_label = hint
        root.addWidget(hint)

        # 主区：纯 chart
        self._chart = GroupedChartWidget()
        self._chart.setMinimumHeight(400)
        self._chart.set_dark_theme(True)
        self._chart.set_profile_store(self._profile_store)
        root.addWidget(self._chart, 1)

    # ============================ 主题 ============================

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
        self._apply_theme()
        widgets = [self._chart, self._range_ctl]
        if self._map_widget is not None:
            widgets.append(self._map_widget)
        for w in widgets:
            if hasattr(w, "set_theme"):
                w.set_theme(theme, scale)
            elif hasattr(w, "set_dark_theme"):
                w.set_dark_theme(theme != "light")

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light")

    def _apply_theme(self):
        p = S.palette(self._theme)
        self.setStyleSheet(f"background-color: {p['bg']}; color: {p['text']};")
        self._file_label.setStyleSheet(
            f"color: {p['text_muted']}; padding: 0 8px; background: transparent;"
        )
        self._stats_label.setStyleSheet(
            f"color: {p['text']}; background: transparent;"
        )
        self._open_btn.setStyleSheet(
            f"background-color: {p['primary']}; color: white; "
            f"border: none; padding: 4px 8px; border-radius: 2px;"
        )
        self._hint_label.setStyleSheet(
            f"color: {p['text_faint']}; font-size: 10px; padding: 0 4px;"
        )

    # ============================ 业务 ============================

    def _on_open_clicked(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Open WindTerm Log", "",
            "Log Files (*.log *.txt);;All Files (*)",
        )
        if not filepath:
            return
        self._load_file(Path(filepath))

    def _load_file(self, path: Path) -> None:
        self.status_message.emit(f"Parsing {path.name}...", 0)
        try:
            result = WindTermLogParser.parse(
                path,
                progress_cb=self._on_parse_progress,
                progress_every=5000,
            )
        except Exception as exc:
            self.status_message.emit(f"Parse failed: {exc}", 5000)
            return

        if not result.columns:
            self.status_message.emit(
                "No numeric columns found in log (check 'track_debug_print_table_header')",
                5000,
            )
            return

        # 1) 构造虚拟 profile + 注入到 ProfileStore
        profile_dict = _build_virtual_profile_dict(result)
        hw = self._profile_store.import_dict(profile_dict)
        if hw is None:
            self.status_message.emit("Failed to build virtual profile", 5000)
            return
        self._chart.set_hw_type(hw)

        # 2) 清空旧数据 + 灌入新数据
        self._data_store.clear()
        for row_idx in range(result.data.shape[0]):
            ts_ms = int(result.timestamps_ms[row_idx])
            samples = [
                ChannelSample(channel_id=i, value=float(result.data[row_idx, i]))
                for i in range(result.data.shape[1])
            ]
            self._data_store.update(DataReport(timestamp=ts_ms, samples=samples))

        # 3) 元信息 + range
        self._current_file = path
        self._loaded_rows = result.data.shape[0]
        self._loaded_cols = len(result.columns)
        self._skipped_rows = result.skipped_rows
        self._dropped_cols = result.dropped_non_numeric_cols
        if self._loaded_rows > 1:
            self._total_sec = (
                float(result.timestamps_ms[-1] - result.timestamps_ms[0]) / 1000.0
            )
        else:
            self._total_sec = 0.0
        self._range_ctl.set_total(self._total_sec)

        # 4) 视图：先 refresh 灌数据，再开 Y 自动范围（避免单调递增计数器列
        #    如 inspvax_n / rmp_seen 把组内 Y 范围拉到几十万）
        self._chart.set_auto_range(True)
        self._chart.refresh(self._data_store)
        self._chart.enable_y_autorange(True)

        self._file_label.setText(f"📄 {path.name}")
        self._stats_label.setText(
            f"行数: {self._loaded_rows} · 列数: {self._loaded_cols}"
        )
        dropped_note = ""
        if self._dropped_cols:
            dropped_note = f"，剔除字符串列 {len(self._dropped_cols)} 个"
        skipped_note = ""
        if self._skipped_rows:
            skipped_note = f"，跳过格式异常行 {self._skipped_rows}"
        self.status_message.emit(
            f"Loaded {self._loaded_rows} rows × {self._loaded_cols} cols"
            f"{dropped_note}{skipped_note}",
            5000,
        )

        # M8: GPS 列检测 → 启用地图按钮 + 同步已开浮窗
        gps_ok = self._detect_gps_columns(result)
        self._map_btn.setEnabled(gps_ok)
        if self._map_widget is not None:
            self._refresh_map_track()

    def _on_parse_progress(self, line_count: int) -> None:
        self.status_message.emit(f"Parsing... {line_count} lines", 0)

    def _on_range_changed(self, start_sec: float, end_sec: float) -> None:
        self._chart.set_auto_range(False)
        self._chart.set_x_range_sec(start_sec, end_sec)
        # M8: 地图轨迹高亮（log 占位时间戳从 0 开始，sec → ms 直接 *1000）
        if self._map_widget is not None:
            start_ms = self._first_ts_ms + start_sec * 1000.0
            end_ms = self._first_ts_ms + end_sec * 1000.0
            self._map_widget.set_track_highlight(start_ms, end_ms)

    # ====================== M8: 地图集成 ======================

    def _detect_gps_columns(self, result: WindTermLogResult) -> bool:
        """在解析结果列名中查 gps_lat / gps_lon；返回 True 表示找到。"""
        self._gps_lat_id = None
        self._gps_lon_id = None
        for i, name in enumerate(result.columns):
            if name == GPS_LAT_CHANNEL_NAME:
                self._gps_lat_id = i
            elif name == GPS_LON_CHANNEL_NAME:
                self._gps_lon_id = i
        return self._gps_lat_id is not None and self._gps_lon_id is not None

    def _toggle_map(self) -> None:
        if self._map_dock is None:
            self._build_map_dock()
            self._refresh_map_track()
        self._map_dock.setVisible(not self._map_dock.isVisible())

    def _build_map_dock(self) -> None:
        from satellite_debug_tool.ui.map_widget import MapWidget
        self._map_widget = MapWidget()
        self._map_widget.set_theme(self._theme, "small")
        self._map_dock = QDockWidget("地图 — Log", self)
        self._map_dock.setAllowedAreas(Qt.NoDockWidgetArea)
        self._map_dock.setFloating(True)
        self._map_dock.setWidget(self._map_widget)
        self._map_dock.resize(800, 600)

    def _refresh_map_track(self) -> None:
        if self._map_widget is None:
            return
        if self._gps_lat_id is None or self._gps_lon_id is None:
            self._map_widget.clear()
            return
        lat_buf = self._data_store.get_channel_by_id(self._gps_lat_id)
        lon_buf = self._data_store.get_channel_by_id(self._gps_lon_id)
        if lat_buf is None or lon_buf is None:
            self._map_widget.clear()
            return
        ts = lat_buf.get_times()
        lats = lat_buf.get_values()
        lon_vals = lon_buf.get_values()
        if ts.size == 0:
            self._map_widget.clear()
            return
        # log 路径 lat/lon 同一帧灌入，时间戳一致；无需 interp
        self._map_widget.clear()
        self._map_widget.set_track(ts, lats, lon_vals)
