"""批量 A: 体验增强小项的单元测试。

覆盖：
- A4 子系统分类 `_classify_subsystem`（不依赖 Qt）
- A1/A2 GroupedChartWidget.jump_to_timestamp 语义
- A5 _ChannelEnableDialog.result_mask 位掩码
- A6 MainWindow._attitude_setting_key hw 分桶格式
"""
from __future__ import annotations

import pytest


# --------- A4: 子系统分类 ---------

class TestSubsystemClassify:
    def test_rules(self):
        from satellite_debug_tool.ui.state_panel_widget import _classify_subsystem

        cases = {
            "TRACE_MODE":       "trace",
            "LOCK_FLAG":        "trace",
            "MODEM_CONNECTED":  "modem",
            "SNR_LOCKED":       "modem",
            "BEACON_LOCKED":    "modem",
            "INS_READY":        "ins",
            "IMU_HEALTHY":      "ins",
            "GPS_3D":           "ins",
            "PLL_LOCKED":       "rf",
            "LO_READY":         "rf",
            "BUC_READY":        "rf",
            "LNB_OK":           "rf",
            "POLARIZATION":     "rf",
            "SOMETHING_ELSE":   "general",
            "CUSTOM_FLAG":      "general",
        }
        for name, expect in cases.items():
            assert _classify_subsystem(name) == expect, (
                f"{name} → got {_classify_subsystem(name)}, want {expect}"
            )

    def test_case_insensitive(self):
        from satellite_debug_tool.ui.state_panel_widget import _classify_subsystem
        assert _classify_subsystem("trace_mode") == "trace"
        assert _classify_subsystem("modem_x") == "modem"

    def test_order_constant_coverage(self):
        """所有子系统 key 都应该在 _SUBSYSTEM_ORDER 里注册。"""
        from satellite_debug_tool.ui.state_panel_widget import (
            _SUBSYSTEM_LABELS, _SUBSYSTEM_ORDER,
        )
        assert set(_SUBSYSTEM_LABELS.keys()) == set(_SUBSYSTEM_ORDER)
        # 至少包含 general 兜底
        assert "general" in _SUBSYSTEM_ORDER


class TestStateItemRowLifecycle:
    def test_highlight_timer_is_owned_by_row(self, qapp):
        from satellite_debug_tool.core.protocol import StateDefEntry, StateType
        from satellite_debug_tool.ui.state_panel_widget import StateItemRow

        row = StateItemRow(
            StateDefEntry(
                state_id=1,
                state_type=int(StateType.BOOL),
                flags=0,
                name="LOCK_FLAG",
                enums=[],
            )
        )

        row.flash_highlight()

        assert row._highlight_timer.parent() is row
        assert row._highlight_timer.isSingleShot()
        assert row._highlight_timer.isActive()


# --------- A1/A2: Chart jump_to_timestamp ---------
#
# 需要 Qt 环境；若 Qt 不可用就 skip（CI 友好）

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class TestChartJumpToTimestamp:
    def test_no_data_returns_false(self, qapp):
        from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget
        w = GroupedChartWidget()
        # 还没 rebuild / 没 profile / 没数据 → 应返回 False，不崩
        assert w.jump_to_timestamp(12345) is False

    def test_valid_jump_sets_xrange(self, qapp):
        """模拟 profile + 一条曲线 + 时间原点，测 setXRange 被正确调用。"""
        import numpy as np
        import pyqtgraph as pg
        from satellite_debug_tool.ui.grouped_chart_widget import GroupedChartWidget

        w = GroupedChartWidget()
        # 直接构造一个最小的 plot + 时间原点，跳过 rebuild 流程
        plot = w._gl.addPlot(row=0, col=0)
        w._plots[0] = plot
        w._x_origin_ms = 1000.0
        w._time_window = 60.0
        ok = w.jump_to_timestamp(61000, window_sec=60.0)
        assert ok is True
        # X 视窗中心应为 (61000 - 1000)/1000 = 60s
        xmin, xmax = plot.viewRange()[0]
        assert abs((xmin + xmax) / 2 - 60.0) < 0.01
        # 窗宽应等于参数
        assert abs((xmax - xmin) - 60.0) < 0.01


# --------- A5: ChannelEnableDialog 掩码拼装 ---------

class TestChannelEnableMask:
    def test_mask_from_checks(self, qapp):
        from satellite_debug_tool.ui.control_panel_widget import _ChannelEnableDialog
        from satellite_debug_tool.core.profile import ProfileStore
        from PySide6.QtWidgets import QCheckBox

        dlg = _ChannelEnableDialog(ProfileStore(), None, 0)
        # 手动注入 3 个通道的 checkbox，勾上 0 和 5
        dlg._checks = {}
        for cid, on in [(0, True), (5, True), (7, False)]:
            cb = QCheckBox()
            cb.setChecked(on)
            dlg._checks[cid] = cb
        assert dlg.result_mask() == (1 << 0) | (1 << 5)

    def test_empty_mask(self, qapp):
        from satellite_debug_tool.ui.control_panel_widget import _ChannelEnableDialog
        from satellite_debug_tool.core.profile import ProfileStore

        dlg = _ChannelEnableDialog(ProfileStore(), None, 0)
        assert dlg.result_mask() == 0

    def test_set_all_invert(self, qapp):
        from satellite_debug_tool.ui.control_panel_widget import _ChannelEnableDialog
        from satellite_debug_tool.core.profile import ProfileStore
        from PySide6.QtWidgets import QCheckBox

        dlg = _ChannelEnableDialog(ProfileStore(), None, 0)
        dlg._checks = {}
        for cid in (0, 1, 2, 3):
            dlg._checks[cid] = QCheckBox()
        dlg._set_all(True)
        assert dlg.result_mask() == 0b1111
        dlg._set_all(False)
        assert dlg.result_mask() == 0
        # 反选
        dlg._checks[0].setChecked(True)
        dlg._invert()
        # 之前：0=T, 1/2/3=F → 反选后 0=F, 1/2/3=T
        assert dlg.result_mask() == 0b1110


# 2026-04-21：A6 的 attitude 绑定分桶持久化功能已移除（手动绑定 combo 删除、
# 通道绑定改为完全 profile 驱动自动绑），对应测试类已删除。
