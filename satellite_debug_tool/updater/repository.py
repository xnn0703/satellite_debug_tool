"""TUF 可信元数据边界；缓存刷新由一个进程 owner 持锁完成。"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import threading
import time
from typing import Iterator, Mapping
import urllib.error
import urllib.parse
import urllib.request

from tuf.api.exceptions import DownloadError, DownloadHTTPError, RepositoryError as TufRepositoryError
from tuf.api.metadata import Metadata, Root, TargetFile
from tuf.ngclient import Updater, UpdaterConfig
from tuf.ngclient.fetcher import FetcherInterface

from satellite_debug_tool.updater.errors import UpdaterError
from satellite_debug_tool.updater.network import HttpsEndpointError, build_https_opener


class RepositoryError(UpdaterError):
    """可信仓库的配置、网络或文件合同错误。"""


def safe_component(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None:
        raise ValueError("repository identity requires a safe path component")
    return value


def target_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("target path requires a bounded relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(p in (".", "..") for p in path.parts):
        raise ValueError("target path requires canonical relative components")
    if re.fullmatch(r"[A-Za-z0-9._+/-]+", value) is None:
        raise ValueError("target path contains unsupported characters")
    return value


def https_base(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("update endpoint requires HTTPS")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("update endpoint requires an HTTPS base URL")
    return value.rstrip("/") + "/"


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def repository_lock(path: Path, timeout: float = 10.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RepositoryError("check_cache_busy", "trusted metadata cache is in use") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)


class HttpsFetcher(FetcherInterface):
    def __init__(self, timeout: float, cancel_event: threading.Event):
        self.timeout = timeout
        self.cancel_event = cancel_event
        self.opener = build_https_opener()

    def _fetch(self, url: str) -> Iterator[bytes]:
        if urllib.parse.urlsplit(url).scheme != "https":
            raise DownloadError("trusted update requests require HTTPS")
        if self.cancel_event.is_set():
            raise DownloadError("update check cancelled")
        request = urllib.request.Request(url, headers={"User-Agent": "SatelliteTerminalTool-Updater/1"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                    raise DownloadError("update redirect requires HTTPS")
                while True:
                    if self.cancel_event.is_set():
                        raise DownloadError("update check cancelled")
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
        except urllib.error.HTTPError as exc:
            raise DownloadHTTPError(str(exc.reason), exc.code) from exc
        except HttpsEndpointError as exc:
            raise DownloadError(str(exc.reason)) from exc


class TrustedRepository:
    """refresh、目标查询与迁移都位于同一 repository_lock 内。"""

    def __init__(self, config: Mapping, cache_dir: Path, *, fetcher: FetcherInterface):
        if config.get("configuration_error"):
            raise RepositoryError("check_configuration_error", str(config["configuration_error"]))
        if config.get("schema") != "satellite.update-config/v1":
            raise RepositoryError("check_configuration_error", "unsupported release configuration schema")
        if not config.get("metadata_base_url") or not config.get("target_base_url") or not config.get("trusted_root"):
            raise RepositoryError("check_source_unconfigured", "HTTPS update endpoints and trusted root await deployment")
        self.app_id = safe_component(config.get("app_id"))
        self.repo_id = safe_component(config.get("repo_id"))
        self.metadata_url = https_base(config["metadata_base_url"])
        self.targets_url = https_base(config["target_base_url"])
        self.bootstrap = json.dumps(config["trusted_root"]).encode("utf-8")
        root = Metadata.from_bytes(self.bootstrap)
        if not isinstance(root.signed, Root):
            raise ValueError("bootstrap requires TUF root metadata")
        self._check_root(root.signed)
        self.cache_dir = Path(cache_dir) / self.app_id / "ops" / self.repo_id
        self.metadata_dir = self.cache_dir / "metadata"
        self.target_dir = self.cache_dir / "targets"
        self.fetcher = fetcher
        self.updater: Updater | None = None
        self.source_generation = 0
        self.bootstrap_endpoints = (self.metadata_url, self.targets_url)
        self._restore_source()

    def _restore_source(self) -> None:
        """保存的入口仅决定目的地址；固定 bootstrap 在每次刷新仍独立验证新源。"""
        path = self.cache_dir / "source-state.json"
        if not path.exists():
            return
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema") != "satellite.update-source-state/v1" or value.get("app_id") != self.app_id or value.get("repo_id") != self.repo_id:
            raise ValueError("saved source state identity differs")
        if type(value.get("generation")) is not int or value["generation"] < 1:
            raise ValueError("saved source generation is invalid")
        self.metadata_url = https_base(value["metadata_base_url"])
        self.targets_url = https_base(value["target_base_url"])
        self.source_generation = value["generation"]

    def _check_root(self, root: Root) -> None:
        if root.unrecognized_fields.get("app_id") != self.app_id or root.unrecognized_fields.get("repo_id") != self.repo_id:
            raise ValueError("root identity differs from configured application/repository")
        if root.consistent_snapshot is not True:
            raise ValueError("release repository requires consistent snapshots")

    def refresh(self, *, allow_bootstrap_recovery: bool = True) -> None:
        previous = (self.metadata_url, self.targets_url)
        try:
            self._refresh_current()
        except DownloadError:
            if not allow_bootstrap_recovery or previous == self.bootstrap_endpoints:
                raise
            self.metadata_url, self.targets_url = self.bootstrap_endpoints
            try:
                self._refresh_current()
            except Exception:
                self.metadata_url, self.targets_url = previous
                raise

    def _refresh_current(self) -> None:
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.target_dir.mkdir(parents=True, exist_ok=True)
        self.updater = Updater(
            str(self.metadata_dir), self.metadata_url, str(self.target_dir), self.targets_url,
            fetcher=self.fetcher, config=UpdaterConfig(app_user_agent="SatelliteTerminalTool/1"),
            bootstrap=self.bootstrap,
        )
        self.updater.refresh()
        self._check_root(Metadata.from_file(str(self.metadata_dir / "root.json")).signed)

    def info(self, path: str) -> TargetFile:
        if self.updater is None:
            raise RuntimeError("repository refresh is required")
        info = self.updater.get_targetinfo(target_path(path))
        if info is None:
            raise RepositoryError("check_target_missing", path)
        if re.fullmatch(r"[0-9a-f]{64}", info.hashes.get("sha256", "")) is None:
            raise ValueError("release target requires SHA-256")
        return info

    def url(self, info: TargetFile) -> str:
        path = PurePosixPath(target_path(info.path))
        digest = next(iter(info.hashes.values()))
        hashed_path = str(path.with_name(f"{digest}.{path.name}"))
        return self.targets_url + urllib.parse.quote(hashed_path, safe="/")

    def read_json(self, path: str, *, max_length: int = 256 * 1024) -> tuple[dict, TargetFile]:
        info = self.info(path)
        if info.length > max_length:
            raise ValueError("release JSON exceeds its bounded size")
        assert self.updater is not None
        local_path = self.updater.find_cached_target(info) or self.updater.download_target(info)
        value = json.loads(Path(local_path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("release JSON requires an object")
        return value, info

    def migrate_sources(self, catalog_path: str) -> None:
        """签名配置指定新地址，新源独立完成 TUF 校验后才保存供下次发现使用。"""
        assert self.updater is not None
        if self.updater.get_targetinfo("bootstrap/sources.json") is None:
            return
        source, _ = self.read_json("bootstrap/sources.json", max_length=64 * 1024)
        if source.get("schema") != "satellite.update-sources/v1" or source.get("app_id") != self.app_id or source.get("repo_id") != self.repo_id:
            raise ValueError("signed source identity differs")
        generation = source.get("generation")
        if type(generation) is not int or generation < max(1, self.source_generation):
            raise ValueError("signed source generation moved backwards")
        if generation == self.source_generation:
            return
        candidates = source.get("repositories")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 4:
            raise ValueError("signed source configuration requires one to four candidates")
        endpoints = [(https_base(item["metadata_base_url"]), https_base(item["target_base_url"])) for item in candidates]
        previous = (self.metadata_url, self.targets_url, self.updater)
        for metadata_url, targets_url in endpoints:
            try:
                self.metadata_url, self.targets_url = metadata_url, targets_url
                self.refresh(allow_bootstrap_recovery=False)
                self.read_json(catalog_path)
            except (OSError, ValueError, RepositoryError, DownloadError, TufRepositoryError):
                self.metadata_url, self.targets_url, self.updater = previous
                continue
            atomic_json(self.cache_dir / "source-state.json", {
                "schema": "satellite.update-source-state/v1", "app_id": self.app_id, "repo_id": self.repo_id,
                "generation": generation, "metadata_base_url": metadata_url, "target_base_url": targets_url,
            })
            self.source_generation = generation
            return
        self.metadata_url, self.targets_url, self.updater = previous
