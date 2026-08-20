import serial
import serial.tools.list_ports
from .base_worker import BaseWorker


class SerialWorker(BaseWorker):
    def __init__(self):
        super().__init__()
        self._serial: serial.Serial | None = None
        self._port = ""
        self._baudrate = 115200

    def connect(self, config: dict) -> bool:
        try:
            self._port = config.get("port", "COM1")
            self._baudrate = config.get("baudrate", 115200)
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=serial.EIGHTBITS,
                stopbits=serial.STOPBITS_ONE,
                parity=serial.PARITY_NONE,
                timeout=0.1,
            )
            self._running = True
            self.start()
            self.connected.emit()
            return True
        except Exception as e:
            self.error.emit(f"Serial connection failed: {e}")
            return False

    def disconnect(self) -> None:
        self._running = False
        if self._serial and self._serial.is_open:
            self._serial.close()
        self._serial = None
        self.disconnected.emit()

    def send(self, data: bytes) -> bool:
        if self._serial and self._serial.is_open:
            try:
                self._serial.write(data)
                return True
            except Exception:
                return False
        return False

    def run(self):
        buffer = bytearray()
        while self._running:
            if self._serial and self._serial.in_waiting > 0:
                try:
                    data = self._serial.read(self._serial.in_waiting)
                    buffer.extend(data)
                    for frame in self._drain_debug_frames(buffer):
                        self.data_received.emit(frame)
                except Exception as e:
                    self.error.emit(f"Serial read error: {e}")
        self._running = False

    @staticmethod
    def list_ports() -> list:
        ports = serial.tools.list_ports.comports()
        return [p.device for p in ports]
