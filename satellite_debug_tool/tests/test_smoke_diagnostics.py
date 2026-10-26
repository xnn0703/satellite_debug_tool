"""无控制台冻结入口的错误回传与普通启动隔离。"""

import json
from pathlib import Path
import subprocess
import sys


def test_smoke_runtime_hook_records_failure_and_exits(tmp_path):
    hook = Path(__file__).resolve().parents[2] / "scripts/frozen_smoke_hook.py"
    output = tmp_path / "smoke-user.json"
    script = "import runpy; runpy.run_path(__import__('sys').argv[1]); raise RuntimeError('frozen startup failure')"
    result = subprocess.run([sys.executable, "-c", script, str(hook), "--smoke-test", f"--smoke-output={output}"], capture_output=True, text=True)
    assert result.returncode == 1
    proof = json.loads(output.read_text())
    assert proof["status"] == "failed"
    assert "frozen startup failure" in proof["traceback"]
    assert output.with_suffix(".stage.txt").read_text() == "python-runtime-ready"


def test_ordinary_start_keeps_the_original_exception_hook(tmp_path):
    hook = Path(__file__).resolve().parents[2] / "scripts/frozen_smoke_hook.py"
    output = tmp_path / "ordinary.json"
    script = "import runpy,sys; original=sys.excepthook; runpy.run_path(sys.argv[1]); assert sys.excepthook is original"
    result = subprocess.run([sys.executable, "-c", script, str(hook), f"--smoke-output={output}"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not output.exists()


def test_smoke_closes_logging_before_temporary_directory_cleanup(tmp_path):
    output = tmp_path / "smoke-user.json"
    script = r'''
import logging,runpy,tempfile
from pathlib import Path
Original = tempfile.TemporaryDirectory
class WindowsDirectory(Original):
    def cleanup(self):
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).is_relative_to(self.name):
                assert handler.stream is None or handler.stream.closed, 'Windows keeps the application log locked'
        super().cleanup()
tempfile.TemporaryDirectory = WindowsDirectory
runpy.run_module('satellite_debug_tool.main_user', run_name='__main__')
'''
    result = subprocess.run([sys.executable, "-c", script, "--smoke-test", f"--smoke-output={output}"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    proof = json.loads(output.read_text())
    assert proof["edition"] == "user"
    assert proof["overview_panels"] == "PASS"
