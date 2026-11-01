"""updater — ops 可信应用更新模块（M52）。

公共 API：
    - compare_versions(a, b) -> int
    - ReleaseChecker / LatestRelease / Asset
    - Downloader：可信摘要、范围验证及断点续传
    - Applier：同盘准备与可恢复切换
"""

from satellite_debug_tool.updater.checker import (
    Asset,
    LatestRelease,
    ReleaseChecker,
    UpdateCheckError,
    compare_versions,
    current_platform,
)
from satellite_debug_tool.updater.downloader import (
    DownloadCancelled,
    DownloadError,
    Downloader,
)
from satellite_debug_tool.updater.applier import (
    Applier,
    ApplyError,
    ApplyResult,
    PayloadContract,
)
from satellite_debug_tool.updater.handoff import (
    UpdateLaunchRequest,
    launch_updater,
    prepare_update_launch_request,
)

__all__ = [
    "Asset",
    "LatestRelease",
    "ReleaseChecker",
    "UpdateCheckError",
    "compare_versions",
    "current_platform",
    "Downloader",
    "DownloadError",
    "DownloadCancelled",
    "Applier",
    "ApplyError",
    "ApplyResult",
    "PayloadContract",
    "UpdateLaunchRequest",
    "launch_updater",
    "prepare_update_launch_request",
]
