"""DeviceProfile JSON 序列化 / 本地缓存。

缓存路径默认 ``~/.satellite_debug_tool/profiles/{hw_type}.json``。
一个 hw_type 一个文件，三张表与各自的 table_ver 存在同一文件内；设备下次
连接时按 hw_type 直接读回，避免再等一次 5s DEFINE 广播。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    EventDefEntry,
    StateDefEntry,
    StateEnumItem,
)
from .models import DeviceProfile
from .semantics import semantics_from_dict, semantics_to_dict


DEFAULT_CACHE_DIR = Path.home() / ".satellite_debug_tool" / "profiles"

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, 2}


# ---------- dataclass ↔ dict 转换 ----------

def _channel_to_dict(c: ChannelDefEntry) -> dict:
    return {
        "channel_id": c.channel_id, "data_type": c.data_type,
        "group_id": c.group_id, "flags": c.flags,
        "name": c.name, "unit": c.unit,
        "display_min": c.display_min, "display_max": c.display_max,
    }


def _channel_from_dict(d: dict) -> ChannelDefEntry:
    return ChannelDefEntry(
        channel_id=int(d["channel_id"]), data_type=int(d["data_type"]),
        group_id=int(d["group_id"]), flags=int(d["flags"]),
        name=str(d["name"]), unit=str(d["unit"]),
        display_min=float(d["display_min"]), display_max=float(d["display_max"]),
    )


def _state_to_dict(s: StateDefEntry) -> dict:
    return {
        "state_id": s.state_id, "state_type": s.state_type,
        "flags": s.flags, "name": s.name,
        "enums": [asdict(e) for e in s.enums],
    }


def _state_from_dict(d: dict) -> StateDefEntry:
    enums = [
        StateEnumItem(value=int(e["value"]), level=int(e["level"]), name=str(e["name"]))
        for e in d.get("enums", [])
    ]
    return StateDefEntry(
        state_id=int(d["state_id"]), state_type=int(d["state_type"]),
        flags=int(d["flags"]), name=str(d["name"]), enums=enums,
    )


def _event_to_dict(e: EventDefEntry) -> dict:
    return {"event_id": e.event_id, "level": e.level, "name": e.name}


def _event_from_dict(d: dict) -> EventDefEntry:
    return EventDefEntry(
        event_id=int(d["event_id"]), level=int(d["level"]), name=str(d["name"]),
    )


def profile_to_dict(profile: DeviceProfile) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "hw_type": profile.hw_type,
        "channel_table_ver": profile.channel_table_ver,
        "state_table_ver": profile.state_table_ver,
        "event_table_ver": profile.event_table_ver,
        "semantics_table_ver": profile.semantics_table_ver,
        "channels": [_channel_to_dict(c) for c in profile.channel_list()],
        "states": [_state_to_dict(s) for s in profile.state_list()],
        "events": [_event_to_dict(e) for e in profile.event_list()],
        "semantics": semantics_to_dict(profile.semantics),
        "meta": None if profile.meta is None else {
            "protocol_ver": profile.meta.protocol_ver,
            "fw_ver": profile.meta.fw_ver,
            "hw_type": profile.meta.hw_type,
            "device_sn": profile.meta.device_sn,
        },
    }


def profile_from_dict(d: dict) -> DeviceProfile:
    schema = int(d.get("schema_version", 0))
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported profile schema_version={schema}")

    p = DeviceProfile(hw_type=str(d["hw_type"]))
    p.channel_table_ver = d.get("channel_table_ver")
    p.state_table_ver = d.get("state_table_ver")
    p.event_table_ver = d.get("event_table_ver")
    p.semantics_table_ver = d.get("semantics_table_ver")

    for c in d.get("channels", []):
        entry = _channel_from_dict(c)
        p.channels[entry.channel_id] = entry
    for s in d.get("states", []):
        entry = _state_from_dict(s)
        p.states[entry.state_id] = entry
    for e in d.get("events", []):
        entry = _event_from_dict(e)
        p.events[entry.event_id] = entry
    if schema >= 2:
        p.semantics = semantics_from_dict(d.get("semantics"))

    meta_dict = d.get("meta")
    if meta_dict:
        from satellite_debug_tool.core.protocol import MetaInfo
        p.meta = MetaInfo(
            protocol_ver=int(meta_dict["protocol_ver"]),
            fw_ver=str(meta_dict["fw_ver"]),
            hw_type=str(meta_dict["hw_type"]),
            device_sn=str(meta_dict["device_sn"]),
        )
    return p


# ---------- 缓存文件读写 ----------

class ProfileCache:
    """本地 JSON 缓存。线程/进程安全不在本层保证。"""

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        self._dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR

    @property
    def dir(self) -> Path:
        return self._dir

    def path_for(self, hw_type: str) -> Path:
        return self._dir / f"{hw_type}.json"

    def save(self, profile: DeviceProfile) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(profile.hw_type)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(profile_to_dict(profile), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    def load(self, hw_type: str) -> Optional[DeviceProfile]:
        path = self.path_for(hw_type)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return profile_from_dict(data)
        except (OSError, json.JSONDecodeError, ValueError, KeyError):
            # 坏缓存：直接忽略，待 DEFINE 帧重填
            return None

    def delete(self, hw_type: str) -> bool:
        path = self.path_for(hw_type)
        if path.is_file():
            path.unlink()
            return True
        return False
