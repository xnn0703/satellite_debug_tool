"""双端问题取证所用的上位机链路追踪测试。"""

from __future__ import annotations

from unittest.mock import Mock

from satellite_debug_tool.core.comm.udp_worker import UdpWorker
from satellite_debug_tool.core.link_trace import (
    FrameTraceLogger,
    TRACE_ENV,
    describe_frame,
    trace_message,
)
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    ChannelDefEntry,
    ChannelDefineTable,
    CmdType,
    EventDefEntry,
    EventDefineTable,
    FrameReceiverV2,
    MetaInfo,
    StateDefEntry,
    StateDefineTable,
    SubCmd,
    build_frame,
    build_request_meta_info,
)
from satellite_debug_tool.core.protocol.handshake import Handshake


def test_trace_default_off(monkeypatch, capsys):
    monkeypatch.delenv(TRACE_ENV, raising=False)
    trace_message("DBG_TEST", "hidden")
    assert capsys.readouterr().out == ""


def test_control_frame_description_contains_fingerprint():
    description = describe_frame(build_request_meta_info())
    assert "cmd=CONTROL(0x03)" in description
    assert "sub=REQUEST_META_INFO(0x02)" in description
    assert "fid=" in description
    assert "frame_len=10" in description


def test_data_trace_is_rate_limited(monkeypatch, capsys):
    monkeypatch.setenv(TRACE_ENV, "1")
    times = iter([0.0, 0.2, 1.2])
    logger = FrameTraceLogger("DBG_TEST", clock=lambda: next(times))
    frame = build_frame(CmdType.DATA_REPORT, b"")

    logger.frame("RX_UDP", frame)
    logger.frame("RX_UDP", frame)
    logger.frame("RX_UDP", frame)

    output = capsys.readouterr().out
    assert output.count("DATA first") == 1
    assert output.count("DATA summary") == 1
    assert "frames=2" in output


def test_udp_send_trace_reports_subcmd(monkeypatch, capsys):
    monkeypatch.setenv(TRACE_ENV, "true")
    worker = UdpWorker()
    worker._sock = Mock()
    frame = build_request_meta_info()
    worker._sock.sendto.return_value = len(frame)

    assert worker.send(frame) is True

    output = capsys.readouterr().out
    assert "DBG_UDP" in output
    assert "TX_UDP" in output
    assert "REQUEST_META_INFO" in output
    assert "result=10" in output


def test_udp_receive_trace_reports_source_and_frame(monkeypatch, capsys):
    monkeypatch.setenv(TRACE_ENV, "1")
    worker = UdpWorker()
    frame = build_request_meta_info()

    class ReceiveOnceSocket:
        def recvfrom(self, _size: int):
            worker._running = False
            return frame, ("192.168.1.12", 4004)

    received: list[bytes] = []
    worker._sock = ReceiveOnceSocket()
    worker._running = True
    worker.data_received.connect(received.append)

    worker.run()

    assert received == [frame]
    output = capsys.readouterr().out
    assert "RX_UDP" in output
    assert "REQUEST_META_INFO" in output
    assert "from=192.168.1.12:4004" in output


def test_receiver_trace_reports_record_type(monkeypatch, capsys):
    monkeypatch.setenv(TRACE_ENV, "on")
    receiver = FrameReceiverV2()

    records = receiver.feed(build_frame(0x11, b"\x01"))

    assert len(records) == 1
    output = capsys.readouterr().out
    assert "DBG_FRAME" in output
    assert "DECODER_OUT" in output
    assert "record=RawFrame" in output


def test_handshake_trace_reports_requests_and_ready(monkeypatch, capsys):
    monkeypatch.setenv(TRACE_ENV, "1")
    sent: list[bytes] = []
    handshake = Handshake(ProfileStore(), sent.append)
    handshake.start()
    handshake.feed(MetaInfo(2, "fw", "esa01", "sn"))
    handshake.feed(ChannelDefineTable(
        1, [ChannelDefEntry(0, 1, 0, 0, "roll", "deg", -180, 180)],
    ))
    handshake.feed(StateDefineTable(1, [StateDefEntry(0, 0, 0, "LOCK")]))
    handshake.feed(EventDefineTable(1, [EventDefEntry(1, 1, "LOCKED")]))

    output = capsys.readouterr().out
    assert "TX REQUEST_META_INFO reason=initial result=accepted" in output
    assert "APPLY META hw='esa01'" in output
    assert "APPLY CHANNEL_DEFINE" in output
    assert "READY hw='esa01'" in output
    assert sent[0][6] == int(SubCmd.REQUEST_META_INFO)
