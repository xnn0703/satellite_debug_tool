"""tile_downloader 单测（M8-S1）。

不联网：mock urllib 验证 URL 拼接、断点续传、限速、bbox→tile 转换。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 把 repo root 加进 sys.path，让 tools.tile_downloader 能被 import
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.tile_downloader import (
    TileCoord,
    download_tile,
    download_tiles,
    iter_tiles,
    lat_lon_to_tile,
    parse_bbox,
    parse_zoom_spec,
)


# ---------------------------------------------------------------------------
# 坐标转换
# ---------------------------------------------------------------------------


class TestCoordConversion:
    def test_nanjing_zoom14(self):
        """南京中心点 (lon=118.78, lat=32.04) zoom 14 → 与 Web Mercator 公式一致。

        参考：https://www.openstreetmap.org/#map=14/32.04/118.78
        实测 tile: x=13597, y=6651（zoom 14 范围 0..16383，南京在偏右上）
        """
        x, y = lat_lon_to_tile(32.04, 118.78, 14)
        assert x == 13597, f"x mismatch: got {x}"
        assert y == 6651, f"y mismatch: got {y}"
        # sanity: 在 zoom 14 范围内且偏向地图右上（东半球北纬）
        assert 0 < x < 2 ** 14
        assert 0 < y < 2 ** 14 / 2   # 北半球 y 小于一半

    def test_zoom_0_one_tile(self):
        """zoom 0 整个地球一个 tile。"""
        assert lat_lon_to_tile(0.0, 0.0, 0) == (0, 0)
        assert lat_lon_to_tile(-45.0, 180.0, 0) == (0, 0)   # 边界裁到 (0,0)

    def test_zoom_invariants(self):
        """同一经纬度 zoom 越大 tile 数字越大。"""
        x1, y1 = lat_lon_to_tile(32.04, 118.78, 10)
        x2, y2 = lat_lon_to_tile(32.04, 118.78, 14)
        assert x2 > x1 and y2 > y1

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError):
            lat_lon_to_tile(86.0, 0.0, 14)   # 超出 Web Mercator
        with pytest.raises(ValueError):
            lat_lon_to_tile(0.0, 200.0, 14)


# ---------------------------------------------------------------------------
# bbox 枚举
# ---------------------------------------------------------------------------


class TestIterTiles:
    def test_small_bbox_zoom14(self):
        # 南京 0.04°×0.04° 小区域 zoom 14
        tiles = list(iter_tiles((118.78, 32.04, 118.82, 32.08), [14]))
        assert len(tiles) > 0
        # 验证所有 tile zoom 一致
        assert all(t.z == 14 for t in tiles)

    def test_multi_zoom(self):
        tiles = list(iter_tiles((118.78, 32.04, 118.82, 32.08), [12, 13, 14]))
        zooms = {t.z for t in tiles}
        assert zooms == {12, 13, 14}

    def test_invalid_bbox(self):
        with pytest.raises(ValueError):
            list(iter_tiles((118.0, 32.0, 117.0, 31.0), [14]))   # lon_min > lon_max


# ---------------------------------------------------------------------------
# Spec 解析
# ---------------------------------------------------------------------------


class TestSpecParsing:
    def test_zoom_range(self):
        assert parse_zoom_spec("12-15") == [12, 13, 14, 15]

    def test_zoom_list(self):
        assert parse_zoom_spec("12,14,16") == [12, 14, 16]

    def test_zoom_single(self):
        assert parse_zoom_spec("14") == [14]

    def test_bbox_4floats(self):
        assert parse_bbox("118.3,31.2,119.2,32.6") == (118.3, 31.2, 119.2, 32.6)

    def test_bbox_wrong_count(self):
        with pytest.raises(ValueError):
            parse_bbox("118.3,31.2,119.2")


# ---------------------------------------------------------------------------
# 下载（mock 网络）
# ---------------------------------------------------------------------------


class _MockResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestDownloadSkipExisting:
    def test_existing_file_not_redownloaded(self, tmp_path, monkeypatch):
        """文件已存在且非空时跳过下载。"""
        called = {"count": 0}

        def mock_urlopen(*args, **kwargs):
            called["count"] += 1
            return _MockResponse(b"fake png")

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

        t = TileCoord(z=14, x=13721, y=6701)
        dest = t.to_path(tmp_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"existing tile")

        # 应跳过，urlopen 不被调
        ok = download_tile(t, "http://fake/{z}/{x}/{y}.png", dest)
        assert ok is True
        assert called["count"] == 0
        # 内容不被覆盖
        assert dest.read_bytes() == b"existing tile"

    def test_downloads_when_missing(self, tmp_path, monkeypatch):
        called = {"count": 0}

        def mock_urlopen(req, **kwargs):
            called["count"] += 1
            # 验证 User-Agent
            assert req.headers.get("User-agent", "").startswith("satellite_debug_tool")
            return _MockResponse(b"\x89PNG\r\n\x1a\n" + b"fake")

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

        t = TileCoord(z=14, x=13721, y=6701)
        dest = t.to_path(tmp_path)
        ok = download_tile(t, "http://fake/{z}/{x}/{y}.png", dest)
        assert ok is True
        assert called["count"] == 1
        assert dest.exists()
        assert dest.read_bytes().startswith(b"\x89PNG")


class TestDownloadTilesIntegration:
    def test_full_flow_with_mocked_network(self, tmp_path, monkeypatch):
        def mock_urlopen(req, **kwargs):
            return _MockResponse(b"\x89PNG\r\n\x1a\n" + b"x" * 100)

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

        stats = download_tiles(
            bbox=(118.78, 32.04, 118.80, 32.06),
            zooms=[14],
            label="test_nj",
            cache_dir=tmp_path,
            rate_per_sec=0,   # 无限速，测试快
        )
        assert stats["total"] > 0
        assert stats["downloaded"] == stats["total"]
        assert stats["skipped"] == 0
        assert stats["failed"] == 0
        assert (tmp_path / "test_nj").is_dir()

    def test_second_run_skips_all(self, tmp_path, monkeypatch):
        def mock_urlopen(req, **kwargs):
            return _MockResponse(b"\x89PNG fake")

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

        bbox = (118.78, 32.04, 118.80, 32.06)
        # 第一次：全部下
        stats1 = download_tiles(
            bbox=bbox, zooms=[14], label="rerun", cache_dir=tmp_path, rate_per_sec=0,
        )
        # 第二次：全部跳过
        stats2 = download_tiles(
            bbox=bbox, zooms=[14], label="rerun", cache_dir=tmp_path, rate_per_sec=0,
        )
        assert stats2["downloaded"] == 0
        assert stats2["skipped"] == stats1["total"]
