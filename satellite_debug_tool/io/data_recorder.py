"""
异步 DataRecorder —— SDB v2/v3 写入。

**关键改动（M5）**：
- write_frame() 只做 `queue.put`，不阻塞 UI 线程
- 后台 threading.Thread 负责写盘
- SDB v2 文件头内嵌 **profile JSON**，回放时可脱机恢复 UI（无需连设备）

SDB v2 保持原始连续帧区格式。SDB v3 使用同一基础文件头，但 JSON 区升级为
会话 metadata，并把每次接收写成带主机纳秒时间戳的 record::

    Magic        4B "SDB\x00"
    Version      2B LE  (v2 = 0x0002, v3 = 0x0003)
    Timestamp    8B LE  unix seconds
    JsonLen      4B LE
    JSON         v2=Profile; v3={profile, session, schema_version}
    Reserved     8B
    v2 FrameBytes: N × raw v2 frame bytes
    v3 Record: type u8 + host_time_ns u64 + payload_len u32 + payload
    Footer       4B 0xEEEEEEEE

**向后兼容**：主工具不再读取 v1（0x0001）；如有历史 .sdb 需要回放，提供
独立 `sdb_v1_convert.py`（后续任务，不在 M5 范围）。
"""

from __future__ import annotations

import json
import queue
import struct
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Union


SDB_MAGIC = b"SDB\x00"
SDB_VERSION_V2 = 0x0002
SDB_VERSION_V3 = 0x0003
SDB_FOOTER = b"\xee\xee\xee\xee"

SDB_RECORD_RX_CHUNK = 0x01
SDB_RECORD_GAP = 0x02
SDB_RECORD_CONTROL_TX = 0x03
SDB_RECORD_METADATA = 0x04
SDB_RECORD_SUMMARY = 0xFE
SDB_V3_RECORD_HEADER = struct.Struct("<BQI")

# 后台队列容量（每项一帧；10 KB/s × 10s ≈ 100k 帧上限的十分之一足矣）
_DEFAULT_QUEUE_SIZE = 10_000
# 后台线程 stop 时等待刷盘的最大秒数
_STOP_TIMEOUT_SEC = 3.0


class DataRecorder:
    """异步 .sdb v2/v3 写盘。

    Args:
        filepath: 目标文件路径（.sdb）
        profile_dict: （可选）写入文件头的 profile JSON；`None` 表示无 profile
        queue_size: 后台队列容量（帧数）
        format_version: 默认保持 v2；客户全量录制显式使用 v3
        metadata: v3 会话元数据
    """

    def __init__(
        self,
        filepath: Union[str, Path],
        profile_dict: Optional[dict] = None,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
        format_version: int = SDB_VERSION_V2,
        metadata: Optional[dict] = None,
    ) -> None:
        if format_version not in (SDB_VERSION_V2, SDB_VERSION_V3):
            raise ValueError(f"unsupported SDB format version: {format_version}")
        self._path = Path(filepath)
        self._profile = profile_dict
        self._format_version = int(format_version)
        self._metadata = dict(metadata or {})
        self._queue: "queue.Queue[Optional[tuple[int, int, bytes]]]" = queue.Queue(
            maxsize=queue_size
        )
        self._thread: Optional[threading.Thread] = None
        self._fp = None
        self._is_recording = False
        self._dropped = 0
        self._pending_gap = 0
        self._written_frames = 0
        self._written_records = 0
        self._write_failed = False
        self._finalize_ok = False

    # ---- 生命周期 ----

    def start(self) -> bool:
        if self._is_recording:
            return True
        try:
            self._fp = open(self._path, "wb")
            self._write_header()
        except OSError:
            self._fp = None
            return False
        self._is_recording = True
        self._dropped = 0
        self._pending_gap = 0
        self._written_frames = 0
        self._written_records = 0
        self._write_failed = False
        self._finalize_ok = False
        self._thread = threading.Thread(
            target=self._run, name="SdbRecorder", daemon=True,
        )
        self._thread.start()
        return True

    def write_frame(
        self, frame_data: bytes, *, host_timestamp_ns: Optional[int] = None
    ) -> bool:
        """非阻塞入队；队满返回 False 并记丢帧。"""
        if not self._is_recording:
            return False
        timestamp_ns = time.time_ns() if host_timestamp_ns is None else int(host_timestamp_ns)
        if self._format_version == SDB_VERSION_V3 and self._pending_gap:
            gap_payload = struct.pack("<I", self._pending_gap)
            try:
                self._queue.put_nowait((SDB_RECORD_GAP, timestamp_ns, gap_payload))
                self._pending_gap = 0
            except queue.Full:
                self._dropped += 1
                self._pending_gap += 1
                return False
        try:
            self._queue.put_nowait(
                (SDB_RECORD_RX_CHUNK, timestamp_ns, bytes(frame_data))
            )
            return True
        except queue.Full:
            self._dropped += 1
            self._pending_gap += 1
            return False

    def write_control_frame(
        self, frame_data: bytes, *, host_timestamp_ns: Optional[int] = None
    ) -> bool:
        """Record an outgoing customer control without replaying it as device data."""
        if not self._is_recording or self._format_version != SDB_VERSION_V3:
            return False
        timestamp_ns = time.time_ns() if host_timestamp_ns is None else int(host_timestamp_ns)
        try:
            self._queue.put_nowait(
                (SDB_RECORD_CONTROL_TX, timestamp_ns, bytes(frame_data))
            )
            return True
        except queue.Full:
            self._dropped += 1
            self._pending_gap += 1
            return False

    def write_metadata_event(
        self, metadata: dict, *, host_timestamp_ns: Optional[int] = None
    ) -> bool:
        if not self._is_recording or self._format_version != SDB_VERSION_V3:
            return False
        timestamp_ns = time.time_ns() if host_timestamp_ns is None else int(host_timestamp_ns)
        payload = json.dumps(
            metadata, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        try:
            self._queue.put_nowait((SDB_RECORD_METADATA, timestamp_ns, payload))
            return True
        except queue.Full:
            self._dropped += 1
            self._pending_gap += 1
            return False

    def stop(self) -> bool:
        if not self._is_recording:
            return False
        self._is_recording = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # Stop must never wait behind a full queue on the UI thread. Drop
            # one pending record, account for it, and place the final sentinel.
            try:
                dropped = self._queue.get_nowait()
            except queue.Empty:
                dropped = None
            if dropped is not None:
                self._dropped += 1
                self._pending_gap += 1
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                self._write_failed = True
                return False
        if self._thread is not None:
            self._thread.join(timeout=_STOP_TIMEOUT_SEC)
            if self._thread.is_alive():
                return False
            self._thread = None
        return self._finalize_ok

    # ---- 查询 ----

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def dropped_count(self) -> int:
        return self._dropped

    @property
    def written_count(self) -> int:
        return self._written_frames

    @property
    def written_record_count(self) -> int:
        return self._written_records

    @property
    def format_version(self) -> int:
        return self._format_version

    @property
    def filepath(self) -> Path:
        return self._path

    # ---- 内部 ----

    def _write_header(self) -> None:
        assert self._fp is not None
        header_json = b""
        if self._format_version == SDB_VERSION_V2:
            header_object = self._profile
        else:
            header_object = {
                "schema_version": 1,
                "profile": self._profile,
                "session": self._metadata,
            }
        if header_object is not None:
            header_json = json.dumps(
                header_object, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")

        self._fp.write(SDB_MAGIC)
        self._fp.write(struct.pack("<H", self._format_version))
        self._fp.write(struct.pack("<Q", int(datetime.now().timestamp())))
        self._fp.write(struct.pack("<I", len(header_json)))
        if header_json:
            self._fp.write(header_json)
        self._fp.write(b"\x00" * 8)   # reserved

    def _write_v3_record(
        self, record_type: int, host_timestamp_ns: int, payload: bytes
    ) -> None:
        assert self._fp is not None
        self._fp.write(
            SDB_V3_RECORD_HEADER.pack(
                int(record_type) & 0xFF,
                int(host_timestamp_ns) & 0xFFFFFFFFFFFFFFFF,
                len(payload),
            )
        )
        self._fp.write(payload)

    def _run(self) -> None:
        """后台写盘循环；哨兵 None 触发退出。"""
        try:
            while True:
                queued = self._queue.get()
                if queued is None:
                    break
                if self._fp is None:
                    break
                record_type, timestamp_ns, payload = queued
                if self._format_version == SDB_VERSION_V2:
                    self._fp.write(payload)
                else:
                    self._write_v3_record(record_type, timestamp_ns, payload)
                self._written_records += 1
                if record_type == SDB_RECORD_RX_CHUNK:
                    self._written_frames += 1
        except OSError:
            self._is_recording = False
            self._write_failed = True
        finally:
            self._finalize_file()

    def _finalize_file(self) -> None:
        try:
            if self._fp is None:
                return
            if self._format_version == SDB_VERSION_V3:
                summary = json.dumps(
                    {
                        "schema_version": 1,
                        "complete": (
                            not self._write_failed
                            and self._dropped == 0
                            and self._pending_gap == 0
                        ),
                        "rx_chunks": self._written_frames,
                        "records": self._written_records,
                        "dropped_chunks": self._dropped,
                        "pending_gap_chunks": self._pending_gap,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self._write_v3_record(SDB_RECORD_SUMMARY, time.time_ns(), summary)
            self._fp.write(SDB_FOOTER)
            self._fp.close()
            self._finalize_ok = not self._write_failed
        except OSError:
            self._write_failed = True
            self._finalize_ok = False
            try:
                if self._fp is not None:
                    self._fp.close()
            except OSError:
                pass
        finally:
            self._fp = None
