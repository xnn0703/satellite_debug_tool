"""Windows 发版的测试退出码由独立 CI step 接收。"""

from pathlib import Path
import re
import configparser

import pytest


def _run_commands(source: str):
    lines = source.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s+)(?:- )?run:\s*(.*)$", line)
        if match is None:
            continue
        indent, value = len(match[1]), match[2]
        if value not in {"|", ">"}:
            yield value
            continue
        body = []
        for following in lines[index + 1:]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following.strip())
        yield "\n".join(body).strip()


def test_source_test_exit_code_is_an_independent_workflow_gate():
    """旧 PowerShell step 用后续翻译命令的 0 覆盖 pytest 崩溃/失败退出码。"""
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/build.yml"
    source = workflow.read_text(encoding="utf-8")
    commands = [command for command in _run_commands(source)
                if "python -m pytest satellite_debug_tool/tests" in command]
    assert len(commands) == 2  # tag release 与普通 CI 均执行完整源码测试。
    for command in commands:
        executable_lines = [line for line in command.splitlines() if line and not line.startswith("#")]
        assert len(executable_lines) == 1, "pytest requires its own CI exit-code boundary"
        assert executable_lines[0].startswith("python -m pytest satellite_debug_tool/tests ")
    assert "continue-on-error: true" not in source


@pytest.mark.parametrize("service,queue", [("publish-downloads", "downloads"), ("verify-publication", "verification")])
def test_server_watcher_detects_completed_request_already_in_queue(tmp_path, service, queue):
    """正式请求先存在、watcher 后激活仍满足条件，partial 写入不触发。"""
    unit = Path(__file__).resolve().parents[2] / f"deploy/updates/satellite-updates-{service}.path"
    config = configparser.ConfigParser()
    config.read(unit)
    watch = config["Path"]
    assert "PathChanged" not in watch
    pattern = watch["PathExistsGlob"]
    assert pattern == f"/srv/satellite-updates/receipts/{queue}/requests/*.json"
    queue_dir = tmp_path / "requests"
    queue_dir.mkdir()
    request = queue_dir / "ready.json"
    partial = queue_dir / "ready.json.part"
    partial.write_text("request")
    assert list(queue_dir.glob(Path(pattern).name)) == []
    partial.rename(request)
    assert list(queue_dir.glob(Path(pattern).name)) == [request]


def test_publication_service_stages_only_from_incoming_into_the_fixed_app_root():
    unit = Path(__file__).resolve().parents[2] / "deploy/updates/satellite-updates-verify-publication.service"
    source = unit.read_text(encoding="utf-8")
    assert "--incoming-root /srv/satellite-updates/incoming" in source
    assert "ReadOnlyPaths=/srv/satellite-updates/incoming" in source
    assert "ReadWritePaths=/srv/satellite-updates/public/v1/apps/satellite-terminal-tool " in source
    assert "RestrictAddressFamilies=AF_UNIX" in source
