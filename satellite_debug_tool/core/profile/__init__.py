"""设备 profile 元数据（channel / state / event 定义表）管理。

ProfileStore 按 ``hw_type`` 分桶保存 DEFINE 表，支持：
- 运行时由 FrameReceiverV2 下发的 DEFINE 帧写入（`apply_channel/state/event_define`）
- 启动时从本地缓存 `~/.satellite_debug_tool/profiles/*.json` 读回（`load_cached`）
- 手动导入/导出（便于离线分析 .sdb）
"""

from .models import DeviceProfile
from .profile_store import ProfileStore
from .cache import ProfileCache, DEFAULT_CACHE_DIR

__all__ = ["DeviceProfile", "ProfileStore", "ProfileCache", "DEFAULT_CACHE_DIR"]
