"""Downloader — 分卷顺序下载 + 重试 + 断点续传 + 进度回调。

公共 API：
    Downloader(timeout=30, max_retries=3, chunk_size=65536, progress_interval=262144)
        .download_all(assets, dest_dir, on_progress=None, cancel_event=None) -> List[Path]

设计要点：
- 顺序下载（不并行）：实现简单 + Gitee API 友好 + 弱网更稳
- 单分卷失败：指数退避 1s / 2s / 4s 重试 3 次（max_retries 可配）
- 断点续传：dest 已存在且部分内容时发 Range header，服务器 206 接续，
  服务器 200 视为不支持 Range 重头下载
- Content-Length 校验：响应头有 length 时与实际写入字节比，不一致视为失败重试
- 进度回调：每累计 progress_interval 字节调一次，避免 GUI 信号过密
- 取消：threading.Event；read 循环每 chunk 检查
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, List, Optional

from satellite_debug_tool.updater.checker import Asset
from satellite_debug_tool.updater.errors import UpdaterError


class DownloadError(UpdaterError):
    """下载失败（重试耗尽 / 校验失败 / 用户取消的统一包装）。"""


class DownloadCancelled(DownloadError):
    """用户主动取消下载。"""

    def __init__(self) -> None:
        super().__init__("download_cancelled")


ProgressCallback = Callable[[int, int], None]   # (bytes_done_total, bytes_total)


@dataclass
class _AssetResult:
    asset: Asset
    path: Path
    bytes_written: int


class Downloader:
    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = 3,
        chunk_size: int = 65536,
        progress_interval: int = 262144,
    ):
        self._timeout = timeout
        self._max_retries = max(1, int(max_retries))
        self._chunk_size = max(4096, int(chunk_size))
        self._progress_interval = max(self._chunk_size, int(progress_interval))

    def download_all(
        self,
        assets: List[Asset],
        dest_dir: Path,
        on_progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[Event] = None,
        opener: Optional[urllib.request.OpenerDirector] = None,
    ) -> List[Path]:
        """顺序下载所有 assets 到 dest_dir，返回 (按顺序的)本地路径列表。

        Args:
            assets: 待下载分卷列表（顺序即写入顺序）
            dest_dir: 目标目录（必须已存在）
            on_progress: 进度回调，参数 (bytes_done_total, bytes_total)
            cancel_event: 用户取消事件（set 后立即中止并抛 DownloadCancelled）
            opener: 测试用 mock urllib OpenerDirector
        """
        if not assets:
            raise DownloadError("download_no_assets")
        if not dest_dir.exists() or not dest_dir.is_dir():
            raise DownloadError(
                "download_destination_missing",
                path=str(dest_dir),
            )

        total_bytes = sum(a.size for a in assets if a.size > 0)
        done_bytes = 0
        results: List[Path] = []

        for asset in assets:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled()
            r = self._download_one(
                asset=asset,
                dest=dest_dir / asset.name,
                done_offset=done_bytes,
                total_bytes=total_bytes,
                on_progress=on_progress,
                cancel_event=cancel_event,
                opener=opener,
            )
            results.append(r.path)
            done_bytes += r.bytes_written

        return results

    # ---- 内部 ----

    def _download_one(
        self,
        asset: Asset,
        dest: Path,
        done_offset: int,
        total_bytes: int,
        on_progress: Optional[ProgressCallback],
        cancel_event: Optional[Event],
        opener: Optional[urllib.request.OpenerDirector],
    ) -> _AssetResult:
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled()
            try:
                written = self._fetch_to_file(
                    asset, dest,
                    done_offset=done_offset,
                    total_bytes=total_bytes,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                    opener=opener,
                )
                # Content-Length 校验
                if asset.size > 0 and dest.stat().st_size != asset.size:
                    raise DownloadError(
                        "download_size_mismatch",
                        asset=asset.name,
                        expected=asset.size,
                        actual=dest.stat().st_size,
                    )
                return _AssetResult(asset=asset, path=dest, bytes_written=written)
            except DownloadCancelled:
                raise
            except (DownloadError, urllib.error.URLError, OSError, TimeoutError) as e:
                last_exc = e
                # 指数退避（除最后一次）
                if attempt < self._max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
        raise DownloadError(
            "download_retries_exhausted",
            str(last_exc or ""),
            asset=asset.name,
            attempts=self._max_retries,
        ) from last_exc

    def _fetch_to_file(
        self,
        asset: Asset,
        dest: Path,
        done_offset: int,
        total_bytes: int,
        on_progress: Optional[ProgressCallback],
        cancel_event: Optional[Event],
        opener: Optional[urllib.request.OpenerDirector],
    ) -> int:
        """一次 HTTP GET + 流式写文件，返回写入字节数（不含已存在部分）。"""
        # 断点续传：dest 已存在 → 发 Range header
        existing = dest.stat().st_size if dest.exists() else 0
        headers = {"User-Agent": "satellite_debug_tool-updater"}
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"

        req = urllib.request.Request(asset.url, headers=headers)
        if opener is not None:
            resp = opener.open(req, timeout=self._timeout)
        else:
            resp = urllib.request.urlopen(req, timeout=self._timeout)

        with resp:
            status = getattr(resp, "status", None) or resp.getcode()
            # 206 = Partial Content（接受 Range）；200 = 全量（服务器不支持 Range）
            if existing > 0 and status == 200:
                # 服务器不支持 Range → 重写整个文件
                existing = 0
                mode = "wb"
            elif status in (200, 206):
                mode = "ab" if existing > 0 else "wb"
            else:
                raise DownloadError(
                    "download_http_error",
                    asset=asset.name,
                    status=status,
                )

            written_this_call = 0
            bytes_since_last_progress = 0
            with open(dest, mode) as f:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelled()
                    chunk = resp.read(self._chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    written_this_call += len(chunk)
                    bytes_since_last_progress += len(chunk)
                    if on_progress and bytes_since_last_progress >= self._progress_interval:
                        bytes_done_total = done_offset + existing + written_this_call
                        on_progress(bytes_done_total, total_bytes)
                        bytes_since_last_progress = 0

            # 收尾：最后一次进度
            if on_progress:
                on_progress(done_offset + existing + written_this_call, total_bytes)
            return existing + written_this_call
