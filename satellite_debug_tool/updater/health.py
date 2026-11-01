"""新版主进程健康回执：实际窗口初始化完成，提交前保持控制入口关闭。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import time

from satellite_debug_tool.package_names import WINDOWS_EXECUTABLE
from satellite_debug_tool.updater.status import write_atomic_json


HEALTH_ENV = "SATELLITE_UPDATE_HEALTH_REQUEST"
HEALTH_SCHEMA = "satellite.update-health/v1"


@dataclass(frozen=True)
class StartupHealthContext:
    directory: Path
    transaction_id: str
    target_version: str
    token: str
    settings_directory: Path | None
    timeout_s: float

    @property
    def receipt_path(self) -> Path:
        return self.directory / "health-receipt.json"

    @property
    def commit_path(self) -> Path:
        return self.directory / "health-commit.json"

    def message(self, state: str) -> dict:
        return {"schema": HEALTH_SCHEMA, "transaction_id": self.transaction_id,
                "target_version": self.target_version, "token": self.token,
                "pid": os.getpid(), "state": state}


def startup_health_context(edition, version: str) -> StartupHealthContext | None:
    """在窗口创建前验证事务，消费环境变量以免传给后续子进程。"""
    filename = os.environ.get(HEALTH_ENV, "")
    if not filename:
        return None
    from satellite_debug_tool.edition import Edition
    from satellite_debug_tool.updater.handoff import load_update_launch_request
    if Edition(edition) is not Edition.OPS:
        raise RuntimeError("application health trial requires operations edition")
    path = Path(filename).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    request = load_update_launch_request(path.parent / "request.json")
    if (data.get("schema") != HEALTH_SCHEMA
            or data.get("transaction_id") != request.transaction_id
            or data.get("target_version") != request.target_version
            or str(version).lstrip("v") != request.target_version.lstrip("v")
            or not isinstance(data.get("token"), str) or len(data["token"]) != 64):
        raise RuntimeError("application health identity differs from update transaction")
    if getattr(sys, "frozen", False):
        if Path(sys.executable).resolve() != request.install_dir / WINDOWS_EXECUTABLE:
            raise RuntimeError("application health executable differs from the installation")
    context = StartupHealthContext(path.parent, request.transaction_id, request.target_version,
                                   data["token"], request.settings_directory,
                                   min(120.0, max(1.0, float(data.get("timeout_s", 40.0)))))
    # Settings 构造前发布进程身份；写入成功前保留启动环境，供中断恢复精确定位。
    import psutil
    started = context.message("started")
    started.update(created=psutil.Process(os.getpid()).create_time(),
                   executable=str(Path(sys.executable).resolve()))
    write_atomic_json(context.directory / "health-process.json", started)
    os.environ.pop(HEALTH_ENV, None)
    return context


def validate_health_message(data: dict, context: StartupHealthContext, *, pid: int,
                            state: str) -> bool:
    return bool(data.get("schema") == HEALTH_SCHEMA
                and data.get("transaction_id") == context.transaction_id
                and data.get("target_version") == context.target_version
                and data.get("token") == context.token
                and data.get("pid") == pid and data.get("state") == state)


def install_startup_health_gate(app, window, settings, context: StartupHealthContext):
    """构造入口只建立静默会话；首帧初始化回执通过后等待 updater 明确提交。"""
    from PySide6.QtCore import QObject, QTimer, Slot
    if settings.device_configuration_blocked:
        raise RuntimeError("application configuration requires recovery")
    broker = getattr(window, "_udp_broker", None)
    live = getattr(window, "_live", None)
    if (broker is not None and broker.isRunning()) or (live is not None and live.is_connected()):
        raise RuntimeError("application startup opened a device transport during health trial")
    # 客户配置租约与 attach 意图区分；构造窗口不会获取控制/采集租约。
    directory = getattr(window, "_customer_devices", None)
    if directory is not None and any(directory.attachment(endpoint) is not None for endpoint in directory.endpoints()):
        raise RuntimeError("application startup attached a device during health trial")
    window.setEnabled(False)

    class HealthGate(QObject):
        def __init__(self):
            super().__init__(window)
            self.started = time.monotonic()
            self.timer = QTimer(self)
            self.timer.setInterval(50)
            self.timer.timeout.connect(self.observe_commit)
            self.previous_update_check = os.environ.get("SATELLITE_UPDATE_CHECK")
            os.environ["SATELLITE_UPDATE_CHECK"] = "0"
            QTimer.singleShot(0, self.report_ready)

        @Slot()
        def report_ready(self):
            try:
                write_atomic_json(context.receipt_path, context.message("ready"))
            except OSError:
                app.exit(1)
                return
            self.timer.start()

        @Slot()
        def observe_commit(self):
            try:
                data = json.loads(context.commit_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = None
            except (OSError, ValueError):
                self.timer.stop()
                app.exit(1)
                return
            if data is not None:
                if not validate_health_message(data, context, pid=os.getpid(), state="committed"):
                    self.timer.stop()
                    app.exit(1)
                    return
                self.timer.stop()
                if self.previous_update_check is None:
                    os.environ.pop("SATELLITE_UPDATE_CHECK", None)
                else:
                    os.environ["SATELLITE_UPDATE_CHECK"] = self.previous_update_check
                window.setEnabled(True)
                return
            if time.monotonic() - self.started >= context.timeout_s:
                self.timer.stop()
                app.exit(1)

    gate = HealthGate()
    window._update_health_gate = gate
    return gate
