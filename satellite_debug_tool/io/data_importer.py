"""
.sdb / .csv 回放读取器（v2）。

.sdb 文件直接用字节流存 v2 协议帧。回放时复用 `FrameReceiverV2`：
依次 feed 文件内容，yield 出其中的 `DataReport` 帧（其它帧类型 M5 再细化）。

CSV 在 v2 下尚未重新定义列格式（原 v1 用通道名为列名已不适用），此处
先返回空迭代器并打印告警，完整实现留到 M5（异步录制 + SDB v2 重写）。
"""

from __future__ import annotations

import struct
import warnings
from typing import Iterator

from satellite_debug_tool.core.protocol import DataReport, FrameReceiverV2


SDB_MAGIC = b"SDB\x00"
SDB_FOOTER = b"\xee\xee\xee\xee"
SDB_HEADER_SIZE = 4 + 2 + 8 + 8   # magic + version + timestamp + reserved


class DataImporter:
    """读取历史录制数据。"""

    @staticmethod
    def read_sdb(filepath: str) -> Iterator[DataReport]:
        """
        读取 .sdb 文件，yield 其中的 `DataReport` 帧。

        文件格式：header(22B) + N 个 v2 协议帧（完整字节） + 4B footer(0xEEEEEEEE)。
        """
        with open(filepath, "rb") as f:
            head = f.read(SDB_HEADER_SIZE)
            if len(head) < SDB_HEADER_SIZE or head[:4] != SDB_MAGIC:
                raise ValueError("Invalid SDB file format (magic mismatch)")
            # header 解析暂仅做格式校验，protocol 字段留到 SDB v2 规范稳定后
            _version = struct.unpack("<H", head[4:6])[0]

            receiver = FrameReceiverV2()
            tail = bytearray()
            while True:
                chunk = f.read(4096)
                if not chunk:
                    break
                tail.extend(chunk)
                if len(tail) >= 4 and tail[-4:] == SDB_FOOTER:
                    # 末尾 4B 为 footer，喂 receiver 时剔除
                    for rec in receiver.feed(bytes(tail[:-4])):
                        if isinstance(rec, DataReport):
                            yield rec
                    tail.clear()
                    break
                # 保留最后 4B 防 footer 跨 chunk 边界
                if len(tail) > 4:
                    for rec in receiver.feed(bytes(tail[:-4])):
                        if isinstance(rec, DataReport):
                            yield rec
                    del tail[:-4]

            # 若文件没有写完整 footer（异常结束），把剩余喂完
            if tail:
                for rec in receiver.feed(bytes(tail)):
                    if isinstance(rec, DataReport):
                        yield rec

    @staticmethod
    def read_csv(filepath: str) -> Iterator[DataReport]:
        """CSV 导入暂未实现（等 SDB v2 + CSV 规范确定后在 M5 重写）。"""
        warnings.warn(
            "CSV import is not supported in protocol v2 yet; will be re-enabled in M5.",
            RuntimeWarning,
            stacklevel=2,
        )
        return iter(())
