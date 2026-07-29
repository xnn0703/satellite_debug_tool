"""updater.checker 单测：版本比较 + API 解析（mock HTTP，不访问真实网络）。"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from satellite_debug_tool.updater.checker import (
    Asset,
    LatestRelease,
    ReleaseChecker,
    UpdateCheckError,
    compare_versions,
    current_platform,
)


# ============================ 版本比较 ============================

class TestCompareVersions:
    @pytest.mark.parametrize("a, b, expected", [
        # 简单整型递增
        ("1.0.0", "1.0.0", 0),
        ("1.0.0", "1.0.1", -1),
        ("1.0.1", "1.0.0", 1),
        ("1.2.0", "1.1.99", 1),
        # 数字而非字符串比较：10 > 9
        ("1.2.10", "1.2.9", 1),
        ("1.2.9", "1.2.10", -1),
        # v 前缀
        ("v1.0.0", "1.0.0", 0),
        ("v1.2.3", "v1.2.4", -1),
        # 段数不同
        ("1.0", "1.0.0", 0),
        ("1.0", "1.0.1", -1),
        # pre-release < release
        ("1.0.0-beta.1", "1.0.0", -1),
        ("1.0.0", "1.0.0-beta.1", 1),
        # 都有 pre：字典序
        ("1.0.0-alpha", "1.0.0-beta", -1),
        ("1.0.0-rc.2", "1.0.0-rc.1", 1),
        # 大版本差距
        ("2.0.0", "1.99.99", 1),
    ])
    def test_compare(self, a, b, expected):
        assert compare_versions(a, b) == expected


# ============================ Asset / LatestRelease 过滤 ============================

class TestAssetsForPlatform:
    def _make_release(self, asset_names):
        return LatestRelease(
            tag_name="v1.0.0",
            assets=[Asset(name=n, url=f"http://x/{n}", size=100) for n in asset_names],
        )

    def test_mac_assets_filtered(self):
        r = self._make_release([
            "satellite_debug_tool-mac-v1.0.0.7z.001",
            "satellite_debug_tool-mac-v1.0.0.7z.002",
            "satellite_debug_tool-win-v1.0.0.7z.001",
            "noise.txt",
        ])
        mac = r.assets_for_platform("mac")
        assert [a.name for a in mac] == [
            "satellite_debug_tool-mac-v1.0.0.7z.001",
            "satellite_debug_tool-mac-v1.0.0.7z.002",
        ]

    def test_win_assets_filtered(self):
        r = self._make_release([
            "satellite_debug_tool-win-v1.0.0.7z.001",
            "satellite_debug_tool-win-v1.0.0.7z.002",
            "satellite_debug_tool-win-v1.0.0.7z.003",
            "satellite_debug_tool-mac-v1.0.0.7z.001",
        ])
        win = r.assets_for_platform("win")
        assert len(win) == 3
        assert all("-win-" in a.name for a in win)

    def test_assets_sorted_by_name(self):
        """分卷名乱序也应排序为 001/002/003。"""
        r = self._make_release([
            "x-mac-v1.0.0.7z.003",
            "x-mac-v1.0.0.7z.001",
            "x-mac-v1.0.0.7z.002",
        ])
        names = [a.name for a in r.assets_for_platform("mac")]
        assert names == [
            "x-mac-v1.0.0.7z.001",
            "x-mac-v1.0.0.7z.002",
            "x-mac-v1.0.0.7z.003",
        ]

    def test_single_archive_is_supported_and_preferred(self):
        r = self._make_release([
            "satellite_debug_tool-win-v1.0.0.7z.001",
            "satellite_debug_tool-win-v1.0.0.7z.002",
            "satellite_debug_tool-win-v1.0.0.7z",
        ])
        win = r.assets_for_platform("win")
        assert [a.name for a in win] == [
            "satellite_debug_tool-win-v1.0.0.7z",
        ]

    def test_no_match_returns_empty(self):
        r = self._make_release(["readme.txt", "x-linux-v1.0.0.7z.001"])
        assert r.assets_for_platform("mac") == []


class TestCurrentPlatform:
    def test_returns_known_value(self):
        p = current_platform()
        assert p in ("mac", "win", "linux")


# ============================ ReleaseChecker (mock HTTP) ============================

class _FakeOpener:
    """通过自定义 OpenerDirector 拦截 urlopen 实现 mock。

    fixture 注入到 ReleaseChecker.fetch_latest(opener=...)，避免真实网络。
    """
    def __init__(self, payload=None, status=200, raise_exc=None):
        self._payload = payload
        self._status = status
        self._raise = raise_exc

    def open(self, req, timeout=None):
        if self._raise is not None:
            raise self._raise
        body = json.dumps(self._payload).encode("utf-8") if isinstance(self._payload, (dict, list)) \
            else (self._payload or b"")
        if isinstance(body, str):
            body = body.encode("utf-8")
        resp = io.BytesIO(body)
        # urllib 要求 response 有 read() + close()，BytesIO 都有
        return resp


class _SequenceOpener:
    def __init__(self, responses):
        self._responses = list(responses)
        self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class TestReleaseChecker:
    def _checker(self):
        return ReleaseChecker(owner="test", repo="repo", api_base="https://api.github.example")

    def test_latest_url_format(self):
        c = self._checker()
        assert c.latest_url == "https://api.github.example/repos/test/repo/releases/latest"

    def test_fetch_success(self):
        c = self._checker()
        opener = _FakeOpener(payload={
            "tag_name": "v1.0.0",
            "body": "release notes here",
            "html_url": "https://github.example/r",
            "assets": [
                {"name": "x-mac-v1.0.0.7z.001",
                 "browser_download_url": "https://dl/1",
                 "size": 1024},
                {"name": "x-mac-v1.0.0.7z.002",
                 "browser_download_url": "https://dl/2",
                 "size": 2048},
            ]
        })
        r = c.fetch_latest(opener=opener)
        assert r.tag_name == "v1.0.0"
        assert r.body == "release notes here"
        assert len(r.assets) == 2
        assert r.assets[0].name == "x-mac-v1.0.0.7z.001"
        assert r.assets[0].size == 1024

    def test_fetch_handles_missing_optional_fields(self):
        c = self._checker()
        opener = _FakeOpener(payload={"tag_name": "v0.5"})
        r = c.fetch_latest(opener=opener)
        assert r.tag_name == "v0.5"
        assert r.body == ""
        assert r.assets == []

    def test_fetch_skips_invalid_assets(self):
        c = self._checker()
        opener = _FakeOpener(payload={
            "tag_name": "v1",
            "assets": [
                {"name": "valid", "browser_download_url": "https://dl"},   # 缺 size 应默认 0
                {"name": "bad-no-url"},                                     # 跳过
                {"browser_download_url": "https://nope"},                   # 跳过
                "not-a-dict",                                               # 跳过
            ]
        })
        r = c.fetch_latest(opener=opener)
        assert len(r.assets) == 1
        assert r.assets[0].name == "valid"
        assert r.assets[0].size == 0

    def test_http_error_raises_update_check_error(self):
        c = self._checker()
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        opener = _FakeOpener(raise_exc=err)
        with pytest.raises(UpdateCheckError) as exc:
            c.fetch_latest(opener=opener)
        assert "404" in str(exc.value)

    def test_network_error_raises_update_check_error(self):
        c = self._checker()
        err = urllib.error.URLError("no route")
        opener = _FakeOpener(raise_exc=err)
        with pytest.raises(UpdateCheckError):
            c.fetch_latest(opener=opener)

    def test_github_rate_limit_falls_back_to_latest_redirect(self):
        c = ReleaseChecker(owner="xnn0703", repo="satellite_debug_tool")
        rate_limit = urllib.error.HTTPError(
            c.latest_url,
            403,
            "rate limit exceeded",
            {},
            None,
        )
        opener = _SequenceOpener([
            rate_limit,
            urllib.error.HTTPError(
                "https://github.com/xnn0703/satellite_debug_tool/releases/latest",
                302,
                "Found",
                {
                    "Location": (
                        "https://github.com/xnn0703/"
                        "satellite_debug_tool/releases/tag/v1.1.0"
                    ),
                },
                None,
            ),
        ])

        release = c.fetch_latest(opener=opener)

        assert release.tag_name == "v1.1.0"
        assert release.html_url.endswith("/releases/tag/v1.1.0")
        assert [a.name for a in release.assets_for_platform("win")] == [
            "satellite_debug_tool-win-v1.1.0.7z",
        ]
        assert release.assets[0].url.endswith(
            "/releases/download/v1.1.0/satellite_debug_tool-win-v1.1.0.7z",
        )
        assert opener.urls == [
            c.latest_url,
            "https://github.com/xnn0703/satellite_debug_tool/releases/latest",
        ]

    def test_bad_json_raises(self):
        c = self._checker()
        opener = _FakeOpener(payload=b"not valid json{")
        with pytest.raises(UpdateCheckError):
            c.fetch_latest(opener=opener)

    def test_missing_tag_name_raises(self):
        c = self._checker()
        opener = _FakeOpener(payload={"body": "no tag"})
        with pytest.raises(UpdateCheckError):
            c.fetch_latest(opener=opener)
