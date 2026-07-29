"""release.config.json 加载 + settings.update schema 单测。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


class TestReleaseConfigLoader:
    def test_defaults_returned_when_no_file(self, tmp_path):
        """override_path 指向不存在的文件 → 退回 DEFAULTS。"""
        from satellite_debug_tool.release_config import DEFAULTS, load_release_config
        cfg = load_release_config(override_path=tmp_path / "nope.json")
        for k in DEFAULTS:
            assert cfg[k] == DEFAULTS[k]

    def test_loads_explicit_file(self, tmp_path):
        from satellite_debug_tool.release_config import load_release_config
        p = tmp_path / "release.config.json"
        p.write_text(json.dumps({
            "github_owner": "myorg",
            "github_repo": "myrepo",
        }), encoding="utf-8")
        cfg = load_release_config(override_path=p)
        assert cfg["github_owner"] == "myorg"
        assert cfg["github_repo"] == "myrepo"
        # 没覆盖的字段用 DEFAULTS
        assert cfg["github_api"] == "https://api.github.com"

    def test_malformed_json_falls_back(self, tmp_path):
        """坏 JSON 不应崩溃，应回退 DEFAULTS。"""
        from satellite_debug_tool.release_config import DEFAULTS, load_release_config
        p = tmp_path / "release.config.json"
        p.write_text("{not valid json", encoding="utf-8")
        cfg = load_release_config(override_path=p)
        assert cfg == DEFAULTS

    def test_repo_root_file_exists(self):
        """开发模式下 repo 根的 release.config.json 应该真实存在。"""
        from satellite_debug_tool import release_config
        root_file = Path(release_config.__file__).resolve().parent.parent / "release.config.json"
        assert root_file.exists(), f"{root_file} 缺失，CI 与本地都需要这个文件"

    def test_default_config_loaded_in_dev(self):
        """无 override 时应能命中 repo 根的 release.config.json。"""
        from satellite_debug_tool.release_config import load_release_config
        cfg = load_release_config()
        for key in ("github_owner", "github_repo", "github_api"):
            assert key in cfg
            assert cfg[key]   # 非空


class TestSettingsUpdateSchema:
    @pytest.fixture
    def tmp_settings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        from satellite_debug_tool.core.config import Settings
        return Settings()

    def test_update_section_exists(self, tmp_settings):
        assert tmp_settings.get("update.auto_check") is True
        assert tmp_settings.get("update.check_interval_hours") == 24
        assert tmp_settings.get("update.last_check_iso") == ""
        assert tmp_settings.get("update.skip_version") == ""

    def test_update_keys_writable(self, tmp_settings, tmp_path):
        tmp_settings.set("update.auto_check", False)
        tmp_settings.set("update.skip_version", "v1.2.3")
        tmp_settings.save()
        # 重新 load 验证持久化
        from satellite_debug_tool.core.config import Settings
        s2 = Settings()
        assert s2.get("update.auto_check") is False
        assert s2.get("update.skip_version") == "v1.2.3"
