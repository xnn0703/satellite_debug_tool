import copy
import json
import os
from pathlib import Path
from typing import Any, Optional


class Settings:
    _LEGACY_TAB_IDS = {
        "实时": "live",
        "Live": "live",
        "live": "live",
        "回放": "playback",
        "Playback": "playback",
        "playback": "playback",
        "Log": "log",
        "log": "log",
        "设备": "device",
        "Device": "device",
        "device": "device",
    }

    DEFAULT_CONFIG = {
        "general": {"connection_type": "Serial"},
        "serial": {"default_baudrate": "115200", "last_port": ""},
        "udp": {"remote_ip": "192.168.1.12", "remote_port": 4004, "local_port": 45678},
        "ui": {
            "time_window": 10.0,
            "theme": "Dark",
            "language": "auto",
            "active_tab_id": "live",
            "max_visible_channels": 8,
            # M10 F3b：Live Tab 主体 QSplitter 的列宽（左 ChannelPanel + chart + attitude + state/event）
            "live_top_splitter_sizes": [],
        },
        "recording": {"default_path": ""},  # 旧字段，保留兼容，不再使用
        "paths": {
            "recording_dir": "",  # Live 录制 / Playback 加载 .sdb 的默认目录
            "log_dir": "",        # Log Tab 导入 WindTerm .log 的默认目录
            "firmware_dir": "",   # Device Tab OTA 选择 .bin 的默认目录
        },
        # M10 F1/F2：chart UX 持久化
        "chart": {
            "normalize": False,                  # F1：归一化 toggle 状态
            "custom_groups": {},                 # F2：{hw_type: {gid_str: {"title": str, "channels": [name, ...]}}}
        },
        # M11：自动升级相关
        "update": {
            "auto_check": True,                  # 启动后台静默检查（默认开）
            "check_interval_hours": 24,          # 距上次检查不足该时长不重复查（避免频繁请求 Release API）
            "last_check_iso": "",                # 上次检查时间（UTC ISO 字符串）
            "skip_version": "",                  # 用户跳过的 tag；同 tag 不再弹窗
        },
        # 地图：天地图在线 token（WGS-84 坐标，与 GPS 一致）；空则回落 OSM 离线缓存
        "map": {
            "tianditu_token": "",                # lbs.tianditu.gov.cn 申请的应用密钥(tk)
        },
    }

    def __init__(self):
        self._config_dir = Path.home() / ".satellite_debug_tool"
        self._config_file = self._config_dir / "settings.json"
        self._config: dict = {}
        self._load()

    def _load(self):
        # deepcopy 防止 _merge_config 把用户值写进 DEFAULT_CONFIG 的子 dict
        # （shallow copy 会让多个 Settings 实例共享嵌套 dict 引用 → 跨实例污染）
        self._config = copy.deepcopy(self.DEFAULT_CONFIG)

        migrated = False
        if self._config_file.exists():
            try:
                with open(self._config_file, "r", encoding="utf-8") as f:
                    user_config = json.load(f)
                    migrated = self._migrate_user_config(user_config)
                    # 合并到默认配置
                    self._merge_config(self._config, user_config)
            except (json.JSONDecodeError, IOError):
                # 如果读取失败，使用默认配置
                pass
        if migrated:
            self.save()

    def _migrate_user_config(self, config: dict) -> bool:
        """Migrate display-text settings to stable IDs before merging defaults."""
        ui = config.get("ui")
        if not isinstance(ui, dict) or "active_tab" not in ui:
            return False
        legacy = ui.pop("active_tab")
        if "active_tab_id" not in ui:
            ui["active_tab_id"] = self._LEGACY_TAB_IDS.get(str(legacy), "live")
        return True

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

    def remove(self, key_path: str) -> bool:
        """删除某个键，返回是否真的删掉了。"""
        keys = key_path.split(".")
        config = self._config
        for key in keys[:-1]:
            if not isinstance(config, dict) or key not in config:
                return False
            config = config[key]
        if isinstance(config, dict) and keys[-1] in config:
            del config[keys[-1]]
            return True
        return False
