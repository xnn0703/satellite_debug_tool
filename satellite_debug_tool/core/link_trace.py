"""可按环境变量开启的 DEBUG UDP/协议链路追踪。"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Callable

TRACE_ENV = "SATELLITE_DEBUG_LINK_TRACE"
_TRUE_VALUES = {"1", "true", "yes", "on"}
FRAME_HEADER_0 = 0xAA
FRAME_HEADER_1 = 0x55
DEVICE_TYPE = 0x0D
FRAME_FOOTER = 0xEE
CMD_DATA_REPORT = 0x01
CMD_CONTROL = 0x03

_CMD_NAMES = {
    0x01: "DATA_REPORT",
    0x02: "COMMAND_RESPONSE",
    0x03: "CONTROL",
    0x04: "META_INFO",
    0x05: "CHANNEL_DEFINE",
    0x06: "STATE_DEFINE",
    0x07: "EVENT_DEFINE",
    0x08: "STATE_REPORT",
    0x09: "EVENT_REPORT",
    0x0A: "HEARTBEAT",
    0x0B: "PARA_TABLE_REPORT",
    0x0C: "PROFILE_SEMANTICS",
    0x0D: "GNSS_SKY_REPORT",
    0x0E: "GNSS_CNR_REPORT",
    0x0F: "GNSS_SAT_REPORT",
    0x10: "GNSS_SIGNAL_REPORT",
}

_SUB_CMD_NAMES = {
    0x01: "DEBUG_ENABLE",
    0x02: "REQUEST_META_INFO",
    0x03: "REQUEST_CHANNEL_DEFINE",
    0x04: "REQUEST_STATE_DEFINE",
    0x05: "REQUEST_EVENT_DEFINE",
    0x06: "USER_MARK",
    0x07: "SET_SAMPLE_RATE",
    0x08: "SET_TRACE_MODE",
    0x09: "CHANNEL_ENABLE_MASK",
    0x0A: "RESET_STATS",
    0x0B: "REQUEST_PARA_TABLE",
    0x0C: "PARA_SET",
    0x0D: "PARA_RESET",
    0x0E: "OTA_BEGIN",
    0x0F: "OTA_DATA",
    0x10: "OTA_END",
    0x11: "OTA_ABORT",
    0x12: "DEVICE_REBOOT",
    0x13: "REQUEST_PROFILE_SEMANTICS",
}


def is_link_trace_enabled() -> bool:
    return os.environ.get(TRACE_ENV, "").strip().lower() in _TRUE_VALUES


def trace_message(component: str, message: str) -> None:
    if not is_link_trace_enabled():
        return
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{component} {timestamp}] {message}", flush=True)


def frame_fields(frame: bytes) -> dict[str, int | str | bool]:
    """提取首个 v2 帧的诊断字段，不承担协议解码。"""
    fields: dict[str, int | str | bool] = {
        "valid": False,
        "frame_len": len(frame),
        "data_len": 0,
        "cmd_type": -1,
        "cmd_name": "UNKNOWN",
        "sub_cmd": -1,
        "sub_name": "NONE",
        "crc": 0,
    }
    if len(frame) < 6:
        return fields
    if frame[0] != FRAME_HEADER_0 or frame[1] != FRAME_HEADER_1 or frame[2] != DEVICE_TYPE:
        return fields

    cmd_type = frame[3]
    data_len = int.from_bytes(frame[4:6], "little")
    expected = 9 + data_len
    fields.update({
        "cmd_type": cmd_type,
        "cmd_name": _CMD_NAMES.get(cmd_type, f"UNKNOWN_{cmd_type:02X}"),
        "data_len": data_len,
        "expected_len": expected,
    })
    if len(frame) < expected or frame[expected - 1] != FRAME_FOOTER:
        return fields

    crc = int.from_bytes(frame[6 + data_len:8 + data_len], "little")
    sub_cmd = frame[6] if cmd_type == CMD_CONTROL and data_len > 0 else -1
    fields.update({
        "valid": True,
        "crc": crc,
        "sub_cmd": sub_cmd,
        "sub_name": (
            _SUB_CMD_NAMES.get(sub_cmd, f"UNKNOWN_{sub_cmd:02X}")
            if sub_cmd >= 0 else "NONE"
        ),
    })
    return fields


def describe_frame(frame: bytes) -> str:
    fields = frame_fields(frame)
    if not fields["valid"]:
        expected = fields.get("expected_len", "?")
        return f"invalid frame_len={len(frame)} expected={expected}"
    sub = ""
    if int(fields["sub_cmd"]) >= 0:
        sub = f" sub={fields['sub_name']}(0x{int(fields['sub_cmd']):02X})"
    return (
        f"fid={int(fields['crc']):04X} "
        f"cmd={fields['cmd_name']}(0x{int(fields['cmd_type']):02X})"
        f"{sub} data_len={int(fields['data_len'])} frame_len={len(frame)}"
    )


class FrameTraceLogger:
    """逐帧追踪；DATA 仅打印首帧和每秒汇总。"""

    def __init__(self, component: str, *, clock: Callable[[], float] = time.monotonic):
        self._component = component
        self._clock = clock
        self._lock = threading.Lock()
        self._data_seen = False
        self._data_count = 0
        self._data_last_log = 0.0
        self._data_last_description = ""

    def message(self, message: str) -> None:
        trace_message(self._component, message)

    def frame(self, stage: str, frame: bytes, detail: str = "") -> None:
        if not is_link_trace_enabled():
            return
        description = describe_frame(frame)
        fields = frame_fields(frame)
        suffix = f" {detail}" if detail else ""
        if fields["valid"] and fields["cmd_type"] == CMD_DATA_REPORT:
            self._record_data(stage, description, suffix)
            return
        trace_message(self._component, f"{stage} {description}{suffix}")

    def _record_data(self, stage: str, description: str, suffix: str) -> None:
        now = self._clock()
        with self._lock:
            self._data_count += 1
            self._data_last_description = description
            if not self._data_seen:
                self._data_seen = True
                self._data_last_log = now
                self._data_count = 0
                trace_message(self._component, f"{stage} DATA first {description}{suffix}")
                return
            if now - self._data_last_log < 1.0:
                return
            count = self._data_count
            last_description = self._data_last_description
            self._data_count = 0
            self._data_last_log = now
        trace_message(
            self._component,
            f"{stage} DATA summary frames={count} last={last_description}{suffix}",
        )


__all__ = [
    "FrameTraceLogger",
    "TRACE_ENV",
    "describe_frame",
    "frame_fields",
    "is_link_trace_enabled",
    "trace_message",
]
