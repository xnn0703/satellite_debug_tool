"""运动平台轨迹预检和单调时钟调度。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from fractions import Fraction
import hashlib
import json
import math
import multiprocessing
import queue
import sys
import threading
import time
from typing import Callable

from .controller import DirectMotionPlatformController
from .lingjing_protocol import build_position_frame, solve_actuator_targets
from .models import MotionActionResult, MotionPlatformError, PlatformPose


# macOS 20 ms 已完成现场验证；Windows 使用供应商上位机验证过的 50 ms。
MACOS_CONTINUOUS_TRAJECTORY_PERIOD_MS = 20
WINDOWS_CONTINUOUS_TRAJECTORY_PERIOD_MS = 50


def continuous_trajectory_period_ms(platform_name: str | None = None) -> int:
    """返回当前打包平台唯一的连续轨迹命令周期。"""

    resolved = sys.platform if platform_name is None else str(platform_name)
    if resolved == "win32":
        return WINDOWS_CONTINUOUS_TRAJECTORY_PERIOD_MS
    return MACOS_CONTINUOUS_TRAJECTORY_PERIOD_MS


CONTINUOUS_TRAJECTORY_PERIOD_MS = continuous_trajectory_period_ms()


class SoftCenterPhase(str, Enum):
    FADING = "fading"
    CENTERING = "centering"
    CENTER_CONFIRMED = "center_confirmed"
    CENTER_FAILED = "center_failed"


@dataclass(frozen=True)
class SineAxis:
    amplitude_deg: float
    frequency_hz: float
    phase_deg: float = 0.0
    enabled: bool = True

    def validate(self) -> None:
        if not all(math.isfinite(value) for value in (self.amplitude_deg, self.frequency_hz, self.phase_deg)):
            raise MotionPlatformError("sine axis values must be finite")
        if self.enabled and (self.amplitude_deg <= 0 or self.frequency_hz <= 0):
            raise MotionPlatformError("enabled sine axis requires positive amplitude and frequency")


@dataclass(frozen=True)
class SineTrajectory:
    ramp_in_s: float
    steady_duration_s: float | None
    ramp_out_s: float
    roll: SineAxis
    pitch: SineAxis
    yaw: SineAxis
    work_z_mm: float = 100.0
    profile_id: str = "trajectory"

    @property
    def sample_period_ms(self) -> int:
        return CONTINUOUS_TRAJECTORY_PERIOD_MS

    @property
    def total_duration_s(self) -> float | None:
        if self.steady_duration_s is None:
            return None
        return self.ramp_in_s + self.steady_duration_s + self.ramp_out_s

    @property
    def sample_count(self) -> int | None:
        duration = self.total_duration_s
        return None if duration is None else int(math.ceil(duration * 1000.0 / self.sample_period_ms))

    def validate(self) -> None:
        if (not all(math.isfinite(value) and value >= 0 for value in (self.ramp_in_s, self.ramp_out_s))
                or (self.steady_duration_s is not None
                    and (not math.isfinite(self.steady_duration_s) or self.steady_duration_s <= 0))):
            raise MotionPlatformError("trajectory durations are invalid")
        if self.steady_duration_s is None and self.ramp_out_s != 0:
            raise MotionPlatformError("continuous trajectory finishes through controlled stop and center")
        for axis in (self.roll, self.pitch, self.yaw):
            axis.validate()
        if not any(axis.enabled for axis in (self.roll, self.pitch, self.yaw)):
            raise MotionPlatformError("trajectory requires an enabled axis")

    def pose_at(self, elapsed_s: float) -> PlatformPose:
        elapsed = max(float(elapsed_s), 0.0)
        duration = self.total_duration_s
        if duration is not None:
            elapsed = min(elapsed, duration)
        envelope = _trajectory_envelope(self, elapsed)
        return PlatformPose(
            _axis_value(self.roll, elapsed, envelope),
            _axis_value(self.pitch, elapsed, envelope),
            _axis_value(self.yaw, elapsed, envelope),
            z_mm=self.work_z_mm,
        )


@dataclass(frozen=True)
class TrajectoryResult:
    intended_points: int | None
    sent_points: int
    skipped_points: int
    maximum_lateness_ms: float
    stopped: bool
    arrival: MotionActionResult | None
    maximum_send_interval_ms: float = 0.0
    p95_send_interval_ms: float = 0.0
    p99_send_interval_ms: float = 0.0
    control_gap_count: int = 0
    timing_fault: bool = False
    command_duration_ms: int = 0
    soft_center_requested: bool = False
    fade_points: int = 0
    center_confirmed: bool = False


@dataclass(frozen=True)
class CompiledTrajectoryPoint:
    """已完成运动学和协议编码的单个轨迹点。"""

    elapsed_s: float
    pose: PlatformPose
    target_pulses: tuple[int, ...]
    frame: bytes


@dataclass(frozen=True)
class CompiledTrajectory:
    """与轨迹及平台配置摘要绑定的不可变发送程序。"""

    fingerprint: str
    command_duration_ms: int
    finite_points: tuple[CompiledTrajectoryPoint, ...]
    prefix_points: tuple[CompiledTrajectoryPoint, ...] = ()
    periodic_points: tuple[CompiledTrajectoryPoint, ...] = ()

    def point_at(self, index: int) -> CompiledTrajectoryPoint:
        if index < 0:
            raise IndexError(index)
        if self.finite_points:
            return self.finite_points[index]
        if index < len(self.prefix_points):
            return self.prefix_points[index]
        if not self.periodic_points:
            raise IndexError(index)
        return self.periodic_points[
            (index - len(self.prefix_points)) % len(self.periodic_points)
        ]


@dataclass(frozen=True)
class TrajectoryPreflightResult:
    """一次轨迹的完整运动学预检事实，供预览和执行共同消费。"""

    trajectory: SineTrajectory
    points: tuple[PlatformPose, ...]
    fingerprint: str
    minimum_extension_mm: float
    minimum_remaining_extension_mm: float
    limiting_axis: int
    limiting_pose: PlatformPose
    compiled: CompiledTrajectory


class MotionTrajectoryRunner:
    def __init__(
        self,
        controller: DirectMotionPlatformController,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._controller = controller
        self._clock_ns = clock_ns
        self._sleep = sleep
        self._uses_default_timing = clock_ns is time.monotonic_ns and sleep is time.sleep

    def preflight(self, trajectory: SineTrajectory) -> TrajectoryPreflightResult:
        trajectory.validate()
        validate_sine_trajectory(self._controller.config, trajectory)
        count = trajectory.sample_count
        prefix_count = 0 if count is not None else max(
            0,
            int(math.ceil(trajectory.ramp_in_s * 1000.0 / trajectory.sample_period_ms)),
        )
        point_count = count if count is not None else prefix_count
        points = tuple(
            trajectory.pose_at((index + 1) * trajectory.sample_period_ms / 1000.0)
            for index in range(point_count)
        )
        minimum_extension = math.inf
        minimum_remaining = math.inf
        limiting_axis = 0
        limiting_pose = (
            points[0]
            if points
            else trajectory.pose_at(trajectory.sample_period_ms / 1000.0)
        )
        maximum_extension = self._controller.config.geometry.maximum_extension_mm
        # 控制器在相邻目标间连续运动；运动学包线使用固定 20 ms 路径分辨率，
        # 不因已资格的发送周期变粗而漏过两条指令之间的不可达姿态。
        envelope_period_ms = min(20, trajectory.sample_period_ms)
        envelope_duration_s = trajectory.total_duration_s
        if envelope_duration_s is None:
            envelope_duration_s = trajectory.ramp_in_s + 60.0
        envelope_count = max(1, math.ceil(envelope_duration_s * 1000.0 / envelope_period_ms))
        envelope_points = (
            trajectory.pose_at((index + 1) * envelope_period_ms / 1000.0)
            for index in range(envelope_count)
        )
        for point in envelope_points:
            try:
                targets = solve_actuator_targets(self._controller.config, point)
            except MotionPlatformError as exc:
                raise MotionPlatformError(
                    f"{exc}; combined pose roll={point.roll_deg:.3f} deg, "
                    f"pitch={point.pitch_deg:.3f} deg, yaw={point.yaw_deg:.3f} deg. "
                    "Single-axis angle limits do not guarantee combined-pose reachability."
                ) from exc
            for axis, extension in enumerate(targets.extensions_mm, start=1):
                if extension < minimum_extension:
                    minimum_extension = extension
                    limiting_axis = axis
                    limiting_pose = point
                minimum_remaining = min(minimum_remaining, maximum_extension - extension)
        if self._controller.config.minimum_duration_ms > CONTINUOUS_TRAJECTORY_PERIOD_MS:
            raise MotionPlatformError(
                "motion platform minimum command time exceeds the fixed "
                f"{CONTINUOUS_TRAJECTORY_PERIOD_MS} ms continuous trajectory contract"
            )
        fingerprint = _preflight_fingerprint(self._controller.config, trajectory)
        command_duration_ms = _command_duration_ms(self._controller.config, trajectory)
        if count is not None:
            finite_points = tuple(
                _compile_point(
                    self._controller.config,
                    point,
                    elapsed_s=(index + 1) * trajectory.sample_period_ms / 1000.0,
                    duration_ms=command_duration_ms,
                )
                for index, point in enumerate(points)
            )
            compiled = CompiledTrajectory(
                fingerprint=fingerprint,
                command_duration_ms=command_duration_ms,
                finite_points=finite_points,
            )
        else:
            prefix_points = tuple(
                _compile_point(
                    self._controller.config,
                    point,
                    elapsed_s=(index + 1) * trajectory.sample_period_ms / 1000.0,
                    duration_ms=command_duration_ms,
                )
                for index, point in enumerate(points)
            )
            repeat_count = _repeat_sample_count(trajectory)
            periodic_points = tuple(
                _compile_point(
                    self._controller.config,
                    trajectory.pose_at(
                        (prefix_count + index + 1) * trajectory.sample_period_ms / 1000.0
                    ),
                    elapsed_s=(prefix_count + index + 1)
                    * trajectory.sample_period_ms
                    / 1000.0,
                    duration_ms=command_duration_ms,
                )
                for index in range(repeat_count)
            )
            compiled = CompiledTrajectory(
                fingerprint=fingerprint,
                command_duration_ms=command_duration_ms,
                finite_points=(),
                prefix_points=prefix_points,
                periodic_points=periodic_points,
            )
        return TrajectoryPreflightResult(
            trajectory=trajectory,
            points=points,
            fingerprint=fingerprint,
            minimum_extension_mm=minimum_extension,
            minimum_remaining_extension_mm=minimum_remaining,
            limiting_axis=limiting_axis,
            limiting_pose=limiting_pose,
            compiled=compiled,
        )

    def run(
        self,
        trajectory: SineTrajectory,
        *,
        stop_event: threading.Event | None = None,
        soft_center_event: threading.Event | None = None,
        center_duration_ms: int = 3000,
        soft_center_phase_callback: Callable[[SoftCenterPhase], None] | None = None,
        preflight: TrajectoryPreflightResult | None = None,
    ) -> TrajectoryResult:
        if trajectory.steady_duration_s is None and stop_event is None:
            raise MotionPlatformError("continuous trajectory requires an explicit stop owner")
        checked = preflight or self.preflight(trajectory)
        if (
            checked.trajectory != trajectory
            or checked.fingerprint != _preflight_fingerprint(self._controller.config, trajectory)
        ):
            raise MotionPlatformError("trajectory preflight result does not match execution request")
        points = checked.points
        compiled = checked.compiled
        if compiled.fingerprint != checked.fingerprint:
            raise MotionPlatformError("compiled trajectory does not match preflight result")
        count = trajectory.sample_count
        stop = stop_event or threading.Event()
        soft_center = soft_center_event or threading.Event()
        period_ns = trajectory.sample_period_ms * 1_000_000
        command_duration_ms = compiled.command_duration_ms
        command_horizon_ns = command_duration_ms * 1_000_000
        next_deadline_ns = self._clock_ns()
        index = 0
        sent = 0
        skipped = 0
        maximum_lateness_ns = 0
        send_times_ns: list[int] = []
        observed_intervals_ns: list[int] = []
        timing_fault = False
        fade_points = 0
        center_confirmed = False
        arrival = None
        process_socket = (
            self._controller.trajectory_process_socket()
            if self._uses_default_timing
            and hasattr(self._controller, "trajectory_process_socket")
            else None
        )
        if process_socket is not None:
            return self._run_isolated(
                trajectory,
                checked,
                stop=stop,
                soft_center=soft_center,
                center_duration_ms=center_duration_ms,
                soft_center_phase_callback=soft_center_phase_callback,
                process_socket=process_socket,
            )
        self._controller.mark_trajectory_active()
        try:
            while (
                (count is None or index < count)
                and not stop.is_set()
                and not soft_center.is_set()
            ):
                deadline_ns = next_deadline_ns
                now = self._clock_ns()
                if now < deadline_ns:
                    self._sleep((deadline_ns - now) / 1_000_000_000.0)
                    now = self._clock_ns()
                if stop.is_set() or soft_center.is_set():
                    break
                if send_times_ns and now - send_times_ns[-1] >= 2 * period_ns:
                    timing_fault = True
                maximum_lateness_ns = max(maximum_lateness_ns, max(0, now - deadline_ns))
                point = compiled.point_at(index)
                event = self._controller.send_compiled_trajectory_point(
                    point.pose,
                    target_pulses=point.target_pulses,
                    frame=point.frame,
                    duration_ms=command_duration_ms,
                )
                # 命令事件时间最接近实际 sendto；循环入口时间还包含运动学/GIL 抢占，
                # 现场已证明会把 57 ms 的线上间隔误记为 83 ms。
                sent_ns = event.monotonic_ns
                if send_times_ns:
                    observed_intervals_ns.append(sent_ns - send_times_ns[-1])
                send_times_ns.append(sent_ns)
                # 使用真实发送时刻重建周期，迟到后不以短间隔追赶旧时间轴。
                next_deadline_ns = self._clock_ns() + period_ns
                sent += 1
                index += 1
            if soft_center.is_set() and sent and not stop.is_set():
                if soft_center_phase_callback is not None:
                    soft_center_phase_callback(SoftCenterPhase.FADING)
                fade = _compile_fade_points(
                    self._controller.config,
                    trajectory,
                    start_elapsed_s=index * trajectory.sample_period_ms / 1000.0,
                    duration_ms=command_duration_ms,
                )
                for point in fade:
                    deadline_ns = next_deadline_ns
                    now = self._clock_ns()
                    if now < deadline_ns:
                        self._sleep((deadline_ns - now) / 1_000_000_000.0)
                        now = self._clock_ns()
                    if stop.is_set():
                        break
                    if send_times_ns and now - send_times_ns[-1] >= 2 * period_ns:
                        timing_fault = True
                    event = self._controller.send_compiled_trajectory_point(
                        point.pose,
                        target_pulses=point.target_pulses,
                        frame=point.frame,
                        duration_ms=command_duration_ms,
                    )
                    sent_ns = event.monotonic_ns
                    if send_times_ns:
                        observed_intervals_ns.append(sent_ns - send_times_ns[-1])
                    send_times_ns.append(sent_ns)
                    next_deadline_ns = self._clock_ns() + period_ns
                    sent += 1
                    fade_points += 1
        except Exception:
            if soft_center.is_set() and soft_center_phase_callback is not None:
                soft_center_phase_callback(SoftCenterPhase.CENTER_FAILED)
            raise
        finally:
            self._controller.mark_trajectory_stopped()
        if soft_center.is_set() and not stop.is_set() and sent:
            if soft_center_phase_callback is not None:
                soft_center_phase_callback(SoftCenterPhase.CENTERING)
            try:
                arrival = self._controller.recover_to_center(duration_ms=int(center_duration_ms))
            except Exception:
                if soft_center_phase_callback is not None:
                    soft_center_phase_callback(SoftCenterPhase.CENTER_FAILED)
                raise
            center_confirmed = arrival.successful
            if soft_center_phase_callback is not None:
                soft_center_phase_callback(
                    SoftCenterPhase.CENTER_CONFIRMED
                    if center_confirmed
                    else SoftCenterPhase.CENTER_FAILED
                )
        elif not stop.is_set() and sent and count is not None and index == count:
            arrival = self._controller.wait_for_target(points[-1], action="trajectory")
        elif soft_center.is_set() and soft_center_phase_callback is not None:
            soft_center_phase_callback(SoftCenterPhase.CENTER_FAILED)
        intervals_ns = observed_intervals_ns or [
            current - previous for previous, current in zip(send_times_ns, send_times_ns[1:])
        ]
        maximum_interval_ns = max(intervals_ns, default=0)
        gap_count = sum(interval > command_horizon_ns for interval in intervals_ns)
        return TrajectoryResult(
            intended_points=count,
            sent_points=sent,
            skipped_points=skipped,
            maximum_lateness_ms=maximum_lateness_ns / 1_000_000.0,
            stopped=stop.is_set() or soft_center.is_set(),
            arrival=arrival,
            maximum_send_interval_ms=maximum_interval_ns / 1_000_000.0,
            p95_send_interval_ms=_percentile_ms(intervals_ns, 0.95),
            p99_send_interval_ms=_percentile_ms(intervals_ns, 0.99),
            control_gap_count=gap_count,
            timing_fault=timing_fault,
            command_duration_ms=command_duration_ms,
            soft_center_requested=soft_center.is_set(),
            fade_points=fade_points,
            center_confirmed=center_confirmed,
        )

    def _run_isolated(
        self,
        trajectory: SineTrajectory,
        checked: TrajectoryPreflightResult,
        *,
        stop: threading.Event,
        soft_center: threading.Event,
        center_duration_ms: int,
        soft_center_phase_callback: Callable[[SoftCenterPhase], None] | None,
        process_socket,
    ) -> TrajectoryResult:
        """让独立进程独占发送时钟，父进程继续拥有状态、反馈和证据。"""

        from .timeline_process import run_timeline_process

        compiled = checked.compiled
        period_ns = trajectory.sample_period_ms * 1_000_000
        context = multiprocessing.get_context("spawn")
        command_queue = context.Queue()
        event_queue = context.Queue()
        process = context.Process(
            target=run_timeline_process,
            args=(
                process_socket,
                (
                    self._controller.config.controller_ipv4,
                    self._controller.config.command_port,
                ),
                tuple(point.frame for point in compiled.finite_points),
                tuple(point.frame for point in compiled.prefix_points),
                tuple(point.frame for point in compiled.periodic_points),
                trajectory.sample_count,
                period_ns,
                self._controller.config,
                trajectory,
                compiled.command_duration_ms,
                command_queue,
                event_queue,
            ),
            name="motion-platform-timeline",
            daemon=True,
        )
        sent = 0
        skipped = 0
        fade_points = 0
        timing_fault = False
        maximum_lateness_ns = 0
        send_times_ns: list[int] = []
        observed_intervals_ns: list[int] = []
        soft_queued = False
        soft_complete = False
        arrival = None
        center_confirmed = False
        self._controller.mark_trajectory_active()
        try:
            process.start()
            while True:
                if stop.is_set():
                    command_queue.put(("stop",))
                if soft_center.is_set() and not soft_queued and not stop.is_set():
                    soft_queued = True
                    if soft_center_phase_callback is not None:
                        soft_center_phase_callback(SoftCenterPhase.FADING)
                    command_queue.put(("soft_center",))
                try:
                    event = event_queue.get(timeout=0.02)
                except queue.Empty:
                    if not process.is_alive():
                        if process.exitcode not in (0, None):
                            raise MotionPlatformError(
                                f"trajectory timeline process exited with code {process.exitcode}"
                            )
                        break
                    continue
                event_type = event[0]
                if event_type == "sent":
                    _, mode, point_index, sent_ns, lateness_ns, fade_point = event
                    point = (
                        checked.compiled.point_at(int(point_index))
                        if mode == "normal"
                        else fade_point
                    )
                    self._controller.accept_external_trajectory_send(
                        point.pose,
                        target_pulses=point.target_pulses,
                        frame=point.frame,
                        duration_ms=compiled.command_duration_ms,
                        sent_monotonic_ns=int(sent_ns),
                    )
                    if send_times_ns:
                        observed_intervals_ns.append(int(sent_ns) - send_times_ns[-1])
                    send_times_ns.append(int(sent_ns))
                    maximum_lateness_ns = max(maximum_lateness_ns, int(lateness_ns))
                    sent += 1
                    if mode == "fade":
                        fade_points += 1
                    continue
                if event_type == "soft_complete":
                    soft_complete = True
                    break
                if event_type == "finished":
                    break
                if event_type == "stopped":
                    break
                if event_type == "timing_fault":
                    timing_fault = True
                    maximum_lateness_ns = max(maximum_lateness_ns, int(event[3]))
                    continue
                if event_type == "error":
                    raise MotionPlatformError(str(event[1]))
                raise MotionPlatformError(f"unknown trajectory timeline event: {event_type}")
        except Exception:
            if soft_queued and soft_center_phase_callback is not None:
                soft_center_phase_callback(SoftCenterPhase.CENTER_FAILED)
            raise
        finally:
            if process.is_alive():
                command_queue.put(("stop",))
                process.join(timeout=1.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1.0)
            command_queue.close()
            event_queue.close()
            self._controller.mark_trajectory_stopped()

        if soft_complete and not stop.is_set():
            if soft_center_phase_callback is not None:
                soft_center_phase_callback(SoftCenterPhase.CENTERING)
            try:
                arrival = self._controller.recover_to_center(
                    duration_ms=int(center_duration_ms)
                )
            except Exception:
                if soft_center_phase_callback is not None:
                    soft_center_phase_callback(SoftCenterPhase.CENTER_FAILED)
                raise
            center_confirmed = arrival.successful
            if soft_center_phase_callback is not None:
                soft_center_phase_callback(
                    SoftCenterPhase.CENTER_CONFIRMED
                    if center_confirmed
                    else SoftCenterPhase.CENTER_FAILED
                )
        elif (
            not stop.is_set()
            and sent
            and trajectory.sample_count is not None
            and sent == trajectory.sample_count
        ):
            arrival = self._controller.wait_for_target(
                checked.points[-1],
                action="trajectory",
            )
        elif soft_queued and soft_center_phase_callback is not None:
            soft_center_phase_callback(SoftCenterPhase.CENTER_FAILED)

        command_horizon_ns = compiled.command_duration_ms * 1_000_000
        maximum_interval_ns = max(observed_intervals_ns, default=0)
        return TrajectoryResult(
            intended_points=trajectory.sample_count,
            sent_points=sent,
            skipped_points=skipped,
            maximum_lateness_ms=maximum_lateness_ns / 1_000_000.0,
            stopped=stop.is_set() or soft_queued,
            arrival=arrival,
            maximum_send_interval_ms=maximum_interval_ns / 1_000_000.0,
            p95_send_interval_ms=_percentile_ms(observed_intervals_ns, 0.95),
            p99_send_interval_ms=_percentile_ms(observed_intervals_ns, 0.99),
            control_gap_count=sum(
                interval > command_horizon_ns for interval in observed_intervals_ns
            ),
            timing_fault=timing_fault,
            command_duration_ms=compiled.command_duration_ms,
            soft_center_requested=soft_queued,
            fade_points=fade_points,
            center_confirmed=center_confirmed,
        )


def _command_duration_ms(config, trajectory: SineTrajectory) -> int:
    if config.minimum_duration_ms > CONTINUOUS_TRAJECTORY_PERIOD_MS:
        raise MotionPlatformError(
            "motion platform minimum command time exceeds the fixed "
            f"{CONTINUOUS_TRAJECTORY_PERIOD_MS} ms continuous trajectory contract"
        )
    return CONTINUOUS_TRAJECTORY_PERIOD_MS


def _compile_point(
    config,
    pose: PlatformPose,
    *,
    elapsed_s: float,
    duration_ms: int,
) -> CompiledTrajectoryPoint:
    targets = solve_actuator_targets(config, pose)
    return CompiledTrajectoryPoint(
        elapsed_s=float(elapsed_s),
        pose=pose,
        target_pulses=targets.pulses,
        frame=build_position_frame(duration_ms, targets.pulses),
    )


def _repeat_sample_count(trajectory: SineTrajectory) -> int:
    """返回离散正弦的最短联合重复周期点数。"""

    sample_s = Fraction(trajectory.sample_period_ms, 1000)
    count = 1
    for axis in (trajectory.roll, trajectory.pitch, trajectory.yaw):
        if not axis.enabled:
            continue
        step_cycles = Fraction(str(axis.frequency_hz)) * sample_s
        count = math.lcm(count, step_cycles.denominator)
        if count > 120_000:
            raise MotionPlatformError(
                "continuous trajectory repeat period exceeds compiled-program limit"
            )
    return count


def _compile_fade_points(
    config,
    trajectory: SineTrajectory,
    *,
    start_elapsed_s: float,
    duration_ms: int,
) -> tuple[CompiledTrajectoryPoint, ...]:
    if trajectory.ramp_out_s <= 0:
        return ()
    count = max(
        1,
        int(math.ceil(trajectory.ramp_out_s * 1000.0 / trajectory.sample_period_ms)),
    )
    period_s = trajectory.sample_period_ms / 1000.0
    start_envelope = _trajectory_envelope(trajectory, start_elapsed_s)
    compiled = []
    for index in range(count):
        progress = (index + 1) / count
        elapsed_s = start_elapsed_s + (index + 1) * period_s
        envelope = start_envelope * math.cos(math.pi * progress / 2.0) ** 2
        pose = _sine_pose(trajectory, elapsed_s, envelope)
        compiled.append(
            _compile_point(
                config,
                pose,
                elapsed_s=elapsed_s,
                duration_ms=duration_ms,
            )
        )
    return tuple(compiled)


def _sine_pose(
    trajectory: SineTrajectory,
    elapsed_s: float,
    envelope: float,
) -> PlatformPose:
    return PlatformPose(
        _axis_value(trajectory.roll, elapsed_s, envelope),
        _axis_value(trajectory.pitch, elapsed_s, envelope),
        _axis_value(trajectory.yaw, elapsed_s, envelope),
        z_mm=trajectory.work_z_mm,
    )


def _trajectory_envelope(trajectory: SineTrajectory, elapsed_s: float) -> float:
    elapsed = max(0.0, float(elapsed_s))
    duration = trajectory.total_duration_s
    envelope = 1.0
    if trajectory.ramp_in_s > 0 and elapsed < trajectory.ramp_in_s:
        envelope = math.sin(math.pi * elapsed / (2.0 * trajectory.ramp_in_s)) ** 2
    if (
        duration is not None
        and trajectory.ramp_out_s > 0
        and elapsed > duration - trajectory.ramp_out_s
    ):
        remaining = max(0.0, duration - elapsed)
        envelope = min(
            envelope,
            math.sin(math.pi * remaining / (2.0 * trajectory.ramp_out_s)) ** 2,
        )
    return envelope


def _axis_value(axis: SineAxis, elapsed_s: float, envelope: float) -> float:
    if not axis.enabled:
        return 0.0
    phase = 2.0 * math.pi * axis.frequency_hz * elapsed_s + math.radians(axis.phase_deg)
    return axis.amplitude_deg * envelope * math.sin(phase)


def _percentile_ms(values_ns: list[int], quantile: float) -> float:
    if not values_ns:
        return 0.0
    ordered = sorted(values_ns)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index] / 1_000_000.0


def _preflight_fingerprint(config, trajectory: SineTrajectory) -> str:
    trajectory_payload = asdict(trajectory)
    trajectory_payload["sample_period_ms"] = CONTINUOUS_TRAJECTORY_PERIOD_MS
    payload = {"trajectory": trajectory_payload, "config": asdict(config)}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_sine_trajectory(config, trajectory: SineTrajectory) -> None:
    trajectory.validate()
    for name, axis in (("roll", trajectory.roll), ("pitch", trajectory.pitch), ("yaw", trajectory.yaw)):
        if not axis.enabled:
            continue
        angle_limit = getattr(config, f"{name}_abs_limit_deg")
        if axis.amplitude_deg > angle_limit:
            raise MotionPlatformError(f"{name} trajectory amplitude exceeds configured limit")
        frequency_limit = getattr(config, f"{name}_max_frequency_hz")
        velocity_limit = getattr(config, f"{name}_max_velocity_deg_s")
        acceleration_limit = getattr(config, f"{name}_max_acceleration_deg_s2")
        if None in (frequency_limit, velocity_limit, acceleration_limit):
            raise MotionPlatformError(f"{name} trajectory operating limits are incomplete")
        velocity = 2.0 * math.pi * axis.frequency_hz * axis.amplitude_deg
        acceleration = (2.0 * math.pi * axis.frequency_hz) ** 2 * axis.amplitude_deg
        if axis.frequency_hz > frequency_limit:
            raise MotionPlatformError(f"{name} trajectory frequency exceeds configured limit")
        if velocity > velocity_limit:
            raise MotionPlatformError(f"{name} trajectory velocity exceeds configured limit")
        if acceleration > acceleration_limit:
            raise MotionPlatformError(f"{name} trajectory acceleration exceeds configured limit")


__all__ = [
    "CompiledTrajectory",
    "CompiledTrajectoryPoint",
    "MotionTrajectoryRunner",
    "CONTINUOUS_TRAJECTORY_PERIOD_MS",
    "MACOS_CONTINUOUS_TRAJECTORY_PERIOD_MS",
    "WINDOWS_CONTINUOUS_TRAJECTORY_PERIOD_MS",
    "continuous_trajectory_period_ms",
    "SineAxis",
    "SineTrajectory",
    "SoftCenterPhase",
    "TrajectoryPreflightResult",
    "TrajectoryResult",
    "validate_sine_trajectory",
]
