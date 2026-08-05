"""
.sdb v2/v3 回放读取器。

对外提供两种使用风格：

1. `DataImporter.open_sdb(path)` → `SdbFile`
   含文件头解析结果（version/timestamp/profile）+ 两个生成器
   （`iter_records` 返回全部 v2 record；`iter_data_reports` 只返回 DataReport）

2. `DataImporter.read_sdb(path)` → `Iterator[DataReport]`（向后兼容的简化入口）

v1 文件（Magic 正常但 Version==1）会被 **拒绝**，要求用户先转换（暂无脚本）。
"""

from __future__ import annotations

import json
import struct
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Union

from satellite_debug_tool.core.protocol import (
    DataReport,
    FrameReceiverV2,
    FrameV2Record,
)


SDB_MAGIC = b"SDB\x00"
SDB_FOOTER = b"\xee\xee\xee\xee"

_HEADER_CORE = 4 + 2 + 8      # magic + version + timestamp
_HEADER_V1_TAIL = 8           # reserved 8
_HEADER_V2_PROFILE_LEN = 4    # 4B profile_len
_HEADER_V2_RESERVED = 8

_VERSION_V2 = 0x0002
_VERSION_V3 = 0x0003

_V3_RECORD_HEADER = struct.Struct("<BQI")
_V3_RX_CHUNK = 0x01
_V3_GAP = 0x02
_V3_CONTROL_TX = 0x03
_V3_METADATA = 0x04
_V3_SUMMARY = 0xFE


class SdbFormatError(ValueError):
    """.sdb 格式错误或版本不支持。"""


@dataclass
class SdbRawRecord:
    host_timestamp_ns: int
    data: bytes


@dataclass
class SdbControlRecord:
    host_timestamp_ns: int
    data: bytes


@dataclass
class SdbGapMarker:
    host_timestamp_ns: int
    dropped_chunks: int


@dataclass
class SdbFile:
    """SDB header, timed raw records, capture metadata and quality summary."""

    version: int
    timestamp: int
    profile: Optional[dict]
    metadata: dict
    quality: dict
    raw_records: tuple[SdbRawRecord, ...]
    control_records: tuple[SdbControlRecord, ...]
    gap_markers: tuple[SdbGapMarker, ...]
    metadata_events: tuple[dict, ...]
    _frame_bytes: bytes = b""

    def iter_timed_records(self) -> Iterator[tuple[int, FrameV2Record]]:
        receiver = FrameReceiverV2()
        if self.version == _VERSION_V2:
            for rec in receiver.feed(self._frame_bytes):
                yield 0, rec
            return
        for raw in self.raw_records:
            for rec in receiver.feed(raw.data):
                yield raw.host_timestamp_ns, rec

    def iter_records(self) -> Iterator[FrameV2Record]:
        """解析全部 v2 record（DataReport / StateReport / EventReport / …）。"""
        for _host_timestamp_ns, rec in self.iter_timed_records():
            yield rec

    def iter_data_reports(self) -> Iterator[DataReport]:
        """仅返回 DataReport（旧调用点兼容）。"""
        for rec in self.iter_records():
            if isinstance(rec, DataReport):
                yield rec


class DataImporter:
    """读取历史录制数据。"""

    @staticmethod
    def open_sdb(filepath: Union[str, Path]) -> SdbFile:
        """完整解析 .sdb v2/v3 文件。"""
        path = Path(filepath)
        raw = path.read_bytes()

        if len(raw) < _HEADER_CORE:
            raise SdbFormatError(f"file too short: {len(raw)}B")
        if raw[:4] != SDB_MAGIC:
            raise SdbFormatError("Invalid SDB file format (magic mismatch)")

        version = struct.unpack("<H", raw[4:6])[0]
        timestamp = struct.unpack("<Q", raw[6:14])[0]

        if version not in (_VERSION_V2, _VERSION_V3):
            raise SdbFormatError(
                f"Unsupported SDB version {version:#x}; v2 and v3 are accepted. "
                "Convert v1 files with sdb_v1_convert.py first."
            )

        off = _HEADER_CORE
        if off + _HEADER_V2_PROFILE_LEN > len(raw):
            raise SdbFormatError("truncated v2 header (profile_len missing)")
        profile_len = struct.unpack("<I", raw[off:off + 4])[0]
        off += 4

        header_object: Optional[dict] = None
        if profile_len > 0:
            if off + profile_len > len(raw):
                raise SdbFormatError("truncated header JSON")
            header_bytes = raw[off:off + profile_len]
            off += profile_len
            try:
                header_object = json.loads(header_bytes.decode("utf-8"))
            except Exception as exc:
                raise SdbFormatError(f"invalid header JSON: {exc}") from exc

        off += _HEADER_V2_RESERVED

        # 帧区：文件尾 4B 是 footer（若存在）
        frame_end = len(raw)
        if raw.endswith(SDB_FOOTER):
            frame_end -= len(SDB_FOOTER)
        footer_present = raw.endswith(SDB_FOOTER)
        if version == _VERSION_V2:
            frame_bytes = b"" if off > frame_end else raw[off:frame_end]
            return SdbFile(
                version=version,
                timestamp=timestamp,
                profile=header_object,
                metadata={},
                quality={"footer_present": footer_present, "complete": footer_present},
                raw_records=(),
                control_records=(),
                gap_markers=(),
                metadata_events=(),
                _frame_bytes=frame_bytes,
            )

        metadata = header_object if isinstance(header_object, dict) else {}
        profile = metadata.get("profile")
        if profile is not None and not isinstance(profile, dict):
            raise SdbFormatError("v3 profile must be an object or null")
        session = metadata.get("session")
        session_metadata = session if isinstance(session, dict) else {}
        raw_records: list[SdbRawRecord] = []
        controls: list[SdbControlRecord] = []
        gaps: list[SdbGapMarker] = []
        metadata_events: list[dict] = []
        summary: dict = {}
        cursor = off
        while cursor < frame_end:
            if cursor + _V3_RECORD_HEADER.size > frame_end:
                raise SdbFormatError("truncated v3 record header")
            record_type, host_ns, payload_len = _V3_RECORD_HEADER.unpack_from(raw, cursor)
            cursor += _V3_RECORD_HEADER.size
            if cursor + payload_len > frame_end:
                raise SdbFormatError("truncated v3 record payload")
            payload = raw[cursor:cursor + payload_len]
            cursor += payload_len
            if record_type == _V3_RX_CHUNK:
                raw_records.append(SdbRawRecord(host_ns, payload))
            elif record_type == _V3_CONTROL_TX:
                controls.append(SdbControlRecord(host_ns, payload))
            elif record_type == _V3_GAP:
                if len(payload) != 4:
                    raise SdbFormatError("invalid v3 gap marker")
                gaps.append(SdbGapMarker(host_ns, struct.unpack("<I", payload)[0]))
            elif record_type in (_V3_METADATA, _V3_SUMMARY):
                try:
                    decoded = json.loads(payload.decode("utf-8"))
                except Exception as exc:
                    raise SdbFormatError(f"invalid v3 JSON record: {exc}") from exc
                if not isinstance(decoded, dict):
                    raise SdbFormatError("v3 JSON record must contain an object")
                if record_type == _V3_SUMMARY:
                    summary = decoded
                else:
                    metadata_events.append(decoded)

        quality = dict(summary)
        quality["footer_present"] = footer_present
        quality["gap_markers"] = len(gaps)
        quality["gap_chunks"] = sum(marker.dropped_chunks for marker in gaps)
        quality["complete"] = bool(summary.get("complete", False) and footer_present)
        return SdbFile(
            version=version,
            timestamp=timestamp,
            profile=profile,
            metadata=session_metadata,
            quality=quality,
            raw_records=tuple(raw_records),
            control_records=tuple(controls),
            gap_markers=tuple(gaps),
            metadata_events=tuple(metadata_events),
        )

    # ---- 向后兼容入口 ----

    @staticmethod
    def read_sdb(filepath: Union[str, Path]) -> Iterator[DataReport]:
        """旧接口：只返回 DataReport 迭代器。"""
        return DataImporter.open_sdb(filepath).iter_data_reports()

    @staticmethod
    def read_csv(filepath: Union[str, Path]) -> Iterator[DataReport]:
        """CSV 导入暂未实现（SDB v2 落地后再补；列格式需 profile 驱动）。"""
        warnings.warn(
            "CSV import is not supported in protocol v2 yet.",
            RuntimeWarning,
            stacklevel=2,
        )
        return iter(())
