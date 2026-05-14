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

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.data import DataStore, EventLog, StateStore
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import DataReport, EventReport, StateReport
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.dashboard_widget import DashboardWidget
from satellite_debug_tool.ui.event_timeline_widget import EventTimelineWidget
from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget
from satellite_debug_tool.ui.time_range_control import TimeRangeControl


class PlaybackView(QWidget):
    """离线 .sdb v2 回放视图。"""

    status_message = Signal(str, int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme = "dark"

        # ---------- 独立的数据/profile 三件套 ----------
        self._data_store = DataStore(buffer_capacity=None)   # 无界，保留全部
        self._profile_store = ProfileStore(cache=None)        # 不写磁盘
        self._state_store = StateStore()
        self._event_log = EventLog()
        self._event_log.event_added.connect(self._on_event_added_for_chart)

        # ---------- 元信息：当前文件 / 时间范围 ----------
        self._current_file: Optional[Path] = None
        self._first_ts_ms: Optional[float] = None   # 第一帧时间戳
        self._last_ts_ms: Optional[float] = None
        self._total_sec: float = 0.0
        self._loaded_count: int = 0

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

        self._file_label = QLabel("（未加载文件）")
        self._file_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        top_layout.addWidget(self._file_label)

        self._total_label = QLabel("时长: —")
        self._total_label.setFixedWidth(140)
        top_layout.addWidget(self._total_label)

        self._range_ctl = TimeRangeControl()
        self._range_ctl.range_changed.connect(self._on_range_changed)
        top_layout.addWidget(self._range_ctl)

        root.addWidget(topbar)

        # ---------- 主区：chart | (dashboard + event_timeline) ----------
        splitter = QSplitter(Qt.Horizontal)

        self._chart = GroupedChartWidget()
        self._chart.setMinimumHeight(400)
        self._chart.set_dark_theme(True)
        self._chart.set_profile_store(self._profile_store)
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
        for w in (self._chart, self._dashboard, self._event_timeline, self._range_ctl):
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
        self._total_label.setStyleSheet(
            f"color: {p['text']}; background: transparent;"
        )
        self._open_btn.setStyleSheet(
            f"background-color: {p['primary']}; color: white; "
            f"border: none; padding: 4px 8px; border-radius: 2px;"
        )

    # ============================ 业务 ============================

    def _on_open_clicked(self):
        last_dir = ""   # 后续可以接 settings 持久化（M7 plan §4.7 已列）
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
        self._first_ts_ms = None
        self._last_ts_ms = None
        self._loaded_count = 0
        # StateStore 没有 clear_all，按 hw_type 清；下面 import_dict 之后重置
        # （这里不主动清，新 profile 重建时 state_panel 会自己刷新）

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

    def _on_range_changed(self, start_sec: float, end_sec: float) -> None:
        """TimeRangeControl 选择新范围。"""
        # 关闭自动范围，使用显式 X 视窗
        self._chart.set_auto_range(False)
        self._chart.set_x_range_sec(start_sec, end_sec)

    def _on_event_added_for_chart(self, record) -> None:
        hw = self._profile_store.current_hw_type()
        if hw is None or record.hw_type != hw:
            return
        self._chart.add_event_marker(
            record.timestamp_ms, record.level,
            name=record.name, event_id=record.event_id,
        )
