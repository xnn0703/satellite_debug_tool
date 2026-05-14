import numpy as np


class ChannelBuffer:
    # 默认 30000 样本 ≈ 5 分钟 @ 100Hz；旧值 2000 会在 100Hz 下 20 秒回绕导致曲线丢历史
    def __init__(self, name: str, capacity: int = 30000):
        self._name = name
        self._capacity = capacity
        self._times = np.zeros(capacity, dtype=np.float64)
        self._values = np.zeros(capacity, dtype=np.float32)
        self._head = 0
        self._count = 0

    def append(self, timestamp: float, value: float) -> None:
        self._times[self._head] = timestamp
        self._values[self._head] = value
        self._head = (self._head + 1) % self._capacity
        if self._count < self._capacity:
            self._count += 1

    def get_times(self) -> np.ndarray:
        if self._count == 0:
            return np.array([], dtype=np.float64)
        if self._count < self._capacity:
            return self._times[: self._count].copy()
        idx = self._head
        return np.concatenate([self._times[idx:], self._times[:idx]])

    def get_values(self) -> np.ndarray:
        if self._count == 0:
            return np.array([], dtype=np.float32)
        if self._count < self._capacity:
            return self._values[: self._count].copy()
        idx = self._head
        return np.concatenate([self._values[idx:], self._values[:idx]])

    def get_latest(self) -> tuple[float, float] | None:
        if self._count == 0:
            return None
        idx = (self._head - 1) % self._capacity
        return (self._times[idx], self._values[idx])

    @property
    def name(self) -> str:
        return self._name
