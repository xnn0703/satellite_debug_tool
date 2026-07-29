"""ReleaseChecker — 查 Gitee Release API + 版本比较。

Gitee Release API 文档：
    GET https://gitee.com/api/v5/repos/{owner}/{repo}/releases/latest
返回 JSON 形如：
    {
      "tag_name": "v1.2.0",
      "name": "...",
      "body": "release notes",
      "html_url": "https://gitee.com/...",
      "assets": [
        {"name": "satellite_debug_tool-mac-v1.2.0.7z.001",
         "browser_download_url": "https://...",
         "size": 31457280},
        ...
      ]
    }

平台资产命名约定（CI 输出）：
    `satellite_debug_tool-{mac,win}-{tag}.7z.{NNN}`  (NNN = 001/002/...)
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from satellite_debug_tool.updater.errors import UpdaterError


# ---------- 异常 ----------

class UpdateCheckError(UpdaterError):
    """检查更新失败（网络/解析/HTTP 错误的统一包装）。"""


# ---------- 数据类 ----------

@dataclass
class Asset:
    name: str
    url: str           # browser_download_url
    size: int = 0      # 字节


@dataclass
class LatestRelease:
    tag_name: str
    body: str = ""
    html_url: str = ""
    assets: List[Asset] = field(default_factory=list)

    def assets_for_platform(self, platform: str) -> List[Asset]:
        """按平台过滤资产并按名称排序（保证 7z 分卷顺序：001/002/...）。

        Args:
            platform: "mac" / "win"（current_platform() 返回值）
        """
        pat = re.compile(rf"-{re.escape(platform)}-.*\.7z\.\d+$", re.IGNORECASE)
        matched = [a for a in self.assets if pat.search(a.name)]
        return sorted(matched, key=lambda a: a.name)


# ---------- 平台 ----------

def current_platform() -> str:
    """返回 'mac' / 'win' / 'linux'。"""
    if sys.platform.startswith("darwin"):
        return "mac"
    if sys.platform.startswith("win"):
        return "win"
    return "linux"


# ---------- 版本比较 ----------

_VERSION_PART_RE = re.compile(r"^(\d+)(.*)$")


def _split_version(s: str) -> tuple[tuple, str]:
    """把 'v1.2.10-beta.1' → ((1,2,10), 'beta.1')；'v1.2.3' → ((1,2,3), '')。

    返回 (release_tuple, prerelease_str)。
    宽松：非数字部分忽略数字后缀。
    """
    s = s.strip().lstrip("vV")
    if "-" in s:
        release, pre = s.split("-", 1)
    elif "+" in s:
        release, pre = s.split("+", 1)
    else:
        release, pre = s, ""
    parts: List[int] = []
    for chunk in release.split("."):
        m = _VERSION_PART_RE.match(chunk.strip())
        if m:
            parts.append(int(m.group(1)))
        else:
            # 完全非数字 → 0；保证比较不抛
            parts.append(0)
    return tuple(parts), pre.strip()


def compare_versions(a: str, b: str) -> int:
    """比较两个版本字符串。

    返回:
        -1: a < b（b 更新）
         0: a == b
        +1: a > b

    支持：
        - 前缀 v（"v1.2.3" == "1.2.3"）
        - 数字段元素比较（"1.2.10" > "1.2.9"）
        - pre-release：有 pre 的版本 < 无 pre 的同 release（"1.0.0-beta" < "1.0.0"）
        - 同 release 都有 pre：pre 字符串字典序比较
    """
    ra, pa = _split_version(a)
    rb, pb = _split_version(b)
    # 对齐长度（短的补 0）
    n = max(len(ra), len(rb))
    ra2 = tuple(list(ra) + [0] * (n - len(ra)))
    rb2 = tuple(list(rb) + [0] * (n - len(rb)))
    if ra2 < rb2:
        return -1
    if ra2 > rb2:
        return 1
    # release 相同，比 pre-release
    if pa == pb:
        return 0
    if not pa and pb:
        return 1   # a 是 release > b 是 pre
    if pa and not pb:
        return -1  # a 是 pre < b 是 release
    # 都有 pre：字典序
    return -1 if pa < pb else 1


# ---------- Checker ----------

class ReleaseChecker:
    """查 Gitee Release API 拿最新版本。

    Args:
        owner / repo / api_base: 一般从 release.config.json 读
        timeout: HTTP 超时秒
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        api_base: str = "https://gitee.com/api/v5",
        timeout: float = 10.0,
    ):
        self._owner = owner
        self._repo = repo
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    @property
    def latest_url(self) -> str:
        return f"{self._api_base}/repos/{self._owner}/{self._repo}/releases/latest"

    def fetch_latest(self, opener: Optional[urllib.request.OpenerDirector] = None) -> LatestRelease:
        """GET API → 解析 → 返回 LatestRelease。

        opener 仅用于单测注入 mock。任何 HTTP / 解析错误都抛 UpdateCheckError。
        """
        req = urllib.request.Request(
            self.latest_url,
            headers={"Accept": "application/json", "User-Agent": "satellite_debug_tool-updater"},
        )
        try:
            if opener is not None:
                resp = opener.open(req, timeout=self._timeout)
            else:
                resp = urllib.request.urlopen(req, timeout=self._timeout)
            with resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise UpdateCheckError(
                "check_http_error",
                str(e.reason),
                status=e.code,
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise UpdateCheckError("check_network_error", str(e)) from e

        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise UpdateCheckError("check_response_parse_error", str(e)) from e

        if not isinstance(data, dict) or "tag_name" not in data:
            raise UpdateCheckError(
                "check_response_format_error",
                str(data)[:200],
            )

        assets_raw = data.get("assets") or []
        assets: List[Asset] = []
        for a in assets_raw:
            if not isinstance(a, dict):
                continue
            name = a.get("name") or ""
            url = a.get("browser_download_url") or ""
            if not name or not url:
                continue
            assets.append(Asset(
                name=str(name),
                url=str(url),
                size=int(a.get("size") or 0),
            ))

        return LatestRelease(
            tag_name=str(data.get("tag_name") or ""),
            body=str(data.get("body") or ""),
            html_url=str(data.get("html_url") or ""),
            assets=assets,
        )
