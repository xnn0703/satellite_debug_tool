"""ChartGroupDialog — 图表分组管理对话框（M10 F2）。

允许用户自定义把哪些通道放进哪个组（chart stacked 模式下的子图划分）。
存到 `settings.chart.custom_groups[hw_type]`，GroupedChartWidget._rebuild_stacked
优先读这里，未配置时回退到 profile 默认 group_id。

UI:
  ┌─ 图表分组管理 [hw_type=afd01] ──────────────────────────┐
  │ 当前组: [组 0: 姿态 ▼]  [+ 新建] [× 删除] [✎ 重命名]   │
  │                                                          │
  │ ┌─ 未分组通道 ──┐    ┌─ 当前组通道 ─────────────────┐  │
  │ │ ch_xx         │ →  │ roll                         │  │
  │ │ ch_yy         │ ←  │ pitch                        │  │
  │ │ ...           │    │ ...                          │  │
  │ └───────────────┘    └──────────────────────────────┘  │
  │                                                          │
  │ [恢复 profile 默认] [取消] [确定]                       │
  └──────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import copy
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.ui.chart_group_titles import (
    default_group_title,
    display_group_title,
    group_uses_default_title,
    is_default_group_title,
)


class ChartGroupDialog(QDialog):
    """图表分组管理对话框。

    Args:
        profile_store: 用于查询当前 hw_type 的通道列表
        settings: 持久化 custom_groups
        hw_type: 要管理的硬件类型；为 None 时禁用编辑（显示"未连接设备"）
    """

    def __init__(
        self,
        profile_store: ProfileStore,
        settings: Settings,
        hw_type: Optional[str],
        parent=None,
    ):
        super().__init__(parent)
        self._profile = profile_store
        self._settings = settings
        self._hw_type = hw_type
        # _groups: gid (int) -> {"title": str, "channels": [name, ...]}
        # 工作副本，确定时才写回 settings
        self._groups: Dict[int, dict] = {}

        self.setWindowTitle(
            tr(
                "Chart group manager [{device}]",
                device=hw_type or tr("No device connected"),
            )
        )
        self.resize(640, 480)

        self._build_ui()
        self._load_initial_groups()
        self._refresh_group_combo()
        self._refresh_lists()
        register_translatable(self)

    # ---- UI 构造 ----

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # 顶部：当前组选择 + 新建/删除/重命名
        top = QHBoxLayout()
        top.addWidget(QLabel(tr("Current group:")))
        self._group_combo = QComboBox()
        self._group_combo.currentIndexChanged.connect(self._on_current_group_changed)
        top.addWidget(self._group_combo, 1)
        self._btn_new = QPushButton(tr("+ New"))
        self._btn_new.clicked.connect(self._on_new_group)
        self._btn_del = QPushButton(tr("× Delete"))
        self._btn_del.clicked.connect(self._on_delete_group)
        self._btn_rename = QPushButton(tr("✎ Rename"))
        self._btn_rename.clicked.connect(self._on_rename_group)
        for b in (self._btn_new, self._btn_del, self._btn_rename):
            top.addWidget(b)
        root.addLayout(top)

        # 中部：左未分组 / 中按钮 / 右当前组
        mid = QHBoxLayout()

        # 左：未分组
        left = QVBoxLayout()
        left.addWidget(QLabel(tr("Unassigned channels")))
        self._list_unassigned = QListWidget()
        self._list_unassigned.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        left.addWidget(self._list_unassigned, 1)
        mid.addLayout(left, 1)

        # 中：移动按钮
        center = QVBoxLayout()
        center.addStretch(1)
        self._btn_to_group = QPushButton("→")
        self._btn_to_group.setToolTip(
            tr("Move selected channels into the current group")
        )
        self._btn_to_group.setFixedWidth(40)
        self._btn_to_group.clicked.connect(self._on_move_to_group)
        self._btn_to_unassigned = QPushButton("←")
        self._btn_to_unassigned.setToolTip(
            tr("Remove selected channels from the current group")
        )
        self._btn_to_unassigned.setFixedWidth(40)
        self._btn_to_unassigned.clicked.connect(self._on_move_to_unassigned)
        center.addWidget(self._btn_to_group)
        center.addSpacing(4)
        center.addWidget(self._btn_to_unassigned)
        center.addStretch(1)
        mid.addLayout(center)

        # 右：当前组
        right = QVBoxLayout()
        right.addWidget(QLabel(tr("Current group channels")))
        self._list_current = QListWidget()
        self._list_current.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        right.addWidget(self._list_current, 1)
        mid.addLayout(right, 1)

        root.addLayout(mid, 1)

        # 底部：恢复默认 + 取消/确定
        bottom = QHBoxLayout()
        self._btn_reset = QPushButton(tr("Restore profile defaults"))
        self._btn_reset.setToolTip(
            tr("Discard custom groups and restore the profile group_id layout")
        )
        self._btn_reset.clicked.connect(self._on_reset_defaults)
        bottom.addWidget(self._btn_reset)
        bottom.addStretch(1)
        self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._button_box.button(QDialogButtonBox.StandardButton.Ok).setText(tr("OK"))
        self._button_box.button(QDialogButtonBox.StandardButton.Cancel).setText(
            tr("Cancel")
        )
        self._button_box.accepted.connect(self._on_accept)
        self._button_box.rejected.connect(self.reject)
        bottom.addWidget(self._button_box)
        root.addLayout(bottom)

        # 未连接设备时禁用整个编辑区
        if self._hw_type is None:
            for w in (self._group_combo, self._btn_new, self._btn_del, self._btn_rename,
                      self._btn_to_group, self._btn_to_unassigned,
                      self._list_unassigned, self._list_current, self._btn_reset,
                      self._button_box.button(QDialogButtonBox.StandardButton.Ok)):
                w.setEnabled(False)

    # ---- 初始数据加载 ----

    def _load_initial_groups(self) -> None:
        """从 settings.chart.custom_groups[hw_type] 加载；不存在则用 profile 默认。"""
        if self._hw_type is None:
            return
        custom = (self._settings.get("chart.custom_groups", {}) or {}).get(self._hw_type)
        if custom:
            # JSON key 是字符串，转回 int
            self._groups = {
                int(gid_str): {
                    "title": str(g.get("title", "")),
                    "title_is_default": is_default_group_title(
                        int(gid_str),
                        str(g.get("title", "")),
                        g.get("title_is_default"),
                    ),
                    "channels": list(g.get("channels", [])),
                }
                for gid_str, g in custom.items()
            }
            return
        # 用 profile 默认：按 group_id 聚合
        self._groups = self._build_groups_from_profile()

    def _build_groups_from_profile(self) -> Dict[int, dict]:
        result: Dict[int, dict] = {}
        if self._hw_type is None:
            return result
        for ch in self._profile.get_channels(self._hw_type):
            gid = int(ch.group_id)
            entry = result.setdefault(
                gid,
                {"title": "", "title_is_default": True, "channels": []},
            )
            entry["channels"].append(ch.name)
        return result

    def _all_channel_names(self) -> List[str]:
        if self._hw_type is None:
            return []
        return [c.name for c in self._profile.get_channels(self._hw_type)]

    def _assigned_channels(self) -> set:
        result = set()
        for g in self._groups.values():
            result.update(g.get("channels", []))
        return result

    # ---- UI 刷新 ----

    def _refresh_group_combo(self) -> None:
        self._group_combo.blockSignals(True)
        self._group_combo.clear()
        for gid in sorted(self._groups.keys()):
            group = self._groups[gid]
            title = display_group_title(gid, group)
            self._group_combo.addItem(
                tr("Group {group_id}: {title}", group_id=gid, title=title),
                gid,
            )
        self._group_combo.blockSignals(False)

    def _current_gid(self) -> Optional[int]:
        if self._group_combo.count() == 0:
            return None
        return self._group_combo.currentData()

    def _refresh_lists(self) -> None:
        self._list_unassigned.clear()
        self._list_current.clear()
        all_chs = self._all_channel_names()
        assigned = self._assigned_channels()
        for name in all_chs:
            if name not in assigned:
                self._list_unassigned.addItem(QListWidgetItem(name))
        gid = self._current_gid()
        if gid is None:
            return
        for name in self._groups[gid]["channels"]:
            self._list_current.addItem(QListWidgetItem(name))

    # ---- 槽函数 ----

    def _on_current_group_changed(self, _idx: int) -> None:
        self._refresh_lists()

    def _on_new_group(self) -> None:
        # 找一个未占用的最小 gid
        new_gid = 0
        while new_gid in self._groups:
            new_gid += 1
        default_title = default_group_title(new_gid)
        title, ok = QInputDialog.getText(
            self,
            tr("New group"),
            tr("Group {group_id} title:", group_id=new_gid),
            text=default_title,
        )
        if not ok:
            return
        title = title.strip()
        use_default = not title or title == default_title
        self._groups[new_gid] = {
            "title": "" if use_default else title,
            "title_is_default": use_default,
            "channels": [],
        }
        self._refresh_group_combo()
        # 选中新建的
        for i in range(self._group_combo.count()):
            if self._group_combo.itemData(i) == new_gid:
                self._group_combo.setCurrentIndex(i)
                break
        self._refresh_lists()

    def _on_delete_group(self) -> None:
        gid = self._current_gid()
        if gid is None:
            return
        if len(self._groups) <= 1:
            QMessageBox.warning(
                self, tr("Cannot delete"), tr("At least one group must remain.")
            )
            return
        group = self._groups[gid]
        title = display_group_title(gid, group)
        ret = QMessageBox.question(
            self,
            tr("Delete group"),
            tr(
                "Delete \"Group {group_id}: {title}\"?\n"
                "Its channels will return to the unassigned list.",
                group_id=gid,
                title=title,
            ),
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        del self._groups[gid]
        self._refresh_group_combo()
        self._refresh_lists()

    def _on_rename_group(self) -> None:
        gid = self._current_gid()
        if gid is None:
            return
        title, ok = QInputDialog.getText(
            self,
            tr("Rename group"),
            tr("New title for group {group_id}:", group_id=gid),
            text=(
                display_group_title(gid, self._groups[gid])
            ),
        )
        if not ok:
            return
        title = title.strip()
        use_default = is_default_group_title(gid, title)
        self._groups[gid]["title"] = "" if use_default else title
        self._groups[gid]["title_is_default"] = use_default
        self._refresh_group_combo()
        # 保持当前选中
        for i in range(self._group_combo.count()):
            if self._group_combo.itemData(i) == gid:
                self._group_combo.setCurrentIndex(i)
                break

    def _on_move_to_group(self) -> None:
        gid = self._current_gid()
        if gid is None:
            return
        names = [item.text() for item in self._list_unassigned.selectedItems()]
        if not names:
            return
        current = self._groups[gid]["channels"]
        for name in names:
            if name not in current:
                current.append(name)
        self._refresh_lists()

    def _on_move_to_unassigned(self) -> None:
        gid = self._current_gid()
        if gid is None:
            return
        names = [item.text() for item in self._list_current.selectedItems()]
        if not names:
            return
        current = self._groups[gid]["channels"]
        self._groups[gid]["channels"] = [c for c in current if c not in names]
        self._refresh_lists()

    def _on_reset_defaults(self) -> None:
        ret = QMessageBox.question(
            self,
            tr("Restore defaults"),
            tr("Discard custom groups and restore the profile group_id layout?"),
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._groups = self._build_groups_from_profile()
        self._refresh_group_combo()
        self._refresh_lists()

    def _on_accept(self) -> None:
        """写回 settings 并 accept。"""
        if self._hw_type is None:
            self.accept()
            return
        all_custom = copy.deepcopy(self._settings.get("chart.custom_groups", {}) or {})
        # JSON 友好：gid 转字符串
        all_custom[self._hw_type] = {
            str(gid): {
                "title": (
                    ""
                    if group_uses_default_title(gid, g)
                    else str(g.get("title", "")).strip()
                ),
                "title_is_default": group_uses_default_title(gid, g),
                "channels": list(g["channels"]),
            }
            for gid, g in self._groups.items()
        }
        self._settings.set("chart.custom_groups", all_custom)
        self._settings.save()
        self.accept()

    # ---- 测试辅助 ----

    def current_groups_snapshot(self) -> Dict[int, dict]:
        """返回工作副本快照（测试用）。"""
        return copy.deepcopy(self._groups)

    def retranslate_ui(self) -> None:
        current_gid = self._current_gid()
        self.setWindowTitle(
            tr(
                "Chart group manager [{device}]",
                device=self._hw_type or tr("No device connected"),
            )
        )
        self._refresh_group_combo()
        if current_gid is not None:
            index = self._group_combo.findData(current_gid)
            if index >= 0:
                self._group_combo.setCurrentIndex(index)
