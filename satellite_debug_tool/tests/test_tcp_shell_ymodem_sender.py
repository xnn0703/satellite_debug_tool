"""M37 YMODEM sender 纯状态机回归。

ScriptedTransport 按脚本逐项返回字节并记录全部发送字节；脚本耗尽后短睡返回空，
配合短超时的 sender 验证失败路径。
"""

from __future__ import annotations

import time

import pytest

from satellite_debug_tool.core.tcp_shell.ymodem_sender import (
    ACK,
    BLOCK_SIZE,
    CAN,
    CRC_CHAR,
    EOT,
    NAK,
    STX,
    YmodemCancelled,
    YmodemError,
    YmodemFileSpec,
    YmodemSendOutcome,
    YmodemSender,
    YmodemTransport,
    _crc16_ccitt,
)


class ScriptedTransport(YmodemTransport):
    def __init__(self, script=()) -> None:
        self.script = [bytes(item) for item in script]
        self.sent: list[bytes] = []
        self.cancel_requested_flag = False

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def recv(self, timeout_s: float) -> bytes:
        if self.script:
            return self.script.pop(0)
        time.sleep(min(timeout_s, 0.005))
        return b""

    def cancel_requested(self) -> bool:
        return self.cancel_requested_flag


def make_sender(**kwargs) -> YmodemSender:
    defaults = dict(
        c_timeout_s=0.2,
        ack_timeout_s=0.2,
        final_ack_timeout_s=0.2,
        max_retries=3,
    )
    defaults.update(kwargs)
    return YmodemSender(**defaults)


def make_spec(size: int = 1500, name: str = "afd01c_app_0.0.131_beta.bin") -> YmodemFileSpec:
    return YmodemFileSpec(name, bytes((index * 7 + 3) & 0xFF for index in range(size)))


def happy_script(block_count: int) -> list[bytes]:
    script = [bytes([CRC_CHAR])]
    script.append(bytes([ACK, CRC_CHAR]))  # header accepted
    script.extend([bytes([ACK])] * block_count)
    script.append(bytes([NAK]))  # first EOT
    script.append(bytes([ACK]))  # final EOT (after device commit)
    script.append(bytes([CRC_CHAR]))  # invite batch-end header
    script.append(bytes([ACK]))  # batch end accepted
    return script


def parse_frame(frame: bytes) -> tuple[int, bytes, int]:
    assert frame[0] == STX
    sequence = frame[1]
    assert frame[2] == 0xFF - sequence
    payload = frame[3:-2]
    crc = int.from_bytes(frame[-2:], "big")
    return sequence, payload, crc


class TestFrameContract:
    def test_crc16_ccitt_known_vector(self):
        assert _crc16_ccitt(b"123456789") == 0x31C3

    def test_file_spec_rejects_path_characters_and_mismatched_digest(self):
        with pytest.raises(YmodemError):
            YmodemFileSpec("a/b.bin", b"x")
        with pytest.raises(YmodemError):
            YmodemFileSpec("name.bin", b"x", sha256="00" * 32)

    def test_file_spec_computes_sha256(self):
        spec = YmodemFileSpec("f.bin", b"abc")
        import hashlib

        assert spec.sha256 == hashlib.sha256(b"abc").hexdigest()


class TestHappyPath:
    def test_full_batch_transfer_frames(self):
        spec = make_spec(1500)  # 2 blocks
        transport = ScriptedTransport(happy_script(2))
        events: list[str] = []
        sender = make_sender(on_event=lambda kind, _d: events.append(kind))
        result = sender.send_batch(spec, transport)

        assert result.completed
        assert result.blocks_sent == 2
        assert result.bytes_confirmed == 1500
        assert result.retransmits == 0
        assert "waiting_c" in events and "batch_end_sent" in events

        header, block1, block2, eot1, eot2, batch_end = transport.sent

        seq, payload, crc = parse_frame(header)
        assert seq == 0
        name_size = f"{spec.filename}\0{spec.size}".encode("ascii") + b"\x00"
        assert payload.startswith(name_size)
        assert payload[len(name_size) :] == bytes(BLOCK_SIZE - len(name_size))
        assert crc == _crc16_ccitt(payload)

        seq1, payload1, _ = parse_frame(block1)
        assert seq1 == 1 and payload1 == spec.data[:BLOCK_SIZE]
        seq2, payload2, _ = parse_frame(block2)
        assert seq2 == 2
        assert payload2.startswith(spec.data[BLOCK_SIZE:])
        pad_length = BLOCK_SIZE - (spec.size - BLOCK_SIZE)
        assert payload2[len(spec.data) - BLOCK_SIZE :] == b"\x1a" * pad_length

        assert eot1 == bytes([EOT]) and eot2 == bytes([EOT])
        seq_end, payload_end, _ = parse_frame(batch_end)
        assert seq_end == 0 and payload_end == bytes(BLOCK_SIZE)

    def test_exact_block_multiple_sends_no_padding_block(self):
        spec = make_spec(BLOCK_SIZE)
        transport = ScriptedTransport(happy_script(1))
        result = make_sender().send_batch(spec, transport)
        assert result.completed and result.blocks_sent == 1


class TestRetransmitAndRejection:
    def test_header_rejected_with_c_is_resent(self):
        spec = make_spec(BLOCK_SIZE)
        transport = ScriptedTransport(
            [bytes([CRC_CHAR]), bytes([CRC_CHAR])] + happy_script(1)[1:]
        )
        events: list[str] = []
        result = make_sender(on_event=lambda kind, _d: events.append(kind)).send_batch(
            spec, transport
        )
        assert result.completed
        assert "header_retransmit" in events
        headers = [frame for frame in transport.sent if frame[0] == STX and frame[1] == 0]
        # original header + one resend + the batch-end header (also sequence 0)
        assert len(headers) == 3

    def test_block_nak_retransmits_same_block(self):
        spec = make_spec(BLOCK_SIZE)
        script = [bytes([CRC_CHAR]), bytes([ACK, CRC_CHAR]), bytes([NAK])] + happy_script(1)[2:]
        transport = ScriptedTransport(script)
        result = make_sender().send_batch(spec, transport)
        assert result.completed
        assert result.retransmits == 1
        block_frames = [f for f in transport.sent if f[0] == STX and f[1] == 1]
        assert len(block_frames) == 2

    def test_retry_limit_raises_error_and_sends_can_can(self):
        spec = make_spec(BLOCK_SIZE)
        script = [bytes([CRC_CHAR]), bytes([ACK, CRC_CHAR])] + [bytes([NAK])] * 5
        transport = ScriptedTransport(script)
        result = make_sender().send_batch(spec, transport)
        assert result.outcome is YmodemSendOutcome.ERROR
        assert "rejected too many times" in result.error
        assert transport.sent[-1] == bytes([CAN, CAN])

    def test_device_can_cancels_transfer(self):
        spec = make_spec(BLOCK_SIZE)
        transport = ScriptedTransport([bytes([CRC_CHAR]), bytes([ACK, CRC_CHAR]), bytes([CAN])])
        result = make_sender().send_batch(spec, transport)
        assert result.outcome is YmodemSendOutcome.CANCELLED
        assert transport.sent[-1] == bytes([CAN, CAN])

    def test_operator_cancel_sends_can_can(self):
        spec = make_spec(BLOCK_SIZE)
        transport = ScriptedTransport([bytes([CRC_CHAR]), bytes([ACK, CRC_CHAR])])
        transport.cancel_requested_flag = True
        result = make_sender().send_batch(spec, transport)
        assert result.outcome is YmodemSendOutcome.CANCELLED
        assert transport.sent[-1] == bytes([CAN, CAN])

    def test_timeout_waiting_for_c_is_error(self):
        transport = ScriptedTransport([])
        result = make_sender().send_batch(make_spec(16), transport)
        assert result.outcome is YmodemSendOutcome.ERROR
        assert "did not send C" in result.error

    def test_first_eot_requires_nak(self):
        spec = make_spec(BLOCK_SIZE)
        script = [bytes([CRC_CHAR]), bytes([ACK, CRC_CHAR]), bytes([ACK]), bytes([ACK])]
        transport = ScriptedTransport(script)
        result = make_sender().send_batch(spec, transport)
        assert result.outcome is YmodemSendOutcome.ERROR
        assert "first EOT" in result.error


class TestByteStreamFragmentation:
    def test_control_bytes_delivered_in_fragments(self):
        """ACK 与 C 分片、跨 recv 到达也能推进状态机。"""
        spec = make_spec(BLOCK_SIZE)
        script = [
            bytes([CRC_CHAR]),
            bytes([ACK]),
            bytes([CRC_CHAR]),
            bytes([ACK]),
            bytes([NAK]),
            bytes([ACK]),
            bytes([CRC_CHAR]),
            bytes([ACK]),
        ]
        transport = ScriptedTransport(script)
        result = make_sender().send_batch(spec, transport)
        assert result.completed

    def test_spurious_bytes_between_controls_are_skipped(self):
        spec = make_spec(BLOCK_SIZE)
        script = [
            b"\x00" + bytes([CRC_CHAR]),  # C 前混入日志字节
            bytes([ACK, CRC_CHAR]),
            b"log" + bytes([ACK]),  # ACK 前混入日志字节
            bytes([NAK]),
            bytes([ACK]),
            bytes([CRC_CHAR]),
            bytes([ACK]),
        ]
        transport = ScriptedTransport(script)
        result = make_sender().send_batch(spec, transport)
        assert result.completed
