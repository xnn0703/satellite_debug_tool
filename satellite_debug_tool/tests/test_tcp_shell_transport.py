"""Shell link 层回归：TCP 真 socket 对拍 + 串口 mock pyserial + 分派合同。

worker 的命令循环/YMODEM 逻辑与物理口无关（全部经 ShellLink 接口），TCP 端到端
已由 test_tcp_shell_worker.py 的 socket 假设备覆盖；本文件锁定 link 层本身。
"""

from __future__ import annotations

import socket
import sys
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


class _FakePyserial:
    """pyserial 3.5 的开口语义：带 port 即打开、dtr/rts 不是构造参数、开口时按当前线状态驱动。"""

    opened: tuple | None = None  # 最近一次 open() 时的完整串口状态

    def __init__(self, port=None, baudrate=9600, bytesize=8, parity="N", stopbits=1,
                 timeout=None, **kwargs):
        if kwargs:
            raise ValueError(f"unexpected keyword arguments: {kwargs!r}")
        self.is_open = False
        self.port, self.baudrate = port, baudrate
        self.bytesize, self.parity, self.stopbits, self.timeout = bytesize, parity, stopbits, timeout
        self._dtr, self._rts = True, True  # pyserial 默认：两线置位
        if port is not None:
            self.open()

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        self._dtr = value

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        self._rts = value

    def open(self):
        type(self).opened = (self.port, self.baudrate, self.bytesize, self.parity,
                             self.stopbits, self.timeout, self._dtr, self._rts)
        self.is_open = True


class TestSerialShellLink:
    def _make_link(self, mock_serial_cls) -> SerialShellLink:
        link = SerialShellLink("/dev/cu.test", 115200)
        with patch("serial.Serial", mock_serial_cls):
            link.open()
        return link

    def test_open_uses_8n1_and_leaves_modem_lines_deasserted(self):
        # 参照终端（COMTool）开口合同：8N1、rts=false、dtr=false。置位会经 CDC 桥
        # 改变桥的行为（首段长输出截断后设备不再回应）。
        _FakePyserial.opened = None
        link = SerialShellLink("COM7", 921600)
        with patch("serial.Serial", _FakePyserial):
            link.open()
        assert link.label == "COM7@921600"
        port, baudrate, bytesize, parity, stopbits, _timeout, dtr, rts = _FakePyserial.opened
        assert (port, baudrate, bytesize, parity, stopbits) == ("COM7", 921600, 8, "N", 1)
        assert (dtr, rts) == (False, False)

    @pytest.mark.skipif(
        sys.platform not in ("darwin", "linux"), reason="需要 POSIX 伪终端与真实 pyserial"
    )
    def test_open_clears_dtr_and_rts_through_real_pyserial(self, monkeypatch):
        import fcntl
        import os
        import pty
        import struct

        import serial.serialposix as posix

        attempted: list[tuple[str, str]] = []
        real_ioctl = fcntl.ioctl

        def recording_ioctl(fd, request, *args):
            if request in (posix.TIOCMBIS, posix.TIOCMBIC):
                bits = struct.unpack("I", args[0])[0]
                attempted.append(
                    ("set" if request == posix.TIOCMBIS else "clear",
                     {posix.TIOCM_DTR: "DTR", posix.TIOCM_RTS: "RTS"}[bits])
                )
                return 0  # 伪终端没有调制解调器线；按真实串口返回成功以观察后续调用
            return real_ioctl(fd, request, *args)

        monkeypatch.setattr(fcntl, "ioctl", recording_ioctl)
        master, slave = pty.openpty()
        try:
            link = SerialShellLink(os.ttyname(slave), 115200)
            link.open()
            try:
                assert attempted == [("clear", "DTR"), ("clear", "RTS")]
                assert (link._serial.dtr, link._serial.rts) == (False, False)
            finally:
                link.close()
        finally:
            os.close(master)
            os.close(slave)

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

    def test_recv_assigns_port_timeout_only_when_it_changes(self):
        # 读取热路径不得每次重设串口超时：pyserial 的 timeout setter 重走端口重配置，USB CDC
        # 虚拟 COM 上该调用实测约 1 ms；数据流入期间每次读取插入这段等待。
        class _CountingStream:
            def __init__(self) -> None:
                self._timeout = 0.05
                self.timeout_sets = 0
                self.in_waiting = 0
                self.read_sizes: list[int] = []

            @property
            def timeout(self):
                return self._timeout

            @timeout.setter
            def timeout(self, value):
                self.timeout_sets += 1
                self._timeout = value

            def read(self, size):
                self.read_sizes.append(size)
                return b"" if size == 1 else b"x" * size

            def close(self):
                return None

        stream = _CountingStream()
        link = SerialShellLink("/dev/cu.test", 115200)
        link._serial = stream
        for _ in range(5):
            assert link.recv(0.05) == b""  # 空转：等待一个字节
        stream.in_waiting = 40
        assert link.recv(0.05) == b"x" * 40  # 有积压：整块取出
        assert stream.read_sizes == [1] * 5 + [40]
        assert stream.timeout_sets == 0  # 取值未变：读取路径一次都不重设
        link.recv(0.2)
        assert stream.timeout_sets == 1 and stream.timeout == 0.2

    @pytest.mark.skipif(
        sys.platform not in ("darwin", "linux"), reason="需要 POSIX 伪终端与真实 pyserial"
    )
    def test_recv_hot_path_skips_port_reconfiguration_on_real_pyserial(self, monkeypatch):
        import os
        import pty
        import termios

        real_tcgetattr = termios.tcgetattr
        reconfigures: list[int] = []
        monkeypatch.setattr(
            termios, "tcgetattr", lambda fd: (reconfigures.append(1), real_tcgetattr(fd))[1]
        )
        master, slave = pty.openpty()
        try:
            link = SerialShellLink(os.ttyname(slave), 115200)
            link.open()
            try:
                reconfigures.clear()
                for _ in range(20):
                    link.recv(0.05)
                assert reconfigures == []  # 同一超时值：真实 pyserial 零次端口重配置
                link.recv(0.2)
                assert len(reconfigures) == 1  # 取值变化：恰好一次
            finally:
                link.close()
        finally:
            os.close(master)
            os.close(slave)

    def test_send_submits_bytes_without_draining(self):
        stream = Mock()
        link = SerialShellLink("/dev/cu.test", 115200)
        link._serial = stream
        link.send(b"rb\r")
        stream.write.assert_called_once_with(b"rb\r")
        stream.flush.assert_not_called()

    def test_send_returns_while_the_wire_is_still_draining(self):
        # send 只等待系统接收；线上未发完不得占用唯一 link owner（停止意图与接收都靠它）。
        release = threading.Event()

        class _SlowDrainStream:
            def write(self, data):
                return len(data)

            def flush(self):
                release.wait(2.0)

            def close(self):
                release.set()

        link = SerialShellLink("/dev/cu.test", 115200)
        link._serial = _SlowDrainStream()
        started = time.monotonic()
        try:
            link.send(b"\r")
            assert time.monotonic() - started < 0.5
            assert link._serial is not None
        finally:
            release.set()


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
