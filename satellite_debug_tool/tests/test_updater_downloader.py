"""updater.downloader 单测：分卷下载 + 重试 + 断点续传 + 进度 + 取消。

所有 HTTP 通过自定义 OpenerDirector mock，不访问真实网络。
"""
from __future__ import annotations

import io
import urllib.error
from pathlib import Path
from threading import Event

import pytest

from satellite_debug_tool.updater.checker import Asset
from satellite_debug_tool.updater.downloader import (
    DownloadCancelled,
    DownloadError,
    Downloader,
)


# ============================ Mock HTTP ============================

class _FakeResponse:
    """模拟 urllib HTTPResponse：read(chunk)+close()+status+getcode()。"""
    def __init__(self, data: bytes, status: int = 200):
        self._buf = io.BytesIO(data)
        self.status = status

    def read(self, n=-1):
        return self._buf.read(n)

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._buf.close()


class _FakeOpener:
    """每次 open() 按顺序消费响应；支持注入异常。"""
    def __init__(self, responses):
        # responses: List[bytes | Exception | (bytes, status)]
        self._responses = list(responses)
        self.requests = []   # 记录被调用的 req（含 Range header）

    def open(self, req, timeout=None):
        self.requests.append(req)
        if not self._responses:
            raise AssertionError("耗尽 _FakeOpener 响应")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):
            data, status = r
            return _FakeResponse(data, status)
        return _FakeResponse(r, 200)


@pytest.fixture
def tmp_dl(tmp_path):
    """临时下载目录。"""
    return tmp_path


# ============================ 基本下载 ============================

class TestBasicDownload:
    def test_single_asset_success(self, tmp_dl):
        a = Asset(name="part.7z.001", url="http://x/1", size=10)
        op = _FakeOpener([b"0123456789"])
        dl = Downloader(max_retries=1)
        result = dl.download_all([a], tmp_dl, opener=op)
        assert len(result) == 1
        assert result[0].name == "part.7z.001"
        assert (tmp_dl / "part.7z.001").read_bytes() == b"0123456789"

    def test_multiple_assets_in_order(self, tmp_dl):
        assets = [
            Asset(name="x.7z.001", url="http://x/1", size=4),
            Asset(name="x.7z.002", url="http://x/2", size=4),
            Asset(name="x.7z.003", url="http://x/3", size=4),
        ]
        op = _FakeOpener([b"AAAA", b"BBBB", b"CCCC"])
        dl = Downloader(max_retries=1)
        paths = dl.download_all(assets, tmp_dl, opener=op)
        assert [p.name for p in paths] == ["x.7z.001", "x.7z.002", "x.7z.003"]
        assert (tmp_dl / "x.7z.001").read_bytes() == b"AAAA"
        assert (tmp_dl / "x.7z.002").read_bytes() == b"BBBB"
        assert (tmp_dl / "x.7z.003").read_bytes() == b"CCCC"

    def test_empty_asset_list_raises(self, tmp_dl):
        op = _FakeOpener([])
        dl = Downloader()
        with pytest.raises(DownloadError):
            dl.download_all([], tmp_dl, opener=op)

    def test_dest_dir_must_exist(self, tmp_path):
        a = Asset(name="x", url="http://x", size=1)
        dl = Downloader()
        with pytest.raises(DownloadError):
            dl.download_all([a], tmp_path / "nonexistent", opener=_FakeOpener([b"a"]))


# ============================ 重试 ============================

class TestRetry:
    def test_succeeds_after_one_failure(self, tmp_dl, monkeypatch):
        # 第一次 URLError，第二次成功
        a = Asset(name="x", url="http://x", size=3)
        op = _FakeOpener([urllib.error.URLError("boom"), b"xyz"])
        # 加速 sleep 避免测试慢
        monkeypatch.setattr("time.sleep", lambda *_: None)
        dl = Downloader(max_retries=3)
        paths = dl.download_all([a], tmp_dl, opener=op)
        assert paths[0].read_bytes() == b"xyz"

    def test_exhausted_retries_raises(self, tmp_dl, monkeypatch):
        a = Asset(name="x", url="http://x", size=3)
        op = _FakeOpener([
            urllib.error.URLError("e1"),
            urllib.error.URLError("e2"),
            urllib.error.URLError("e3"),
        ])
        monkeypatch.setattr("time.sleep", lambda *_: None)
        dl = Downloader(max_retries=3)
        with pytest.raises(DownloadError) as exc:
            dl.download_all([a], tmp_dl, opener=op)
        assert "重试 3 次仍失败" in str(exc.value)


# ============================ Content-Length 校验 ============================

class TestSizeValidation:
    def test_size_mismatch_triggers_retry(self, tmp_dl, monkeypatch):
        """声称 size=10 但实际只下到 5 → 视为失败 → 重试。"""
        a = Asset(name="x", url="http://x", size=10)
        # 第一次只返回 5 字节，第二次返回正常 10 字节
        op = _FakeOpener([b"01234", b"0123456789"])
        monkeypatch.setattr("time.sleep", lambda *_: None)
        dl = Downloader(max_retries=2)
        paths = dl.download_all([a], tmp_dl, opener=op)
        assert paths[0].read_bytes() == b"0123456789"

    def test_size_zero_means_no_check(self, tmp_dl):
        """asset.size = 0（API 未给）→ 不校验，按实际数据接收。"""
        a = Asset(name="x", url="http://x", size=0)
        op = _FakeOpener([b"any"])
        dl = Downloader(max_retries=1)
        paths = dl.download_all([a], tmp_dl, opener=op)
        assert paths[0].read_bytes() == b"any"


# ============================ 进度回调 ============================

class TestProgress:
    def test_progress_called(self, tmp_dl):
        a = Asset(name="x", url="http://x", size=10)
        op = _FakeOpener([b"0123456789"])
        captured = []
        dl = Downloader(max_retries=1, chunk_size=4096, progress_interval=4096)
        dl.download_all([a], tmp_dl, on_progress=lambda d, t: captured.append((d, t)),
                        opener=op)
        # 至少最后一次（收尾）必触发
        assert len(captured) >= 1
        # 最后一次的 d 应该 = 10
        assert captured[-1][0] == 10
        assert captured[-1][1] == 10

    def test_progress_total_sums_all_assets(self, tmp_dl):
        assets = [
            Asset(name="x.7z.001", url="http://x/1", size=10),
            Asset(name="x.7z.002", url="http://x/2", size=20),
        ]
        op = _FakeOpener([b"A" * 10, b"B" * 20])
        captured = []
        dl = Downloader(max_retries=1)
        dl.download_all(assets, tmp_dl, on_progress=lambda d, t: captured.append((d, t)),
                        opener=op)
        # 任何一次回调的 total 都应是 30
        assert all(t == 30 for _d, t in captured)
        # 最终累计 d = 30
        assert captured[-1][0] == 30


# ============================ 取消 ============================

class TestCancellation:
    def test_cancel_before_start_raises(self, tmp_dl):
        a = Asset(name="x", url="http://x", size=1)
        ev = Event()
        ev.set()
        op = _FakeOpener([b"a"])
        dl = Downloader(max_retries=1)
        with pytest.raises(DownloadCancelled):
            dl.download_all([a], tmp_dl, cancel_event=ev, opener=op)


# ============================ 断点续传 ============================

class TestResume:
    def test_existing_file_sends_range_header(self, tmp_dl):
        """目标文件已存在 5 字节，请求应带 Range: bytes=5-。"""
        a = Asset(name="x", url="http://x", size=10)
        (tmp_dl / "x").write_bytes(b"01234")   # pre-existing 5 bytes
        # 服务器响应 206 Partial：返回剩下 5 字节
        op = _FakeOpener([(b"56789", 206)])
        dl = Downloader(max_retries=1)
        dl.download_all([a], tmp_dl, opener=op)
        # 文件最终应该是完整 10 字节
        assert (tmp_dl / "x").read_bytes() == b"0123456789"
        # 请求 header 含 Range
        req = op.requests[0]
        assert req.get_header("Range") == "bytes=5-"

    def test_server_ignores_range_restarts(self, tmp_dl):
        """目标已存在，但服务器返回 200 全量 → 整个文件重写。"""
        a = Asset(name="x", url="http://x", size=10)
        (tmp_dl / "x").write_bytes(b"OLDXX")
        # 服务器忽略 Range，返回 200 + 完整内容
        op = _FakeOpener([(b"NEW0123456", 200)])
        dl = Downloader(max_retries=1)
        dl.download_all([a], tmp_dl, opener=op)
        assert (tmp_dl / "x").read_bytes() == b"NEW0123456"
