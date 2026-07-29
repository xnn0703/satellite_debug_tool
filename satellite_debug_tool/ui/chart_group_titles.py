"""Stable built-in chart-group titles shared by dialogs and charts."""

from __future__ import annotations

from satellite_debug_tool.i18n import tr


DEFAULT_GROUP_ALIASES = {
    0: {"Attitude", "Attitude (orientation)", "姿态", "姿态（Attitude）"},
    1: {"Pointing", "Pointing (beam)", "指向", "指向（Pointing）"},
    2: {"Signal", "信号", "信号（Signal）"},
    3: {"PID / error", "PID/Error", "PID/误差", "PID / 误差"},
    4: {"Position", "Position (GPS)", "位置", "位置（GPS）"},
}


def default_group_title(group_id: int) -> str:
    return {
        0: tr("Attitude"),
        1: tr("Pointing"),
        2: tr("Signal"),
        3: tr("PID / error"),
        4: tr("Position"),
    }.get(group_id, tr("Group {group_id}", group_id=group_id))


def is_default_group_title(
    group_id: int,
    title: str,
    explicit: object = None,
) -> bool:
    if explicit is not None:
        return bool(explicit)
    if not title.strip():
        return True
    return title.strip() in DEFAULT_GROUP_ALIASES.get(
        group_id,
        {f"Group {group_id}", f"组 {group_id}"},
    )


def group_uses_default_title(group_id: int, group: dict) -> bool:
    if "title_is_default" in group:
        if not bool(group.get("title_is_default")):
            return False
        title = str(group.get("title", "")).strip()
        return not title or is_default_group_title(group_id, title)
    return is_default_group_title(group_id, str(group.get("title", "")))


def display_group_title(group_id: int, group: dict | None = None) -> str:
    if group is None or group_uses_default_title(group_id, group):
        return default_group_title(group_id)
    title = str(group.get("title", "")).strip()
    return title or default_group_title(group_id)
