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


class TestGpsDetectionAndMap:
    """M8: GPS channel 检测启用地图按钮。"""

    def test_no_profile_disables_map_button(self, qapp):
        from satellite_debug_tool.ui.playback_view import PlaybackView
        pv = PlaybackView()
        # 没有 profile / 未加载文件 → 按钮 disabled
        assert pv._map_btn.isEnabled() is False
        # _detect_gps_channels 也应返回 False
        assert pv._detect_gps_channels() is False

    def test_profile_with_gps_enables_button(self, qapp):
        """构造一个含 gps_lat / gps_lon 的虚拟 profile，检测应通过。"""
        from satellite_debug_tool.ui.playback_view import PlaybackView
        pv = PlaybackView()
        profile_dict = {
            "schema_version": 1,
            "hw_type": "test_hw",
            "channel_table_ver": 1,
            "state_table_ver": 0,
            "event_table_ver": 0,
            "channels": [
                {"channel_id": 0, "data_type": 0, "group_id": 4, "flags": 0,
                 "name": "gps_lat", "unit": "°",
                 "display_min": -90.0, "display_max": 90.0},
                {"channel_id": 1, "data_type": 0, "group_id": 4, "flags": 0,
                 "name": "gps_lon", "unit": "°",
                 "display_min": -180.0, "display_max": 180.0},
                {"channel_id": 2, "data_type": 0, "group_id": 0, "flags": 0,
                 "name": "yaw", "unit": "°",
                 "display_min": 0.0, "display_max": 360.0},
            ],
            "states": [],
            "events": [],
            "meta": None,
        }
        pv._profile_store.import_dict(profile_dict)
        assert pv._detect_gps_channels() is True
        assert pv._gps_lat_id == 0
        assert pv._gps_lon_id == 1

    def test_profile_without_gps_returns_false(self, qapp):
        from satellite_debug_tool.ui.playback_view import PlaybackView
        pv = PlaybackView()
        profile_dict = {
            "schema_version": 1,
            "hw_type": "test_hw",
            "channel_table_ver": 1,
            "state_table_ver": 0,
            "event_table_ver": 0,
            "channels": [
                {"channel_id": 0, "data_type": 0, "group_id": 0, "flags": 0,
                 "name": "yaw", "unit": "°",
                 "display_min": 0.0, "display_max": 360.0},
            ],
            "states": [],
            "events": [],
            "meta": None,
        }
        pv._profile_store.import_dict(profile_dict)
        assert pv._detect_gps_channels() is False

    def test_profile_with_gps_roles_enables_button(self, qapp):
        """通道名不叫 gps_lat/gps_lon，但 schema v2 role 正确时应启用地图。"""
        from satellite_debug_tool.ui.playback_view import PlaybackView
        pv = PlaybackView()
        profile_dict = {
            "schema_version": 2,
            "hw_type": "test_hw",
            "channel_table_ver": 1,
            "state_table_ver": 0,
            "event_table_ver": 0,
            "semantics_table_ver": 1,
            "channels": [
                {"channel_id": 10, "data_type": 0, "group_id": 4, "flags": 0,
                 "name": "lat_deg", "unit": "°",
                 "display_min": -90.0, "display_max": 90.0},
                {"channel_id": 11, "data_type": 0, "group_id": 4, "flags": 0,
                 "name": "lon_deg", "unit": "°",
                 "display_min": -180.0, "display_max": 180.0},
            ],
            "states": [],
            "events": [],
            "semantics": {
                "channels": {
                    "10": {"roles": ["gps_lat"]},
                    "11": {"roles": ["gps_lon"]},
                },
                "states": {},
                "capabilities": {},
            },
            "meta": None,
        }
        pv._profile_store.import_dict(profile_dict)
        assert pv._detect_gps_channels("test_hw") is True
        assert pv._gps_lat_id == 10
        assert pv._gps_lon_id == 11


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


class TestLegacyProfilePlayback:
    def test_embedded_legacy_gps_fix_value_three_stays_rtk(self, qapp, tmp_path):
        """旧 SDB 必须按其内嵌旧枚举解释，不能套用当前 GPS_FIX 表。"""
        from satellite_debug_tool.core.protocol import CmdType, build_frame
        from satellite_debug_tool.io.data_recorder import DataRecorder
        from satellite_debug_tool.ui.playback_view import PlaybackView

        legacy_profile = {
            "schema_version": 1,
            "hw_type": "afd01",
            "channel_table_ver": 1,
            "state_table_ver": 1,
            "event_table_ver": 1,
            "channels": [],
            "states": [
                {
                    "state_id": 2,
                    "state_type": 1,
                    "flags": 1,
                    "name": "GPS_FIX",
                    "enums": [
                        {"value": 0, "level": 2, "name": "NO_FIX"},
                        {"value": 1, "level": 0, "name": "2D"},
                        {"value": 2, "level": 0, "name": "3D"},
                        {"value": 3, "level": 0, "name": "RTK"},
                    ],
                }
            ],
            "events": [],
            "meta": None,
        }
        state_payload = (100).to_bytes(4, "little") + bytes([1, 2, 3])
        path = tmp_path / "legacy-gps-fix.sdb"
        recorder = DataRecorder(path, profile_dict=legacy_profile)
        assert recorder.start()
        assert recorder.write_frame(build_frame(CmdType.STATE_REPORT, state_payload))
        assert recorder.stop()

        view = PlaybackView()
        view._load_file(path)

        state = view._profile_store.get_state("afd01", 2)
        assert state is not None
        assert next(item.name for item in state.enums if item.value == 3) == "RTK"
        assert view._state_store.get_value("afd01", 2) == 3
        assert view._dashboard._status_chips[2]._value_label.text() == "RTK"
        view.close()
