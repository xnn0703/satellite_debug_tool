"""只读流式复算历史台架 SDB 的新跟踪判据，JSON 输出到标准输出。

用法：PYTHONPATH=. .venv/bin/python scripts/analyze_tracking_criteria.py BATCH_DIR
历史 attempt、报告和 SDB 保持原样；结果仅用于新旧判据对照。
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterator

from satellite_debug_tool.core.production.tracking_criteria import TrackingCriteriaPolicy, TrackingEvaluator
from satellite_debug_tool.core.protocol import FrameReceiverV2, ServiceFastState
from satellite_debug_tool.io.sdb_schema import (
    SDB_FOOTER, SDB_HEADER_CORE, SDB_HEADER_RESERVED_SIZE, SDB_MAGIC,
    SDB_PROFILE_LENGTH, SDB_RECORD_RX_CHUNK, SDB_VERSION_V3,
    iter_record_envelopes, read_exact,
)


def _wall_ns(value: str) -> int:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    delta = instant - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def iter_fast_states(path: Path) -> Iterator[tuple[int, ServiceFastState]]:
    """共用 SDB envelope 和协议 parser；读取最多 64 KiB，不生成旁索引。"""
    receiver = FrameReceiverV2()
    with path.open("rb") as stream:
        magic, version, _timestamp = SDB_HEADER_CORE.unpack(
            read_exact(stream, SDB_HEADER_CORE.size, context="SDB header")
        )
        if magic != SDB_MAGIC or version != SDB_VERSION_V3:
            raise ValueError(f"SDB v3 host timestamps required: {path}")
        length = SDB_PROFILE_LENGTH.unpack(read_exact(stream, SDB_PROFILE_LENGTH.size, context="metadata length"))[0]
        stream.seek(length + SDB_HEADER_RESERVED_SIZE, 1)
        start = stream.tell()
        end = path.stat().st_size
        stream.seek(end - len(SDB_FOOTER))
        if stream.read(len(SDB_FOOTER)) == SDB_FOOTER:
            end -= len(SDB_FOOTER)
        for envelope in iter_record_envelopes(stream, start_offset=start, end_offset=end):
            if envelope.record_type != SDB_RECORD_RX_CHUNK:
                continue
            remaining = envelope.payload_length
            while remaining:
                data = read_exact(stream, min(remaining, 64 * 1024), context="RX payload")
                remaining -= len(data)
                for record in receiver.feed(data):
                    if isinstance(record, ServiceFastState):
                        yield envelope.host_timestamp_ns, record


def analyze(batch_dir: Path, serial_number: str | None = None) -> dict:
    with closing(sqlite3.connect((batch_dir / "batch.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        offsets = [
            _wall_ns(row["wall_time_utc"]) - int(row["monotonic_ns"])
            for row in db.execute("SELECT wall_time_utc, monotonic_ns FROM events")
        ]
        attempts = [dict(row) for row in db.execute(
            "SELECT * FROM attempts WHERE test_id IN ('static_acquisition', 'locked_rocking', 'power_on_rocking') ORDER BY attempt_id"
        )]
    if not offsets:
        raise ValueError("batch events lack host/monotonic anchors")
    ordered_offsets = sorted(offsets)
    middle = len(ordered_offsets) // 2
    offset = (ordered_offsets[middle] if len(ordered_offsets) % 2
              else (ordered_offsets[middle - 1] + ordered_offsets[middle]) // 2)
    policy = TrackingCriteriaPolicy()
    devices: dict[str, list[dict]] = {}
    for attempt in attempts:
        serial = attempt["serial_number"]
        if serial_number is not None and serial != serial_number:
            continue
        if attempt["started_monotonic_ns"] is None:
            continue
        old = json.loads(attempt["result_json"])
        duration = float(old["prerequisite_evidence"]["duration_s"])
        evaluator = TrackingEvaluator(policy, duration, acquisition_required=attempt["test_id"] != "locked_rocking")
        evaluator.start(0.0)
        devices.setdefault(serial, []).append({
            "attempt": attempt, "duration": duration, "evaluator": evaluator,
            "start_wall_ns": int(attempt["started_monotonic_ns"]) + offset,
            "first_sample_s": None, "last_sample_s": None,
        })
    result = []
    for serial, windows in devices.items():
        paths = sorted((batch_dir / "devices" / serial / "evidence").glob("*.sdb"))
        if len(paths) != 1:
            raise ValueError(f"exactly one SDB required for deterministic time ordering: {serial}")
        for host_ns, sample in iter_fast_states(paths[0]):
            for window in windows:
                elapsed = (host_ns - window["start_wall_ns"]) / 1_000_000_000.0
                if 0 <= elapsed <= window["duration"]:
                    if window["first_sample_s"] is None:
                        window["first_sample_s"] = elapsed
                    window["last_sample_s"] = elapsed
                    valid_mask = (1 << 1) | (1 << 2) | (1 << 11)
                    window["evaluator"].observe(
                        elapsed, sample.tracking_phase == 3, sample.locked, sample.snr_db,
                        (sample.valid_mask & valid_mask) == valid_mask,
                    )
        for window in windows:
            outcome = window["evaluator"].finish(window["duration"])
            result.append({
                "serial_number": serial, "sdb_path": str(paths[0]),
                "attempt_id": window["attempt"]["attempt_id"], "test_id": window["attempt"]["test_id"],
                "historical_status": window["attempt"]["status"],
                "start_wall_ns": window["start_wall_ns"],
                "first_sample_s": window["first_sample_s"], "last_sample_s": window["last_sample_s"],
                "verdict": outcome.verdict, "performance_verdict": outcome.performance_verdict,
                "data_quality": outcome.data_quality, "tracking_criteria": dict(outcome.metrics),
            })
    return {
        "source": str(batch_dir), "comparison_only": True,
        "clock_alignment": {"method": "median_event_wall_minus_monotonic_ns", "offset_ns": offset,
                            "offset_range_ns": max(offsets) - min(offsets), "event_count": len(offsets),
                            "note": "事件 UTC 精度为毫秒；使用整数换算，主机记录时间不等于物理跳变时间。"},
        "windows": result,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_dir", type=Path)
    parser.add_argument("--serial-number")
    args = parser.parse_args()
    print(json.dumps(analyze(args.batch_dir.expanduser().resolve(), args.serial_number), ensure_ascii=False, indent=2))
