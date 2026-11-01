"""从 TUF 可信渠道清单选择本应用的发行；包名不参与身份判断。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import platform as host_platform
import re
import sys
import threading
from typing import Mapping

from satellite_debug_tool.edition import Edition, application_online_update_enabled, build_edition
from satellite_debug_tool.updater.errors import UpdaterError
from satellite_debug_tool.versioning import compare_versions, parse_version


class UpdateCheckError(UpdaterError):
    """检查阶段可呈现的稳定错误。"""


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int = 0
    sha256: str = ""
    target_path: str = ""


@dataclass(frozen=True)
class DeltaRelease:
    asset: Asset
    base_manifest: Asset
    base_manifest_sha256: str
    target_manifest_sha256: str
    base_build_id: str


@dataclass
class LatestRelease:
    tag_name: str
    body: str = ""
    html_url: str = ""
    assets: list[Asset] = field(default_factory=list)
    identity: dict = field(default_factory=dict)
    tree_manifest: Asset | None = None
    deltas: tuple[DeltaRelease, ...] = ()
    installation_mode: str = "manual"
    catalog_sha256: str = ""

    def assets_for_platform(self, platform: str, *, edition=None) -> list[Asset]:
        selected = build_edition() if edition is None else Edition(edition)
        if selected is Edition.USER:
            return []
        canonical = canonical_platform(platform)
        if self.identity.get("edition") != selected.value or self.identity.get("platform") != canonical:
            return []
        return list(self.assets)


def current_platform() -> str:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("win"):
        return "win"
    return "linux"


def canonical_platform(value: str) -> str:
    result = {"win": "windows", "mac": "macos", "windows": "windows", "macos": "macos", "linux": "linux"}.get(value)
    if result is None:
        raise ValueError("unsupported update platform")
    return result


def current_architecture() -> str:
    if getattr(sys, "frozen", False):
        import json
        path = Path(__file__).resolve().parents[1] / "build_identity.json"
        value = json.loads(path.read_text(encoding="utf-8")).get("architecture")
    else:
        value = host_platform.machine().lower()
    result = {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(value)
    if result is None:
        raise ValueError("unsupported update architecture")
    return result


def _os_tuple(value: str) -> tuple[int, ...]:
    if re.fullmatch(r"\d+(?:\.\d+){0,3}", value) is None:
        raise ValueError("OS version requires numeric components")
    return tuple(int(part) for part in value.split("."))


class ReleaseChecker:
    def __init__(
        self, config: Mapping, *, edition: Edition = Edition.OPS,
        cache_dir: Path | None = None, platform: str | None = None,
        architecture: str | None = None, timeout: float = 8.0, fetcher=None,
        updater_version: str = "1.0.0", config_schema: int = 1,
        os_version: str | None = None,
    ):
        self.config = dict(config)
        self.edition = Edition(edition)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path.home() / ".satellite_debug_tool" / "updates" / "trust"
        self.platform = canonical_platform(platform or current_platform())
        self.architecture = architecture or current_architecture()
        self.timeout = timeout
        self.fetcher = fetcher
        self.updater_version = updater_version
        self.config_schema = config_schema
        self.os_version = os_version
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def fetch_latest(self) -> LatestRelease:
        if self.edition is Edition.USER or (getattr(sys, "frozen", False) and not application_online_update_enabled()):
            raise UpdateCheckError("check_manual_edition", "this application edition uses manual installation")
        try:
            from satellite_debug_tool.updater.repository import HttpsFetcher, TrustedRepository, repository_lock, safe_component
            channel = safe_component(self.config.get("channel"))
            if channel not in ("stable", "beta") or self.architecture not in ("x86_64", "arm64"):
                raise ValueError("unsupported update channel or architecture")
            fetcher = self.fetcher or HttpsFetcher(self.timeout, self._cancel_event)
            repository = TrustedRepository(self.config, self.cache_dir, fetcher=fetcher)
            catalog_path = f"catalog/ops/{channel}/{self.platform}/{self.architecture}.json"
            with repository_lock(repository.cache_dir / "refresh.lock"):
                repository.refresh()
                repository.migrate_sources(catalog_path)
                catalog, info = repository.read_json(catalog_path)
                release = self._parse_catalog(catalog, repository)
                release.catalog_sha256 = info.hashes["sha256"]
                return release
        except UpdateCheckError:
            raise
        except UpdaterError as exc:
            raise UpdateCheckError(exc.code, exc.detail, **exc.context) from exc
        except Exception as exc:
            if self._cancel_event.is_set():
                raise UpdateCheckError("check_cancelled", "update check cancelled") from exc
            raise UpdateCheckError("check_trust_error", str(exc)) from exc

    def _parse_catalog(self, catalog: dict, repository) -> LatestRelease:
        if catalog.get("schema") != "satellite.update-catalog/v1":
            raise ValueError("unsupported release catalog schema")
        identity = catalog.get("identity")
        if not isinstance(identity, dict):
            raise ValueError("catalog requires release identity")
        expected = {"app_id": self.config["app_id"], "edition": "ops", "platform": self.platform, "architecture": self.architecture}
        if any(identity.get(key) != value for key, value in expected.items()):
            raise ValueError("release identity differs from this application")
        if catalog.get("channel") != self.config["channel"]:
            raise ValueError("release channel differs")
        version = identity.get("version")
        parsed = parse_version(version, complete=True)
        if self.config["channel"] == "stable" and parsed.prerelease:
            raise ValueError("stable channel requires a stable version")
        if not isinstance(identity.get("build_id"), str) or re.fullmatch(r"[0-9a-f]{40}", identity["build_id"]) is None:
            raise ValueError("catalog requires an exact source commit identity")
        if type(catalog.get("update_protocol")) is not int or catalog["update_protocol"] != 1:
            raise ValueError("release requires an unsupported update protocol")
        minimum = catalog.get("min_updater_version")
        parse_version(minimum, complete=True)
        if compare_versions(self.updater_version, minimum) < 0:
            raise ValueError("release requires a newer updater; manual installation is available")
        config_range = catalog.get("config_schema")
        if not isinstance(config_range, dict) or any(type(config_range.get(k)) is not int for k in ("min", "max")):
            raise ValueError("release requires explicit configuration schema support")
        if not config_range["min"] <= self.config_schema <= config_range["max"]:
            raise ValueError("release configuration schema differs")
        minimum_os = _os_tuple(catalog.get("min_os", ""))
        host_os = self.os_version
        if host_os is None:
            host_os = host_platform.mac_ver()[0] if self.platform == "macos" else host_platform.version()
        host_tuple = _os_tuple(host_os)
        length = max(len(minimum_os), len(host_tuple))
        if host_tuple + (0,) * (length - len(host_tuple)) < minimum_os + (0,) * (length - len(minimum_os)):
            raise ValueError("release requires a newer operating system")
        automatic = self.platform == "windows"
        expected_layout = "windows-onedir-v1" if automatic else "macos-app-v1"
        if self.platform not in ("windows", "macos") or catalog.get("layout_version") != expected_layout:
            raise ValueError("release layout differs from supported installer")
        if catalog.get("package_format") != ("7z" if automatic else "zip"):
            raise ValueError("release archive format differs")
        mode = "automatic" if automatic else "manual"
        if catalog.get("installation_mode") != mode:
            raise ValueError("release installation policy differs")

        def asset(path: str) -> Asset:
            info = repository.info(path)
            digest = info.hashes["sha256"]
            return Asset(f"{digest}.{PurePosixPath(info.path).name}", repository.url(info), info.length, digest, info.path)

        complete = asset(catalog["package"])
        tree = asset(catalog["tree_manifest"]) if automatic else None
        deltas: list[DeltaRelease] = []
        declared_deltas = catalog.get("deltas", [])
        if not isinstance(declared_deltas, list) or len(declared_deltas) > 1 or (declared_deltas and not automatic):
            raise ValueError("release supports one previous Windows baseline delta")
        for item in declared_deltas:
            base = asset(item["base_manifest"])
            candidate = asset(item["package"])
            if item.get("base_manifest_sha256") != base.sha256 or item.get("target_manifest_sha256") != tree.sha256:
                raise ValueError("delta manifest identity differs from trusted targets")
            base_build_id = item.get("base_build_id")
            if not isinstance(base_build_id, str) or re.fullmatch(r"[0-9a-f]{40}", base_build_id) is None or base_build_id == identity["build_id"]:
                raise ValueError("delta requires a distinct exact base build")
            if candidate.size < complete.size:
                deltas.append(DeltaRelease(candidate, base, base.sha256, tree.sha256, base_build_id))
        return LatestRelease(
            tag_name=str(version), body=str(catalog.get("notes", "")),
            html_url=str(catalog.get("release_url", "")), assets=[complete],
            identity=dict(identity), tree_manifest=tree, deltas=tuple(deltas), installation_mode=mode,
        )
