import socket
from .base_worker import BaseWorker
from satellite_debug_tool.core.link_trace import FrameTraceLogger


class UdpWorker(BaseWorker):
    def __init__(self):
        super().__init__()
        self._sock: socket.socket | None = None
        self._local_addr = ("0.0.0.0", 45678)
        self._remote_addr = ("192.168.1.12", 4004)
        self._trace = FrameTraceLogger("DBG_UDP")

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
            self._trace.message(
                f"CONNECT local={self._local_addr[0]}:{self._local_addr[1]} "
                f"remote={self._remote_addr[0]}:{self._remote_addr[1]}"
            )
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
        self._trace.message("DISCONNECT")
        self.disconnected.emit()

    def send(self, data: bytes) -> bool:
        if self._sock:
            try:
                sent = self._sock.sendto(data, self._remote_addr)
                self._trace.frame(
                    "TX_UDP", data,
                    f"to={self._remote_addr[0]}:{self._remote_addr[1]} result={sent}",
                )
                return sent == len(data)
            except Exception as exc:
                self._trace.frame("TX_UDP_FAIL", data, f"error={exc!r}")
                return False
        self._trace.message(f"TX_UDP_FAIL socket=none frame_len={len(data)}")
        return False

    def run(self):
        buffer = bytearray()
        while self._running:
            if self._sock:
                try:
                    data, addr = self._sock.recvfrom(4096)
                    self._trace.frame(
                        "RX_UDP", data,
                        f"from={addr[0]}:{addr[1]} datagram_len={len(data)}",
                    )
                    buffer.extend(data)
                    for frame in self._drain_debug_frames(buffer):
                        self.data_received.emit(frame)
                except socket.timeout:
                    pass
                except Exception as e:
                    self._trace.message(f"RX_UDP_FAIL error={e!r}")
                    self.error.emit(f"UDP read error: {e}")
        self._running = False
