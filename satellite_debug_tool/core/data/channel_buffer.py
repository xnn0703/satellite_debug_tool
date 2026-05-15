"""ChannelBuffer：每个通道的时间序列样本缓冲。

两种模式：

- **环形（有界）**：`capacity = int`（默认 30000 ≈ 5 分钟 @ 100Hz）。预分配
  ndarray，append O(1)。适合实时模式（Live Tab），内存可控且写入快。
- **无界**：`capacity = None`。内部用 list 累加，`get_times/get_values`
  一次性 `np.asarray` 转出。适合回放 / log 解析（PlaybackView / LogView），
  数据一次性灌入后只读，不会无限增长。

M7 改造目的：让 PlaybackView 能完整保留几小时录制不被 30000 环回淹没，
同时 Live 路径行为不变（同样的默认 capacity = 30000）。
"""

from __future__ import annotations

from typing import Optional

import numpy as np


class ChannelBuffer:
    def __init__(self, name: str, capacity: Optional[int] = 30000):
        self._name = name
        self._capacity = capacity
        if capacity is None:
            # 无界模式：list 累加，转 ndarray 时缓存结果直到下次 append
            self._times_list: list[float] = []
            self._values_list: list[float] = []
            self._times_cache: Optional[np.ndarray] = None
            self._values_cache: Optional[np.ndarray] = None
        else:
            # 环形模式：预分配 ndarray
            self._times = np.zeros(capacity, dtype=np.float64)
            self._values = np.zeros(capacity, dtype=np.float32)
            self._head = 0
            self._count = 0

    def append(self, timestamp: float, value: float) -> None:
        if self._capacity is None:
            self._times_list.append(float(timestamp))
            self._values_list.append(float(value))
            # 失效缓存
            self._times_cache = None
            self._values_cache = None
            return
        self._times[self._head] = timestamp
        self._values[self._head] = value
        self._head = (self._head + 1) % self._capacity
        if self._count < self._capacity:
            self._count += 1

    def get_times(self) -> np.ndarray:
        if self._capacity is None:
            if self._times_cache is None:
                self._times_cache = np.asarray(self._times_list, dtype=np.float64)
            return self._times_cache
        if self._count == 0:
            return np.array([], dtype=np.float64)
        if self._count < self._capacity:
            return self._times[: self._count].copy()
        idx = self._head
        return np.concatenate([self._times[idx:], self._times[:idx]])

    def get_values(self) -> np.ndarray:
        if self._capacity is None:
            if self._values_cache is None:
                self._values_cache = np.asarray(self._values_list, dtype=np.float32)
            return self._values_cache
        if self._count == 0:
            return np.array([], dtype=np.float32)
        if self._count < self._capacity:
            return self._values[: self._count].copy()
        idx = self._head
        return np.concatenate([self._values[idx:], self._values[:idx]])

    def get_tail(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        """取最近 n 个 (times, values) 样本（M9 性能优化）。

        比 get_times() + get_values() 各拷贝整个 buffer 快得多 —— 实时
        chart 只需要当前视窗内的点（120s @ 100Hz ≈ 12000），不必每次
        把 30000 全拷贝出来传给 pyqtgraph.setData。

        n 大于实际数据点数时返回全部；返回的 ndarray dtype 与 get_times /
        get_values 一致 (float64 / float32)。
        """
        if n <= 0:
            return (np.array([], dtype=np.float64),
                    np.array([], dtype=np.float32))
        if self._capacity is None:
            total = len(self._times_list)
            n = min(n, total)
            if n == 0:
                return (np.array([], dtype=np.float64),
                        np.array([], dtype=np.float32))
            return (
                np.asarray(self._times_list[-n:], dtype=np.float64),
                np.asarray(self._values_list[-n:], dtype=np.float32),
            )

        # 环形 buffer
        n = min(n, self._count)
        if n == 0:
            return (np.array([], dtype=np.float64),
                    np.array([], dtype=np.float32))
        if self._count < self._capacity:
            # 未满：数据线性占据 [0, _count)，最新 n 个 = [_count - n, _count)
            start = self._count - n
            return (
                self._times[start:self._count].copy(),
                self._values[start:self._count].copy(),
            )
        # 已满：最新 n 个起点 = (_head - n) mod _capacity（_head 指向下次写入位置）
        start = (self._head - n) % self._capacity
        end = start + n
        if end <= self._capacity:
            return (
                self._times[start:end].copy(),
                self._values[start:end].copy(),
            )
        # 跨边界：拼两段
        first_n = self._capacity - start
        wrap_n = n - first_n
        return (
            np.concatenate([self._times[start:], self._times[:wrap_n]]),
            np.concatenate([self._values[start:], self._values[:wrap_n]]),
        )

    def get_latest(self) -> tuple[float, float] | None:
        if self._capacity is None:
            if not self._values_list:
                return None
            return (self._times_list[-1], self._values_list[-1])
        if self._count == 0:
            return None
        idx = (self._head - 1) % self._capacity
        return (self._times[idx], self._values[idx])

    def __len__(self) -> int:
        if self._capacity is None:
            return len(self._values_list)
        return self._count

    @property
    def name(self) -> str:
        return self._name

    @property
    def capacity(self) -> Optional[int]:
        return self._capacity
