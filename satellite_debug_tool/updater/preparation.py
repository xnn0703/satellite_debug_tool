"""把可信发行下载为完整包或完整文件树，交给同一个安装事务。"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from threading import Event
from typing import Callable
from uuid import uuid4
import zipfile

from satellite_debug_tool.updater.checker import Asset, LatestRelease
from satellite_debug_tool.updater.downloader import DownloadCancelled, DownloadError, Downloader
from satellite_debug_tool.updater.payload import PayloadError, apply_delta, read_manifest, verify_tree


@dataclass(frozen=True)
class PreparedDownload:
    package: Path
    asset: Asset
    tree_manifest: Path | None = None
    prepared_payload: Path | None = None
    installation_mode: str = "manual"


def prepare_download(latest: LatestRelease, directory: Path, *, install_dir: Path | None = None,
                     cancel_event: Event | None = None, on_progress=None,
                     on_status: Callable[[str], None] | None = None,
                     downloader: Downloader | None = None) -> PreparedDownload:
    if len(latest.assets) != 1:
        raise PayloadError("release requires one complete package")
    downloader = downloader or Downloader()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    def fetch(asset: Asset, subdirectory: str) -> Path:
        target = directory / subdirectory
        target.mkdir(exist_ok=True)
        return downloader.download_all([asset], target, on_progress=on_progress,
                                       cancel_event=cancel_event)[0]

    def status(source: str) -> None:
        if on_status is not None:
            on_status(source)

    complete = latest.assets[0]
    if latest.installation_mode == "manual":
        return PreparedDownload(fetch(complete, "packages"), complete)
    if latest.installation_mode != "automatic" or latest.tree_manifest is None:
        raise PayloadError("automatic release requires a trusted file-tree manifest")
    status("Downloading the trusted file manifest...")
    tree_path = fetch(latest.tree_manifest, "target-manifest")
    tree = read_manifest(tree_path)
    if tree["identity"] != latest.identity:
        raise PayloadError("file-tree identity differs from the trusted release")
    if install_dir is not None and latest.deltas:
        delta = latest.deltas[0]
        base_ready = False
        base = None
        try:
            from satellite_debug_tool.updater.install_platform import installed_identity
            installed = installed_identity(Path(install_dir))
            if not isinstance(installed, dict):
                raise PayloadError("installed build identity requires an object")
            if installed.get("build_id") == delta.base_build_id:
                status("Checking the installed files for an incremental update...")
                base_path = fetch(delta.base_manifest, "base-manifest")
                base = read_manifest(base_path)
                if base["identity"]["build_id"] != delta.base_build_id:
                    raise PayloadError("base manifest build differs from the delta contract")
                verify_tree(Path(install_dir), base, cancel_event)
                base_ready = True
        except DownloadCancelled:
            raise
        except (PayloadError, DownloadError, OSError, ValueError):
            status("The installed baseline differs; downloading the complete package...")
        if base_ready:
            try:
                status("Downloading the incremental package...")
                package = fetch(delta.asset, "packages")
                status("Reconstructing and verifying the complete new application...")
                payload = apply_delta(Path(install_dir), package, base, tree,
                                      directory / ("payload-" + uuid4().hex), cancel_event)
                return PreparedDownload(package, delta.asset, tree_path, payload, "automatic")
            except DownloadCancelled:
                raise
            except (PayloadError, DownloadError, zipfile.BadZipFile, ValueError):
                # 整包和增量指向同一可信目标；一次回退后沿用完整包校验。
                status("Incremental preparation failed; downloading the complete package...")
    status("Downloading the complete package...")
    return PreparedDownload(fetch(complete, "packages"), complete, tree_path, None, "automatic")
