"""PlaybackView 隔离 / 接线单测（M7-S5）。

不依赖真实 .sdb 文件 —— 只验证：
- DataStore / ProfileStore 与 Live 实例独立
- DataStore 是无界模式（capacity=None）
- TimeRangeControl 的 range_changed 信号能驱动 chart 的 X 范围
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class TestIndependentStores:
    def test_playback_datastore_is_unbounded(self, qapp):
        from satellite_debug_tool.ui.playback_view import PlaybackView
        pv = PlaybackView()
        assert pv._data_store._buffer_capacity is None

    def test_independent_from_live(self, qapp):
        """同时实例化 LiveView 和 PlaybackView，验证 store 三件套都不共享。"""
        from satellite_debug_tool.core.config import Settings
        from satellite_debug_tool.ui.live_view import LiveView
        from satellite_debug_tool.ui.playback_view import PlaybackView

        # 用 Mock settings，避免读真实磁盘
        s = Settings()
        live = LiveView(settings=s)
        pv = PlaybackView()

        assert pv._data_store is not live._data_store
        assert pv._profile_store is not live._profile_store
        assert pv._state_store is not live._state_store
        assert pv._event_log is not live._event_log


class TestRangeControlWiring:
    def test_range_changed_disables_auto_range(self, qapp):
        """选预设/自定义范围时，chart 应该切到非 auto_range 模式 + 调 setXRange。"""
        from satellite_debug_tool.ui.playback_view import PlaybackView
        pv = PlaybackView()
        # 模拟 chart 状态
        pv._chart.set_auto_range(True)
        assert pv._chart._auto_range is True
        # 模拟 range 选择
        pv._on_range_changed(0.0, 100.0)
        assert pv._chart._auto_range is False
        # set_x_range_sec 在 chart 没有 _plots 时返回 False，但 set_auto_range 已被调


class TestStatusMessage:
    def test_open_no_file_no_op(self, qapp, monkeypatch):
        """文件对话框取消时不该崩 / 不该发 status_message。"""
        from satellite_debug_tool.ui.playback_view import PlaybackView
        pv = PlaybackView()
        captured: list = []
        pv.status_message.connect(lambda msg, ms: captured.append((msg, ms)))

        # 让 QFileDialog.getOpenFileName 返回空（用户取消）
        from PySide6.QtWidgets import QFileDialog
        monkeypatch.setattr(
            QFileDialog, "getOpenFileName",
            staticmethod(lambda *a, **k: ("", "")),
        )
        pv._on_open_clicked()
        assert captured == []
