"""
异步 DataRecorder —— SDB v2 写入。

**关键改动（M5）**：
- write_frame() 只做 `queue.put`，不阻塞 UI 线程
- 后台 threading.Thread 负责写盘
- SDB v2 文件头内嵌 **profile JSON**，回放时可脱机恢复 UI（无需连设备）

文件格式 v2::

    Magic        4B "SDB\x00"
    Version      2B LE  (v2 = 0x0002)
    Timestamp    8B LE  unix seconds
    ProfileLen   4B LE  （0 表示无 profile，下同）
    ProfileJSON  <ProfileLen> 字节 UTF-8
    Reserved     8B
    FrameBytes   N × raw v2 帧字节（完整 envelope，不做额外分隔）
    Footer       4B 0xEEEEEEEE

**向后兼容**：主工具不再读取 v1（0x0001）；如有历史 .sdb 需要回放，提供
独立 `sdb_v1_convert.py`（后续任务，不在 M5 范围）。
"""

from __future__ import annotations

import json
import queue
import struct
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Union


SDB_MAGIC = b"SDB\x00"
SDB_VERSION_V2 = 0x0002
SDB_FOOTER = b"\xee\xee\xee\xee"

# 后台队列容量（每项一帧；10 KB/s × 10s ≈ 100k 帧上限的十分之一足矣）
_DEFAULT_QUEUE_SIZE = 10_000
# 后台线程 stop 时等待刷盘的最大秒数
_STOP_TIMEOUT_SEC = 3.0


class DataRecorder:
    """异步 .sdb v2 写盘。

    Args:
        filepath: 目标文件路径（.sdb）
        profile_dict: （可选）写入文件头的 profile JSON；`None` 表示无 profile
        queue_size: 后台队列容量（帧数）
    """

    def __init__(
        self,
        filepath: Union[str, Path],
        profile_dict: Optional[dict] = None,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._path = Path(filepath)
        self._profile = profile_dict
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=queue_size)
        self._thread: Optional[threading.Thread] = None
        self._fp = None
        self._is_recording = False
        self._dropped = 0
        self._written_frames = 0

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
        self._written_frames = 0
        self._thread = threading.Thread(
            target=self._run, name="SdbRecorder", daemon=True,
        )
        self._thread.start()
        return True

    def write_frame(self, frame_data: bytes) -> bool:
        """非阻塞入队；队满返回 False 并记丢帧。"""
        if not self._is_recording:
            return False
        try:
            self._queue.put_nowait(bytes(frame_data))
            return True
        except queue.Full:
            self._dropped += 1
            return False

    def stop(self) -> bool:
        if not self._is_recording:
            return False
        self._is_recording = False
        self._queue.put(None)   # 哨兵
        if self._thread is not None:
            self._thread.join(timeout=_STOP_TIMEOUT_SEC)
            self._thread = None
        try:
            if self._fp is not None:
                self._fp.write(SDB_FOOTER)
                self._fp.close()
        except OSError:
            return False
        finally:
            self._fp = None
        return True

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
    def filepath(self) -> Path:
        return self._path

    # ---- 内部 ----

    def _write_header(self) -> None:
        assert self._fp is not None
        profile_json = b""
        if self._profile is not None:
            profile_json = json.dumps(
                self._profile, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")

        self._fp.write(SDB_MAGIC)
        self._fp.write(struct.pack("<H", SDB_VERSION_V2))
        self._fp.write(struct.pack("<Q", int(datetime.now().timestamp())))
        self._fp.write(struct.pack("<I", len(profile_json)))
        if profile_json:
            self._fp.write(profile_json)
        self._fp.write(b"\x00" * 8)   # reserved

    def _run(self) -> None:
        """后台写盘循环；哨兵 None 触发退出。"""
        while True:
            item = self._queue.get()
            if item is None:
                break
            if self._fp is None:
                break
            try:
                self._fp.write(item)
                self._written_frames += 1
            except OSError:
                # 写失败：置位停止（避免死循环），让上层感知
                self._is_recording = False
                break
