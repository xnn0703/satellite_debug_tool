"""ChartGroupDialog 单测：分组增删 / 通道移动 / settings 持久化 / 默认恢复。

不弹真窗，全部通过 API 调用 + 直接访问内部状态。
"""
from __future__ import annotations

import os
from typing import List

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from satellite_debug_tool.core.config import Settings
    return Settings()


@pytest.fixture
def profile_store_with_channels(qapp):
    """构造一个有 6 个通道、跨 3 个 group_id 的 ProfileStore。"""
    from satellite_debug_tool.core.profile import ProfileStore
    from satellite_debug_tool.core.protocol import ChannelDefEntry

    ps = ProfileStore()

    def _make(cid: int, name: str, gid: int) -> ChannelDefEntry:
        return ChannelDefEntry(
            channel_id=cid, data_type=1, group_id=gid, flags=0,
            name=name, unit="deg", display_min=-180.0, display_max=180.0,
        )

    channels: List[ChannelDefEntry] = [
        _make(0, "roll", 0),
        _make(1, "pitch", 0),
        _make(2, "yaw", 0),
        _make(3, "ant_az", 1),
        _make(4, "ant_el", 1),
        _make(5, "snr", 2),
    ]
    ps.apply_channel_define("afd01", table_ver=1, entries=channels)
    return ps


def _open_dialog(qapp, profile_store, settings, hw_type="afd01"):
    from satellite_debug_tool.ui.chart_group_dialog import ChartGroupDialog
    dlg = ChartGroupDialog(profile_store, settings, hw_type)
    return dlg


class TestInitialLoad:
    def test_loads_profile_defaults_when_no_settings(
        self, qapp, profile_store_with_channels, tmp_settings,
    ):
        dlg = _open_dialog(qapp, profile_store_with_channels, tmp_settings)
        snap = dlg.current_groups_snapshot()
        assert set(snap.keys()) == {0, 1, 2}
        assert snap[0]["channels"] == ["roll", "pitch", "yaw"]
        assert snap[1]["channels"] == ["ant_az", "ant_el"]
        assert snap[2]["channels"] == ["snr"]
        assert snap[0]["title"] == "姿态"
        assert snap[1]["title"] == "指向"
        dlg.deleteLater()

    def test_loads_custom_groups_from_settings(
        self, qapp, profile_store_with_channels, tmp_settings,
    ):
        tmp_settings.set("chart.custom_groups", {
            "afd01": {
                "0": {"title": "My Group A", "channels": ["roll"]},
                "5": {"title": "My Group B", "channels": ["pitch", "yaw", "snr"]},
            }
        })
        dlg = _open_dialog(qapp, profile_store_with_channels, tmp_settings)
        snap = dlg.current_groups_snapshot()
        assert set(snap.keys()) == {0, 5}
        assert snap[0]["title"] == "My Group A"
        assert snap[5]["channels"] == ["pitch", "yaw", "snr"]
        dlg.deleteLater()

    def test_unconnected_disables_editing(self, qapp, profile_store_with_channels, tmp_settings):
        dlg = _open_dialog(qapp, profile_store_with_channels, tmp_settings, hw_type=None)
        assert dlg._btn_new.isEnabled() is False
        assert dlg._btn_del.isEnabled() is False
        assert dlg._list_unassigned.isEnabled() is False
        dlg.deleteLater()


class TestUnassignedChannels:
    def test_all_assigned_initially_no_unassigned(
        self, qapp, profile_store_with_channels, tmp_settings,
    ):
        dlg = _open_dialog(qapp, profile_store_with_channels, tmp_settings)
        assert dlg._list_unassigned.count() == 0
        dlg.deleteLater()

    def test_move_out_of_group_appears_in_unassigned(
        self, qapp, profile_store_with_channels, tmp_settings,
    ):
        dlg = _open_dialog(qapp, profile_store_with_channels, tmp_settings)
        # 切到组 0，选中 yaw，移出
        dlg._group_combo.setCurrentIndex(0)
        for i in range(dlg._list_current.count()):
            if dlg._list_current.item(i).text() == "yaw":
                dlg._list_current.item(i).setSelected(True)
                break
        dlg._on_move_to_unassigned()
        unassigned_names = [
            dlg._list_unassigned.item(i).text() for i in range(dlg._list_unassigned.count())
        ]
        assert "yaw" in unassigned_names
        # 组 0 内已经没有 yaw
        snap = dlg.current_groups_snapshot()
        assert "yaw" not in snap[0]["channels"]
        dlg.deleteLater()


class TestGroupCRUD:
    def test_new_group_added(self, qapp, profile_store_with_channels, tmp_settings, monkeypatch):
        from PySide6.QtWidgets import QInputDialog
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw: ("MyNewGroup", True))
        dlg = _open_dialog(qapp, profile_store_with_channels, tmp_settings)
        before = set(dlg.current_groups_snapshot().keys())
        dlg._on_new_group()
        after = set(dlg.current_groups_snapshot().keys())
        new = after - before
        assert len(new) == 1
        gid = new.pop()
        assert dlg.current_groups_snapshot()[gid]["title"] == "MyNewGroup"
        dlg.deleteLater()

    def test_delete_group_returns_channels_to_unassigned(
        self, qapp, profile_store_with_channels, tmp_settings, monkeypatch,
    ):
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **kw: QMessageBox.StandardButton.Yes,
        )
        dlg = _open_dialog(qapp, profile_store_with_channels, tmp_settings)
        dlg._group_combo.setCurrentIndex(0)   # 选中组 0（姿态：roll/pitch/yaw）
        dlg._on_delete_group()
        snap = dlg.current_groups_snapshot()
        assert 0 not in snap
        # roll/pitch/yaw 应在未分组
        names = [dlg._list_unassigned.item(i).text() for i in range(dlg._list_unassigned.count())]
        for n in ("roll", "pitch", "yaw"):
            assert n in names
        dlg.deleteLater()

    def test_cannot_delete_last_group(
        self, qapp, profile_store_with_channels, tmp_settings, monkeypatch,
    ):
        from PySide6.QtWidgets import QMessageBox
        # 给一个只含 1 组的初始 settings
        tmp_settings.set("chart.custom_groups", {
            "afd01": {"0": {"title": "Only", "channels": ["roll"]}}
        })
        warned = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **kw: warned.append(True),
        )
        dlg = _open_dialog(qapp, profile_store_with_channels, tmp_settings)
        dlg._on_delete_group()
        assert warned == [True]
        assert 0 in dlg.current_groups_snapshot()   # 没真删
        dlg.deleteLater()

    def test_rename_group(self, qapp, profile_store_with_channels, tmp_settings, monkeypatch):
        from PySide6.QtWidgets import QInputDialog
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw: ("RenamedTitle", True))
        dlg = _open_dialog(qapp, profile_store_with_channels, tmp_settings)
        dlg._group_combo.setCurrentIndex(0)
        dlg._on_rename_group()
        assert dlg.current_groups_snapshot()[0]["title"] == "RenamedTitle"
        dlg.deleteLater()


class TestPersistence:
    def test_accept_writes_to_settings(self, qapp, profile_store_with_channels, tmp_settings):
        dlg = _open_dialog(qapp, profile_store_with_channels, tmp_settings)
        dlg._groups[0]["title"] = "ChangedTitle"
        dlg._on_accept()
        on_disk = tmp_settings.get("chart.custom_groups")
        assert "afd01" in on_disk
        assert on_disk["afd01"]["0"]["title"] == "ChangedTitle"
        dlg.deleteLater()

    def test_reject_does_not_write(self, qapp, profile_store_with_channels, tmp_settings):
        dlg = _open_dialog(qapp, profile_store_with_channels, tmp_settings)
        dlg._groups[0]["title"] = "ShouldNotPersist"
        dlg.reject()
        assert tmp_settings.get("chart.custom_groups") in (None, {})
        dlg.deleteLater()


class TestReset:
    def test_reset_to_profile_defaults(
        self, qapp, profile_store_with_channels, tmp_settings, monkeypatch,
    ):
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **kw: QMessageBox.StandardButton.Yes,
        )
        # 先弄一个奇怪的自定义状态
        tmp_settings.set("chart.custom_groups", {
            "afd01": {"99": {"title": "Weird", "channels": ["roll", "snr"]}}
        })
        dlg = _open_dialog(qapp, profile_store_with_channels, tmp_settings)
        assert 99 in dlg.current_groups_snapshot()
        dlg._on_reset_defaults()
        snap = dlg.current_groups_snapshot()
        assert set(snap.keys()) == {0, 1, 2}    # 回到 profile 默认
        dlg.deleteLater()
