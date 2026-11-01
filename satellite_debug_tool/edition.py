"""构建身份与产品范围；用户偏好和命令行均不能改变冻结产物的类型。"""

from __future__ import annotations

from enum import Enum
import json
from pathlib import Path
import re
import sys

APP_ID = "satellite-terminal-tool"


class Edition(str, Enum):
    USER = "user"
    OPS = "ops"


class ApplicationUpdatePolicy(str, Enum):
    MANUAL = "manual"
    ONLINE = "online"


def build_edition() -> Edition:
    if getattr(sys, "frozen", False):
        path = Path(__file__).with_name("build_identity.json")
        return Edition(json.loads(path.read_text(encoding="utf-8"))["edition"])
    return Edition.OPS


def edition_of(settings=None) -> Edition:
    if getattr(sys, "frozen", False):
        return build_edition()
    return Edition(getattr(settings, "edition", build_edition()))


def application_update_policy(settings=None) -> ApplicationUpdatePolicy:
    """应用更新能力属于构建身份；源码入口显式提供相同 edition。"""
    return (ApplicationUpdatePolicy.ONLINE if edition_of(settings) is Edition.OPS
            else ApplicationUpdatePolicy.MANUAL)


def application_online_update_enabled(settings=None) -> bool:
    return application_update_policy(settings) is ApplicationUpdatePolicy.ONLINE


def create_build_identity(*, edition: Edition, version: str, platform: str,
                          architecture: str, build_id: str) -> dict[str, str]:
    """构建与发布共用一个身份；源码 SHA 与架构均明确记录。"""
    architecture = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(
        architecture.lower(), architecture.lower())
    if architecture not in ("x86_64", "arm64"):
        raise ValueError("build architecture requires x86_64 or arm64")
    if platform not in ("win", "mac"):
        raise ValueError("build platform requires win or mac")
    if re.fullmatch(r"[0-9a-f]{40}", build_id) is None:
        raise ValueError("build identity requires the exact source commit")
    if not version:
        raise ValueError("build identity requires a version")
    return {"app_id": APP_ID, "edition": Edition(edition).value, "version": version,
            "platform": platform, "architecture": architecture, "build_id": build_id}


def validate_payload_identity(root: Path, edition: Edition, version: str = "") -> None:
    """在替换旧安装前验证唯一载荷中的构建身份。"""
    from satellite_debug_tool.versioning import compare_versions

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
