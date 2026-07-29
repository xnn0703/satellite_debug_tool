"""读取 release.config.json：updater 用来定位 GitHub Release。

查找顺序（首次命中即返回）：
  1. PyInstaller 打包后的运行时目录：`sys._MEIPASS / release.config.json`
  2. 安装目录（与可执行文件同级）：`exe_dir / release.config.json`
  3. 源码根（开发模式）：repo_root / release.config.json
  4. 兜底：内置 DEFAULTS（同 release.config.json 文件内容）

提供：
- `load_release_config() -> dict`：返回完整的 GitHub Release 配置
- `DEFAULTS`：常量，作为最后兜底
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional


DEFAULTS = {
    "github_owner": "xnn0703",
    "github_repo": "satellite_debug_tool",
    "github_api": "https://api.github.com",
}


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    # PyInstaller 运行时
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.append(Path(meipass) / "release.config.json")
    # 可执行文件同级（onedir 模式时 _internal 旁边）
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).parent / "release.config.json")
    # 源码根（开发模式）：from this file 往上 2 层（satellite_debug_tool/release_config.py → repo_root）
    paths.append(Path(__file__).resolve().parent.parent / "release.config.json")
    return paths


def load_release_config(override_path: Optional[Path] = None) -> dict:
    """加载并返回完整的 release config dict。

    缺字段用 DEFAULTS 兜底；JSON 解析失败也回 DEFAULTS（升级机制不应该让程序崩溃）。

    Args:
        override_path: 测试用，强制使用某个具体文件路径。
    """
    config = dict(DEFAULTS)
    paths = [override_path] if override_path is not None else _candidate_paths()
    for p in paths:
        if p is None or not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                config.update({k: v for k, v in data.items() if isinstance(v, str) and v})
            break
        except (json.JSONDecodeError, OSError):
            continue
    return config
