"""构建边界回归：user 人工完整包，ops 单文件更新器。"""

from pathlib import Path
import json

import pytest

from scripts import build_editions


@pytest.mark.parametrize("edition", ["user", "ops"])
def test_only_ops_builds_and_embeds_application_updater(tmp_path, monkeypatch, edition):
    monkeypatch.setattr(build_editions, "ROOT", tmp_path)
    monkeypatch.setattr(build_editions.sys, "platform", "win32")
    folder = "SatelliteTerminalManager" if edition == "user" else "SatelliteTerminalManagerOps"
    payload = tmp_path / "dist" / edition / folder
    docs = tmp_path / "doc"
    docs.mkdir()
    for name in ("user_edition_guide.md", "WINDOWS_QUICK_START.md", "user_manual.md", "user_manual_en.md",
                 "production_automation_operator_guide.md"):
        (docs / name).write_text("guide")
    commands = []

    def run(*arguments, env=None):
        commands.append(arguments)
        if arguments[-1] == "satellite_debug_tool.spec":
            payload.mkdir(parents=True)
            (payload / "SatelliteTerminalManager.exe").write_bytes(b"application")
        elif arguments[-1] == "updater.spec":
            (payload.parent / "updater.exe").write_bytes(b"updater")

    monkeypatch.setattr(build_editions, "run", run)
    build_editions.build(edition)
    assert (payload / "updater.exe").exists() is (edition == "ops")
    assert any(command[-1] == "updater.spec" for command in commands) is (edition == "ops")
    assert (tmp_path / "release" / f"build-{edition}.json").is_file()
    metrics = json.loads((tmp_path / "release" / f"build-{edition}.json").read_text())
    assert Path(metrics["archive"]).name == (
        f"satellite_terminal_manager-{'ops-' if edition == 'ops' else ''}windows-"
        f"{build_editions.platform.machine().lower().replace('amd64', 'x86_64')}-v{build_editions.__version__}.zip")
