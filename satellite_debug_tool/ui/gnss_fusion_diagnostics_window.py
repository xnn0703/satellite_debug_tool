"""GNSS 实时数据窗：消费 INS TRACE kind2 的实测 P/V/COG 与逐包融合摘要。

数据源是 LiveView.frame_received 分发的 InsTraceRecord；设备需处于
Debug + 完整采集（full capture）模式才有工程流。窗口只读记录、只做
展示，不发送命令、不修改会话状态。schema1（100B）帧只更新实测值，
schema2（164B）帧额外更新融合摘要与时间字段。
"""
from __future__ import annotations

import math
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.i18n import tr
from satellite_debug_tool.core.protocol.frame_v2 import InsTraceRecord

_FIX_NAMES = ("无定位", "2D", "3D", "RTK 固定", "差分", "RTK 浮动", "过期")
_STAGE_NAMES = ("未处理", "健康失效", "时间线拒绝", "已执行准入")
_TIMELINE_REJECT_NAMES = {1: "参数无效", 2: "健康失效", 3: "时间无效", 4: "重复", 5: "乱序", 7: "过旧", 8: "超前"}
_PV_REJECT_NAMES = {1: "定位无效", 2: "精度不足", 3: "过期", 4: "未初始化", 5: "滤波拒绝"}
_GROUP_NAMES = ("HP", "VP", "HV", "VV")
_RECENT_ROWS = 20
_STALE_AFTER_S = 3.0


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(int(fraction * (len(sorted_values) - 1)), len(sorted_values) - 1)
    return sorted_values[index]


def _finite(value: object, digits: int) -> str:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    return f"{number:.{digits}f}" if math.isfinite(number) else "—"


class _ValueCard(QFrame):
    """一组同类实测值的标题卡。"""

    def __init__(self, title: str, rows: int) -> None:
        super().__init__()
        self.setObjectName("gnssValueCard")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QGridLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("gnssValueCardTitle")
        layout.addWidget(title_label, 0, 0, 1, 2)
        self._names: list[QLabel] = []
        self._values: list[QLabel] = []
        for row in range(rows):
            name = QLabel()
            value = QLabel("—")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(name, row + 1, 0)
            layout.addWidget(value, row + 1, 1)
            self._names.append(name)
            self._values.append(value)

    def set_row(self, index: int, name: str, value: str) -> None:
        self._names[index].setText(name)
        self._values[index].setText(value)


class GnssFusionDiagnosticsWindow(QMainWindow):
    """实时显示 GNSS 实测 P/V/COG，底部附逐包融合摘要。"""

    def __init__(self, *, theme: str = "dark", parent: Optional[QWidget] = None) -> None:
        flags = (
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        super().__init__(parent, flags)
        self._samples = 0
        self._stage_counts = [0, 0, 0, 0]
        self._timeline_rejects: dict[int, int] = {}
        self._pv_rejects: dict[int, int] = {}
        self._deltas_ms: list[float] = []
        self._delta_over_gate = 0
        self._group_attempted = [0, 0, 0, 0]
        self._group_accepted = [0, 0, 0, 0]
        self._velocity_valid = 0
        self._first_time_us: Optional[int] = None
        self._last_time_us: Optional[int] = None
        self._last_frame_wall = 0.0
        self._recent: list[InsTraceRecord] = []

        central = QWidget()
        central.setObjectName("gnssFusionWindow")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(7)

        self._status_label = QLabel()
        self._status_label.setObjectName("gnssFusionStatus")
        layout.addWidget(self._status_label)

        cards = QHBoxLayout()
        cards.setSpacing(7)
        self._position_card = _ValueCard(tr("Position"), 5)
        self._velocity_card = _ValueCard(tr("Velocity"), 6)
        self._heading_card = _ValueCard(tr("Course"), 2)
        self._time_card = _ValueCard(tr("Time / packet"), 5)
        for card in (self._position_card, self._velocity_card, self._heading_card, self._time_card):
            cards.addWidget(card, 1)
        layout.addLayout(cards)

        self._stats_label = QLabel()
        self._stats_label.setObjectName("gnssFusionSummary")
        self._stats_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._stats_label)

        self._table = QTableWidget(0, 13)
        self._table.setHorizontalHeaderLabels(
            ["时间(s)", "seq", "fix", "卫星", "lat(°)", "lon(°)", "vN(m/s)", "vE(m/s)", "vD(m/s)",
             "水平(m/s)", "COG(°)", "速度有效", "融合"]
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table, 1)

        button_row = QHBoxLayout()
        self._reset_button = QPushButton(tr("Reset statistics"))
        self._reset_button.clicked.connect(self.reset_statistics)
        button_row.addStretch(1)
        button_row.addWidget(self._reset_button)
        layout.addLayout(button_row)

        self.setCentralWidget(central)
        self.resize(980, 520)
        self.setWindowTitle(tr("GNSS live data"))

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(500)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start()
        self._refresh()

    def feed_record(self, record: object) -> None:
        """消费一条解码记录；kind2 实测值入表，schema2 帧再累计融合摘要。"""
        if not isinstance(record, InsTraceRecord) or record.kind != 2:
            return
        values = record.values
        if record.schema == 2:
            stage = int(values.get("processing_stage", 0))
            if not 0 <= stage <= 3:
                return
            self._samples += 1
            self._stage_counts[stage] += 1
            if record.flags & 0x4:
                self._velocity_valid += 1
            if stage in (2, 3):
                reject = int(values.get("timeline_reject", 0))
                if reject != 0:
                    self._timeline_rejects[reject] = self._timeline_rejects.get(reject, 0) + 1
            if stage == 3:
                pre_filter_us = int(values.get("pre_filter_state_us", 0))
                if record.timestamp_us and pre_filter_us:
                    delta_ms = (record.timestamp_us - pre_filter_us) / 1000.0
                    self._deltas_ms.append(delta_ms)
                    if abs(delta_ms) > 8.0:
                        self._delta_over_gate += 1
                pv_reject = int(values.get("pv_reject", 0))
                if pv_reject != 0:
                    self._pv_rejects[pv_reject] = self._pv_rejects.get(pv_reject, 0) + 1
                attempted = int(values.get("group_attempted_mask", 0))
                accepted = int(values.get("group_accepted_mask", 0))
                for index in range(4):
                    if attempted & (1 << index):
                        self._group_attempted[index] += 1
                        if accepted & (1 << index):
                            self._group_accepted[index] += 1
        self._last_frame_wall = time.monotonic()
        self._first_time_us = self._first_time_us if self._first_time_us is not None else record.timestamp_us
        self._last_time_us = record.timestamp_us
        self._recent.append(record)
        if len(self._recent) > _RECENT_ROWS:
            del self._recent[:-_RECENT_ROWS]

    def reset_statistics(self) -> None:
        self._samples = 0
        self._stage_counts = [0, 0, 0, 0]
        self._timeline_rejects.clear()
        self._pv_rejects.clear()
        self._deltas_ms.clear()
        self._delta_over_gate = 0
        self._group_attempted = [0, 0, 0, 0]
        self._group_accepted = [0, 0, 0, 0]
        self._velocity_valid = 0
        self._first_time_us = None
        self._last_time_us = None
        self._recent.clear()
        self._refresh()

    @Slot()
    def _refresh(self) -> None:
        if not self._recent or time.monotonic() - self._last_frame_wall > _STALE_AFTER_S:
            self._status_label.setText(
                tr("Waiting for engineering stream: enable Debug and full capture (recording).")
            )
        else:
            self._status_label.setText(tr("Engineering stream active"))

        if not self._recent:
            self._stats_label.setText("—")
            self._table.setRowCount(0)
            return

        record = self._recent[-1]
        values = record.values
        fix_class = int(values.get("fix_class", 0))
        self._position_card.set_row(0, "定位", _FIX_NAMES[fix_class] if fix_class < len(_FIX_NAMES) else str(fix_class))
        self._position_card.set_row(1, "卫星", f"{values.get('num_svs', '—')}")
        self._position_card.set_row(2, "纬度", _finite(values.get("lat_deg"), 7))
        self._position_card.set_row(3, "经度", _finite(values.get("lon_deg"), 7))
        self._position_card.set_row(
            4, "高程/标准差", f"{_finite(values.get('alt_m'), 1)} m  H {_finite(values.get('pos_h_std_m'), 2)}"
            f"  V {_finite(values.get('pos_v_std_m'), 2)}")

        velocity_valid = bool(record.flags & 0x4)
        self._velocity_card.set_row(0, "有效性", "有效" if velocity_valid else "无效")
        self._velocity_card.set_row(1, "北向", f"{_finite(values.get('vel_n_ms'), 3)} m/s")
        self._velocity_card.set_row(2, "东向", f"{_finite(values.get('vel_e_ms'), 3)} m/s")
        self._velocity_card.set_row(3, "垂向", f"{_finite(values.get('vel_d_ms'), 3)} m/s")
        self._velocity_card.set_row(4, "水平", f"{_finite(values.get('speed_ms'), 3)} m/s")
        self._velocity_card.set_row(
            5, "标准差", f"H {_finite(values.get('vel_h_std_ms'), 3)}  V {_finite(values.get('vel_v_std_ms'), 3)}")

        cog_rad = values.get("cog_rad")
        cog_text = "—" if cog_rad is None else f"{math.degrees(float(cog_rad)):.1f}°"
        cog_std = values.get("cog_std_rad")
        std_text = "—" if cog_std is None else f"{math.degrees(float(cog_std)):.1f}°"
        self._heading_card.set_row(0, "COG", cog_text)
        self._heading_card.set_row(1, "COG 标准差", std_text)

        tow = values.get("gps_tow_s")
        tow_text = "—" if tow is None or not math.isfinite(float(tow)) else f"{int(values.get('gps_week', 0))}/{float(tow):.3f}"
        received_us = int(values.get("driver_received_timestamp_us", 0) or record.received_timestamp_us)
        latency_s = values.get("velocity_latency_s")
        latency_text = "—" if latency_s is None else f"{float(latency_s) * 1000.0:+.1f} ms"
        self._time_card.set_row(0, "GPS 历元", tow_text)
        self._time_card.set_row(1, "名义时刻", f"{record.timestamp_us / 1000.0:.1f} ms")
        self._time_card.set_row(2, "接收时刻", f"{received_us / 1000.0:.1f} ms")
        self._time_card.set_row(3, "速度时延", latency_text)
        self._time_card.set_row(4, "速度类型", f"{values.get('velocity_type', '—')}")

        self._stats_label.setText(self._summary_text())
        self._table.setRowCount(len(self._recent))
        for row, item in enumerate(self._recent):
            item_values = item.values
            stage = int(item_values.get("processing_stage", -1)) if item.schema == 2 else -1
            fusion_text = _STAGE_NAMES[stage] if 0 <= stage <= 3 else "schema1"
            cog = item_values.get("cog_rad")
            entries = (
                f"{item.timestamp_us / 1_000_000.0:.3f}",
                str(item.source_seq),
                _FIX_NAMES[int(item_values.get("fix_class", 0))],
                str(item_values.get("num_svs", "—")),
                _finite(item_values.get("lat_deg"), 6),
                _finite(item_values.get("lon_deg"), 6),
                _finite(item_values.get("vel_n_ms"), 2),
                _finite(item_values.get("vel_e_ms"), 2),
                _finite(item_values.get("vel_d_ms"), 2),
                _finite(item_values.get("speed_ms"), 2),
                "—" if cog is None else f"{math.degrees(float(cog)):.1f}",
                "是" if item.flags & 0x4 else "否",
                fusion_text,
            )
            for column, text in enumerate(entries):
                self._table.setItem(row, column, QTableWidgetItem(text))

    def _summary_text(self) -> str:
        if self._samples == 0:
            return "融合摘要: 暂无 schema2 帧。"
        total = self._samples
        admitted = self._stage_counts[3]
        stages = " | ".join(f"{name} {count}" for name, count in zip(_STAGE_NAMES, self._stage_counts))
        velocity_pct = 100.0 * self._velocity_valid / total
        delta_sorted = sorted(self._deltas_ms)
        if delta_sorted:
            mean = sum(delta_sorted) / len(delta_sorted)
            p95 = _percentile(delta_sorted, 0.95)
            delta_text = f"时间差 mean {mean:+.1f}/p95 {p95:+.1f}ms 超8ms {self._delta_over_gate}"
        else:
            delta_text = "时间差 —"
        timeline_text = "、".join(
            f"{_TIMELINE_REJECT_NAMES.get(key, key)}×{count}"
            for key, count in sorted(self._timeline_rejects.items()))
        pv_text = "、".join(
            f"{_PV_REJECT_NAMES.get(key, key)}×{count}" for key, count in sorted(self._pv_rejects.items()))
        groups = "  ".join(
            f"{name} {100.0 * accepted / attempted:.0f}%" if attempted else f"{name} —"
            for name, attempted, accepted in zip(_GROUP_NAMES, self._group_attempted, self._group_accepted))
        return (
            f"融合摘要: {total} 包  速度有效 {velocity_pct:.1f}%  {stages}（准入 {100.0 * admitted / total:.1f}%）\n"
            f"{delta_text}  时间线拒因: {timeline_text or '—'}  P/V 拒因: {pv_text or '—'}  "
            f"四组: {groups}"
        )
