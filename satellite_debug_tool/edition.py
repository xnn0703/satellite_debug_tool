"""构建身份与产品范围；用户偏好和命令行均不能改变冻结产物的类型。"""

from __future__ import annotations

from enum import Enum
import json
from pathlib import Path
import sys


class Edition(str, Enum):
    USER = "user"
    OPS = "ops"


def build_edition() -> Edition:
    if getattr(sys, "frozen", False):
        path = Path(__file__).with_name("build_identity.json")
        return Edition(json.loads(path.read_text(encoding="utf-8"))["edition"])
    return Edition.OPS


def edition_of(settings) -> Edition:
    return Edition(getattr(settings, "edition", build_edition()))


def validate_payload_identity(root: Path, edition: Edition, version: str = "") -> None:
    """在替换旧安装前验证唯一载荷中的构建身份。"""
    from satellite_debug_tool.updater.checker import compare_versions

    path = root / "_internal" / "satellite_debug_tool" / "build_identity.json"
    if not path.exists():
        # 已发布的全量包截至 1.7.0 使用固定 ops 合同。
        if edition is Edition.OPS and version and compare_versions(version, "1.7.0") <= 0:
            return
        raise ValueError("update payload requires a build identity")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(not isinstance(data.get(key), str) for key in ("edition", "version", "platform")):
        raise ValueError("update payload requires a complete build identity")
    # 当前自动安装载荷合同是 Windows onedir；macOS 通过独立签名 app 分发。
    if data["platform"] != "win":
        raise ValueError("update payload platform differs from the Windows installer")
    if Edition(data["edition"]) is not edition:
        raise ValueError("update payload edition differs from the installed edition")
    if version and compare_versions(str(data["version"]), version) != 0:
        raise ValueError("update payload version differs from the requested release")
