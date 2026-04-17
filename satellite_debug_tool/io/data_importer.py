"""
.sdb v2 回放读取器。

对外提供两种使用风格：

1. `DataImporter.open_sdb(path)` → `SdbFile`
   含文件头解析结果（version/timestamp/profile）+ 两个生成器
   （`iter_records` 返回全部 v2 record；`iter_data_reports` 只返回 DataReport）

2. `DataImporter.read_sdb(path)` → `Iterator[DataReport]`（向后兼容的简化入口）

v1 文件（Magic 正常但 Version==1）会被 **拒绝**，要求用户先转换（暂无脚本）。
"""

from __future__ import annotations

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


class SdbFormatError(ValueError):
    """.sdb 格式错误或版本不支持。"""


@dataclass
class SdbFile:
    """.sdb v2 文件句柄（文件头 + 帧区字节切片）。"""

    version: int
    timestamp: int
    profile: Optional[dict]
    _frame_bytes: bytes

    def iter_records(self) -> Iterator[FrameV2Record]:
        """解析全部 v2 record（DataReport / StateReport / EventReport / …）。"""
        receiver = FrameReceiverV2()
        for rec in receiver.feed(self._frame_bytes):
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
        """完整解析 .sdb v2 文件（header + 帧区）。"""
        path = Path(filepath)
        raw = path.read_bytes()

        if len(raw) < _HEADER_CORE:
            raise SdbFormatError(f"file too short: {len(raw)}B")
        if raw[:4] != SDB_MAGIC:
            raise SdbFormatError("Invalid SDB file format (magic mismatch)")

        version = struct.unpack("<H", raw[4:6])[0]
        timestamp = struct.unpack("<Q", raw[6:14])[0]

        if version != _VERSION_V2:
            raise SdbFormatError(
                f"Unsupported SDB version {version:#x}; only v2 (0x0002) is accepted. "
                "Convert v1 files with sdb_v1_convert.py first."
            )

        off = _HEADER_CORE
        if off + _HEADER_V2_PROFILE_LEN > len(raw):
            raise SdbFormatError("truncated v2 header (profile_len missing)")
        profile_len = struct.unpack("<I", raw[off:off + 4])[0]
        off += 4

        profile: Optional[dict] = None
        if profile_len > 0:
            if off + profile_len > len(raw):
                raise SdbFormatError("truncated profile JSON")
            profile_bytes = raw[off:off + profile_len]
            off += profile_len
            try:
                import json
                profile = json.loads(profile_bytes.decode("utf-8"))
            except Exception as exc:
                raise SdbFormatError(f"invalid profile JSON: {exc}") from exc

        off += _HEADER_V2_RESERVED

        # 帧区：文件尾 4B 是 footer（若存在）
        frame_end = len(raw)
        if raw.endswith(SDB_FOOTER):
            frame_end -= len(SDB_FOOTER)
        if off > frame_end:
            frame_bytes = b""
        else:
            frame_bytes = raw[off:frame_end]

        return SdbFile(
            version=version,
            timestamp=timestamp,
            profile=profile,
            _frame_bytes=frame_bytes,
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
