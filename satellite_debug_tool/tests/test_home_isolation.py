"""每项测试拥有独立的用户配置和型号缓存目录。"""

import ntpath
from pathlib import Path

import pytest

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.profile import DeviceProfile, ProfileCache
from satellite_debug_tool.edition import Edition


@pytest.mark.parametrize("round_number", (1, 2))
def test_each_test_starts_with_its_own_home(tmp_path: Path, round_number: int) -> None:
    """两项使用相同配置键与型号；后一项从自己的空目录开始。"""
    assert Path.home() == tmp_path
    assert Path(ntpath.expanduser("~")) == tmp_path

    settings = Settings(edition=Edition.OPS)
    assert settings.config_directory == tmp_path / ".satellite_debug_tool"
    assert settings.get("test.home_isolation_round") is None
    settings.set("test.home_isolation_round", round_number)
    settings.save()
    assert Settings(edition=Edition.OPS).get("test.home_isolation_round") == round_number
    assert (settings.config_directory / "settings.json").is_file()

    cache = ProfileCache()
    assert cache.dir == settings.config_directory / "profiles"
    assert cache.load("test-home-isolation") is None
    cache.save(DeviceProfile(hw_type="test-home-isolation"))
    assert cache.path_for("test-home-isolation").is_file()
