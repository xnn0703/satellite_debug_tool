"""M45 有界接收环：一条 shell 连接内 worker 与 UI 帧消费之间的字节通道。

单生产者（worker 读循环）、单消费者（UI flush 帧）。入队、排空、
通知边沿与清屏代际在同一把锁内协调：worker 仅在环进入"待观察"态的
翻转时刻发出一次 rx_pending，UI 按帧排空；容量满时生产者按有界等待
片段让出，由调用方在片段间处理命令与停止。残余（push_final）只接收
停止时当前读取块尚未入环的后缀，移交后生产结束。
"""
from __future__ import annotations

import enum
import threading
from typing import NamedTuple

from .transport import MAX_READ_CHUNK

DEFAULT_CAPACITY_BYTES = 512 * 1024
DEFAULT_WAIT_SEGMENT_S = 0.02


class PushStatus(str, enum.Enum):
    """push 单次调用的直接结果，只陈述本次接纳与等待事实。"""

    ACCEPTED = "accepted"  # 本次数据全部接纳
    PARTIAL = "partial"  # 容量接纳前缀，剩余由调用方继续投递
    RETRY = "retry"  # 等满一个片段仍无容量；调用方处理命令后重试
    STOPPED = "stopped"  # wake_stop 已置位；调用方移交残余并收尾


class PushResult(NamedTuple):
    accepted: int
    status: PushStatus
    notify: bool  # 本次操作使环进入待观察态（数据或代际），仅此时 worker 发 rx_pending


class RxRing:
    """有界接收通道；每条 shell 连接独占一个实例，由该连接的 worker 持有。"""

    def __init__(
        self,
        *,
        capacity_bytes: int = DEFAULT_CAPACITY_BYTES,
        wait_segment_s: float = DEFAULT_WAIT_SEGMENT_S,
    ) -> None:
        if capacity_bytes < 1:
            raise ValueError("rx ring capacity must be positive")
        if wait_segment_s <= 0:
            raise ValueError("rx ring wait segment must be positive")
        self.capacity_bytes = int(capacity_bytes)
        self._wait_segment_s = float(wait_segment_s)
        self._condition = threading.Condition()
        self._segments: list[bytes] = []
        self._queued = 0
        self._residual = b""
        self._flush_owed = False  # UI 欠一次 flush 帧观察（数据或代际）
        self._stopped = False
        self._finalized = False
        self._epoch = 0

    # -- producer（worker 线程）--------------------------------------------------

    def push(self, data: bytes) -> PushResult:
        """接纳 data 前缀；返回 (已接纳字节数, 状态, 是否产生通知边沿)。

        容量已满时最多等待一个片段：被排空释放的空间或 wake_stop 唤醒；
        等满片段仍无容量返回 RETRY，调用方在片段间处理命令与停止。
        """

        with self._condition:
            if self._finalized:
                raise RuntimeError("rx ring production is finalized")
            if self._stopped:
                return PushResult(0, PushStatus.STOPPED, False)
            accepted = self._accept_locked(data)
            if not accepted:
                self._condition.wait(self._wait_segment_s)
                if self._stopped:
                    return PushResult(0, PushStatus.STOPPED, False)
                accepted = self._accept_locked(data)
                if not accepted:
                    return PushResult(0, PushStatus.RETRY, False)
            status = PushStatus.ACCEPTED if accepted == len(data) else PushStatus.PARTIAL
            notified = not self._flush_owed
            self._flush_owed = True
            return PushResult(accepted, status, notified)

    def push_final(self, data: bytes) -> bool:
        """停止路径的已读残余移交：非阻塞、不占容量预算；返回是否需要通知。

        只接收当前读取块尚未入环的后缀（≤ MAX_READ_CHUNK）；移交后生产结束，
        后续 push/push_final 抛错，clear 不再推进代际。
        """

        if len(data) > MAX_READ_CHUNK:
            raise ValueError("residual exceeds one read chunk")
        with self._condition:
            if self._finalized:
                raise RuntimeError("rx ring production is finalized")
            notified = False
            if data or self._segments:
                notified = not self._flush_owed
                self._flush_owed = True
                self._residual += data
            self._finalized = True
            return notified

    def clear(self) -> bool:
        """清屏代际：丢弃已入环字节并推进代际；返回是否需要通知 UI 观察。

        worker 手中已读未入环的后缀由调用方丢弃，不经过本方法。
        """

        with self._condition:
            if self._finalized:
                return False
            self._segments.clear()
            self._queued = 0
            self._epoch += 1
            notified = not self._flush_owed
            self._flush_owed = True
            return notified

    def wake_stop(self) -> None:
        """停止唤醒：打断容量等待；生产者随后以 push_final 收尾。"""

        with self._condition:
            self._stopped = True
            self._condition.notify_all()

    # -- consumer（UI 线程）------------------------------------------------------

    def drain(self, max_bytes: int) -> tuple[bytes, int]:
        """按预算取出字节；返回 (数据, 数据所属清屏代际)。排空后通知边沿复位。"""

        if max_bytes < 0:
            raise ValueError("drain budget must not be negative")
        with self._condition:
            chunks: list[bytes] = []
            remaining = int(max_bytes)
            while remaining > 0 and self._segments:
                segment = self._segments[0]
                if len(segment) <= remaining:
                    chunks.append(self._segments.pop(0))
                    remaining -= len(segment)
                    self._queued -= len(segment)
                else:
                    chunks.append(segment[:remaining])
                    self._segments[0] = segment[remaining:]
                    self._queued -= remaining
                    remaining = 0
            if remaining > 0 and self._residual:
                taken = self._residual[:remaining]
                self._residual = self._residual[len(taken) :]
                chunks.append(taken)
            if not self._segments and not self._residual:
                self._flush_owed = False
            self._condition.notify_all()
            return b"".join(chunks), self._epoch

    @property
    def backlog_bytes(self) -> int:
        with self._condition:
            return self._queued + len(self._residual)

    @property
    def wait_segment_s(self) -> float:
        return self._wait_segment_s

    @property
    def epoch(self) -> int:
        with self._condition:
            return self._epoch

    # -- internals -----------------------------------------------------------------

    def _accept_locked(self, data: bytes) -> int:
        space = self.capacity_bytes - self._queued
        accepted = min(len(data), max(0, space))
        if accepted:
            self._segments.append(data[:accepted])
            self._queued += accepted
        return accepted


__all__ = [
    "DEFAULT_CAPACITY_BYTES",
    "DEFAULT_WAIT_SEGMENT_S",
    "PushResult",
    "PushStatus",
    "RxRing",
]
