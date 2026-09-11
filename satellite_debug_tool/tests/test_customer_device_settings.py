"""M25 customer-device settings migration and recovery contracts."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from satellite_debug_tool.core.config import (
    Settings,
    SettingsPersistenceResult,
    SettingsSaveError,
    _AtomicWriteError,
)


def _settings_path(tmp_path):
    directory = tmp_path / ".satellite_debug_tool"
    directory.mkdir(exist_ok=True)
    return directory / "settings.json"


def _load(tmp_path, monkeypatch, raw=None) -> Settings:
    monkeypatch.setenv("HOME", str(tmp_path))
    if raw is not None:
        path = _settings_path(tmp_path)
        if isinstance(raw, bytes):
            path.write_bytes(raw)
        else:
            path.write_text(json.dumps(raw), encoding="utf-8")
    return Settings()


def test_new_install_has_empty_customer_directory_and_unified_port(
    tmp_path, monkeypatch
) -> None:
    settings = _load(tmp_path, monkeypatch)

    assert settings.get("customer.devices") == []
    assert settings.get("customer.active_endpoint") is None
    assert settings.get("device_udp.local_port") == 45678
    assert not settings.device_configuration_blocked


def test_legacy_endpoint_and_historical_default_ports_migrate_atomically(
    tmp_path, monkeypatch
) -> None:
    settings = _load(
        tmp_path,
        monkeypatch,
        {
            "udp": {
                "remote_ip": "192.168.1.12",
                "remote_port": 4004,
                "local_port": 45678,
            },
            "production": {"local_port": 45679},
        },
    )

    endpoint = {"ip": "192.168.1.12", "port": 4004}
    assert settings.get("customer.devices") == [endpoint]
    assert settings.get("customer.active_endpoint") == endpoint
    assert settings.get("device_udp.local_port") == 45678

    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["config_migrations"]["device_udp_port_v1"] is True
    assert saved["config_migrations"]["customer_devices_v1"] is True
    assert "remote_ip" not in saved.get("udp", {})
    assert "remote_port" not in saved.get("udp", {})
    assert "local_port" not in saved.get("udp", {})
    assert "local_port" not in saved.get("production", {})


@pytest.mark.parametrize(
    ("udp_port", "production_port", "expected"),
    [
        (50100, 45679, 50100),
        (45678, 50101, 50101),
        (50102, 50102, 50102),
    ],
)
def test_port_migration_selects_the_single_custom_fact(
    tmp_path, monkeypatch, udp_port, production_port, expected
) -> None:
    settings = _load(
        tmp_path,
        monkeypatch,
        {
            "udp": {"local_port": udp_port},
            "production": {"local_port": production_port},
        },
    )

    assert settings.get("device_udp.local_port") == expected
    assert not settings.device_configuration_blocked


def test_conflicting_custom_ports_fail_closed_without_rewriting_source(
    tmp_path, monkeypatch
) -> None:
    source = {
        "udp": {"local_port": 50100},
        "production": {"local_port": 50101},
    }
    path = _settings_path(tmp_path)
    original = json.dumps(source).encode("utf-8")
    path.write_bytes(original)
    monkeypatch.setenv("HOME", str(tmp_path))

    settings = Settings()

    assert settings.device_configuration_blocked
    assert "conflict" in settings.device_configuration_error
    assert path.read_bytes() == original


def test_explicit_customer_list_is_normalized_and_active_must_be_a_member(
    tmp_path, monkeypatch
) -> None:
    settings = _load(
        tmp_path,
        monkeypatch,
        {
            "customer": {
                "devices": [
                    {"ip": "192.168.001.013", "port": "4004"},
                    {"ip": "192.168.1.12", "port": 4004},
                ],
                "active_endpoint": {"ip": "192.168.1.99", "port": 4004},
            }
        },
    )

    # Python's canonical IPv4 parser intentionally rejects ambiguous leading zeroes.
    assert settings.device_configuration_blocked


def test_marker_without_customer_devices_fails_closed(tmp_path, monkeypatch) -> None:
    settings = _load(
        tmp_path,
        monkeypatch,
        {
            "config_migrations": {
                "device_udp_port_v1": True,
                "customer_devices_v1": True,
            },
            "device_udp": {"local_port": 45678},
            "customer": {},
        },
    )

    assert settings.device_configuration_blocked
    assert "without devices" in settings.device_configuration_error


def test_marked_schema_cleans_residual_legacy_keys_and_resets_invalid_active(
    tmp_path, monkeypatch
) -> None:
    settings = _load(
        tmp_path,
        monkeypatch,
        {
            "config_migrations": {
                "device_udp_port_v1": True,
                "customer_devices_v1": True,
            },
            "device_udp": {"local_port": 50000},
            "customer": {
                "devices": [{"ip": "192.168.1.13", "port": 4004}],
                "active_endpoint": {"ip": "192.168.1.12", "port": 4004},
            },
            "udp": {
                "local_port": 45678,
                "remote_ip": "192.168.1.12",
                "remote_port": 4004,
            },
            "production": {"local_port": 45679},
        },
    )

    assert not settings.device_configuration_blocked
    assert settings.get("customer.active_endpoint") is None
    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert saved.get("udp") == {}
    assert "local_port" not in saved.get("production", {})


def test_corrupt_main_and_missing_backup_enter_read_only_recovery(
    tmp_path, monkeypatch
) -> None:
    path = _settings_path(tmp_path)
    original = b"{ definitely-not-json"
    path.write_bytes(original)
    monkeypatch.setenv("HOME", str(tmp_path))

    settings = Settings()

    assert settings.read_only_recovery
    assert settings.device_configuration_blocked
    settings.set("ui.theme", "light")
    with pytest.raises(SettingsSaveError):
        settings.save()
    assert path.read_bytes() == original


def test_verified_backup_recovers_a_corrupt_primary(tmp_path, monkeypatch) -> None:
    settings = _load(tmp_path, monkeypatch)
    settings.set("ui.theme", "light")
    settings.save()
    settings.set("ui.theme", "dark_hc")
    settings.save()
    settings._config_file.write_bytes(b"not-json")

    recovered = Settings()

    assert not recovered.read_only_recovery
    assert recovered.get("ui.theme") == "light"
    assert any("verified backup" in item for item in recovered.load_diagnostics)


def test_partial_migration_failure_and_unrelated_save_preserve_original_bytes(
    tmp_path, monkeypatch
) -> None:
    raw = {
        "ui": {"theme": "dark"},
        "udp": {
            "local_port": 45678,
            "remote_ip": "192.168.1.12",
        },
        "production": {"local_port": 45679},
    }
    path = _settings_path(tmp_path)
    original = json.dumps(raw).encode("utf-8")
    path.write_bytes(original)
    monkeypatch.setenv("HOME", str(tmp_path))

    settings = Settings()
    assert settings.device_configuration_blocked
    settings.set("ui.theme", "light")
    with pytest.raises(SettingsSaveError):
        settings.save()
    assert path.read_bytes() == original


@pytest.mark.parametrize("marker", [False, "true", 1])
def test_present_but_non_true_migration_marker_fails_closed(
    tmp_path, monkeypatch, marker
) -> None:
    settings = _load(
        tmp_path,
        monkeypatch,
        {
            "config_migrations": {"device_udp_port_v1": marker},
            "device_udp": {"local_port": 45678},
        },
    )

    assert settings.device_configuration_blocked
    assert "marker must be true" in settings.device_configuration_error


def test_float_port_is_not_truncated(tmp_path, monkeypatch) -> None:
    settings = _load(
        tmp_path,
        monkeypatch,
        {"udp": {"remote_ip": "192.168.1.12", "remote_port": 4004.9}},
    )

    assert settings.device_configuration_blocked
    assert "must be an integer" in settings.device_configuration_error


def test_backup_is_one_self_verifying_atomic_envelope(tmp_path, monkeypatch) -> None:
    settings = _load(tmp_path, monkeypatch)
    settings.set("ui.theme", "light")
    settings.save()
    settings.set("ui.theme", "dark_hc")
    settings.save()

    envelope = json.loads(settings._backup_file.read_text(encoding="utf-8"))
    assert envelope["schema"] == 1
    assert set(envelope) == {"schema", "sha256", "payload_base64"}
    assert not (settings._config_dir / "settings.json.bak.sha256").exists()


def test_verified_backup_restore_failure_keeps_device_paths_blocked(
    tmp_path, monkeypatch
) -> None:
    settings = _load(tmp_path, monkeypatch)
    settings.set("ui.theme", "light")
    settings.save()
    settings.set("ui.theme", "dark_hc")
    settings.save()
    settings._config_file.write_bytes(b"broken-main")

    original_write = Settings._atomic_write_bytes.__func__

    def fail_primary_restore(cls, path, data):
        if path.name == "settings.json":
            raise _AtomicWriteError("injected restore failure", committed=False)
        return original_write(cls, path, data)

    monkeypatch.setattr(
        Settings,
        "_atomic_write_bytes",
        classmethod(fail_primary_restore),
    )
    recovered = Settings()

    assert recovered.read_only_recovery
    assert recovered.device_configuration_blocked
    assert recovered._config_file.read_bytes() == b"broken-main"


def test_migration_commit_failure_is_captured_by_constructor(
    tmp_path, monkeypatch
) -> None:
    path = _settings_path(tmp_path)
    source = {"udp": {"remote_ip": "192.168.1.12", "remote_port": 4004}}
    original = json.dumps(source).encode("utf-8")
    path.write_bytes(original)
    monkeypatch.setenv("HOME", str(tmp_path))

    original_write = Settings._atomic_write_bytes.__func__

    def fail_primary_commit(cls, target, data):
        if target.name == "settings.json":
            raise _AtomicWriteError("injected migration failure", committed=False)
        return original_write(cls, target, data)

    monkeypatch.setattr(
        Settings,
        "_atomic_write_bytes",
        classmethod(fail_primary_commit),
    )
    settings = Settings()

    assert settings.read_only_recovery
    assert settings.device_configuration_blocked
    assert path.read_bytes() == original


def test_post_replace_durability_failure_enters_recovery(tmp_path, monkeypatch) -> None:
    settings = _load(tmp_path, monkeypatch)

    def fail_directory_sync(_path):
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(Settings, "_fsync_parent_directory", fail_directory_sync)
    settings.set("ui.theme", "light")
    with pytest.raises(SettingsSaveError):
        settings.save()

    assert settings.read_only_recovery
    assert settings.device_configuration_blocked
    # Replace happened before durability became uncertain; callers are never told
    # that the original bytes are intact.
    assert json.loads(settings._config_file.read_text(encoding="utf-8"))["ui"][
        "theme"
    ] == "light"


def test_migration_mkstemp_failure_enters_constructor_recovery(
    tmp_path, monkeypatch
) -> None:
    path = _settings_path(tmp_path)
    original = json.dumps(
        {"udp": {"remote_ip": "192.168.1.12", "remote_port": 4004}}
    ).encode("utf-8")
    path.write_bytes(original)
    monkeypatch.setenv("HOME", str(tmp_path))

    def fail_mkstemp(*_args, **_kwargs):
        raise OSError("injected mkstemp failure")

    monkeypatch.setattr("satellite_debug_tool.core.config.tempfile.mkstemp", fail_mkstemp)
    settings = Settings()

    assert settings.read_only_recovery
    assert settings.device_configuration_blocked
    assert path.read_bytes() == original


def test_verified_restore_mkdir_failure_enters_constructor_recovery(
    tmp_path, monkeypatch
) -> None:
    settings = _load(tmp_path, monkeypatch)
    settings.set("ui.theme", "light")
    settings.save()
    settings.set("ui.theme", "dark_hc")
    settings.save()
    settings._config_file.write_bytes(b"broken-main")

    original_mkdir = type(settings._config_dir).mkdir

    def fail_config_mkdir(path, *args, **kwargs):
        if path == settings._config_dir:
            raise OSError("injected mkdir failure")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(type(settings._config_dir), "mkdir", fail_config_mkdir)
    recovered = Settings()

    assert recovered.read_only_recovery
    assert recovered.device_configuration_blocked
    assert recovered._config_file.read_bytes() == b"broken-main"


def test_existing_primary_read_failure_is_typed_and_preserves_bytes(
    tmp_path, monkeypatch
) -> None:
    settings = _load(tmp_path, monkeypatch)
    settings.save()
    original_read_bytes = type(settings._config_file).read_bytes
    original = original_read_bytes(settings._config_file)

    def fail_primary_read(path):
        if path == settings._config_file:
            raise OSError("injected read failure")
        return original_read_bytes(path)

    monkeypatch.setattr(type(settings._config_file), "read_bytes", fail_primary_read)
    settings.set("ui.theme", "light")
    with pytest.raises(SettingsSaveError):
        settings.save()

    assert original_read_bytes(settings._config_file) == original


def test_save_refuses_to_replace_non_object_primary(tmp_path, monkeypatch) -> None:
    settings = _load(tmp_path, monkeypatch)
    original = b"[]\n"
    _settings_path(tmp_path).write_bytes(original)
    settings.set("ui.theme", "light")

    with pytest.raises(SettingsSaveError):
        settings.save()

    assert settings._config_file.read_bytes() == original


def test_dual_damage_evidence_contains_exact_bytes_and_hashes(
    tmp_path, monkeypatch
) -> None:
    path = _settings_path(tmp_path)
    backup = path.with_name("settings.json.bak")
    primary_bytes = b"broken-primary\x00"
    backup_bytes = b"[]\n"
    path.write_bytes(primary_bytes)
    backup.write_bytes(backup_bytes)
    monkeypatch.setenv("HOME", str(tmp_path))

    settings = Settings()
    evidence = settings.recovery_evidence()

    assert settings.read_only_recovery
    assert evidence["primary"]["sha256"] == hashlib.sha256(primary_bytes).hexdigest()
    assert base64.b64decode(evidence["primary"]["payload_base64"]) == primary_bytes
    assert evidence["backup"]["sha256"] == hashlib.sha256(backup_bytes).hexdigest()
    assert base64.b64decode(evidence["backup"]["payload_base64"]) == backup_bytes


def test_preference_persistence_keeps_damaged_primary_unchanged(
    tmp_path, monkeypatch
) -> None:
    path = _settings_path(tmp_path)
    original = b"broken-primary"
    path.write_bytes(original)
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()

    settings.set("ui.theme", "light")
    assert (
        settings.persist_preferences()
        is SettingsPersistenceResult.MEMORY_ONLY
    )
    assert path.read_bytes() == original


def test_preference_io_failure_returns_failed_without_throwing(
    tmp_path, monkeypatch
) -> None:
    settings = _load(tmp_path, monkeypatch)
    settings.save()
    original_write = Settings._atomic_write_bytes.__func__

    def fail_backup(cls, target, data):
        if target.name == "settings.json.bak":
            raise _AtomicWriteError("injected preference failure", committed=False)
        return original_write(cls, target, data)

    monkeypatch.setattr(Settings, "_atomic_write_bytes", classmethod(fail_backup))
    settings.set("ui.theme", "light")

    assert settings.persist_preferences() is SettingsPersistenceResult.FAILED


def test_rebuild_requires_exact_confirmation_without_writing(
    tmp_path, monkeypatch
) -> None:
    path = _settings_path(tmp_path)
    original = b"broken-primary"
    path.write_bytes(original)
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()

    with pytest.raises(SettingsSaveError):
        settings.rebuild_device_settings(
            local_port=45678,
            devices=[],
            confirmation="rebuild_device_settings",
        )

    assert path.read_bytes() == original
    assert not tuple(settings._config_dir.glob("settings_recovery_*.json"))


def test_rebuild_preserves_evidence_and_commits_clean_snapshot(
    tmp_path, monkeypatch
) -> None:
    path = _settings_path(tmp_path)
    backup = path.with_name("settings.json.bak")
    primary_bytes = b"broken-primary"
    backup_bytes = b"broken-backup"
    path.write_bytes(primary_bytes)
    backup.write_bytes(backup_bytes)
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    endpoint = {"ip": "192.168.1.13", "port": 4004}

    evidence_path = settings.rebuild_device_settings(
        local_port=50100,
        devices=[endpoint],
        active_endpoint=endpoint,
        confirmation=Settings.REBUILD_CONFIRMATION,
    )

    assert evidence_path is not None and evidence_path.exists()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert base64.b64decode(evidence["primary"]["payload_base64"]) == primary_bytes
    assert base64.b64decode(evidence["backup"]["payload_base64"]) == backup_bytes
    rebuilt = json.loads(path.read_text(encoding="utf-8"))
    assert rebuilt["device_udp"] == {"local_port": 50100}
    assert rebuilt["customer"] == {
        "devices": [endpoint],
        "active_endpoint": endpoint,
    }
    assert rebuilt["config_migrations"] == {
        "device_udp_port_v1": True,
        "customer_devices_v1": True,
    }
    assert rebuilt.get("udp") == {}
    assert "local_port" not in rebuilt["production"]
    assert not settings.read_only_recovery
    assert not settings.device_configuration_blocked

    reloaded = Settings()
    assert not reloaded.device_configuration_blocked
    assert reloaded.get("customer.devices") == [endpoint]


def test_export_recovery_evidence_writes_verified_payload(tmp_path, monkeypatch) -> None:
    path = _settings_path(tmp_path)
    path.write_bytes(b"broken-primary")
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    destination = tmp_path / "settings-evidence.json"

    result = settings.export_recovery_evidence(destination)

    assert result == destination
    exported = json.loads(destination.read_text(encoding="utf-8"))
    assert exported["device_configuration_blocked"] is True
    assert base64.b64decode(exported["primary"]["payload_base64"]) == b"broken-primary"
