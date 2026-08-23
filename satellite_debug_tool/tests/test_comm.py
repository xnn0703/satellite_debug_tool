import pytest
from unittest.mock import Mock, patch
from satellite_debug_tool.core.comm.base_worker import BaseWorker
from satellite_debug_tool.core.comm.serial_worker import SerialWorker
from satellite_debug_tool.core.comm.udp_worker import UdpWorker


class TestBaseWorker:
    def test_is_connected_init(self):
        worker = SerialWorker()
        assert worker.is_connected() is False

    def test_not_implemented_methods(self):
        worker = BaseWorker()
        with pytest.raises(NotImplementedError):
            worker.connect({})
        with pytest.raises(NotImplementedError):
            worker.disconnect()
        with pytest.raises(NotImplementedError):
            worker.send(b"")


class TestSerialWorker:
    def test_init(self):
        worker = SerialWorker()
        assert worker._serial is None
        assert worker._running is False

    @patch("serial.Serial")
    def test_connect_success(self, mock_serial):
        mock_serial_instance = Mock()
        mock_serial.return_value = mock_serial_instance
        mock_serial_instance.is_open = True

        worker = SerialWorker()
        config = {"port": "COM3", "baudrate": 115200}
        with patch.object(worker, "start"):
            result = worker.connect(config)
        assert result is True

    @patch("serial.Serial")
    def test_connect_invalid_port(self, mock_serial):
        mock_serial.side_effect = Exception("Port not found")
        worker = SerialWorker()
        config = {"port": "INVALID_PORT", "baudrate": 115200}
        result = worker.connect(config)
        assert result is False

    def test_disconnect(self):
        worker = SerialWorker()
        worker._running = True
        worker._serial = Mock()
        worker._serial.is_open = True
        worker.disconnect()
        assert worker._running is False

    def test_receive_emits_the_exact_raw_read_chunk(self):
        worker = SerialWorker()
        payload = b"serial-noise\xAA\x55partial-frame"

        class ReadOnceSerial:
            is_open = True

            @property
            def in_waiting(self) -> int:
                return len(payload)

            def read(self, _size: int) -> bytes:
                worker._running = False
                return payload

        received: list[bytes] = []
        worker._serial = ReadOnceSerial()
        worker._running = True
        worker.data_received.connect(received.append)

        worker.run()

        assert received == [payload]


class TestUdpWorker:
    def test_init(self):
        worker = UdpWorker()
        assert worker._sock is None
        assert worker._running is False
        assert worker._local_addr == ("0.0.0.0", 45678)
        assert worker._remote_addr == ("192.168.1.12", 4004)

    @patch("socket.socket")
    def test_connect_success(self, mock_socket):
        mock_sock_instance = Mock()
        mock_socket.return_value = mock_sock_instance

        worker = UdpWorker()
        config = {
            "local_port": 45678,
            "remote_ip": "192.168.1.100",
            "remote_port": 5000,
        }
        with patch.object(worker, "start"):
            result = worker.connect(config)
        assert result is True
        assert worker._remote_addr == ("192.168.1.100", 5000)

    def test_disconnect(self):
        worker = UdpWorker()
        worker._running = True
        worker._sock = Mock()
        worker.disconnect()
        assert worker._running is False
        assert worker._sock is None

    def test_receive_emits_the_exact_raw_datagram(self):
        worker = UdpWorker()
        payload = b"udp-noise\xAA\x55partial-frame"
        receive_sizes: list[int] = []

        class ReceiveOnceSocket:
            def recvfrom(self, size: int):
                receive_sizes.append(size)
                worker._running = False
                return payload, ("192.168.1.12", 4004)

        received: list[bytes] = []
        worker._sock = ReceiveOnceSocket()
        worker._running = True
        worker.data_received.connect(received.append)

        worker.run()

        assert received == [payload]
        assert receive_sizes == [65535]

    def test_receive_rejects_datagrams_from_other_endpoint(self):
        worker = UdpWorker()
        accepted = b"configured-device"
        calls = iter(
            (
                (b"other-device", ("192.168.1.99", 4004)),
                (accepted, ("192.168.1.12", 4004)),
            )
        )

        class ReceiveSocket:
            def recvfrom(self, _size: int):
                data, endpoint = next(calls)
                if data is accepted:
                    worker._running = False
                return data, endpoint

        received: list[bytes] = []
        worker._sock = ReceiveSocket()
        worker._running = True
        worker.data_received.connect(received.append)

        worker.run()

        assert received == [accepted]
        assert worker.rejected_datagram_count == 1

    def test_deliberate_socket_close_does_not_emit_read_error(self):
        worker = UdpWorker()

        class ClosedSocket:
            def recvfrom(self, _size: int):
                worker._running = False
                raise OSError("socket closed")

        errors: list[str] = []
        worker._sock = ClosedSocket()
        worker._running = True
        worker.error.connect(errors.append)

        worker.run()

        assert errors == []


class TestUdpDefaultConfig:
    def test_default_remote_ip(self):
        worker = UdpWorker()
        assert worker._remote_addr[0] == "192.168.1.12"

    def test_default_remote_port(self):
        worker = UdpWorker()
        assert worker._remote_addr[1] == 4004

    def test_default_local_port(self):
        worker = UdpWorker()
        assert worker._local_addr[1] == 45678

    @patch("socket.socket")
    def test_custom_config(self, mock_socket):
        mock_sock_instance = Mock()
        mock_socket.return_value = mock_sock_instance

        worker = UdpWorker()
        config = {"local_port": 12345, "remote_ip": "10.0.0.1", "remote_port": 8080}
        with patch.object(worker, "start"):
            worker.connect(config)
        assert worker._local_addr == ("0.0.0.0", 12345)
        assert worker._remote_addr == ("10.0.0.1", 8080)
