"""可信目标下载；断点绑定摘要，最终校验后原子交付。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from threading import Event
import time
from typing import Callable, List, Optional
import urllib.error
import urllib.parse
import urllib.request

from satellite_debug_tool.updater.checker import Asset
from satellite_debug_tool.updater.errors import UpdaterError
from satellite_debug_tool.updater.network import HttpsEndpointError, build_https_opener


class DownloadError(UpdaterError):
    """可信目标下载失败。"""


class DownloadCancelled(DownloadError):
    def __init__(self) -> None:
        super().__init__("download_cancelled")


ProgressCallback = Callable[[int, int], None]


def _cancel(event: Event | None) -> None:
    if event is not None and event.is_set():
        raise DownloadCancelled()


def _hash(path: Path, event: Event | None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _cancel(event)
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class Downloader:
    def __init__(self, timeout: float = 8.0, max_retries: int = 3,
                 chunk_size: int = 65536, progress_interval: int = 262144):
        self._timeout = float(timeout)
        self._max_retries = max(1, int(max_retries))
        self._chunk_size = max(4096, int(chunk_size))
        self._progress_interval = max(self._chunk_size, int(progress_interval))

    def download_all(self, assets: List[Asset], dest_dir: Path,
                     on_progress: Optional[ProgressCallback] = None,
                     cancel_event: Optional[Event] = None,
                     opener: Optional[urllib.request.OpenerDirector] = None) -> List[Path]:
        if not assets:
            raise DownloadError("download_no_assets")
        dest_dir = Path(dest_dir)
        if not dest_dir.is_dir() or dest_dir.is_symlink():
            raise DownloadError("download_destination_missing", path=str(dest_dir))
        names = set()
        for asset in assets:
            name = asset.name
            if (not name or name in {".", ".."} or "/" in name or "\\" in name or ":" in name
                    or any(ord(c) < 32 for c in name) or name.casefold() in names):
                raise DownloadError("download_asset_invalid", "invalid or repeated filename")
            names.add(name.casefold())
            if (type(asset.size) is not int or asset.size < 0
                    or not re.fullmatch(r"[0-9a-f]{64}", getattr(asset, "sha256", ""))):
                raise DownloadError("download_asset_invalid", "trusted length and SHA-256 are required")
            if urllib.parse.urlsplit(asset.url).scheme != "https":
                raise DownloadError("download_asset_invalid", "HTTPS target is required")
        total = sum(a.size for a in assets)
        done = 0
        paths = []
        for asset in assets:
            _cancel(cancel_event)
            dest = dest_dir / asset.name
            self._download_one(asset, dest, done, total, on_progress, cancel_event, opener or build_https_opener())
            done += asset.size
            paths.append(dest)
        return paths

    def _download_one(self, asset, dest, done, total, progress, event, opener):
        partial = dest.with_name(dest.name + ".part")
        record = dest.with_name(dest.name + ".part.json")
        if any(p.is_symlink() for p in (dest, partial, record)):
            raise DownloadError("download_asset_invalid", "download paths require regular files")
        if dest.is_file() and dest.stat().st_size == asset.size and _hash(dest, event) == asset.sha256:
            if progress:
                progress(done + asset.size, total)
            return
        dest.unlink(missing_ok=True)
        identity = {"target_path": getattr(asset, "target_path", ""), "size": asset.size, "sha256": asset.sha256}
        state = {}
        if record.is_file() and record.stat().st_size <= 16384:
            try:
                state = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
        if not isinstance(state, dict) or state.get("identity") != identity:
            partial.unlink(missing_ok=True)
            state = {"identity": identity}
        if partial.exists() and partial.stat().st_size > asset.size:
            partial.unlink()
        self._save_state(record, state)
        for attempt in range(self._max_retries):
            _cancel(event)
            try:
                # 完整断点先校验，避免 bytes=<完整长度>- 引起 416。
                if partial.is_file() and partial.stat().st_size == asset.size:
                    self._finish(asset, partial, dest, record, event)
                else:
                    self._fetch(asset, partial, record, state, done, total, progress, event, opener)
                    self._finish(asset, partial, dest, record, event)
                if progress:
                    progress(done + asset.size, total)
                return
            except DownloadCancelled:
                raise
            except HttpsEndpointError as exc:
                partial.unlink(missing_ok=True)
                record.unlink(missing_ok=True)
                raise DownloadError("download_response_invalid", str(exc.reason)) from exc
            except DownloadError as exc:
                if exc.code in {"download_hash_mismatch", "download_range_invalid", "download_response_invalid"}:
                    partial.unlink(missing_ok=True)
                    record.unlink(missing_ok=True)
                    raise
                last_error = exc
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_error = exc
            if attempt + 1 < self._max_retries:
                if event is not None:
                    if event.wait(2**attempt):
                        raise DownloadCancelled()
                else:
                    time.sleep(2**attempt)
        raise DownloadError("download_retries_exhausted", str(last_error),
                            asset=asset.name, attempts=self._max_retries) from last_error

    @staticmethod
    def _save_state(record: Path, state: dict) -> None:
        temporary = record.with_suffix(record.suffix + ".tmp")
        if temporary.is_symlink():
            raise DownloadError("download_asset_invalid", "resume metadata requires a regular file")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, record)

    @staticmethod
    def _finish(asset, partial, dest, record, event):
        actual = partial.stat().st_size
        if actual != asset.size:
            raise DownloadError("download_size_mismatch", asset=asset.name, expected=asset.size, actual=actual)
        if _hash(partial, event) != asset.sha256:
            raise DownloadError("download_hash_mismatch", "downloaded bytes differ from the trusted target", asset=asset.name)
        os.replace(partial, dest)
        record.unlink(missing_ok=True)

    def _fetch(self, asset, partial, record, state, done, total, progress, event, opener):
        existing = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "satellite-terminal-updater", "Accept-Encoding": "identity"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
            etag = state.get("etag")
            if isinstance(etag, str) and etag.startswith('"') and etag.endswith('"'):
                headers["If-Range"] = etag
        request = urllib.request.Request(asset.url, headers=headers)
        with opener.open(request, timeout=self._timeout) as response:
            response_url = response.geturl() if hasattr(response, "geturl") else asset.url
            if urllib.parse.urlsplit(response_url).scheme != "https":
                raise DownloadError("download_response_invalid", "redirect requires HTTPS")
            status = response.status
            response_headers = response.headers
            if response_headers.get("Content-Encoding", "identity") not in {"identity", ""}:
                raise DownloadError("download_response_invalid", "encoded target response")
            if status == 200:
                existing = 0
                expected_response = asset.size
            elif status == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response_headers.get("Content-Range", ""))
                if match is None:
                    raise DownloadError("download_range_invalid", "Content-Range is required")
                start, end, length = map(int, match.groups())
                if start != existing or length != asset.size or not start <= end < length:
                    raise DownloadError("download_range_invalid", "Content-Range differs from the target and resume point")
                expected_response = end - start + 1
            else:
                raise DownloadError("download_http_error", asset=asset.name, status=status)
            declared = response_headers.get("Content-Length")
            if declared is not None and (not declared.isdecimal() or int(declared) != expected_response):
                raise DownloadError("download_response_invalid", "Content-Length differs from the trusted range")
            etag = response_headers.get("ETag")
            state.pop("etag", None)
            if isinstance(etag, str) and etag.startswith('"') and etag.endswith('"'):
                state["etag"] = etag
            self._save_state(record, state)
            received = 0
            pending = 0
            with partial.open("ab" if existing else "wb") as stream:
                while True:
                    _cancel(event)
                    block = response.read(min(self._chunk_size, expected_response - received + 1))
                    if not block:
                        break
                    received += len(block)
                    if received > expected_response:
                        raise DownloadError("download_response_invalid", "target response exceeds trusted length")
                    stream.write(block)
                    pending += len(block)
                    if progress and pending >= self._progress_interval:
                        progress(done + existing + received, total)
                        pending = 0
            if progress:
                progress(done + existing + received, total)
            if received != expected_response:
                raise DownloadError("download_size_mismatch", asset=asset.name, expected=expected_response, actual=received)
