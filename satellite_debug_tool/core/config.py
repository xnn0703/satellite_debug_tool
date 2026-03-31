import json
import os
from pathlib import Path
from typing import Any, Optional


class Settings:
    DEFAULT_CONFIG = {
        "general": {"connection_type": "Serial"},
        "serial": {"default_baudrate": "115200", "last_port": ""},
        "udp": {"remote_ip": "192.168.1.12", "remote_port": 4004, "local_port": 45678},
        "ui": {"time_window": 10.0, "theme": "Dark", "max_visible_channels": 8},
        "recording": {"default_path": ""},
    }

    def __init__(self):
        self._config_dir = Path.home() / ".satellite_debug_tool"
        self._config_file = self._config_dir / "settings.json"
        self._config: dict = {}
        self._load()

    def _load(self):
        # 如果文件不存在，使用默认配置
        self._config = self.DEFAULT_CONFIG.copy()

        if self._config_file.exists():
            try:
                with open(self._config_file, "r", encoding="utf-8") as f:
                    user_config = json.load(f)
                    # 合并到默认配置
                    self._merge_config(self._config, user_config)
            except (json.JSONDecodeError, IOError):
                # 如果读取失败，使用默认配置
                pass

    def _merge_config(self, default: dict, user: dict):
        """递归合并用户配置到默认配置"""
        for key, value in user.items():
            if (
                key in default
                and isinstance(default[key], dict)
                and isinstance(value, dict)
            ):
                self._merge_config(default[key], value)
            else:
                default[key] = value

    def save(self):
        # 确保目录存在
        self._config_dir.mkdir(parents=True, exist_ok=True)
        # 写入 settings.json
        with open(self._config_file, "w", encoding="utf-8") as f:
            json.dump(self._config, f, indent=4, ensure_ascii=False)

    def get(self, key_path: str, default: Any = None) -> Any:
        # 支持点分隔路径，如 "ui.theme" 返回 self._config["ui"]["theme"]
        keys = key_path.split(".")
        value = self._config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def set(self, key_path: str, value: Any):
        # 支持点分隔路径，如 set("ui.theme", "Light")
        keys = key_path.split(".")
        config = self._config
        for key in keys[:-1]:
            if key not in config:
                config[key] = {}
            config = config[key]
        config[keys[-1]] = value
