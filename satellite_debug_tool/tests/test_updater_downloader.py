"""可信下载回归：摘要、断点身份、HTTP 范围、取消；全部使用本地替身。"""
from __future__ import annotations

import hashlib
import io
import json
from threading import Event
import urllib.error
import urllib.request
import urllib.response
from email.message import Message

import pytest

from satellite_debug_tool.updater.checker import Asset
from satellite_debug_tool.updater.downloader import DownloadCancelled, DownloadError, Downloader
from satellite_debug_tool.updater.network import build_https_opener


def asset(data=b"0123456789", name="package.7z"):
    return Asset(name, "https://updates.example/" + name, len(data), hashlib.sha256(data).hexdigest(), name)


class Response:
    def __init__(self, data, status=200, headers=None, callback=None):
        self.data = io.BytesIO(data)
        self.status = status
        self.headers = headers or {}
        self.callback = callback

    def read(self, count):
        if self.callback:
            self.callback()
        return self.data.read(count)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.data.close()


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def resume(directory, item, data, **extra):
    (directory / (item.name + ".part")).write_bytes(data)
    state = {"identity": {"target_path": item.target_path, "size": item.size, "sha256": item.sha256}, **extra}
    (directory / (item.name + ".part.json")).write_text(json.dumps(state))


def test_download_and_final_digest(tmp_path):
    item = asset()
    progress = []
    paths = Downloader().download_all([item], tmp_path, opener=Opener(Response(b"0123456789")),
                                     on_progress=lambda *p: progress.append(p))
    assert paths[0].read_bytes() == b"0123456789"
    assert progress[-1] == (10, 10)
    assert list(tmp_path.iterdir()) == paths


def test_multiple_targets_have_combined_progress(tmp_path):
    items = [asset(b"ab", "a"), asset(b"xyz", "b")]
    progress = []
    paths = Downloader().download_all(items, tmp_path, opener=Opener(Response(b"ab"), Response(b"xyz")),
                                     on_progress=lambda *p: progress.append(p))
    assert [p.read_bytes() for p in paths] == [b"ab", b"xyz"]
    assert progress[-1] == (5, 5)


@pytest.mark.parametrize("item", [
    Asset("x", "https://x/x", 3, ""),
    Asset("../x", "https://x/x", 3, "a" * 64),
    Asset("x", "http://x/x", 3, "a" * 64),
])
def test_missing_trust_path_and_plain_http_are_rejected(tmp_path, item):
    opener = Opener()
    with pytest.raises(DownloadError):
        Downloader().download_all([item], tmp_path, opener=opener)
    assert opener.requests == []


@pytest.mark.parametrize("redirect", ["http://updates.example/package.7z", "https://name:secret@updates.example/package.7z"])
@pytest.mark.parametrize("consumer", ["download", "metadata"])
def test_https_redirect_rejected_before_next_request(tmp_path, monkeypatch, redirect, consumer):
    """真实 urllib 重定向链：不向降级地址或含凭据地址发出第二次请求。"""
    requests = []

    class RedirectServer(urllib.request.BaseHandler):
        handler_order = 300

        def https_open(self, request):
            requests.append(request.full_url)
            headers = Message()
            headers["Location"] = redirect
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            response.msg = "Found"
            return response

        def http_open(self, request):
            requests.append(request.full_url)
            raise AssertionError("plain HTTP request was sent")

    opener = build_https_opener()
    opener.add_handler(RedirectServer())
    if consumer == "download":
        monkeypatch.setattr("satellite_debug_tool.updater.downloader.build_https_opener", lambda: opener)
        with pytest.raises(DownloadError) as caught:
            Downloader().download_all([asset()], tmp_path)
        assert caught.value.code == "download_response_invalid"
    else:
        from tuf.api.exceptions import DownloadError as TufDownloadError
        from satellite_debug_tool.updater.repository import HttpsFetcher
        monkeypatch.setattr("satellite_debug_tool.updater.repository.build_https_opener", lambda: opener)
        with pytest.raises(TufDownloadError):
            list(HttpsFetcher(1, Event())._fetch(asset().url))
    assert requests == [asset().url]


def test_wrong_bytes_with_correct_length_are_rejected(tmp_path):
    with pytest.raises(DownloadError) as caught:
        Downloader().download_all([asset()], tmp_path, opener=Opener(Response(b"XXXXXXXXXX")))
    assert caught.value.code == "download_hash_mismatch"
    assert not (tmp_path / "package.7z").exists()


def test_valid_range_resume_and_etag(tmp_path):
    item = asset()
    resume(tmp_path, item, b"01234", etag='"first"')
    opener = Opener(Response(b"56789", 206, {"Content-Range": "bytes 5-9/10", "Content-Length": "5"}))
    path = Downloader().download_all([item], tmp_path, opener=opener)[0]
    assert path.read_bytes() == b"0123456789"
    assert opener.requests[0].get_header("Range") == "bytes=5-"
    assert opener.requests[0].get_header("If-range") == '"first"'


@pytest.mark.parametrize("content_range", ["bytes 4-8/10", "bytes 5-9/20", "", "bytes 5-15/10"])
def test_incorrect_content_range_is_never_appended(tmp_path, content_range):
    item = asset()
    resume(tmp_path, item, b"01234")
    with pytest.raises(DownloadError) as caught:
        Downloader().download_all([item], tmp_path,
            opener=Opener(Response(b"56789", 206, {"Content-Range": content_range})))
    assert caught.value.code == "download_range_invalid"
    assert not (tmp_path / item.name).exists()


def test_range_ignored_200_rewrites_partial(tmp_path):
    item = asset()
    resume(tmp_path, item, b"OLD")
    path = Downloader().download_all([item], tmp_path, opener=Opener(Response(b"0123456789")))[0]
    assert path.read_bytes() == b"0123456789"


def test_another_target_partial_is_discarded(tmp_path):
    resume(tmp_path, asset(b"ABCDEFGHIJ"), b"ABCDE")
    opener = Opener(Response(b"0123456789"))
    Downloader().download_all([asset()], tmp_path, opener=opener)
    assert opener.requests[0].get_header("Range") is None


def test_complete_cache_and_complete_partial_avoid_416_request(tmp_path):
    item = asset()
    resume(tmp_path, item, b"0123456789")
    opener = Opener()
    Downloader().download_all([item], tmp_path, opener=opener)
    Downloader().download_all([item], tmp_path, opener=opener)
    assert opener.requests == []


@pytest.mark.parametrize("headers,data", [
    ({"Content-Length": "5"}, b"0123456789"),
    ({"Content-Encoding": "gzip"}, b"0123456789"),
    ({}, b"01234567890"),
])
def test_response_bounds_and_encoding(tmp_path, headers, data):
    with pytest.raises(DownloadError) as caught:
        Downloader().download_all([asset()], tmp_path, opener=Opener(Response(data, headers=headers)))
    assert caught.value.code == "download_response_invalid"


def test_network_retry_keeps_valid_partial(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    opener = Opener(Response(b"01234"), Response(b"56789", 206, {"Content-Range": "bytes 5-9/10"}))
    path = Downloader(max_retries=2).download_all([asset()], tmp_path, opener=opener)[0]
    assert path.read_bytes() == b"0123456789"
    assert opener.requests[1].get_header("Range") == "bytes=5-"


def test_retry_exhaustion_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    opener = Opener(urllib.error.URLError("down"), urllib.error.URLError("still down"))
    with pytest.raises(DownloadError) as caught:
        Downloader(max_retries=2).download_all([asset()], tmp_path, opener=opener)
    assert caught.value.code == "download_retries_exhausted"
    assert caught.value.context["attempts"] == 2


def test_cancel_interrupts_read_loop_and_preserves_resume(tmp_path):
    event = Event()
    opener = Opener(Response(b"0123456789", callback=event.set))
    with pytest.raises(DownloadCancelled):
        Downloader().download_all([asset()], tmp_path, opener=opener, cancel_event=event)
    assert not (tmp_path / "package.7z").exists()
    assert (tmp_path / "package.7z.part.json").exists()


def test_empty_known_target_is_verified(tmp_path):
    path = Downloader().download_all([asset(b"")], tmp_path, opener=Opener(Response(b"")))[0]
    assert path.read_bytes() == b""


def test_unknown_length_cannot_accept_arbitrary_bytes(tmp_path):
    with pytest.raises(DownloadError):
        Downloader().download_all([asset(b"")], tmp_path, opener=Opener(Response(b"unexpected")))


def test_empty_targets_or_missing_destination(tmp_path):
    with pytest.raises(DownloadError):
        Downloader().download_all([], tmp_path)
    with pytest.raises(DownloadError):
        Downloader().download_all([asset()], tmp_path / "missing")


def test_symlink_download_path_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve")
    (tmp_path / "package.7z").symlink_to(outside)
    with pytest.raises(DownloadError):
        Downloader().download_all([asset()], tmp_path)
    assert outside.read_bytes() == b"preserve"
