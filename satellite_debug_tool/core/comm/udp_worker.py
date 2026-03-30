import socket
from .base_worker import BaseWorker


class UdpWorker(BaseWorker):
    def __init__(self):
        super().__init__()
        self._sock: socket.socket | None = None
        self._local_addr = ("0.0.0.0", 45678)
        self._remote_addr = ("192.168.1.12", 4004)

    def connect(self, config: dict) -> bool:
        try:
            local_port = config.get("local_port", 45678)
            remote_ip = config.get("remote_ip", "192.168.1.12")
            remote_port = config.get("remote_port", 4004)
            self._local_addr = ("0.0.0.0", local_port)
            self._remote_addr = (remote_ip, remote_port)
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(self._local_addr)
            self._sock.settimeout(0.1)
            self._running = True
            self.start()
            self.connected.emit()
            return True
        except Exception as e:
            self.error.emit(f"UDP connection failed: {e}")
            return False

    def disconnect(self) -> None:
        self._running = False
        if self._sock:
            self._sock.close()
            self._sock = None
        self.disconnected.emit()

    def send(self, data: bytes) -> bool:
        if self._sock:
            try:
                self._sock.sendto(data, self._remote_addr)
                return True
            except Exception:
                return False
        return False

    def run(self):
        buffer = bytearray()
        while self._running:
            if self._sock:
                try:
                    data, addr = self._sock.recvfrom(4096)
                    buffer.extend(data)
                    while buffer:
                        idx = buffer.find(b"\xaa\x55")
                        if idx < 0:
                            buffer.clear()
                            break
                        buffer = buffer[idx:]
                        if len(buffer) >= 9:
                            data_len = int.from_bytes(buffer[4:6], "little")
                            frame_len = 9 + data_len
                            if len(buffer) >= frame_len:
                                frame = bytes(buffer[:frame_len])
                                self.data_received.emit(frame)
                                buffer = buffer[frame_len:]
                            else:
                                break
                        else:
                            break
                except socket.timeout:
                    pass
                except Exception as e:
                    self.error.emit(f"UDP read error: {e}")
        self._running = False
