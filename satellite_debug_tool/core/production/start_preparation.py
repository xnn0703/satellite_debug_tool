"""正式启动准备：电源确认后等待设备准入，界面只呈现等待原因。"""
from __future__ import annotations

from enum import Enum
from typing import Callable
from PySide6.QtCore import QObject, QTimer, Signal, Slot


class PreparationState(str, Enum):
    IDLE = "idle"
    PREPARING_POWER = "preparing_power"
    WAITING_DEVICES = "waiting_devices"
    ADMITTED = "admitted"
    STOPPED = "stopped"


class ProductionStartPreparation(QObject):
    admitted = Signal(object)
    waiting = Signal(str)

    def __init__(self, admission: Callable[[], tuple[tuple, str]], *, parent=None) -> None:
        super().__init__(parent)
        self._admission = admission
        self.state = PreparationState.IDLE
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.tick)

    def begin(self) -> None:
        if self.state not in {PreparationState.IDLE, PreparationState.STOPPED}:
            raise RuntimeError("production preparation is already active")
        self.state = PreparationState.PREPARING_POWER

    def confirm_power(self) -> None:
        if self.state is not PreparationState.PREPARING_POWER:
            raise RuntimeError("production power confirmation requires a preparing owner")
        self.state = PreparationState.WAITING_DEVICES
        self._timer.start()
        self.tick()

    @Slot()
    def tick(self) -> None:
        if self.state is not PreparationState.WAITING_DEVICES:
            return
        participants, reason = self._admission()
        if reason:
            self.waiting.emit(reason)
            return
        if not participants:
            raise RuntimeError("device admission requires participants")
        self._timer.stop()
        self.state = PreparationState.ADMITTED
        self.admitted.emit(participants)

    def stop(self) -> None:
        self._timer.stop()
        self.state = PreparationState.STOPPED
