"""独立进程中的运动平台 UDP 时间线发送器。"""

from __future__ import annotations

import os
import queue
import socket
import time
from typing import Sequence


_DEADLINE_GUARD_NS = 1_000_000


def _begin_windows_timer_resolution():
    """让当前 Windows 发送子进程的等待粒度满足固定 20 ms 时间线。"""
    if os.name != "nt":
        return None
    import ctypes

    winmm = ctypes.WinDLL("winmm")
    if winmm.timeBeginPeriod(1) != 0:
        raise OSError("timeBeginPeriod(1) failed")
    return winmm


def run_timeline_process(
    transport: socket.socket,
    endpoint: tuple[str, int],
    finite_frames: tuple[bytes, ...],
    prefix_frames: tuple[bytes, ...],
    periodic_frames: tuple[bytes, ...],
    intended_points: int | None,
    period_ns: int,
    config,
    trajectory,
    command_duration_ms: int,
    command_queue,
    event_queue,
) -> None:
    """按单调时钟发送预编译帧；所有业务状态仍由父进程拥有。"""

    program_mode = "normal"
    fade_points = ()
    normal_index = 0
    fade_index = 0
    next_deadline_ns = time.monotonic_ns()
    last_sent_ns: int | None = None
    winmm = None
    try:
        winmm = _begin_windows_timer_resolution()
        while True:
            deadline_ns = next_deadline_ns
            while True:
                remaining_ns = deadline_ns - time.monotonic_ns()
                if remaining_ns <= 0:
                    break
                try:
                    if remaining_ns > _DEADLINE_GUARD_NS:
                        command = command_queue.get(
                            timeout=(remaining_ns - _DEADLINE_GUARD_NS)
                            / 1_000_000_000.0
                        )
                    else:
                        command = command_queue.get_nowait()
                except queue.Empty:
                    # macOS的Queue超时通常会多睡约1 ms；最后1 ms保持命令轮询，
                    # 让20 ms时间线不系统性退化为约21 ms。
                    continue
                action = command[0]
                if action == "stop":
                    event_queue.put(("stopped", normal_index, fade_index))
                    return
                if action == "soft_center" and program_mode == "normal":
                    from .trajectory import _compile_fade_points

                    fade_points = _compile_fade_points(
                        config,
                        trajectory,
                        start_elapsed_s=normal_index * trajectory.sample_period_ms / 1000.0,
                        duration_ms=command_duration_ms,
                    )
                    program_mode = "fade"
                    fade_index = 0
                    deadline_ns = next_deadline_ns
            while True:
                try:
                    command = command_queue.get_nowait()
                except queue.Empty:
                    break
                action = command[0]
                if action == "stop":
                    event_queue.put(("stopped", normal_index, fade_index))
                    return
                if action == "soft_center" and program_mode == "normal":
                    from .trajectory import _compile_fade_points

                    fade_points = _compile_fade_points(
                        config,
                        trajectory,
                        start_elapsed_s=normal_index * trajectory.sample_period_ms / 1000.0,
                        duration_ms=command_duration_ms,
                    )
                    program_mode = "fade"
                    fade_index = 0

            if program_mode == "normal":
                if intended_points is not None and normal_index >= intended_points:
                    event_queue.put(("finished", normal_index, fade_index))
                    return
                frame = _normal_frame(
                    normal_index,
                    finite_frames,
                    prefix_frames,
                    periodic_frames,
                )
                point_index = normal_index
            else:
                if fade_index >= len(fade_points):
                    event_queue.put(("soft_complete", normal_index, fade_index))
                    return
                fade_point = fade_points[fade_index]
                frame = fade_point.frame
                point_index = fade_index

            now_ns = time.monotonic_ns()
            actual_gap_ns = (
                now_ns - last_sent_ns if last_sent_ns is not None else 0
            )
            if last_sent_ns is not None and actual_gap_ns >= 2 * int(period_ns):
                event_queue.put(
                    ("timing_fault", program_mode, point_index, now_ns - deadline_ns)
                )
            size = transport.sendto(frame, endpoint)
            if size != len(frame):
                event_queue.put(("error", "controller datagram was not sent completely"))
                return
            sent_ns = time.monotonic_ns()
            last_sent_ns = sent_ns
            # 迟到后以本次真实发送时刻重建相位，禁止为追赶旧绝对时间轴而连续突发。
            next_deadline_ns = sent_ns + int(period_ns)
            event_queue.put(
                (
                    "sent",
                    program_mode,
                    point_index,
                    sent_ns,
                    now_ns - deadline_ns,
                    fade_point if program_mode == "fade" else None,
                )
            )
            if program_mode == "normal":
                normal_index += 1
            else:
                fade_index += 1
    except BaseException as exc:
        event_queue.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        if winmm is not None:
            winmm.timeEndPeriod(1)
        transport.close()


def _normal_frame(
    index: int,
    finite_frames: Sequence[bytes],
    prefix_frames: Sequence[bytes],
    periodic_frames: Sequence[bytes],
) -> bytes:
    if finite_frames:
        return finite_frames[index]
    if index < len(prefix_frames):
        return prefix_frames[index]
    return periodic_frames[(index - len(prefix_frames)) % len(periodic_frames)]


__all__ = ["run_timeline_process"]
