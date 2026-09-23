import base64
import copy
import hashlib
import ipaddress
import json
import logging
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class SettingsSaveError(RuntimeError):
    """Raised when settings cannot be committed without losing evidence."""


class DeviceSettingsMigrationError(ValueError):
    """Raised when legacy device settings do not have one deterministic result."""


class SettingsPersistenceResult(str, Enum):
    """Affirmative outcome of an ordinary preference persistence attempt."""

    PERSISTED = "persisted"
    MEMORY_ONLY = "memory_only"
    FAILED = "failed"


class _AtomicWriteError(OSError):
    """Internal write failure with an explicit replace commit boundary."""

    def __init__(self, message: str, *, committed: bool) -> None:
        super().__init__(message)
        self.committed = bool(committed)


class Settings:
    REBUILD_CONFIRMATION = "REBUILD_DEVICE_SETTINGS"

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
        # 旧 ``udp.remote_*`` / ``udp.local_port`` 只在原始文件迁移阶段读取，
        # 不再作为运行期权威状态源。
        "udp": {},
        "device_udp": {"local_port": 45678},
        "external_power": {"host": ""},
        "iperf": {
            "executable": "",
            "server": "60.205.157.141",
            "local_host": "",
            "protocol": "udp",
            "direction": "both",
            "ul_port": 5201,
            "dl_port": 5202,
            "ul_rate": "491K",
            "dl_rate": "200K",
            "continuous": True,
            "duration_hours": 24.0,
        },
        "customer": {"devices": [], "active_endpoint": None},
        "config_migrations": {
            "device_udp_port_v1": True,
            "customer_devices_v1": True,
        },
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
            "production_dir": "",  # 批量试产数据库、SDB 和报告输出根目录
            "production_report_dir": "",  # 每台设备正式报告与证据归档根目录
        },
        "production": {
            "last_operator": "",
            "last_recipe": "",
            "discovery_cidr": "192.168.1.0/24",
            "device_port": 4004,
            "max_devices": 4,
            "fixture_profile_id": "",
            "ms6222_port": "",
            "product_template_id": "",
            "station_profile_id": "",
            "device_count": 1,
            "report_branding": {
                "company_name": "",
                "logo_path": "",
                "header": "",
                "footer": "",
                "tester_role": "",
                "reviewer_role": "",
            },
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
        self._backup_file = self._config_dir / "settings.json.bak"
        self._config: dict = {}
        self._read_only_recovery = False
        self._device_configuration_blocked = False
        self._device_configuration_error = ""
        self._load_diagnostics: list[str] = []
        self._preference_persistence_notice_emitted = False
        self._load()

    def _load(self):
        """Load, validate and migrate the raw JSON before applying defaults."""

        self._config = copy.deepcopy(self.DEFAULT_CONFIG)
        if not self._config_file.exists():
            return

        user_config: dict[str, Any]
        try:
            user_config = self._read_json_object(self._config_file)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            self._load_diagnostics.append(f"main settings invalid: {exc}")
            try:
                user_config = self._read_verified_backup()
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as backup_exc:
                self._load_diagnostics.append(f"backup settings invalid: {backup_exc}")
                self._read_only_recovery = True
                self._device_configuration_blocked = True
                self._device_configuration_error = "settings main and backup are invalid"
                return

            self._load_diagnostics.append("settings restored from verified backup")
            try:
                self._atomic_write_bytes(
                    self._config_file,
                    self._json_bytes(user_config),
                )
            except _AtomicWriteError as exc:
                self._load_diagnostics.append(f"main settings restore failed: {exc}")
                self._merge_config(self._config, user_config)
                self._read_only_recovery = True
                self._device_configuration_blocked = True
                self._device_configuration_error = (
                    "verified settings backup could not be restored durably"
                )
                return

        migrated = False
        candidate = copy.deepcopy(user_config)
        try:
            migrated = self._migrate_user_config(candidate)
        except DeviceSettingsMigrationError as exc:
            self._device_configuration_blocked = True
            self._device_configuration_error = str(exc)
            self._load_diagnostics.append(f"device settings migration blocked: {exc}")
            self._merge_config(self._config, user_config)
            return
        self._merge_config(self._config, candidate)
        if migrated:
            try:
                self.save()
            except SettingsSaveError as exc:
                self._read_only_recovery = True
                self._device_configuration_blocked = True
                self._device_configuration_error = "device settings migration commit failed"
                self._load_diagnostics.append(f"device settings migration save failed: {exc}")

    def _migrate_user_config(self, config: dict) -> bool:
        """Migrate display-text settings to stable IDs before merging defaults."""
        if not isinstance(config, dict):
            raise DeviceSettingsMigrationError("settings root must be a JSON object")
        migrated = self._migrate_device_udp_port(config)
        migrated = self._migrate_customer_devices(config) or migrated
        ui = config.get("ui")
        if not isinstance(ui, dict) or "active_tab" not in ui:
            return migrated
        legacy = ui.pop("active_tab")
        if "active_tab_id" not in ui:
            ui["active_tab_id"] = self._LEGACY_TAB_IDS.get(str(legacy), "live")
        return True

    @staticmethod
    def _valid_port(value: object) -> int:
        if isinstance(value, bool):
            raise DeviceSettingsMigrationError("UDP local port must be an integer")
        if isinstance(value, int):
            port = value
        elif isinstance(value, str) and value.isascii() and value.isdecimal():
            port = int(value, 10)
        else:
            raise DeviceSettingsMigrationError("UDP local port must be an integer")
        if not 1 <= port <= 65535:
            raise DeviceSettingsMigrationError("UDP local port must be in 1..65535")
        return port

    @classmethod
    def _normalize_customer_device(cls, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise DeviceSettingsMigrationError("customer device must be an object")
        raw_ip = str(value.get("ip", "")).strip()
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise DeviceSettingsMigrationError(f"invalid customer IPv4 endpoint: {raw_ip}") from exc
        if (
            ip.version != 4
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
            or int(ip) == 0xFFFFFFFF
        ):
            raise DeviceSettingsMigrationError(f"customer endpoint is not IPv4 unicast: {raw_ip}")
        port = cls._valid_port(value.get("port"))
        return {"ip": str(ip), "port": port}

    @classmethod
    def _normalize_customer_devices(cls, value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            raise DeviceSettingsMigrationError("customer.devices must be a list")
        if len(value) > 4:
            raise DeviceSettingsMigrationError("customer.devices supports at most 4 endpoints")
        result: list[dict[str, object]] = []
        seen: set[tuple[str, int]] = set()
        for raw in value:
            item = cls._normalize_customer_device(raw)
            endpoint = (str(item["ip"]), int(item["port"]))
            if endpoint in seen:
                raise DeviceSettingsMigrationError(
                    f"duplicate customer endpoint: {endpoint[0]}:{endpoint[1]}"
                )
            seen.add(endpoint)
            result.append(item)
        return result

    @classmethod
    def _normalize_active_endpoint(
        cls,
        value: object,
        devices: list[dict[str, object]],
    ) -> dict[str, object] | None:
        if value is None:
            return None
        active = cls._normalize_customer_device(value)
        endpoint = (str(active["ip"]), int(active["port"]))
        members = {(str(item["ip"]), int(item["port"])) for item in devices}
        return active if endpoint in members else None

    @classmethod
    def _migrate_device_udp_port(cls, config: dict[str, Any]) -> bool:
        migrations = config.get("config_migrations")
        if migrations is not None and not isinstance(migrations, dict):
            raise DeviceSettingsMigrationError("config_migrations must be an object")
        migrations = migrations if isinstance(migrations, dict) else {}
        device_udp = config.get("device_udp")
        if device_udp is not None and not isinstance(device_udp, dict):
            raise DeviceSettingsMigrationError("device_udp must be an object")
        marker_present = "device_udp_port_v1" in migrations
        if marker_present and migrations["device_udp_port_v1"] is not True:
            raise DeviceSettingsMigrationError(
                "device_udp_port_v1 migration marker must be true"
            )
        marker = marker_present
        if marker:
            if not isinstance(device_udp, dict) or "local_port" not in device_udp:
                raise DeviceSettingsMigrationError(
                    "device_udp migration marker exists without local_port"
                )
            normalized = cls._valid_port(device_udp["local_port"])
            changed = device_udp.get("local_port") != normalized
            device_udp["local_port"] = normalized
            udp = config.get("udp")
            if isinstance(udp, dict) and "local_port" in udp:
                udp.pop("local_port")
                changed = True
            production = config.get("production")
            if isinstance(production, dict) and "local_port" in production:
                production.pop("local_port")
                changed = True
            return changed

        if isinstance(device_udp, dict) and "local_port" in device_udp:
            selected = cls._valid_port(device_udp["local_port"])
        else:
            udp = config.get("udp")
            production = config.get("production")
            udp_present = isinstance(udp, dict) and "local_port" in udp
            production_present = isinstance(production, dict) and "local_port" in production
            udp_port = cls._valid_port(udp["local_port"]) if udp_present else None
            production_port = (
                cls._valid_port(production["local_port"])
                if production_present
                else None
            )
            if udp_port is None and production_port is None:
                selected = 45678
            elif production_port is None:
                selected = int(udp_port)
            elif udp_port is None:
                selected = int(production_port)
            elif udp_port == production_port:
                selected = udp_port
            elif udp_port == 45678 and production_port == 45679:
                selected = 45678
            elif udp_port == 45678 and production_port != 45679:
                selected = production_port
            elif production_port == 45679 and udp_port != 45678:
                selected = udp_port
            else:
                raise DeviceSettingsMigrationError(
                    "legacy customer and production UDP local ports conflict"
                )

        config["device_udp"] = {"local_port": selected}
        migrations["device_udp_port_v1"] = True
        config["config_migrations"] = migrations
        udp = config.get("udp")
        if isinstance(udp, dict):
            udp.pop("local_port", None)
        production = config.get("production")
        if isinstance(production, dict):
            production.pop("local_port", None)
        return True

    @classmethod
    def _migrate_customer_devices(cls, config: dict[str, Any]) -> bool:
        migrations = config.get("config_migrations")
        if migrations is not None and not isinstance(migrations, dict):
            raise DeviceSettingsMigrationError("config_migrations must be an object")
        migrations = migrations if isinstance(migrations, dict) else {}
        customer = config.get("customer")
        if customer is not None and not isinstance(customer, dict):
            raise DeviceSettingsMigrationError("customer must be an object")
        marker_present = "customer_devices_v1" in migrations
        if marker_present and migrations["customer_devices_v1"] is not True:
            raise DeviceSettingsMigrationError(
                "customer_devices_v1 migration marker must be true"
            )
        marker = marker_present
        if marker:
            if not isinstance(customer, dict) or "devices" not in customer:
                raise DeviceSettingsMigrationError(
                    "customer device migration marker exists without devices"
                )
            devices = cls._normalize_customer_devices(customer["devices"])
            active = cls._normalize_active_endpoint(
                customer.get("active_endpoint"), devices
            )
            changed = (
                customer.get("devices") != devices
                or customer.get("active_endpoint") != active
            )
            customer["devices"] = devices
            customer["active_endpoint"] = active
            udp = config.get("udp")
            if isinstance(udp, dict) and "remote_ip" in udp:
                udp.pop("remote_ip")
                changed = True
            if isinstance(udp, dict) and "remote_port" in udp:
                udp.pop("remote_port")
                changed = True
            return changed

        if isinstance(customer, dict) and "devices" in customer:
            devices = cls._normalize_customer_devices(customer["devices"])
            active = cls._normalize_active_endpoint(
                customer.get("active_endpoint"), devices
            )
        else:
            udp = config.get("udp")
            ip_present = isinstance(udp, dict) and "remote_ip" in udp
            port_present = isinstance(udp, dict) and "remote_port" in udp
            if not ip_present and not port_present:
                devices = []
                active = None
            elif ip_present and port_present:
                item = cls._normalize_customer_device(
                    {"ip": udp["remote_ip"], "port": udp["remote_port"]}
                )
                devices = [item]
                active = item
            else:
                raise DeviceSettingsMigrationError(
                    "legacy customer endpoint is incomplete"
                )

        config["customer"] = {
            "devices": devices,
            "active_endpoint": active,
        }
        migrations["customer_devices_v1"] = True
        config["config_migrations"] = migrations
        udp = config.get("udp")
        if isinstance(udp, dict):
            udp.pop("remote_ip", None)
            udp.pop("remote_port", None)
        return True

    @staticmethod
    def _read_json_object(path: Path) -> dict[str, Any]:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("settings root must be a JSON object")
        return value

    def _read_verified_backup(self) -> dict[str, Any]:
        envelope = json.loads(self._backup_file.read_text(encoding="utf-8"))
        if not isinstance(envelope, dict) or envelope.get("schema") != 1:
            raise ValueError("settings backup envelope is invalid")
        encoded = envelope.get("payload_base64")
        expected = envelope.get("sha256")
        if not isinstance(encoded, str) or not isinstance(expected, str):
            raise ValueError("settings backup envelope fields are invalid")
        try:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeError, ValueError) as exc:
            raise ValueError("settings backup payload is invalid") from exc
        actual = hashlib.sha256(raw).hexdigest()
        if not expected or actual != expected:
            raise ValueError("settings backup checksum mismatch")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("settings backup root must be a JSON object")
        return value

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

    @staticmethod
    def _json_bytes(value: dict[str, Any]) -> bytes:
        return (json.dumps(value, indent=4, ensure_ascii=False) + "\n").encode("utf-8")

    @staticmethod
    def _backup_envelope_bytes(payload: bytes) -> bytes:
        envelope = {
            "schema": 1,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "payload_base64": base64.b64encode(payload).decode("ascii"),
        }
        return Settings._json_bytes(envelope)

    @staticmethod
    def _fsync_parent_directory(path: Path) -> None:
        if os.name == "nt":
            return
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @classmethod
    def _atomic_write_bytes(cls, path: Path, data: bytes) -> None:
        fd: Optional[int] = None
        temp_name: Optional[str] = None
        replaced = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            stream = os.fdopen(fd, "wb")
            fd = None
            with stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
            replaced = True
            cls._fsync_parent_directory(path)
        except BaseException as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if not replaced and temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            raise _AtomicWriteError(str(exc), committed=replaced) from exc

    def save(self):
        if self._read_only_recovery or self._device_configuration_blocked:
            raise SettingsSaveError(
                "settings are read-only until recovery evidence is resolved"
            )
        if self._config_file.exists():
            try:
                existing = self._config_file.read_bytes()
                parsed = json.loads(existing.decode("utf-8"))
                if not isinstance(parsed, dict):
                    raise ValueError("settings root must be a JSON object")
                self._atomic_write_bytes(
                    self._backup_file,
                    self._backup_envelope_bytes(existing),
                )
            except _AtomicWriteError as exc:
                if exc.committed:
                    self._read_only_recovery = True
                    self._device_configuration_blocked = True
                    self._device_configuration_error = (
                        "settings backup durability is unconfirmed"
                    )
                raise SettingsSaveError(f"settings backup save failed: {exc}") from exc
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise SettingsSaveError(
                    "settings primary is invalid; refusing to replace recovery evidence"
                ) from exc
        try:
            self._atomic_write_bytes(self._config_file, self._json_bytes(self._config))
        except _AtomicWriteError as exc:
            if exc.committed:
                self._read_only_recovery = True
                self._device_configuration_blocked = True
                self._device_configuration_error = "settings commit durability is unconfirmed"
            raise SettingsSaveError(f"settings save failed: {exc}") from exc

    def persist_preferences(self) -> SettingsPersistenceResult:
        """Persist ordinary preferences without interrupting unrelated UI actions."""

        if self._read_only_recovery or self._device_configuration_blocked:
            self._report_preference_persistence_once(
                "preferences retained in memory while device settings require recovery"
            )
            return SettingsPersistenceResult.MEMORY_ONLY
        try:
            self.save()
        except SettingsSaveError as exc:
            self._report_preference_persistence_once(
                f"preference persistence failed: {exc}"
            )
            return SettingsPersistenceResult.FAILED
        return SettingsPersistenceResult.PERSISTED

    def _report_preference_persistence_once(self, message: str) -> None:
        if self._preference_persistence_notice_emitted:
            return
        self._preference_persistence_notice_emitted = True
        self._load_diagnostics.append(message)
        logging.getLogger(__name__).warning(message)

    @staticmethod
    def _evidence_entry(path: Path) -> dict[str, Any]:
        entry: dict[str, Any] = {"path": str(path), "exists": path.exists()}
        if not entry["exists"]:
            return entry
        try:
            payload = path.read_bytes()
        except OSError as exc:
            entry["read_error"] = str(exc)
            return entry
        entry.update(
            {
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "payload_base64": base64.b64encode(payload).decode("ascii"),
            }
        )
        try:
            parsed = json.loads(payload.decode("utf-8"))
            entry["json_root_type"] = type(parsed).__name__
            if not isinstance(parsed, dict):
                entry["parse_error"] = "settings root must be a JSON object"
        except (UnicodeError, json.JSONDecodeError) as exc:
            entry["parse_error"] = str(exc)
        return entry

    def recovery_evidence(self) -> dict[str, Any]:
        """Return exact main/backup bytes and diagnostics without mutating disk."""

        return {
            "schema": 1,
            "read_only_recovery": self._read_only_recovery,
            "device_configuration_blocked": self._device_configuration_blocked,
            "device_configuration_error": self._device_configuration_error,
            "diagnostics": list(self._load_diagnostics),
            "primary": self._evidence_entry(self._config_file),
            "backup": self._evidence_entry(self._backup_file),
        }

    def export_recovery_evidence(self, destination: Path | str) -> Path:
        target = Path(destination)
        try:
            self._atomic_write_bytes(
                target,
                self._json_bytes(self.recovery_evidence()),
            )
        except _AtomicWriteError as exc:
            raise SettingsSaveError(f"settings recovery export failed: {exc}") from exc
        return target

    def _preserve_recovery_evidence(self) -> Path | None:
        evidence = self.recovery_evidence()
        if not evidence["primary"]["exists"] and not evidence["backup"]["exists"]:
            return None
        payload = self._json_bytes(evidence)
        digest = hashlib.sha256(payload).hexdigest()
        target = self._config_dir / f"settings_recovery_{digest}.json"
        if target.exists():
            try:
                if target.read_bytes() != payload:
                    raise SettingsSaveError(
                        "settings recovery evidence path contains different bytes"
                    )
            except OSError as exc:
                raise SettingsSaveError(
                    f"settings recovery evidence cannot be verified: {exc}"
                ) from exc
            return target
        try:
            self._atomic_write_bytes(target, payload)
        except _AtomicWriteError as exc:
            raise SettingsSaveError(
                f"settings recovery evidence could not be preserved: {exc}"
            ) from exc
        return target

    def rebuild_device_settings(
        self,
        *,
        local_port: object,
        devices: object,
        active_endpoint: object = None,
        confirmation: str,
    ) -> Path | None:
        """Build one clean settings snapshot after explicit evidence-preserving repair."""

        if confirmation != self.REBUILD_CONFIRMATION:
            raise SettingsSaveError("settings rebuild confirmation does not match")
        normalized_port = self._valid_port(local_port)
        normalized_devices = self._normalize_customer_devices(devices)
        normalized_active = self._normalize_active_endpoint(
            active_endpoint,
            normalized_devices,
        )
        if active_endpoint is not None and normalized_active is None:
            raise DeviceSettingsMigrationError(
                "customer.active_endpoint must be one of customer.devices"
            )

        candidate = copy.deepcopy(self.DEFAULT_CONFIG)
        candidate["device_udp"] = {"local_port": normalized_port}
        candidate["customer"] = {
            "devices": normalized_devices,
            "active_endpoint": normalized_active,
        }
        candidate["config_migrations"] = {
            "device_udp_port_v1": True,
            "customer_devices_v1": True,
        }
        payload = self._json_bytes(candidate)
        evidence_path = self._preserve_recovery_evidence()
        try:
            self._atomic_write_bytes(
                self._backup_file,
                self._backup_envelope_bytes(payload),
            )
            self._atomic_write_bytes(self._config_file, payload)
        except _AtomicWriteError as exc:
            self._read_only_recovery = True
            self._device_configuration_blocked = True
            self._device_configuration_error = "settings rebuild commit failed"
            raise SettingsSaveError(f"settings rebuild failed: {exc}") from exc

        self._config = candidate
        self._read_only_recovery = False
        self._device_configuration_blocked = False
        self._device_configuration_error = ""
        self._load_diagnostics.append("device settings rebuilt from safe defaults")
        return evidence_path

    @property
    def read_only_recovery(self) -> bool:
        return self._read_only_recovery

    @property
    def device_configuration_blocked(self) -> bool:
        return self._device_configuration_blocked

    @property
    def device_configuration_error(self) -> str:
        return self._device_configuration_error

    @property
    def load_diagnostics(self) -> tuple[str, ...]:
        return tuple(self._load_diagnostics)

    @property
    def config_directory(self) -> Path:
        return self._config_dir

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
