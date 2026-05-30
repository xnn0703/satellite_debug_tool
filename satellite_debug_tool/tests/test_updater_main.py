"""updater.main 单测：CLI 解析 + restart_app 平台分支 + 端到端 main()。

不真正 spawn 进程；用 monkeypatch 拦截 subprocess.Popen 和 wait_for_pid。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


# 让 main 模块 import 起来不依赖 PySide6
@pytest.fixture(scope="module")
def updater_main():
    from satellite_debug_tool.updater import main as m
    return m


class TestCLIParsing:
    def test_help_exits_zero(self, updater_main):
        with pytest.raises(SystemExit) as exc:
            updater_main.main(["--help"])
        assert exc.value.code == 0


class TestWaitForPid:
    def test_returns_true_when_pid_missing(self, updater_main):
        # 用一个肯定不存在的 PID（pid 上限通常 2^22）
        assert updater_main.wait_for_pid(2**30, timeout=0.5) is True


class TestRestartApp:
    def test_mac_app_uses_open(self, updater_main, tmp_path, monkeypatch):
        captured = []
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            "subprocess.Popen",
            lambda *args, **kw: captured.append((args, kw)) or object(),
        )
        app = tmp_path / "MyApp.app"
        app.mkdir()
        updater_main.restart_app(app)
        # 第一次调用的 args[0] 应为 ["open", str(app)]
        assert captured
        call_args = captured[0][0][0]
        assert call_args[0] == "open"
        assert call_args[1] == str(app)

    def test_win_finds_exe(self, updater_main, tmp_path, monkeypatch):
        captured = []
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            "subprocess.Popen",
            lambda *args, **kw: captured.append((args, kw)) or object(),
        )
        install = tmp_path / "MyApp"
        install.mkdir()
        (install / "MyApp.exe").write_bytes(b"PE-stub")
        updater_main.restart_app(install)
        assert captured
        # 启动的 path 应该是 install/MyApp.exe
        call_args = captured[0][0][0]
        assert call_args == [str(install / "MyApp.exe")]

    def test_explicit_restart_cmd_overrides(self, updater_main, tmp_path, monkeypatch):
        captured = []
        monkeypatch.setattr(
            "subprocess.Popen",
            lambda *args, **kw: captured.append((args, kw)) or object(),
        )
        updater_main.restart_app(tmp_path / "ignored", restart_cmd="echo hi")
        assert captured[0][0][0] == "echo hi"
        assert captured[0][1].get("shell") is True

    def test_no_executable_found_warns_only(self, updater_main, tmp_path, monkeypatch, caplog):
        """install_dir 内没有可执行 → log.warning，不崩。"""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(
            "subprocess.Popen",
            lambda *args, **kw: pytest.fail("不应启动任何进程"),
        )
        empty = tmp_path / "Empty"
        empty.mkdir()
        # 不应抛异常
        updater_main.restart_app(empty)


class TestEndToEndMain:
    def test_main_success_path(self, updater_main, tmp_path, monkeypatch):
        """端到端：构造 7z 分卷 + 假 install_dir → main() 返回 0。"""
        py7zr = pytest.importorskip("py7zr")
        from satellite_debug_tool.tests.test_updater_applier import (
            _make_7z_with_app, _split_file,
        )

        # 1. 准备分卷
        arc = _make_7z_with_app(tmp_path, app_name="MyApp",
                                contents={"v.txt": b"NEW"})
        vols = _split_file(arc, tmp_path, "split", vol_size=64)

        # 2. 准备假 install_dir
        installed = tmp_path / "MyApp"
        installed.mkdir()
        (installed / "v.txt").write_text("OLD")

        # 3. 拦截 wait_for_pid 和 restart_app，不真等不真启
        monkeypatch.setattr(updater_main, "wait_for_pid", lambda *a, **kw: True)
        monkeypatch.setattr(updater_main, "restart_app", lambda *a, **kw: None)
        monkeypatch.setattr(updater_main, "self_relocate_and_relaunch", lambda *a, **kw: None)

        workdir = tmp_path / "_work"
        rc = updater_main.main([
            "--pid", "99999",
            "--install-dir", str(installed),
            "--workdir", str(workdir),
            "--volumes", *(str(v) for v in vols),
        ])
        assert rc == 0
        # 验证 install_dir 被替换
        assert (installed / "v.txt").read_bytes() == b"NEW"
        # backup 存在
        backups = list(tmp_path.glob("MyApp.bak.*"))
        assert len(backups) == 1
        assert (backups[0] / "v.txt").read_text() == "OLD"
