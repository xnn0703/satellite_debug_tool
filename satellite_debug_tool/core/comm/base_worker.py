from PySide6.QtCore import QThread, Signal


class BaseWorker(QThread):
    connected = Signal()
    disconnected = Signal()
    error = Signal(str)
    data_received = Signal(bytes)

    def __init__(self):
        super().__init__()
        self._running = False

    def connect(self, config: dict) -> bool:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def is_connected(self) -> bool:
        return self._running

    def send(self, data: bytes) -> bool:
        raise NotImplementedError
