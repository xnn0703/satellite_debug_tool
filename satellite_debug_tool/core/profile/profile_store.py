"""ProfileStore — 多 hw_type 的 DEFINE 表聚合 + 本地缓存读写。

典型调用链::

    store = ProfileStore()
    store.attach_cache(ProfileCache())

    # 启动时先尝试从缓存预热
    store.load_cached("afd01")

    # receiver 拿到 DEFINE 帧后调用
    store.apply_meta(meta_info)
    store.apply_channel_define("afd01", table_ver, entries)
    ...

    # UI 侧：
    store.profile_changed.connect(lambda hw_type: ...)
    for ch in store.get_channels("afd01"):
        ...
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional

from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    EventDefEntry,
    MetaInfo,
    ProfileSemanticsReport,
    StateDefEntry,
    SubCmd,
)
from .cache import ProfileCache, profile_from_dict, profile_to_dict
from .models import DeviceProfile
from .semantics import (
    CONTROL_SUBCMD_SET_TRACE_MODE,
    CONTROL_VALUE_FROM_ENUM_VALUE,
    ChannelSemantic,
    ControlBinding,
    ProfileSemantics,
    StateSemantic,
    infer_channel_roles,
    infer_state_role,
)


class ProfileStore(QObject):
    """按 hw_type 分桶的 profile 聚合。UI 订阅 `profile_changed` 即可整体重建。"""

    # hw_type 对应的 profile 任一表有变化时发射
    profile_changed = Signal(str)

    def __init__(self, cache: Optional[ProfileCache] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._profiles: Dict[str, DeviceProfile] = {}
        self._cache: Optional[ProfileCache] = cache
        self._current_hw_type: Optional[str] = None

    # ----- 缓存附加 -----

    def attach_cache(self, cache: ProfileCache) -> None:
        self._cache = cache

    # ----- 读取 -----

    def current_hw_type(self) -> Optional[str]:
        return self._current_hw_type

    def get_profile(self, hw_type: str) -> Optional[DeviceProfile]:
        return self._profiles.get(hw_type)

    def has_profile(self, hw_type: str) -> bool:
        return hw_type in self._profiles

    def is_ready(self, hw_type: str) -> bool:
        p = self._profiles.get(hw_type)
        return p is not None and p.is_ready()

    def get_channels(self, hw_type: str) -> List[ChannelDefEntry]:
        p = self._profiles.get(hw_type)
        return [] if p is None else p.channel_list()

    def get_states(self, hw_type: str) -> List[StateDefEntry]:
        p = self._profiles.get(hw_type)
        return [] if p is None else p.state_list()

    def get_events(self, hw_type: str) -> List[EventDefEntry]:
        p = self._profiles.get(hw_type)
        return [] if p is None else p.event_list()

    def get_channel(self, hw_type: str, channel_id: int) -> Optional[ChannelDefEntry]:
        p = self._profiles.get(hw_type)
        return None if p is None else p.get_channel(channel_id)

    def get_state(self, hw_type: str, state_id: int) -> Optional[StateDefEntry]:
        p = self._profiles.get(hw_type)
        return None if p is None else p.get_state(state_id)

    def get_event(self, hw_type: str, event_id: int) -> Optional[EventDefEntry]:
        p = self._profiles.get(hw_type)
        return None if p is None else p.get_event(event_id)

    def find_channels_by_role(self, hw_type: str, role: str) -> List[ChannelDefEntry]:
        """按 semantic role 查 channel；显式 role 优先，缺失时按旧名称推断。"""
        p = self._profiles.get(hw_type)
        if p is None:
            return []
        wanted = role.strip().lower()
        explicit: List[ChannelDefEntry] = []
        for cid, semantic in p.semantics.channels.items():
            if wanted in {r.strip().lower() for r in semantic.roles}:
                entry = p.get_channel(cid)
                if entry is not None:
                    explicit.append(entry)
        if explicit:
            return sorted(explicit, key=lambda c: c.channel_id)

        fallback = [
            ch for ch in p.channel_list()
            if wanted in infer_channel_roles(ch.name)
        ]
        return sorted(fallback, key=lambda c: c.channel_id)

    def find_channel_by_role(self, hw_type: str, role: str) -> Optional[ChannelDefEntry]:
        channels = self.find_channels_by_role(hw_type, role)
        return channels[0] if channels else None

    def find_state_by_role(self, hw_type: str, role: str) -> Optional[StateDefEntry]:
        """按 semantic role 查 state；显式 role 优先，缺失时按旧名称推断。"""
        p = self._profiles.get(hw_type)
        if p is None:
            return None
        wanted = role.strip().lower()
        explicit: List[StateDefEntry] = []
        for sid, semantic in p.semantics.states.items():
            if semantic.role is not None and semantic.role.strip().lower() == wanted:
                entry = p.get_state(sid)
                if entry is not None:
                    explicit.append(entry)
        if explicit:
            return sorted(explicit, key=lambda s: s.state_id)[0]

        for state in p.state_list():
            if infer_state_role(state.name) == wanted:
                return state
        return None

    def get_state_control_binding(self, hw_type: str, state_id: int) -> Optional[ControlBinding]:
        """返回 state 的可控绑定。

        如果显式语义声明了该 state 但没有 control，视作只读，不再走旧 fallback。
        这样新固件可声明 role，同时避免误报尚未实现的控制能力。
        """
        p = self._profiles.get(hw_type)
        if p is None:
            return None
        explicit = p.semantics.states.get(state_id)
        if explicit is not None:
            return explicit.control

        state = p.get_state(state_id)
        if state is None:
            return None
        if state_id == 0 or infer_state_role(state.name) == "trace_mode":
            return ControlBinding(
                subcmd=CONTROL_SUBCMD_SET_TRACE_MODE,
                value_from=CONTROL_VALUE_FROM_ENUM_VALUE,
            )
        return None

    def has_capability(self, hw_type: str, name: str, default: bool = False) -> bool:
        p = self._profiles.get(hw_type)
        if p is None:
            return default
        if name in p.semantics.capabilities:
            return bool(p.semantics.capabilities[name])
        return default

    # ----- 写入：来自 FrameReceiverV2 下发 -----

    def apply_meta(self, meta: MetaInfo) -> None:
        """收到 META_INFO。设置 current_hw_type 并 upsert profile。

        **幂等性**：META 每 5 秒由下位机广播一次，内容通常不变。如果
        ``protocol_ver / fw_ver / hw_type / device_sn`` 都与已记录值相同，
        就不发 ``profile_changed`` 信号——否则 UI 会每 5 秒整体重建一次，
        表现为"曲线周期性闪烁"。仅在 hw_type 切换、固件升级或首次到达时
        才触发重建。
        """
        hw = meta.hw_type or "unknown"
        first_time = hw not in self._profiles
        hw_switched = self._current_hw_type != hw
        p = self._profiles.setdefault(hw, DeviceProfile(hw_type=hw))

        prev = p.meta
        content_changed = (
            prev is None
            or prev.protocol_ver != meta.protocol_ver
            or prev.fw_ver != meta.fw_ver
            or prev.hw_type != meta.hw_type
            or prev.device_sn != meta.device_sn
        )
        p.meta = meta
        self._current_hw_type = hw

        if first_time or hw_switched or content_changed:
            self._persist(hw)
            self.profile_changed.emit(hw)

    def apply_channel_define(
        self, hw_type: str, table_ver: int, entries: Iterable[ChannelDefEntry]
    ) -> None:
        p = self._get_or_create(hw_type)
        if p.channel_table_ver == table_ver and p.channels:
            # 版本未变：跳过（幂等）
            return
        p.channel_table_ver = int(table_ver)
        p.channels = {e.channel_id: e for e in entries}
        self._persist(hw_type)
        self.profile_changed.emit(hw_type)

    def apply_state_define(
        self, hw_type: str, table_ver: int, entries: Iterable[StateDefEntry]
    ) -> None:
        p = self._get_or_create(hw_type)
        if p.state_table_ver == table_ver and p.states:
            return
        p.state_table_ver = int(table_ver)
        p.states = {e.state_id: e for e in entries}
        self._persist(hw_type)
        self.profile_changed.emit(hw_type)

    def apply_event_define(
        self, hw_type: str, table_ver: int, entries: Iterable[EventDefEntry]
    ) -> None:
        p = self._get_or_create(hw_type)
        if p.event_table_ver == table_ver and p.events:
            return
        p.event_table_ver = int(table_ver)
        p.events = {e.event_id: e for e in entries}
        self._persist(hw_type)
        self.profile_changed.emit(hw_type)

    def apply_profile_semantics(self, hw_type: str, report: ProfileSemanticsReport) -> None:
        """收到 PROFILE_SEMANTICS 扩展帧，更新当前 hw_type 的语义层。"""
        p = self._get_or_create(hw_type)
        semantics = self._semantics_from_report(report)
        if p.semantics_table_ver == report.table_ver and p.semantics == semantics:
            return
        p.semantics_table_ver = int(report.table_ver)
        p.semantics = semantics
        self._persist(hw_type)
        self.profile_changed.emit(hw_type)

    # ----- 本地缓存 -----

    def load_cached(self, hw_type: str) -> bool:
        """尝试从缓存预热指定 hw_type 的 profile，成功返回 True。"""
        if self._cache is None:
            return False
        cached = self._cache.load(hw_type)
        if cached is None:
            return False
        self._profiles[hw_type] = cached
        self._current_hw_type = hw_type
        self.profile_changed.emit(hw_type)
        return True

    # ----- 手动导入/导出 -----

    def export(self, hw_type: str, path: Path) -> bool:
        import json
        p = self._profiles.get(hw_type)
        if p is None:
            return False
        Path(path).write_text(
            json.dumps(profile_to_dict(p), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True

    def import_(self, path: Path) -> Optional[str]:
        """导入 profile JSON 文件，返回导入的 hw_type（失败返回 None）。"""
        import json
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return None
        return self.import_dict(data)

    def import_dict(self, data: dict) -> Optional[str]:
        """从 dict 导入 profile（用于 .sdb v2 文件头恢复）；失败返回 None。"""
        try:
            profile = profile_from_dict(data)
        except Exception:
            return None
        self._profiles[profile.hw_type] = profile
        self.profile_changed.emit(profile.hw_type)
        return profile.hw_type

    # ----- 其它 -----

    def clear(self, hw_type: Optional[str] = None) -> None:
        if hw_type is None:
            self._profiles.clear()
            self._current_hw_type = None
        else:
            self._profiles.pop(hw_type, None)
            if self._current_hw_type == hw_type:
                self._current_hw_type = None

    # ----- 内部 -----

    def _get_or_create(self, hw_type: str) -> DeviceProfile:
        p = self._profiles.get(hw_type)
        if p is None:
            p = DeviceProfile(hw_type=hw_type)
            self._profiles[hw_type] = p
        return p

    def _persist(self, hw_type: str) -> None:
        if self._cache is None:
            return
        p = self._profiles.get(hw_type)
        if p is not None:
            try:
                self._cache.save(p)
            except OSError:
                # 缓存写失败不影响运行时
                pass

    def _semantics_from_report(self, report: ProfileSemanticsReport) -> ProfileSemantics:
        semantics = ProfileSemantics()
        for ch in report.channels:
            roles = [str(r).strip().lower() for r in ch.roles if str(r).strip()]
            if roles:
                semantics.channels[int(ch.channel_id)] = ChannelSemantic(roles=roles)
        for state in report.states:
            role = str(state.role).strip().lower()
            control = None
            if state.control_subcmd:
                control = ControlBinding(
                    subcmd=self._control_subcmd_name(state.control_subcmd),
                    value_from=self._control_value_from_name(state.control_value_from),
                )
            if role or control is not None:
                semantics.states[int(state.state_id)] = StateSemantic(
                    role=role or None,
                    control=control,
                )
        semantics.capabilities = {
            str(cap.name): bool(cap.supported) for cap in report.capabilities
        }
        return semantics

    def _control_subcmd_name(self, value: int) -> str:
        try:
            return SubCmd(int(value)).name
        except ValueError:
            return f"0x{int(value) & 0xFF:02X}"

    def _control_value_from_name(self, value: int) -> str:
        if int(value) == 0:
            return CONTROL_VALUE_FROM_ENUM_VALUE
        return f"raw_{int(value) & 0xFF}"
