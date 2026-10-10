"""updater — 自动升级模块（M11）。

公共 API：
    - compare_versions(a, b) -> int
    - ReleaseChecker / LatestRelease / Asset
    - Downloader (P4)
    - Applier (P5)
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
