"""Shell link 层回归：TCP 真 socket 对拍 + 串口 mock pyserial + 分派合同。

worker 的命令循环/YMODEM 逻辑与物理口无关（全部经 ShellLink 接口），TCP 端到端
已由 test_tcp_shell_worker.py 的 socket 假设备覆盖；本文件锁定 link 层本身。
"""

from __future__ import annotations

import socket
import threading
import time
from unittest.mock import Mock, patch

import pytest

from satellite_debug_tool.core.tcp_shell.transport import (
    SerialShellLink,
    ShellLinkError,
    TcpShellLink,
    create_link,
)


class _Listener:
    def __init__(self) -> None:
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.conn: socket.socket | None = None
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        self.conn, _ = self.server.accept()

    def peer_send(self, data: bytes) -> None:
        deadline = time.monotonic() + 2.0
        while self.conn is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert self.conn is not None
        self.conn.sendall(data)

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
        self.server.close()


class TestTcpShellLink:
    def test_open_send_recv_label(self):
        listener = _Listener()
        try:
            link = TcpShellLink("127.0.0.1", listener.port)
            assert link.label == f"127.0.0.1:{listener.port}"
            link.open()
            listener.peer_send(b"hello")
            deadline = time.monotonic() + 2.0
            data = b""
            while not data and time.monotonic() < deadline:
                data = link.recv(0.2)
            assert data == b"hello"
            link.send(b"ok")
            assert link.recv(0.05) == b""  # 超时返回空
            link.close()
            link.close()  # 幂等
        finally:
            listener.close()

    def test_open_failure_raises_link_error(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        # 占住端口但不 accept；未决队列满前仍会成功，用未监听的已绑定端口不可靠，
        # 改用非法地址触发失败。
        blocker.close()
        link = TcpShellLink("256.256.256.256", 1, connect_timeout_s=0.2)
        with pytest.raises(ShellLinkError):
            link.open()

    def test_peer_close_raises_and_closes(self):
        listener = _Listener()
        try:
            link = TcpShellLink("127.0.0.1", listener.port)
            link.open()
            listener.peer_send(b"x")
            deadline = time.monotonic() + 2.0
            while link.recv(0.1) != b"x" and time.monotonic() < deadline:
                pass
            listener.close()
            with pytest.raises(ShellLinkError):
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    link.recv(0.2)
        finally:
            listener.close()


class TestSerialShellLink:
    def _make_link(self, mock_serial_cls) -> SerialShellLink:
        link = SerialShellLink("/dev/cu.test", 115200)
        with patch("serial.Serial", mock_serial_cls):
            link.open()
        return link

    def test_open_uses_8n1_and_label(self):
        mock_cls = Mock()
        link = SerialShellLink("COM7", 921600)
        with patch("serial.Serial", mock_cls):
            link.open()
        assert link.label == "COM7@921600"
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["port"] == "COM7"
        assert kwargs["baudrate"] == 921600
        assert kwargs["bytesize"] is not None and kwargs["parity"] is not None

    def test_open_failure_raises_link_error(self):
        mock_cls = Mock(side_effect=OSError("no such port"))
        link = SerialShellLink("COMX", 115200)
        with patch("serial.Serial", mock_cls):
            with pytest.raises(ShellLinkError, match="no such port"):
                link.open()

    def test_recv_returns_pending_block_and_single_byte(self):
        stream = Mock()
        stream.in_waiting = 4
        stream.read = Mock(side_effect=lambda n: b"abcd"[:n])
        link = SerialShellLink("/dev/cu.test", 115200)
        link._serial = stream
        assert link.recv(0.1) == b"abcd"
        stream.in_waiting = 0
        stream.read = Mock(return_value=b"z")
        assert link.recv(0.1) == b"z"

    def test_recv_error_closes_and_raises(self):
        stream = Mock()
        stream.in_waiting = 3
        stream.read = Mock(side_effect=OSError("device unplugged"))
        link = SerialShellLink("/dev/cu.test", 115200)
        link._serial = stream
        with pytest.raises(ShellLinkError):
            link.recv(0.1)
        assert link._serial is None
        stream.close.assert_called()

    def test_send_writes_and_flushes(self):
        stream = Mock()
        link = SerialShellLink("/dev/cu.test", 115200)
        link._serial = stream
        link.send(b"rb\r")
        stream.write.assert_called_once_with(b"rb\r")
        stream.flush.assert_called_once()


class TestCreateLinkDispatch:
    def test_tcp_and_serial_dispatch(self):
        tcp = create_link({"transport": "tcp", "host": "1.2.3.4", "port": 1235})
        assert isinstance(tcp, TcpShellLink)
        assert tcp.label == "1.2.3.4:1235"
        serial = create_link(
            {"transport": "serial", "port": "/dev/cu.usb", "baudrate": 115200}
        )
        assert isinstance(serial, SerialShellLink)
        assert serial.label == "/dev/cu.usb@115200"

    def test_udp_is_refused(self):
        # 设备侧没有 UDP shell 服务；未知 transport 明确拒绝而不是静默降级。
        with pytest.raises(ShellLinkError, match="unsupported"):
            create_link({"transport": "udp", "host": "1.2.3.4", "port": 9000})


class TestSerialWorkerPath:
    def test_controller_serial_target_reports_connect_failure(
        self, qapplication_session
    ):
        from satellite_debug_tool.core.tcp_shell.controller import TcpShellController
        from satellite_debug_tool.i18n import tr  # noqa: F401 - 保持 conftest locale

        controller = TcpShellController()
        failures: list[str] = []
        controller.connect_failed.connect(failures.append)
        controller.connect_device(
            {"transport": "serial", "port": "definitely-not-a-port", "baudrate": 115200}
        )
        deadline = time.monotonic() + 5.0
        while not failures and time.monotonic() < deadline:
            qapplication_session.processEvents()
            time.sleep(0.01)
        controller.shutdown()
        assert failures, "serial open failure must surface as connect_failed"
        assert "definitely-not-a-port" not in failures[0] or failures
