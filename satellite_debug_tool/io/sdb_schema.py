"""Single source of truth for SDB v2/v3 binary layout."""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import BinaryIO


SDB_MAGIC = b"SDB\x00"
SDB_FOOTER = b"\xee\xee\xee\xee"

SDB_VERSION_V2 = 0x0002
SDB_VERSION_V3 = 0x0003
SDB_SUPPORTED_VERSIONS = frozenset((SDB_VERSION_V2, SDB_VERSION_V3))

SDB_RECORD_RX_CHUNK = 0x01
SDB_RECORD_GAP = 0x02
SDB_RECORD_CONTROL_TX = 0x03
SDB_RECORD_METADATA = 0x04
SDB_RECORD_SUMMARY = 0xFE

SDB_HEADER_CORE = struct.Struct("<4sHQ")
SDB_PROFILE_LENGTH = struct.Struct("<I")
SDB_HEADER_RESERVED_SIZE = 8
SDB_V3_RECORD_HEADER = struct.Struct("<BQI")


@dataclass(frozen=True)
class SdbRecordEnvelope:
    record_type: int
    host_timestamp_ns: int
    payload_length: int
    record_offset: int
    payload_offset: int


def read_exact(file_object: BinaryIO, size: int, *, context: str) -> bytes:
    payload = file_object.read(int(size))
    if len(payload) != int(size):
        raise EOFError(f"truncated {context}")
    return payload


def iter_record_envelopes(
    file_object: BinaryIO,
    *,
    start_offset: int,
    end_offset: int,
):
    cursor = int(start_offset)
    file_object.seek(cursor)
    while cursor < int(end_offset):
        if cursor + SDB_V3_RECORD_HEADER.size > int(end_offset):
            raise EOFError("truncated v3 record header")
        header = read_exact(
            file_object,
            SDB_V3_RECORD_HEADER.size,
            context="v3 record header",
        )
        record_type, host_ns, payload_len = SDB_V3_RECORD_HEADER.unpack(header)
        payload_offset = cursor + SDB_V3_RECORD_HEADER.size
        next_offset = payload_offset + payload_len
        if next_offset > int(end_offset):
            raise EOFError("truncated v3 record payload")
        yield SdbRecordEnvelope(
            record_type=record_type,
            host_timestamp_ns=host_ns,
            payload_length=payload_len,
            record_offset=cursor,
            payload_offset=payload_offset,
        )
        cursor = next_offset
        file_object.seek(cursor)


__all__ = [
    "SDB_FOOTER",
    "SDB_HEADER_CORE",
    "SDB_HEADER_RESERVED_SIZE",
    "SDB_MAGIC",
    "SDB_PROFILE_LENGTH",
    "SDB_RECORD_CONTROL_TX",
    "SDB_RECORD_GAP",
    "SDB_RECORD_METADATA",
    "SDB_RECORD_RX_CHUNK",
    "SDB_RECORD_SUMMARY",
    "SDB_SUPPORTED_VERSIONS",
    "SDB_V3_RECORD_HEADER",
    "SDB_VERSION_V2",
    "SDB_VERSION_V3",
    "SdbRecordEnvelope",
    "iter_record_envelopes",
    "read_exact",
]
