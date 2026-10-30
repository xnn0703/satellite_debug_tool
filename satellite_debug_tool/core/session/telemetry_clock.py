"""设备采集时间段的单一准入 owner；协议没有 boot ID，时间段不代表物理重启证明。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from satellite_debug_tool.core.protocol import (
    DataReport, Heartbeat, ServiceFastState, ServiceComponentHealth,
)
from satellite_debug_tool.core.product.timestamps import U32_HALF_RANGE, U32_MODULUS


@dataclass(frozen=True)
class TimedRecord:
    record: object
    received: float


@dataclass(frozen=True)
class ClockAdmission:
    records: tuple[TimedRecord, ...] = ()
    starts_epoch: bool = False


class TelemetryClock:
    """同一流三个严格推进的反向样本确认新段；单个旧包和重复包无法确认。

    候选最多保留 256 帧、5 秒。新段确认后，以主机经过时间加 2 秒传输
    抖动约束前向跨度，隔离旧段迟到包。u32 自然回绕按模数连续展开。
    此合同依据连续采集证据，连续重放的旧流需由协议 boot ID 才能区分。
    """

    _CONFIRMING_STREAMS = (DataReport, Heartbeat, ServiceFastState, ServiceComponentHealth)
    _JITTER_MS = 2000

    def __init__(self, *, host_time_available: bool = True) -> None:
        self._host_time_available = host_time_available
        self.reset()

    def reset(self) -> None:
        self._anchors: dict[type, int] = {}
        self._anchor_hosts: dict[type, float] = {}
        self._pending: list[TimedRecord] = []
        self._progress: dict[type, tuple[int, int]] = {}
        self._fence: Optional[tuple[int, float]] = None

    @staticmethod
    def timestamp(record: object) -> Optional[int]:
        value = record.uptime_ms if isinstance(record, Heartbeat) else getattr(record, "timestamp", None)
        return None if value is None else int(value) & (U32_MODULUS - 1)

    def admit(self, record: object, received: float) -> ClockAdmission:
        item = TimedRecord(record, received)
        raw = self.timestamp(record)
        if raw is None:
            return ClockAdmission((item,))
        kind = type(record)
        if self._pending and (received - self._pending[0].received > 5.0 or len(self._pending) >= 256):
            self._pending.clear()
            self._progress.clear()
        if self._fence is not None and self._host_time_available:
            start, host = self._fence
            delta = (raw - start) & (U32_MODULUS - 1)
            expected_span = max(0.0, received - host) * 1000 + self._JITTER_MS
            forward_jump = delta < U32_HALF_RANGE and delta > expected_span
            high_old_segment = delta >= U32_HALF_RANGE and raw > start
            if forward_jump or high_old_segment:
                return ClockAdmission()
        previous = self._anchors.get(kind)
        backward = False
        if previous is not None:
            delta = (raw - previous) % U32_MODULUS
            elapsed = max(0.0, received - self._anchor_hosts[kind]) * 1000
            # 回绕必须符合主机经过时间；长期运行后的归零也能建立候选。
            wrap_restart = (self._host_time_available and raw < previous
                            and delta < U32_HALF_RANGE and delta > elapsed + self._JITTER_MS)
            backward = delta >= U32_HALF_RANGE or wrap_restart
        if backward:
            self._pending.append(item)
            if isinstance(record, self._CONFIRMING_STREAMS):
                last, count = self._progress.get(kind, (raw, 0))
                step = (raw - last) % U32_MODULUS
                count = count + 1 if 0 < step < U32_HALF_RANGE else (count if step == 0 else 1)
                self._progress[kind] = (raw, max(1, count))
                if count >= 3:
                    # 只回放确认流候选起点之后、属于新时间范围的样本。
                    pending = tuple(self._pending)
                    self.reset()
                    self._fence = (raw, received)
                    accepted: list[TimedRecord] = []
                    for candidate in pending:
                        stamp = self.timestamp(candidate.record)
                        if stamp is None or (raw - stamp) % U32_MODULUS >= U32_HALF_RANGE:
                            continue
                        prior = self._anchors.get(type(candidate.record))
                        if prior is None or (stamp - prior) % U32_MODULUS < U32_HALF_RANGE:
                            self._anchors[type(candidate.record)] = stamp
                            self._anchor_hosts[type(candidate.record)] = candidate.received
                            accepted.append(candidate)
                    return ClockAdmission(tuple(accepted), True)
            return ClockAdmission()
        # 当前流继续前进，撤销该流的迟到候选；其他流保留独立证据。
        self._progress.pop(kind, None)
        self._pending = [entry for entry in self._pending if type(entry.record) is not kind]
        self._anchors[kind] = raw
        self._anchor_hosts[kind] = received
        if self._fence is not None:
            start, _host = self._fence
            if 0 < (raw - start) % U32_MODULUS < U32_HALF_RANGE:
                self._fence = (raw, received)
        return ClockAdmission((item,))
