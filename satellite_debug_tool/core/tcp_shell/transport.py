"""Byte-stream link layer for the shell session (TCP or serial).

The shell terminal, YMODEM batch sender and session evidence treat the device
as one raw byte stream. Physical entry points verified on the firmware side:
TCP 1235 and the shell UART (letter shell console / RT-Thread msh console).
No firmware provides a UDP shell, so UDP is not a supported transport here.
"""

from __future__ import annotations

import socket


class ShellLinkError(RuntimeError):
    """Link open/reopen failure with an operator-readable reason."""


class ShellLink:
    """One byte-stream link owned and driven by the session worker thread."""

    label = ""

    def open(self) -> None:
        raise NotImplementedError

    def recv(self, timeout_s: float) -> bytes:
        """Return whatever arrived within the timeout (empty on timeout)."""
        raise NotImplementedError

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class TcpShellLink(ShellLink):
    def __init__(self, host: str, port: int, *, connect_timeout_s: float = 3.0) -> None:
        self._host = str(host)
        self._port = int(port)
        self._connect_timeout_s = float(connect_timeout_s)
        self._socket: socket.socket | None = None
        self.label = f"{self._host}:{self._port}"

    def open(self) -> None:
        try:
            connection = socket.create_connection(
                (self._host, self._port), timeout=self._connect_timeout_s
            )
        except OSError as exc:
            raise ShellLinkError(f"{type(exc).__name__}: {exc}") from exc
        connection.settimeout(0.05)
        self._socket = connection

    def recv(self, timeout_s: float) -> bytes:
        connection = self._require_open()
        connection.settimeout(max(timeout_s, 0.001))
        try:
            data = connection.recv(65536)
        except socket.timeout:
            return b""
        except OSError as exc:
            self.close()
            raise ShellLinkError(f"{type(exc).__name__}: {exc}") from exc
        if not data:
            self.close()
            raise ShellLinkError("peer-closed")
        return data

    def send(self, data: bytes) -> None:
        connection = self._require_open()
        try:
            connection.sendall(data)
        except OSError as exc:
            self.close()
            raise ShellLinkError(f"{type(exc).__name__}: {exc}") from exc

    def close(self) -> None:
        connection, self._socket = self._socket, None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _require_open(self) -> socket.socket:
        if self._socket is None:
            raise ShellLinkError("socket-closed")
        return self._socket


class SerialShellLink(ShellLink):
    """Raw byte pipe over pyserial (8N1); the shell console on the device side."""

    def __init__(self, port: str, baudrate: int) -> None:
        self._port = str(port)
        self._baudrate = int(baudrate)
        self._serial = None
        self.label = f"{self._port}@{self._baudrate}"

    def open(self) -> None:
        import serial

        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=serial.EIGHTBITS,
                stopbits=serial.STOPBITS_ONE,
                parity=serial.PARITY_NONE,
                timeout=0.05,
            )
        except Exception as exc:  # noqa: BLE001 - pyserial raises device-specific types
            self._serial = None
            raise ShellLinkError(f"{type(exc).__name__}: {exc}") from exc

    def recv(self, timeout_s: float) -> bytes:
        stream = self._require_open()
        try:
            stream.timeout = max(timeout_s, 0.001)
            waiting = stream.in_waiting
            if waiting:
                return bytes(stream.read(waiting))
            return bytes(stream.read(1))
        except Exception as exc:  # noqa: BLE001 - unplug mid-read raises OSError
            self.close()
            raise ShellLinkError(f"{type(exc).__name__}: {exc}") from exc

    def send(self, data: bytes) -> None:
        stream = self._require_open()
        try:
            stream.write(data)
            stream.flush()
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise ShellLinkError(f"{type(exc).__name__}: {exc}") from exc

    def close(self) -> None:
        stream, self._serial = self._serial, None
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - close must never raise upstream
                pass

    def _require_open(self):
        if self._serial is None:
            raise ShellLinkError("serial-closed")
        return self._serial


def create_link(target: dict, *, connect_timeout_s: float = 3.0) -> ShellLink:
    """Build the link for a target dict; unknown transports are refused."""

    transport = str(target.get("transport", "")).lower()
    if transport == "tcp":
        return TcpShellLink(
            target["host"], int(target["port"]), connect_timeout_s=connect_timeout_s
        )
    if transport == "serial":
        return SerialShellLink(target["port"], int(target.get("baudrate", 115200)))
    raise ShellLinkError(f"unsupported shell transport: {transport!r}")


def list_serial_ports() -> list[str]:
    import serial.tools.list_ports

    return [port.device for port in serial.tools.list_ports.comports()]


__all__ = [
    "ShellLink",
    "ShellLinkError",
    "SerialShellLink",
    "TcpShellLink",
    "create_link",
    "list_serial_ports",
]
