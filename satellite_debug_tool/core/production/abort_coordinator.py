"""批次中止的唯一收尾状态：等待真实退出事实，主线程持续响应。"""
from __future__ import annotations

from enum import Enum
from typing import Callable, Optional
from PySide6.QtCore import QObject, QThread, Signal, Slot


class ProductionCleanupWorker(QThread):
    progress = Signal(object)

    def __init__(
        self,
        cleanup: Callable[[], object],
        *,
        parent=None,
        report_progress: bool = False,
    ) -> None:
        super().__init__(parent)
        self._cleanup = cleanup
        self._report_progress = bool(report_progress)
        self.result: object = None
        self.error = ""

    def run(self) -> None:
        try:
            if self._report_progress:
                def emit_progress(*args) -> None:
                    self.progress.emit(args)

                self.result = self._cleanup(emit_progress)
            else:
                self.result = self._cleanup()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"


class AbortState(str, Enum):
    IDLE = "idle"
    STOPPING = "stopping"
    ABORTED = "aborted"
    FAILED = "failed"


class ProductionAbortCoordinator(QObject):
    changed = Signal(object)
    settled = Signal(bool, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.state = AbortState.IDLE
        self._cleanup: Optional[ProductionCleanupWorker] = None
        self._motion_done = True
        self._power_result = True
        self._power_finished = True
        self._error = ""
        self._power = None

    @property
    def error(self) -> str:
        return self._error

    @property
    def active(self) -> bool:
        return self.state is AbortState.STOPPING

    def request(self, *, stop_progress: Callable[[], None], power=None,
                cleanup: Optional[Callable[[], object]] = None) -> None:
        if self.state is not AbortState.IDLE:
            return
        self.state = AbortState.STOPPING
        self._power = power
        self._power_result = power is None
        self._power_finished = power is None or not power.isRunning()
        self._motion_done = cleanup is None
        if power is not None:
            power.finished.connect(self._on_power_finished)
        self.changed.emit(self.state)  # 所有同步信号/刷新之前先发布中止事实。
        if power is not None:
            power.request_verified_off()
        try:
            stop_progress()
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
        if cleanup is not None:
            self._cleanup = ProductionCleanupWorker(cleanup, parent=self)
            self._cleanup.finished.connect(self._on_cleanup_finished)
            self._cleanup.start()
        self._check_settled()

    def confirm_power_off(self) -> None:
        if self.active:
            self._power_result = True
            self._check_settled()

    def fail_power(self, reason: str) -> None:
        if self.active:
            self._error = str(reason)
            self._power_result = True
            self.changed.emit(self.state)
            self._check_settled()

    @Slot()
    def _on_power_finished(self) -> None:
        self._power_finished = True
        self._check_settled()

    @Slot()
    def _on_cleanup_finished(self) -> None:
        self._motion_done = True
        if self._cleanup is not None and self._cleanup.error:
            self._error = self._cleanup.error
        self._check_settled()

    def _check_settled(self) -> None:
        if not self.active or not (self._motion_done and self._power_result and self._power_finished):
            return
        self.state = AbortState.FAILED if self._error else AbortState.ABORTED
        if self._power is not None:
            self._power.finished.disconnect(self._on_power_finished)
        self.changed.emit(self.state)
        self.settled.emit(not self._error, self._error)
