"""PlaybackView —— 离线 .sdb 回放 Tab（M7-S5）。

完全独立于 LiveView：自己的 DataStore（无界）+ ProfileStore（无磁盘缓存）
+ StateStore + EventLog。导入文件时不会污染 Live Tab 的实时数据，反之亦然。

UI 结构：
    顶部一行：[Open .sdb] [文件名 label] [总时长 label] [TimeRangeControl]
    主区：    [GroupedChartWidget] | [Dashboard + EventTimeline] (Horizontal Splitter)
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
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.data import DataStore, EventLog, GnssStore, StateStore
from satellite_debug_tool.core.profile import (
    CHANNEL_ROLE_GPS_LAT,
    CHANNEL_ROLE_GPS_LON,
    ProfileStore,
)
from satellite_debug_tool.core.protocol import (
    DataReport,
    EventReport,
    GnssCnrReport,
    GnssSatReport,
    GnssSignalReport,
    GnssSkyReport,
    StateReport,
)
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.dashboard_widget import DashboardWidget
from satellite_debug_tool.ui.event_timeline_widget import EventTimelineWidget
from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget
from satellite_debug_tool.ui.time_range_control import TimeRangeControl


class PlaybackView(QWidget):
    """离线 .sdb v2 回放视图。"""

    status_message = Signal(str, int)

    def __init__(self, parent: Optional[QWidget] = None, settings=None):
        super().__init__(parent)
        self._theme = "dark"
        self._settings = settings   # 可选；用于读取 paths.recording_dir 作为打开默认目录

        # ---------- 独立的数据/profile 三件套 ----------
        self._data_store = DataStore(buffer_capacity=None)   # 无界，保留全部
        self._profile_store = ProfileStore(cache=None)        # 不写磁盘
        self._state_store = StateStore()
        self._event_log = EventLog()
        self._gnss_store = GnssStore(keep_history=True, parent=self)
        self._event_log.event_added.connect(self._on_event_added_for_chart)
        self._event_log.event_added.connect(self._on_event_added_for_map)

        # ---------- 元信息：当前文件 / 时间范围 ----------
        self._current_file: Optional[Path] = None
        self._first_ts_ms: Optional[float] = None   # 第一帧时间戳
        self._last_ts_ms: Optional[float] = None
        self._total_sec: float = 0.0
        self._loaded_count: int = 0

        # ---------- M8: 地图浮窗（懒加载） ----------
        self._map_widget = None
        self._map_dock: Optional[QDockWidget] = None
        self._gnss_widget = None
        self._gnss_dock: Optional[QDockWidget] = None
        self._gps_lat_id: Optional[int] = None
        self._gps_lon_id: Optional[int] = None

        self._setup_ui()
        self._apply_theme()

    # ============================ UI ============================

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(3)

        # ---------- 顶部一行：Open + 文件名 + 总时长 + TimeRange ----------
        topbar = QWidget()
        top_layout = QHBoxLayout(topbar)
        top_layout.setContentsMargins(4, 2, 4, 2)
        top_layout.setSpacing(6)

        self._open_btn = QPushButton("Open .sdb")
        self._open_btn.setFixedSize(100, 28)
        self._open_btn.setToolTip("打开 .sdb v2 文件（含 profile 自动恢复）")
        self._open_btn.clicked.connect(self._on_open_clicked)
        top_layout.addWidget(self._open_btn)

        # M9：清除按钮
        self._clear_btn = QPushButton("清除")
        self._clear_btn.setFixedSize(60, 28)
        self._clear_btn.setEnabled(False)
        self._clear_btn.setToolTip("清空当前回放数据 + 曲线 + 事件 + 地图轨迹（释放内存）")
        self._clear_btn.clicked.connect(self._on_clear_clicked)
        top_layout.addWidget(self._clear_btn)

        self._file_label = QLabel("（未加载文件）")
        self._file_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        top_layout.addWidget(self._file_label)

        self._total_label = QLabel("时长: —")
        self._total_label.setFixedWidth(140)
        top_layout.addWidget(self._total_label)

        self._range_ctl = TimeRangeControl()
        self._range_ctl.range_changed.connect(self._on_range_changed)
        top_layout.addWidget(self._range_ctl)

        # M8: "地图"按钮 — 检测到 GPS channel 时启用
        self._map_btn = QPushButton("地图")
        self._map_btn.setFixedSize(60, 28)
        self._map_btn.setEnabled(False)
        self._map_btn.setToolTip("打开/关闭离线地图浮窗（需要 gps_lat / gps_lon channel）")
        self._map_btn.clicked.connect(self._toggle_map)
        top_layout.addWidget(self._map_btn)

        self._gnss_btn = QPushButton("GNSS")
        self._gnss_btn.setFixedSize(64, 28)
        self._gnss_btn.setEnabled(False)
        self._gnss_btn.setToolTip("打开天空图与逐频点 C/N₀ 快照回放")
        self._gnss_btn.clicked.connect(self._toggle_gnss)
        top_layout.addWidget(self._gnss_btn)

        root.addWidget(topbar)

        # ---------- 主区：chart | (dashboard + event_timeline) ----------
        splitter = QSplitter(Qt.Horizontal)

        self._chart = GroupedChartWidget()
        self._chart.setMinimumHeight(400)
        self._chart.set_dark_theme(True)
        self._chart.set_profile_store(self._profile_store)
        self._chart.set_settings(self._settings)   # M10 P7
        # M8：单图/分组切换后重新 refresh，避免曲线消失
        self._chart.mode_changed.connect(self._on_chart_mode_changed)
        splitter.addWidget(self._chart)

        right_panel = QSplitter(Qt.Vertical)
        right_panel.setMinimumWidth(260)
        self._dashboard = DashboardWidget(self._profile_store, self._state_store)
        right_panel.addWidget(self._dashboard)
        self._event_timeline = EventTimelineWidget(self._event_log)
        self._event_timeline.jump_requested.connect(self._chart.jump_to_timestamp)
        right_panel.addWidget(self._event_timeline)
        right_panel.setStretchFactor(0, 1)
        right_panel.setStretchFactor(1, 1)
        splitter.addWidget(right_panel)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    # ============================ 主题 ============================

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
        self._apply_theme()
        # 广播到子 widget
        widgets = [self._chart, self._dashboard, self._event_timeline, self._range_ctl]
        if self._map_widget is not None:
            widgets.append(self._map_widget)
        if self._gnss_widget is not None:
            widgets.append(self._gnss_widget)
        for w in widgets:
            if hasattr(w, "set_theme"):
                w.set_theme(theme, scale)
            elif hasattr(w, "set_dark_theme"):
                w.set_dark_theme(theme != "light")

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light")

    def _apply_theme(self):
        p = S.palette(self._theme)
        self.setStyleSheet(f"PlaybackView {{ background-color: {p['bg']}; color: {p['text']}; }}")
        self._file_label.setStyleSheet(
            f"color: {p['text_muted']}; padding: 0 8px; background: transparent;"
        )
        self._total_label.setStyleSheet(
            f"color: {p['text']}; background: transparent;"
        )
        self._open_btn.setStyleSheet(
            f"background-color: {p['primary']}; color: white; "
            f"border: none; padding: 4px 8px; border-radius: 2px;"
        )

    # ============================ 业务 ============================

    def _on_open_clicked(self):
        last_dir = ""
        if self._settings is not None:
            last_dir = self._settings.get("paths.recording_dir", "") or ""
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Open SDB v2 Recording", last_dir,
            "SDB Files (*.sdb);;All Files (*)",
        )
        if not filepath:
            return
        self._load_file(Path(filepath))

    def _load_file(self, path: Path) -> None:
        """阻塞解析（一次性读全文件 → SdbFile.iter_records）。"""
        self.status_message.emit(f"Loading {path.name}...", 0)
        try:
            sdb = DataImporter.open_sdb(path)
        except Exception as exc:
            self.status_message.emit(f"Failed to open: {exc}", 5000)
            return

        # 1) 清空所有现有数据 + profile
        self._data_store.clear()
        self._event_log.clear()
        self._state_store.clear()
        self._gnss_store.clear()
        self._first_ts_ms = None
        self._last_ts_ms = None
        self._loaded_count = 0
        # 2) 恢复 profile（如有）
        hw = self._profile_store.current_hw_type()
        if sdb.profile is not None:
            new_hw = self._profile_store.import_dict(sdb.profile)
            if new_hw is not None:
                hw = new_hw
                self._chart.set_hw_type(hw)
                self._dashboard.set_hw_type(hw)

        # 3) 灌入帧
        for rec in sdb.iter_records():
            if isinstance(rec, DataReport):
                self._data_store.update(rec)
                self._loaded_count += 1
                ts = float(rec.timestamp)
                if self._first_ts_ms is None or ts < self._first_ts_ms:
                    self._first_ts_ms = ts
                if self._last_ts_ms is None or ts > self._last_ts_ms:
                    self._last_ts_ms = ts
            elif hw is not None and isinstance(rec, StateReport):
                self._state_store.update(hw, rec)
            elif hw is not None and isinstance(rec, EventReport):
                self._event_log.add(hw, rec, self._profile_store)
            elif isinstance(rec, (GnssSkyReport, GnssCnrReport, GnssSatReport, GnssSignalReport)):
                self._gnss_store.update(rec)
                ts = float(rec.timestamp)
                if self._first_ts_ms is None or ts < self._first_ts_ms:
                    self._first_ts_ms = ts
                if self._last_ts_ms is None or ts > self._last_ts_ms:
                    self._last_ts_ms = ts

        # 4) 计算总时长 + 推到 TimeRangeControl
        if self._first_ts_ms is not None and self._last_ts_ms is not None:
            self._total_sec = max(0.0, (self._last_ts_ms - self._first_ts_ms) / 1000.0)
        else:
            self._total_sec = 0.0
        self._range_ctl.set_total(self._total_sec)

        # 5) 推到 chart：先 auto_range 让初始视图显示全部
        self._chart.set_auto_range(True)
        self._chart.refresh(self._data_store)
        self._dashboard.refresh(self._data_store)
        # 之后 _on_range_changed 会切回手动模式（用户选预设/自定义时）

        # 6) UI 元信息
        self._current_file = path
        self._file_label.setText(f"📄 {path.name}")
        self._total_label.setText(
            f"时长: {self._total_sec:.1f}s · {self._loaded_count} 帧"
        )
        mb_est = self._loaded_count * 8 * 12 / (1024 * 1024)   # 粗估 8ch × 12B/sample
        self.status_message.emit(
            f"Loaded {self._loaded_count} DataReport(s) over {self._total_sec:.1f}s "
            f"(~{mb_est:.1f} MB)",
            5000,
        )

        # M8: GPS 检测 → 启用/禁用地图按钮；已开的地图也同步刷新
        # 注意：profile_store.import_dict 不会自动 set current_hw_type，
        # 所以必须传 hw 进来；hw 是本方法上面解析到的 hw_type
        gps_ok = self._detect_gps_channels(hw)
        self._map_btn.setEnabled(gps_ok)
        self._gnss_btn.setEnabled(self._gnss_store.has_data())
        if self._map_widget is not None:
            self._refresh_map_track()
            self._refresh_map_events()

        # M9: 启用"清除"按钮
        self._clear_btn.setEnabled(True)

    def _on_range_changed(self, start_sec: float, end_sec: float) -> None:
        """TimeRangeControl 选择新范围。"""
        # 关闭自动范围，使用显式 X 视窗
        self._chart.set_auto_range(False)
        self._chart.set_x_range_sec(start_sec, end_sec)
        # M8: 地图轨迹高亮（sec 相对 chart 时间原点 → 绝对 ms）
        if self._map_widget is not None and self._first_ts_ms is not None:
            start_ms = self._first_ts_ms + start_sec * 1000.0
            end_ms = self._first_ts_ms + end_sec * 1000.0
            self._map_widget.set_track_highlight(start_ms, end_ms)

    def _on_event_added_for_chart(self, record) -> None:
        hw = self._profile_store.current_hw_type()
        if hw is None or record.hw_type != hw:
            return
        self._chart.add_event_marker(
            record.timestamp_ms, record.level,
            name=record.name, event_id=record.event_id,
        )

    def _on_chart_mode_changed(self, mode: str) -> None:
        """单图 / 分组 切换后重灌数据 + 事件竖线 + 范围保持。"""
        if self._loaded_count == 0:
            return
        self._chart.refresh(self._data_store)
        # 回放路径 profile display_min/max 一般是合理的，不主动 autorange
        # 但如果用户切换后 Y 表现异常可手动开下面这行：
        # self._chart.enable_y_autorange(True)
        # 重新画事件竖线（_clear_plots 已清掉旧的）
        for rec in self._event_log.all():
            self._chart.add_event_marker(
                rec.timestamp_ms, rec.level,
                name=rec.name, event_id=rec.event_id,
            )

    def _on_clear_clicked(self) -> None:
        """M9：清空所有回放数据 + UI 状态，释放内存。"""
        # 1) 数据三件套
        self._data_store.clear()
        self._event_log.clear()
        self._state_store.clear()
        self._gnss_store.clear()
        # 2) 元信息
        self._current_file = None
        self._first_ts_ms = None
        self._last_ts_ms = None
        self._total_sec = 0.0
        self._loaded_count = 0
        self._gps_lat_id = None
        self._gps_lon_id = None
        # 3) 曲线 + 事件竖线
        self._chart.clear()
        # 4) Dashboard / EventTimeline 刷新（_event_log 已清）
        self._dashboard.refresh(self._data_store)
        if hasattr(self._event_timeline, "_list"):
            self._event_timeline._list.clear()
            if hasattr(self._event_timeline, "_update_count"):
                self._event_timeline._update_count()
        # 5) 地图
        if self._map_widget is not None:
            self._map_widget.clear()
        self._map_btn.setEnabled(False)
        self._gnss_btn.setEnabled(False)
        # 6) UI 标签
        self._file_label.setText("（未加载文件）")
        self._total_label.setText("时长: —")
        self._range_ctl.set_total(0.0)
        # 7) 自身按钮
        self._clear_btn.setEnabled(False)
        self.status_message.emit("回放数据已清除", 2000)

    def _toggle_gnss(self) -> None:
        if self._gnss_dock is None:
            from satellite_debug_tool.ui.gnss_widget import GnssWidget
            self._gnss_widget = GnssWidget(self._gnss_store, playback=True)
            self._gnss_widget.set_theme(self._theme)
            self._gnss_dock = QDockWidget("GNSS 天空图与逐频点 C/N₀ — Playback", self)
            self._gnss_dock.setAllowedAreas(Qt.NoDockWidgetArea)
            self._gnss_dock.setFloating(True)
            self._gnss_dock.setWidget(self._gnss_widget)
            self._gnss_dock.resize(1180, 730)
        self._gnss_dock.setVisible(not self._gnss_dock.isVisible())

    # ====================== M8: 地图集成 ======================

    def _detect_gps_channels(self, hw_type: Optional[str] = None) -> bool:
        """在 profile 中查 gps_lat / gps_lon channel；返回 True 表示找到。

        Args:
            hw_type: 显式指定 hw_type；None 时回退到 current_hw_type 或
                首个已知 profile（import_dict 不会自动 set current_hw_type，
                所以加载完 .sdb 后调用方应传 hw 进来）。
        """
        self._gps_lat_id = None
        self._gps_lon_id = None
        hw = hw_type or self._profile_store.current_hw_type()
        if hw is None:
            # 兜底：取 ProfileStore 里第一个已知 hw_type
            for candidate in ("windterm_log",):  # 占位（不会用到）
                if self._profile_store.has_profile(candidate):
                    hw = candidate
                    break
            if hw is None:
                # 最后一次尝试：扫所有已注册 profile
                profiles = getattr(self._profile_store, "_profiles", {})
                if profiles:
                    hw = next(iter(profiles))
        if hw is None:
            return False
        lat = self._profile_store.find_channel_by_role(hw, CHANNEL_ROLE_GPS_LAT)
        lon = self._profile_store.find_channel_by_role(hw, CHANNEL_ROLE_GPS_LON)
        if lat is not None:
            self._gps_lat_id = lat.channel_id
        if lon is not None:
            self._gps_lon_id = lon.channel_id
        return self._gps_lat_id is not None and self._gps_lon_id is not None

    def _toggle_map(self) -> None:
        if self._map_dock is None:
            self._build_map_dock()
            self._refresh_map_track()
            self._refresh_map_events()
        self._map_dock.setVisible(not self._map_dock.isVisible())

    def _build_map_dock(self) -> None:
        from satellite_debug_tool.ui.map_widget import MapWidget
        token = ""
        if self._settings is not None:
            token = self._settings.get("map.tianditu_token", "") or ""
        self._map_widget = MapWidget(tianditu_token=token)
        self._map_widget.set_theme(self._theme, "small")
        self._map_dock = QDockWidget("地图 — 回放", self)
        self._map_dock.setAllowedAreas(Qt.NoDockWidgetArea)
        self._map_dock.setFloating(True)
        self._map_dock.setWidget(self._map_widget)
        self._map_dock.resize(800, 600)

    def _refresh_map_track(self) -> None:
        """从 DataStore 拉 gps_lat / gps_lon 灌到 MapWidget。"""
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
        lon_times = lon_buf.get_times()
        lon_vals = lon_buf.get_values()
        if ts.size == 0 or lon_times.size == 0:
            self._map_widget.clear()
            return
        # lat / lon 一般同步上报（都来自同一 DataReport），但稳妥起见用 lat 时间戳
        # 在 lon 时间序列上做线性内插
        lons = np.interp(ts, lon_times, lon_vals)
        self._map_widget.clear()
        self._map_widget.set_track(ts, lats, lons)

    def _refresh_map_events(self) -> None:
        """灌当前已积累的所有事件到地图。"""
        if self._map_widget is None or self._gps_lat_id is None:
            return
        for rec in self._event_log.all():
            self._add_event_to_map(rec)

    def _on_event_added_for_map(self, record) -> None:
        """事件实时到达时往地图加 marker（仅在地图已构建且检测到 GPS 时）。"""
        if self._map_widget is None or self._gps_lat_id is None:
            return
        hw = self._profile_store.current_hw_type()
        if hw is None or record.hw_type != hw:
            return
        self._add_event_to_map(record)

    def _add_event_to_map(self, record) -> None:
        lat_buf = self._data_store.get_channel_by_id(self._gps_lat_id)
        lon_buf = self._data_store.get_channel_by_id(self._gps_lon_id)
        if lat_buf is None or lon_buf is None:
            return
        lat_times = lat_buf.get_times()
        lat_vals = lat_buf.get_values()
        lon_times = lon_buf.get_times()
        lon_vals = lon_buf.get_values()
        if lat_times.size == 0 or lon_times.size == 0:
            return
        # 线性内插得到事件时刻的 lat/lon（端点外推按端点值）
        lat = float(np.interp(record.timestamp_ms, lat_times, lat_vals))
        lon = float(np.interp(record.timestamp_ms, lon_times, lon_vals))
        self._map_widget.add_event(
            record.timestamp_ms, record.name, record.level, lat, lon
        )
