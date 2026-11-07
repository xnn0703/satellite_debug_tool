"""逐设备跟踪判据；时间、事件和质量事实由同一纯领域对象维护。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class TrackingCriteriaPolicy:
    acquisition_stable_s: float = 5.0
    acquisition_timeout_s: float = 300.0
    interruption_max_s: float = 2.0
    interruptions_per_30min: int = 5
    interruption_ratio_max: float = 0.005
    recovery_stable_s: float = 3.0
    maximum_sample_gap_s: float = 2.0
    attitude_error_max_deg: float = 3.0

    def validate(self) -> None:
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a positive number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not isinstance(self.interruptions_per_30min, int):
            raise ValueError("interruptions_per_30min must be an integer")
        if self.interruption_ratio_max > 1:
            raise ValueError("interruption_ratio_max must be at most 1")
        if self.acquisition_stable_s > self.acquisition_timeout_s:
            raise ValueError("acquisition_stable_s must fit acquisition_timeout_s")

    @classmethod
    def from_mapping(cls, payload: Optional[Mapping[str, Any]] = None) -> "TrackingCriteriaPolicy":
        if payload is not None and not isinstance(payload, Mapping):
            raise ValueError("tracking_criteria must be an object")
        values = {} if payload is None else dict(payload)
        unknown = set(values) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError("unknown tracking criteria: " + ", ".join(sorted(unknown)))
        policy = cls(**values)
        policy.validate()
        return policy

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass(frozen=True)
class TrackingResult:
    verdict: str
    performance_verdict: str
    data_quality: str
    metrics: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    @property
    def incomplete(self) -> bool:
        return self.verdict == "INCOMPLETE"


@dataclass
class _Interruption:
    start_at: float
    end_at: Optional[float] = None
    duration_s: Optional[float] = None
    known_duration_s: float = 0.0
    longest_known_segment_s: float = 0.0
    confirmed: bool = False
    evidence_complete: bool = True


class TrackingEvaluator:
    """评价一个固定工况窗口；未知区间不产生推断的性能失败。"""

    def __init__(self, policy: TrackingCriteriaPolicy, duration_s: float,
                 acquisition_required: bool = True, *,
                 attitude_mode: bool = False) -> None:
        policy.validate()
        if isinstance(duration_s, bool) or not math.isfinite(duration_s) or duration_s <= 0:
            raise ValueError("duration_s must be finite and positive")
        if acquisition_required and duration_s <= policy.acquisition_timeout_s + policy.recovery_stable_s:
            raise ValueError("duration_s must exceed acquisition_timeout_s plus recovery_stable_s")
        self.policy = policy
        self.duration_s = float(duration_s)
        self.acquisition_required = bool(acquisition_required)
        self.attitude_mode = bool(attitude_mode)
        self.started_at: Optional[float] = None
        self.acquired_at: Optional[float] = None
        self.power_on_monotonic_s: Optional[float] = None
        self.gnss_fixed_at: Optional[float] = None
        self.modem_online_at: Optional[float] = None
        self.tracking_lock_at: Optional[float] = None
        self._tracking_lock_since: Optional[float] = None
        self._now: Optional[float] = None
        self._last_sample_at: Optional[float] = None
        self._last_valid_at: Optional[float] = None
        self._last_healthy = False
        self._healthy_since: Optional[float] = None
        self._acquisition_closed = False
        self._quality: list[str] = []
        self._failures: list[str] = []
        self._events: list[_Interruption] = []
        self._event: Optional[_Interruption] = None
        self._segment_start: Optional[float] = None
        self._segment_accounted_s = 0.0
        self._recovery_start: Optional[float] = None
        self._sample_count = 0
        self._valid_sample_count = 0
        self._maximum_observed_gap_s = 0.0
        self._attitude_max_deg = 0.0
        self._attitude_sum_deg = 0.0
        self._attitude_sample_count = 0
        self._result: Optional[TrackingResult] = None

    @property
    def acquired(self) -> bool:
        return self.acquired_at is not None

    @property
    def healthy(self) -> bool:
        return (self._last_healthy and self._last_valid_at is not None and self._now is not None
                and self._now - self._last_valid_at <= self.policy.maximum_sample_gap_s)

    @property
    def healthy_duration_s(self) -> float:
        """当前连续健康证据的首末样本间隔；墙钟空转不增加此值。"""
        if not self.healthy or self._healthy_since is None:
            return 0.0
        return self._last_valid_at - self._healthy_since

    @property
    def recovery_pending(self) -> bool:
        return self._event is not None

    @property
    def quality_reasons(self) -> tuple[str, ...]:
        return tuple(self._quality)

    @property
    def failure_reasons(self) -> tuple[str, ...]:
        return tuple(self._failures)

    @property
    def interruption_limit(self) -> int:
        return math.ceil(self.policy.interruptions_per_30min * self.duration_s / 1800.0)

    def start(self, started_at: float, power_on_monotonic_s: Optional[float] = None) -> None:
        if self.started_at is not None:
            raise RuntimeError("tracking evaluator has already started")
        self.started_at = self._finite_time(started_at)
        self._now = self.started_at
        if power_on_monotonic_s is not None:
            anchor = self._finite_time(power_on_monotonic_s)
            self.power_on_monotonic_s = min(anchor, self.started_at)
        else:
            self.power_on_monotonic_s = self.started_at
        if not self.acquisition_required:
            self.acquired_at = self.started_at

    def observe(self, received_at: float, phase_locked: bool, locked: bool,
                snr: float, fields_valid: bool, *,
                gnss_fixed: Optional[bool] = None,
                modem_online: Optional[bool] = None,
                attitude_error_deg: Optional[float] = None) -> None:
        self._require_started()
        if self._result is not None:
            return
        at = self._finite_time(received_at)
        if at < self.started_at:
            return
        if self._last_sample_at is not None and at <= self._last_sample_at:
            return  # 重复/迟到样本不推进连续证据。
        if at > self._deadline:
            self.tick(self._deadline)
            return
        self._check_gap(at)
        self._now = max(at, self._now)
        self._last_sample_at = at
        self._sample_count += 1
        if gnss_fixed and self.gnss_fixed_at is None:
            self.gnss_fixed_at = at
        if modem_online and self.modem_online_at is None:
            self.modem_online_at = at
        if self.attitude_mode:
            # 跟踪锁定是与判定无关的独立时间线：参考缺失与遮挡期间照常记录。
            tracking_locked = (
                fields_valid and phase_locked is True and locked is True
                and math.isfinite(snr) and snr > 0
            )
            if tracking_locked:
                if self._tracking_lock_since is None:
                    self._tracking_lock_since = at
                if (self.tracking_lock_at is None
                        and at - self._tracking_lock_since >= self.policy.acquisition_stable_s):
                    self.tracking_lock_at = at
            else:
                self._tracking_lock_since = None
            # 车载模式：姿态误差是唯一健康事实；SNR/锁定仅记录不判定，
            # 遮挡导致的 SNR 掉 0 不构成中断或失败。
            if attitude_error_deg is None or not math.isfinite(attitude_error_deg):
                self._mark_unknown("attitude_reference_missing")
                self._check_acquisition_timeout(at)
                return
            self._attitude_max_deg = max(self._attitude_max_deg, attitude_error_deg)
            self._attitude_sum_deg += attitude_error_deg
            self._attitude_sample_count += 1
            healthy = attitude_error_deg <= self.policy.attitude_error_max_deg
        else:
            if not fields_valid or not math.isfinite(snr):
                self._mark_unknown("invalid_tracking_sample")
                self._check_acquisition_timeout(at)
                return
            healthy = phase_locked is True and locked is True and snr > 0
        self._last_valid_at = at
        self._valid_sample_count += 1
        self._last_healthy = healthy
        if healthy:
            if self._healthy_since is None:
                self._healthy_since = at
        else:
            self._healthy_since = None
        if not self.acquired:
            if self._acquisition_closed:
                return
            if healthy:
                if self._healthy_since is None:
                    self._healthy_since = at
                if (at - self._healthy_since >= self.policy.acquisition_stable_s
                        and at <= self.started_at + self.policy.acquisition_timeout_s):
                    self.acquired_at = at
            else:
                self._healthy_since = None
            self._check_acquisition_timeout(at)
            return
        self._observe_tracking(at, healthy)

    def tick(self, now: float) -> None:
        self._require_started()
        if self._result is not None:
            return
        at = min(self._finite_time(now), self._deadline)
        if at < self.started_at:
            raise ValueError("tick must follow start")
        self._now = max(at, self._now)
        self._check_gap(at)
        self._check_acquisition_timeout(at)

    def finish(self, now: float) -> TrackingResult:
        self._require_started()
        if self._result is not None:
            return self._result
        if self._finite_time(now) < self._deadline:
            raise ValueError("tracking observation window is still running")
        self.tick(self._deadline)
        if not self._sample_count:
            self._mark_unknown("tracking_samples_missing")
        if not self._valid_sample_count:
            self._mark_unknown("valid_tracking_samples_missing")
        if self._last_valid_at is None or self._deadline - self._last_valid_at > self.policy.maximum_sample_gap_s:
            self._mark_unknown("observation_end_evidence_missing")
        complete = self.acquired and self._event is None
        performance = "FAIL" if self._failures else "PASS" if complete and not self._quality else "INCOMPLETE"
        quality = "INCOMPLETE" if self._quality else "COMPLETE"
        self._result = TrackingResult(performance, performance, quality, self.metrics())
        return self._result

    def metrics(self) -> dict[str, Any]:
        observation_s = (None if self.acquired_at is None else self._deadline - self.acquired_at)
        total = sum(event.known_duration_s for event in self._events)
        longest = max((event.longest_known_segment_s for event in self._events), default=0.0)
        anchor = self.power_on_monotonic_s if self.power_on_monotonic_s is not None else self.started_at
        return {
            "rule_version": 1,
            "policy": self.policy.to_payload(),
            "duration_s": self.duration_s,
            "acquisition_required": self.acquisition_required,
            "attitude_mode": self.attitude_mode,
            "power_on_monotonic_s": anchor,
            "healthy_duration_s": self.healthy_duration_s,
            "acquired_at": self.acquired_at,
            "acquisition_elapsed_s": (None if self.acquired_at is None else self.acquired_at - self.started_at),
            "lock_elapsed_s": (None if self.acquired_at is None else self.acquired_at - anchor),
            "attitude_aligned_elapsed_s": (
                None if not self.attitude_mode or self.acquired_at is None
                else self.acquired_at - anchor
            ),
            "tracking_lock_elapsed_s": (
                None if not self.attitude_mode or self.tracking_lock_at is None
                else self.tracking_lock_at - anchor
            ),
            "gnss_fixed_at": self.gnss_fixed_at,
            "gnss_lock_elapsed_s": (None if self.gnss_fixed_at is None else self.gnss_fixed_at - anchor),
            "modem_online_at": self.modem_online_at,
            "modem_online_elapsed_s": (None if self.modem_online_at is None else self.modem_online_at - anchor),
            "observation_duration_s": observation_s,
            "interruption_count": len(self._events),
            "interruption_limit": self.interruption_limit,
            "interruption_max_s": longest,
            "interruption_total_s": total,
            "interruption_ratio": None if not observation_s else total / observation_s,
            "recovery_pending": self.recovery_pending,
            "sample_count": self._sample_count,
            "valid_sample_count": self._valid_sample_count,
            "maximum_observed_gap_s": self._maximum_observed_gap_s,
            "attitude_error_max_deg": (None if not self._attitude_sample_count else self._attitude_max_deg),
            "attitude_error_mean_deg": (
                None if not self._attitude_sample_count
                else self._attitude_sum_deg / self._attitude_sample_count
            ),
            "attitude_sample_count": self._attitude_sample_count,
            "attitude_error_limit_deg": (
                self.policy.attitude_error_max_deg if self.attitude_mode else None
            ),
            "quality_reasons": list(self._quality),
            "failure_reasons": list(self._failures),
            "interruptions": [asdict(event) for event in self._events],
        }

    @property
    def _deadline(self) -> float:
        self._require_started()
        return self.started_at + self.duration_s

    @staticmethod
    def _finite_time(value: float) -> float:
        if isinstance(value, bool) or not math.isfinite(value):
            raise ValueError("tracking time must be finite")
        return float(value)

    def _require_started(self) -> None:
        if self.started_at is None:
            raise RuntimeError("tracking evaluator requires start")

    def _mark_failure(self, reason: str) -> None:
        if reason not in self._failures:
            self._failures.append(reason)

    def _mark_unknown(self, reason: str) -> None:
        if reason not in self._quality:
            self._quality.append(reason)
        self._healthy_since = None
        self._last_healthy = False
        self._recovery_start = None
        if self._event is not None:
            self._event.evidence_complete = False
            self._segment_start = None
            self._segment_accounted_s = 0.0

    def _check_gap(self, at: float) -> None:
        previous = self.started_at if self._last_sample_at is None else self._last_sample_at
        gap = max(0.0, at - previous)
        self._maximum_observed_gap_s = max(self._maximum_observed_gap_s, gap)
        if gap > self.policy.maximum_sample_gap_s:
            self._mark_unknown("observation_start_evidence_missing" if self._last_sample_at is None
                               else "tracking_sample_gap")

    def _check_acquisition_timeout(self, at: float) -> None:
        if self.acquired or self._acquisition_closed:
            return
        if at >= self.started_at + self.policy.acquisition_timeout_s:
            self._acquisition_closed = True
            if self._quality or not self._valid_sample_count:
                self._mark_unknown("acquisition_evidence_incomplete")
            else:
                self._mark_failure("acquisition_timeout")

    def _observe_tracking(self, at: float, healthy: bool) -> None:
        if self._event is None:
            if healthy:
                return
            self._event = _Interruption(start_at=at)
            self._events.append(self._event)
            self._segment_start = at
            self._segment_accounted_s = 0.0
            if len(self._events) > self.interruption_limit:
                self._mark_failure("interruption_count_exceeded")
        if self._segment_start is None:
            self._segment_start = at
        if healthy:
            if self._recovery_start is None:
                self._recovery_start = at
                self._extend_known_event(at)
            if at - self._recovery_start >= self.policy.recovery_stable_s:
                self._event.end_at = self._recovery_start
                self._event.confirmed = True
                if self._event.evidence_complete:
                    self._event.duration_s = self._event.end_at - self._event.start_at
                self._event = None
                self._segment_start = None
                self._segment_accounted_s = 0.0
                self._recovery_start = None
        else:
            self._recovery_start = None
            self._extend_known_event(at)

    def _extend_known_event(self, at: float) -> None:
        span = at - self._segment_start
        self._event.known_duration_s += max(0.0, span - self._segment_accounted_s)
        self._event.longest_known_segment_s = max(self._event.longest_known_segment_s, span)
        self._segment_accounted_s = span
        if span > self.policy.interruption_max_s:
            self._mark_failure("interruption_duration_exceeded")
        total = sum(event.known_duration_s for event in self._events)
        allowance = (self._deadline - self.acquired_at) * self.policy.interruption_ratio_max
        if total > allowance:
            self._mark_failure("interruption_ratio_exceeded")
