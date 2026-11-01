"""Windows 发版的测试退出码由独立 CI step 接收。"""

from pathlib import Path
import re


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
