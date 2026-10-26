"""Strict YMODEM batch sender aligned with the AFD01 firmware receiver.

The firmware receiver (code/shared/MiddleWare/components/ymodem/ymodem.c)
follows standard YMODEM batch semantics with these binding details:

- The receiver polls ``C`` while waiting for the batch header.
- A rejected header (CRC error) is answered by another ``C`` rather than NAK,
  so the sender treats ``C`` while expecting a header ACK as "resend header".
- A data block with a bad frame is answered by NAK; a duplicate previous
  block is answered by ACK; an out-of-order block is answered by NAK.
- The first EOT is answered by NAK; after the second EOT the device commits
  the candidate to flash (which may take seconds) and only then sends ACK.
  The trailing ``C`` for the batch-end header is emitted on the next poll
  timeout, so the sender must wait for it after the final ACK.
- The batch ends with a sequence-0 header whose payload starts with two NUL
  bytes (empty filename), answered by a single ACK.
- Header and batch-end frames are sent as SOH/128 and data blocks as STX/1024.
  The RT-Thread rym component (XESA01) validates the batch-end header as a
  fixed 128-byte block regardless of the frame marker, so an STX batch-end
  frame always fails its CRC check there; the AFD01 receiver accepts both
  block sizes for every frame. A header longer than 128 bytes falls back to
  STX, which both receivers also accept.
- The sender does not cap the payload size: whether the destination can hold
  the file is decided by the device, which aborts the transfer with a single
  CAN. An empty payload is refused here because its header would be
  indistinguishable from the batch-end header.
- A receiver may run a slow storage operation before acknowledging: the
  RT-Thread yota command erases the whole destination partition in on_begin
  before the header ACK (~7 s for 552 KiB on XESA01, bounded by the 1 MiB
  partition), and the AFD01 firmware commits to flash before the final-EOT
  ACK. Both waits therefore use the storage-latency window
  (``final_ack_timeout_s``), while per-block ACKs use the short window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
from typing import Callable, Mapping

SOH = 0x01
STX = 0x02
EOT = 0x04
ACK = 0x06
NAK = 0x15
CAN = 0x18
CRC_CHAR = 0x43

BLOCK_SIZE = 1024
HEADER_BLOCK_SIZE = 128


class YmodemError(RuntimeError):
    """Protocol violation or timeout that terminates the transfer."""


class YmodemCancelled(RuntimeError):
    """The transfer was cancelled by the user or the device (CAN)."""


class YmodemTransport:
    """Byte-stream transport used by the sender; production wraps a socket."""

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def recv(self, timeout_s: float) -> bytes:
        """Return whatever bytes arrived within the timeout (may be empty)."""
        raise NotImplementedError

    def cancel_requested(self) -> bool:
        return False


@dataclass(frozen=True)
class YmodemFileSpec:
    filename: str
    data: bytes
    sha256: str = field(default="")

    def __post_init__(self) -> None:
        if not self.filename or "/" in self.filename or "\\" in self.filename:
            raise YmodemError(f"invalid ymodem filename: {self.filename!r}")
        if not self.data:
            raise YmodemError("ymodem payload is empty")
        digest = hashlib.sha256(self.data).hexdigest()
        if self.sha256 and self.sha256.lower() != digest:
            raise YmodemError("payload does not match the expected SHA-256")
        object.__setattr__(self, "sha256", digest)

    @property
    def size(self) -> int:
        return len(self.data)

    @classmethod
    def from_path(cls, path, *, filename: str = "", expected_sha256: str = "") -> "YmodemFileSpec":
        from pathlib import Path

        file_path = Path(path)
        data = file_path.read_bytes()
        return cls(filename or file_path.name, data, expected_sha256)


class YmodemSendOutcome(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class YmodemResult:
    outcome: YmodemSendOutcome
    blocks_sent: int = 0
    retransmits: int = 0
    bytes_confirmed: int = 0
    duration_s: float = 0.0
    error: str = ""

    @property
    def completed(self) -> bool:
        return self.outcome is YmodemSendOutcome.COMPLETED


EventCallback = Callable[[str, Mapping[str, object]], None]
ProgressCallback = Callable[[int, int, int], None]


class YmodemSender:
    """Drive one YMODEM batch over a byte transport, single read owner."""

    def __init__(
        self,
        *,
        c_timeout_s: float = 15.0,
        ack_timeout_s: float = 5.0,
        final_ack_timeout_s: float = 20.0,
        max_retries: int = 10,
        on_event: EventCallback | None = None,
        on_progress: ProgressCallback | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._c_timeout_s = float(c_timeout_s)
        self._ack_timeout_s = float(ack_timeout_s)
        self._final_ack_timeout_s = float(final_ack_timeout_s)
        self._max_retries = int(max_retries)
        self._on_event = on_event or (lambda _type, _details: None)
        self._on_progress = on_progress or (lambda _confirmed, _total, _retransmits: None)
        self._monotonic = monotonic_clock or _default_monotonic

    def send_batch(self, spec: YmodemFileSpec, transport: YmodemTransport) -> YmodemResult:
        started = self._monotonic()
        reader = _ControlReader(transport)
        result = YmodemResult(outcome=YmodemSendOutcome.ERROR)
        try:
            self._wait_for_c(reader, transport)
            self._send_header(spec, reader, transport, result)
            self._send_blocks(spec, reader, transport, result)
            self._send_eot_sequence(reader, transport, result)
            self._send_batch_end(spec, reader, transport)
            result.outcome = YmodemSendOutcome.COMPLETED
        except YmodemCancelled as exc:
            _try_send_cancel(transport)
            result.outcome = YmodemSendOutcome.CANCELLED
            result.error = str(exc) or "cancelled"
            self._on_event("cancelled", {"reason": result.error})
        except YmodemError as exc:
            _try_send_cancel(transport)
            result.error = str(exc)
            self._on_event("failed", {"reason": result.error})
        result.duration_s = self._monotonic() - started
        return result

    # -- stages ---------------------------------------------------------------

    def _wait_for_c(self, reader: _ControlReader, transport: YmodemTransport) -> None:
        self._on_event("waiting_c", {})
        control = reader.next_control(self._c_timeout_s)
        if control == CRC_CHAR:
            return
        if control is None:
            raise YmodemError("device did not send C after rb")
        raise YmodemError(f"unexpected byte while waiting for C: 0x{control:02X}")

    def _send_header(
        self,
        spec: YmodemFileSpec,
        reader: _ControlReader,
        transport: YmodemTransport,
        result: YmodemResult,
    ) -> None:
        header_payload = (
            spec.filename.encode("ascii") + b"\x00" + str(spec.size).encode("ascii") + b"\x00"
        )
        if len(header_payload) <= HEADER_BLOCK_SIZE:
            frame = _build_frame(0, header_payload.ljust(HEADER_BLOCK_SIZE, b"\x00"), SOH)
        else:
            frame = _build_frame(0, header_payload.ljust(BLOCK_SIZE, b"\x00"))
        retries = 0
        self._on_event("header_sent", {"filename": spec.filename, "size": spec.size})
        while True:
            transport.send(frame)
            # The receiver may erase the destination partition in on_begin
            # before acknowledging the header (RT-Thread yota), so the header
            # ACK waits on the storage-latency window, not the block window.
            control = reader.next_control(self._final_ack_timeout_s)
            if control == ACK:
                control = self._expect_c_after_header(reader, transport)
                if control == CRC_CHAR:
                    return
                raise YmodemError(f"unexpected byte after header ACK: 0x{control:02X}")
            # The firmware answers a rejected header with another C, not NAK.
            if control == CRC_CHAR or control == NAK:
                retries += 1
                result.retransmits = max(result.retransmits, retries)
                if retries > self._max_retries:
                    raise YmodemError("header rejected too many times")
                self._on_event("header_retransmit", {"attempt": retries})
                continue
            if control is None:
                raise YmodemError("timeout waiting for header acknowledgement")
            raise YmodemError(f"unexpected byte after header: 0x{control:02X}")

    def _expect_c_after_header(
        self, reader: _ControlReader, transport: YmodemTransport
    ) -> int | None:
        control = reader.next_control(self._c_timeout_s)
        if control is None:
            raise YmodemError("timeout waiting for C after header acknowledgement")
        return control

    def _send_blocks(
        self,
        spec: YmodemFileSpec,
        reader: _ControlReader,
        transport: YmodemTransport,
        result: YmodemResult,
    ) -> None:
        total_blocks = (spec.size + BLOCK_SIZE - 1) // BLOCK_SIZE
        sequence = 1
        confirmed = 0
        retries = 0
        while sequence <= total_blocks:
            chunk = spec.data[(sequence - 1) * BLOCK_SIZE : sequence * BLOCK_SIZE]
            frame = _build_frame(sequence & 0xFF, chunk.ljust(BLOCK_SIZE, b"\x1A"))
            transport.send(frame)
            self._on_event("block_sent", {"sequence": sequence, "blocks": total_blocks})
            control = reader.next_control(self._ack_timeout_s)
            if control == ACK:
                confirmed = min(sequence * BLOCK_SIZE, spec.size)
                result.blocks_sent = sequence
                result.bytes_confirmed = confirmed
                self._on_progress(confirmed, spec.size, result.retransmits)
                sequence += 1
                retries = 0
                continue
            if control == NAK:
                retries += 1
                result.retransmits += 1
                if retries > self._max_retries:
                    raise YmodemError(f"block {sequence} rejected too many times")
                self._on_event("block_retransmit", {"sequence": sequence, "attempt": retries})
                continue
            if control is None:
                raise YmodemError(f"timeout waiting for acknowledgement of block {sequence}")
            raise YmodemError(
                f"unexpected byte after block {sequence}: 0x{control:02X}"
            )

    def _send_eot_sequence(
        self, reader: _ControlReader, transport: YmodemTransport, result: YmodemResult
    ) -> None:
        transport.send(bytes([EOT]))
        self._on_event("eot_sent", {"stage": 1})
        control = reader.next_control(self._ack_timeout_s)
        if control != NAK:
            if control is None:
                raise YmodemError("timeout waiting for NAK after first EOT")
            raise YmodemError(f"unexpected byte after first EOT: 0x{control:02X}")
        transport.send(bytes([EOT]))
        self._on_event("eot_sent", {"stage": 2})
        # The device commits the candidate to flash before acknowledging.
        control = reader.next_control(self._final_ack_timeout_s)
        if control != ACK:
            if control is None:
                raise YmodemError("timeout waiting for ACK after final EOT")
            raise YmodemError(f"unexpected byte after final EOT: 0x{control:02X}")

    def _send_batch_end(
        self, spec: YmodemFileSpec, reader: _ControlReader, transport: YmodemTransport
    ) -> None:
        control = reader.next_control(self._c_timeout_s)
        if control != CRC_CHAR:
            if control is None:
                raise YmodemError("timeout waiting for C before batch end")
            raise YmodemError(f"unexpected byte before batch end: 0x{control:02X}")
        frame = _build_frame(0, bytes(HEADER_BLOCK_SIZE), SOH)
        transport.send(frame)
        self._on_event("batch_end_sent", {})
        control = reader.next_control(self._final_ack_timeout_s)
        if control != ACK:
            if control is None:
                raise YmodemError("timeout waiting for batch end acknowledgement")
            raise YmodemError(f"unexpected byte after batch end: 0x{control:02X}")


class _ControlReader:
    """Buffer received bytes and hand out protocol control bytes one by one.

    Only protocol control bytes are returned; shell echo and log noise that
    leak in before the firmware silences them (the ``rb`` echo) are skipped.
    """

    CONTROL_BYTES = frozenset({SOH, STX, EOT, ACK, NAK, CAN, CRC_CHAR})

    def __init__(self, transport: YmodemTransport) -> None:
        self._transport = transport
        self._pending: list[int] = []
        self.skipped_noise = 0

    def next_control(self, timeout_s: float) -> int | None:
        """Return the next control byte within timeout_s, or None on timeout.

        The firmware aborts with a single CAN (ymodem.c abort_candidate), so
        any CAN cancels the transfer immediately.
        """
        deadline = _default_monotonic() + float(timeout_s)
        while True:
            while self._pending:
                byte = self._pending.pop(0)
                if byte not in self.CONTROL_BYTES:
                    self.skipped_noise += 1
                    continue
                if byte == CAN:
                    raise YmodemCancelled("device cancelled the transfer")
                return byte
            remaining = deadline - _default_monotonic()
            if remaining <= 0:
                return None
            data = self._transport.recv(min(remaining, 1.0))
            if data:
                self._pending.extend(data)
                continue
            if self._transport.cancel_requested():
                raise YmodemCancelled("cancelled by operator")


def _build_frame(sequence: int, payload: bytes, marker: int = STX) -> bytes:
    block = HEADER_BLOCK_SIZE if marker == SOH else BLOCK_SIZE
    if len(payload) != block:
        raise YmodemError("ymodem payload must be exactly one block")
    return (
        bytes([marker, sequence & 0xFF, 0xFF - (sequence & 0xFF)])
        + payload
        + _crc16_ccitt(payload).to_bytes(2, "big")
    )


def _crc16_ccitt(data: bytes, seed: int = 0x0000) -> int:
    crc = seed
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc


def _try_send_cancel(transport: YmodemTransport) -> None:
    try:
        transport.send(bytes([CAN, CAN]))
    except Exception:  # noqa: BLE001 - best-effort cancel on a failing link
        pass


def _default_monotonic() -> float:
    import time

    return time.monotonic()


__all__ = [
    "ACK",
    "BLOCK_SIZE",
    "CAN",
    "CRC_CHAR",
    "EOT",
    "HEADER_BLOCK_SIZE",
    "NAK",
    "SOH",
    "STX",
    "YmodemCancelled",
    "YmodemError",
    "YmodemFileSpec",
    "YmodemResult",
    "YmodemSendOutcome",
    "YmodemSender",
    "YmodemTransport",
]
