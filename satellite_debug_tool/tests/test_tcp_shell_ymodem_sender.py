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
    HEADER_BLOCK_SIZE,
    NAK,
    SOH,
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
    """脚本项为 bytes 或 (bytes, delay_s)：后者在到期前 recv 返回空、到期才交付，
    模拟设备在确认前的同步耗时（如 RT-Thread yota 头包 ACK 前擦分区）。"""

    def __init__(self, script=()) -> None:
        self.script = list(script)
        self.sent: list[bytes] = []
        self.cancel_requested_flag = False
        self._due_at: float | None = None

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def recv(self, timeout_s: float) -> bytes:
        if self.script:
            item = self.script[0]
            if isinstance(item, tuple):
                data, delay_s = item
                now = time.monotonic()
                if self._due_at is None:
                    self._due_at = now + delay_s
                if now >= self._due_at:
                    self.script.pop(0)
                    self._due_at = None
                    return bytes(data)
                time.sleep(min(timeout_s, 0.005))
                return b""
            return bytes(self.script.pop(0))
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
    marker = frame[0]
    assert marker in (SOH, STX)
    block = HEADER_BLOCK_SIZE if marker == SOH else BLOCK_SIZE
    assert len(frame) == 3 + block + 2
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

    def test_file_spec_accepts_payloads_beyond_the_removed_4mib_cap(self):
        """回归：移除上位机自加的 4 MiB 上限；文件能否容纳由设备端 CAN 决定。

        旧实现（MAX_PAYLOAD_BYTES）在此抛 "payload size out of range"。
        """
        data = b"\x00" * (4 * 1024 * 1024 + 1)
        spec = YmodemFileSpec("littlefs_assets.bin", data)
        assert spec.size == len(data)

    def test_file_spec_rejects_empty_payload(self):
        # 空文件 header 与批结束帧（空文件名）不可区分，协议层必须拒绝。
        with pytest.raises(YmodemError, match="empty"):
            YmodemFileSpec("name.bin", b"")

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

        # 头包与批结束帧是 SOH/128，数据块是 STX/1024（块类型合同见 sender docstring）。
        seq, payload, crc = parse_frame(header)
        assert header[0] == SOH
        assert seq == 0
        name_size = f"{spec.filename}\0{spec.size}".encode("ascii") + b"\x00"
        assert payload.startswith(name_size)
        assert payload[len(name_size) :] == bytes(HEADER_BLOCK_SIZE - len(name_size))
        assert crc == _crc16_ccitt(payload)

        seq1, payload1, _ = parse_frame(block1)
        assert block1[0] == STX
        assert seq1 == 1 and payload1 == spec.data[:BLOCK_SIZE]
        seq2, payload2, _ = parse_frame(block2)
        assert block2[0] == STX
        assert seq2 == 2
        assert payload2.startswith(spec.data[BLOCK_SIZE:])
        pad_length = BLOCK_SIZE - (spec.size - BLOCK_SIZE)
        assert payload2[len(spec.data) - BLOCK_SIZE :] == b"\x1a" * pad_length

        assert eot1 == bytes([EOT]) and eot2 == bytes([EOT])
        seq_end, payload_end, _ = parse_frame(batch_end)
        assert batch_end[0] == SOH
        assert seq_end == 0 and payload_end == bytes(HEADER_BLOCK_SIZE)

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
        headers = [frame for frame in transport.sent if frame[0] in (SOH, STX) and frame[1] == 0]
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


class TestRtThreadReceiverCompatibility:
    """XESA01（RT-Thread rym 组件）接收端合同回归。

    RT-Thread `_rym_do_fin` 对批结束头硬编码按 SOH 包长（133 字节）读取并以
    前 128 字节 payload 校验 CRC，不看帧头标记；STX 批结束帧在 131/132 位置
    取到的是全零 payload，CRC 必然失败——这是当时上位机给 XESA01 传输失败、
    只能用 WindTerm（SOH 批结束）绕过的根因。
    """

    def _sent_frames(self, spec: YmodemFileSpec) -> list[bytes]:
        transport = ScriptedTransport(happy_script((spec.size + BLOCK_SIZE - 1) // BLOCK_SIZE))
        result = make_sender().send_batch(spec, transport)
        assert result.completed
        return transport.sent

    def test_batch_end_header_passes_fixed_128_byte_fin_validation(self):
        frames = self._sent_frames(make_spec(BLOCK_SIZE))
        batch_end = frames[-1]
        # RT-Thread 只读 133 字节：marker+seq+~seq+128 payload+crc。
        assert batch_end[0] == SOH
        assert len(batch_end) == 133
        assert batch_end[1] == 0 and batch_end[2] == 0xFF
        recv_crc = int.from_bytes(batch_end[131:133], "big")
        assert recv_crc == _crc16_ccitt(batch_end[3:131])

    def test_header_frame_is_soh_128(self):
        frames = self._sent_frames(make_spec(16))
        header = frames[0]
        assert header[0] == SOH and len(header) == 133

    def test_oversized_header_falls_back_to_stx(self):
        long_name = "a" * 200 + ".bin"
        frames = self._sent_frames(YmodemFileSpec(long_name, bytes(16)))
        header = frames[0]
        assert header[0] == STX and len(header) == 1 + 2 + BLOCK_SIZE + 2
        assert header[3 : 3 + len(long_name)] == long_name.encode("ascii")

    def test_header_ack_waits_out_partition_erase_delay(self):
        """RT-Thread yota 在头包 ACK 前同步擦分区（XESA01 552 KiB 实测 ~7 s）。

        真机会话证据（20261019T071000Z）：头包 +5.00 s 按 ack_timeout 超时放弃，
        设备 +6.87 s 才回 ACK+C。头包 ACK 必须等在 final_ack 存储窗口上。
        """
        spec = make_spec(BLOCK_SIZE)
        script = [bytes([CRC_CHAR]), (bytes([ACK, CRC_CHAR]), 0.8)]
        script += happy_script(1)[2:]
        sender = make_sender(ack_timeout_s=0.3, final_ack_timeout_s=3.0)
        result = sender.send_batch(spec, ScriptedTransport(script))
        assert result.completed
        assert result.blocks_sent == 1

    def test_header_ack_window_is_the_storage_window(self):
        """直接锁定合同：头包 ACK 窗口取 final_ack_timeout_s，与 ack_timeout_s 无关。"""
        spec = make_spec(16)
        script = [bytes([CRC_CHAR]), (bytes([ACK, CRC_CHAR]), 0.8)]
        script += happy_script(1)[2:]
        sender = make_sender(ack_timeout_s=5.0, final_ack_timeout_s=0.3)
        result = sender.send_batch(spec, ScriptedTransport(script))
        assert result.outcome is YmodemSendOutcome.ERROR
        assert "timeout waiting for header acknowledgement" in result.error


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
