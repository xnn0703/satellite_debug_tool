"""Frozen curve extraction, stage statistics, and deterministic report charts."""

from __future__ import annotations

import csv
from datetime import datetime
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

import numpy as np
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QPointF, QRectF
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen

from satellite_debug_tool.core.profile import (
    CHANNEL_ROLE_PITCH,
    CHANNEL_ROLE_ROLL,
    CHANNEL_ROLE_SNR,
    CHANNEL_ROLE_YAW,
    ProfileStore,
)
from satellite_debug_tool.core.protocol import DataReport, ServiceFastState
from satellite_debug_tool.io.data_importer import DataImporter, SdbFormatError


_ROLES = (CHANNEL_ROLE_SNR, CHANNEL_ROLE_ROLL, CHANNEL_ROLE_PITCH, CHANNEL_ROLE_YAW)
_ROLE_LABELS = {
    CHANNEL_ROLE_SNR: "SNR",
    CHANNEL_ROLE_ROLL: "Roll",
    CHANNEL_ROLE_PITCH: "Pitch",
    CHANNEL_ROLE_YAW: "Yaw",
    "locked": "Lock",
    "attitude_error": "Attitude error",
}
_ROLE_UNITS = {
    CHANNEL_ROLE_SNR: "dB",
    CHANNEL_ROLE_ROLL: "deg",
    CHANNEL_ROLE_PITCH: "deg",
    CHANNEL_ROLE_YAW: "deg",
    "locked": "state",
    "attitude_error": "deg",
}
_COLORS = {
    CHANNEL_ROLE_SNR: QColor("#00796B"),
    CHANNEL_ROLE_ROLL: QColor("#1565C0"),
    CHANNEL_ROLE_PITCH: QColor("#EF6C00"),
    CHANNEL_ROLE_YAW: QColor("#7B1FA2"),
    "locked": QColor("#455A64"),
    "attitude_error": QColor("#C62828"),
}
_MAX_FROZEN_POINTS = 2400
_SCENARIO_GROUPS = (
    ("static", "静态工况", ("static_acquisition",)),
    ("locked_rocking", "锁定后摇摆工况", ("locked_rocking",)),
    ("power_on_rocking", "摇摆中上电工况", ("power_on_rocking",)),
    ("vehicle_dynamic", "车载动态工况", ("locked_drive", "power_on_drive")),
)
_CROSS_SCENARIO_TEST_IDS = frozenset(
    {"gnss", "imu_static", "external_ins", "whole_navigation"}
)


def build_report_analysis(
    sdb_paths: Sequence[Path],
    attempts: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    batch_output_dir: Path,
) -> dict[str, Any]:
    """Extract immutable report series and derive attempt-window conclusions."""

    product: dict[str, list[tuple[int, float]]] = {role: [] for role in (*_ROLES, "locked")}
    legacy: dict[str, list[tuple[int, float]]] = {role: [] for role in _ROLES}
    sources: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in sdb_paths:
        try:
            sdb = DataImporter.open_sdb(path)
            role_ids = _legacy_role_ids(sdb.profile)
            product_count = 0
            legacy_count = 0
            for host_ns, record in sdb.iter_timed_records():
                if isinstance(record, ServiceFastState):
                    fields = (
                        (6, CHANNEL_ROLE_ROLL, record.roll_deg),
                        (7, CHANNEL_ROLE_PITCH, record.pitch_deg),
                        (8, CHANNEL_ROLE_YAW, record.yaw_deg),
                        (11, CHANNEL_ROLE_SNR, record.snr_db),
                    )
                    for bit, role, value in fields:
                        if record.valid_mask & (1 << bit) and math.isfinite(float(value)):
                            product[role].append((int(host_ns), float(value)))
                            product_count += 1
                    product["locked"].append((int(host_ns), 1.0 if record.locked else 0.0))
                elif isinstance(record, DataReport):
                    for sample in record.samples:
                        role = role_ids.get(int(sample.channel_id))
                        if role is not None and math.isfinite(float(sample.value)):
                            legacy[role].append((int(host_ns), float(sample.value)))
                            legacy_count += 1
            sources.append(
                {
                    "file": path.name,
                    "sha256": _sha256(path),
                    "product_samples": product_count,
                    "legacy_samples": legacy_count,
                }
            )
        except (OSError, ValueError, SdbFormatError) as exc:
            errors.append(f"{path.name}: {exc}")

    role_sources: dict[str, str] = {}
    selected: dict[str, list[tuple[int, float]]] = {}
    for role in _ROLES:
        if product[role]:
            selected[role] = product[role]
            role_sources[role] = "product_service"
        else:
            selected[role] = legacy[role]
            role_sources[role] = "profile_semantics" if legacy[role] else "unavailable"
    if product["locked"]:
        selected["locked"] = product["locked"]
        role_sources["locked"] = "product_service"
    active_sources = {value for value in role_sources.values() if value != "unavailable"}
    source_kind = active_sources.pop() if len(active_sources) == 1 else ("mixed" if active_sources else "unavailable")
    offset_ns = _wall_monotonic_offset(events)
    fixture = _load_fixture_comparison(batch_output_dir, offset_ns)
    if fixture:
        selected = {**selected, "attitude_error": fixture}

    all_times = [timestamp for values in selected.values() for timestamp, _ in values]
    origin_ns = min(all_times) if all_times else None
    frozen_series: dict[str, dict[str, Any]] = {}
    for role, values in selected.items():
        ordered = sorted(values)
        reduced = _downsample_extrema(ordered, _MAX_FROZEN_POINTS)
        frozen_series[role] = {
            "label": _ROLE_LABELS[role],
            "unit": _ROLE_UNITS[role],
            "sample_count": len(ordered),
            "points": [
                [round((timestamp - int(origin_ns)) / 1_000_000_000.0, 6), value]
                for timestamp, value in reduced
            ] if origin_ns is not None else [],
        }

    stage_rows = []
    for attempt in attempts:
        start_wall = _to_wall_ns(attempt.get("started_monotonic_ns"), offset_ns)
        end_wall = _to_wall_ns(attempt.get("ended_monotonic_ns"), offset_ns)
        stats = {}
        for role, values in selected.items():
            window = [
                (timestamp, value)
                for timestamp, value in values
                if (start_wall is None or timestamp >= start_wall)
                and (end_wall is None or timestamp <= end_wall)
            ]
            stats[role] = _series_statistics(window)
        stage_rows.append(
            {
                "attempt_id": int(attempt["attempt_id"]),
                "test_id": str(attempt["test_id"]),
                "ordinal": int(attempt["ordinal"]),
                "status": str(attempt["status"]),
                "phase": attempt.get("phase"),
                "start_s": _relative_seconds(start_wall, origin_ns),
                "end_s": _relative_seconds(end_wall, origin_ns),
                "statistics": stats,
                "conclusion": _stage_conclusion(stats, fixture_available=bool(fixture)),
            }
        )

    missing = [role for role in _ROLES if not selected.get(role)]
    scenario_groups = _build_scenario_groups(stage_rows)
    return {
        "schema": "satellite-debug-tool/report-analysis",
        "schema_version": 1,
        "algorithm_version": "curve-analysis-v1",
        "time_basis": "host_wall_time_utc",
        "source_kind": source_kind if all_times else "unavailable",
        "role_sources": role_sources,
        "source_files": sources,
        "source_errors": errors,
        "origin_wall_time_ns": origin_ns,
        "clock_alignment": {
            "method": "event_wall_time_minus_monotonic_median",
            "offset_ns": offset_ns,
            "available": offset_ns is not None,
        },
        "series": frozen_series,
        "stages": stage_rows,
        "scenario_groups": scenario_groups,
        "cross_scenario_stages": [
            stage for stage in stage_rows if stage["test_id"] in _CROSS_SCENARIO_TEST_IDS
        ],
        "missing_series": missing,
        "ms6222_comparison_available": bool(fixture),
        "summary": _overall_summary(frozen_series, stage_rows, bool(fixture)),
    }


def render_report_charts(analysis: Mapping[str, Any]) -> dict[str, bytes]:
    """Render report PNGs with Qt, which is already part of the application bundle."""

    series = analysis.get("series", {})
    charts: dict[str, bytes] = {}
    snr_roles = [role for role in (CHANNEL_ROLE_SNR, "locked") if series.get(role, {}).get("points")]
    if snr_roles:
        charts["analysis/snr_timeline.png"] = _render_chart(
            series, snr_roles, "Full-process SNR and lock state", analysis.get("stages", [])
        )
    attitude_roles = [role for role in (CHANNEL_ROLE_ROLL, CHANNEL_ROLE_PITCH, CHANNEL_ROLE_YAW) if series.get(role, {}).get("points")]
    if attitude_roles:
        charts["analysis/attitude_timeline.png"] = _render_chart(
            series, attitude_roles, "Full-process device attitude", analysis.get("stages", [])
        )
    if series.get("attitude_error", {}).get("points"):
        charts["analysis/convergence_timeline.png"] = _render_chart(
            series, ["attitude_error"], "MS6222 attitude error and convergence trend", analysis.get("stages", [])
        )
    for scenario in analysis.get("scenario_groups", []):
        start = scenario.get("start_s")
        end = scenario.get("end_s")
        if start is None or end is None:
            continue
        window_series = _slice_frozen_series(series, float(start), float(end))
        base = f"analysis/scenarios/{scenario['scenario_id']}"
        scenario_stages = scenario.get("stages", [])
        scenario_snr = [
            role
            for role in (CHANNEL_ROLE_SNR, "locked")
            if window_series.get(role, {}).get("points")
        ]
        if scenario_snr:
            charts[f"{base}/snr.png"] = _render_chart(
                window_series,
                scenario_snr,
                f"{scenario['label']} SNR and lock state",
                scenario_stages,
            )
        scenario_attitude = [
            role
            for role in (
                CHANNEL_ROLE_ROLL,
                CHANNEL_ROLE_PITCH,
                CHANNEL_ROLE_YAW,
                "attitude_error",
            )
            if window_series.get(role, {}).get("points")
        ]
        if scenario_attitude:
            charts[f"{base}/attitude.png"] = _render_chart(
                window_series,
                scenario_attitude,
                f"{scenario['label']} attitude and MS6222 error",
                scenario_stages,
            )
    return charts


def _build_scenario_groups(stages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for scenario_id, label, test_ids in _SCENARIO_GROUPS:
        selected = [stage for stage in stages if stage["test_id"] in test_ids]
        starts = [float(stage["start_s"]) for stage in selected if stage.get("start_s") is not None]
        ends = [float(stage["end_s"]) for stage in selected if stage.get("end_s") is not None]
        if not selected:
            state = "not_enabled"
            conclusion = "本批次未启用该工况。"
        elif not starts or not ends:
            state = "time_window_missing"
            conclusion = "该工况已启用，但缺少可用于曲线切片的测试时间窗。"
        else:
            has_samples = any(
                stats.get("available")
                for stage in selected
                for stats in stage.get("statistics", {}).values()
            )
            state = "analyzed" if has_samples else "curve_data_missing"
            conclusions = [str(stage.get("conclusion", "")).strip() for stage in selected]
            conclusion = " ".join(value for value in conclusions if value)
            if not has_samples:
                conclusion = "该工况时间窗已记录，但窗口内没有可分析的过程曲线。 " + conclusion
        verdicts = {str(stage.get("status", "")).upper() for stage in selected}
        automatic_result = (
            "FAIL"
            if "FAIL" in verdicts
            else "INCOMPLETE"
            if verdicts & {"INCOMPLETE", "ABORTED"}
            else "PASS"
            if "PASS" in verdicts
            else "NOT_ENABLED"
        )
        groups.append(
            {
                "scenario_id": scenario_id,
                "label": label,
                "test_ids": list(test_ids),
                "state": state,
                "automatic_result": automatic_result,
                "start_s": min(starts) if starts else None,
                "end_s": max(ends) if ends else None,
                "stages": selected,
                "conclusion": conclusion,
            }
        )
    return groups


def _slice_frozen_series(
    series: Mapping[str, Any], start_s: float, end_s: float
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for role, payload in series.items():
        points = [
            point
            for point in payload.get("points", [])
            if start_s <= float(point[0]) <= end_s
        ]
        result[role] = {**dict(payload), "sample_count": len(points), "points": points}
    return result


def _legacy_role_ids(profile: Any) -> dict[int, str]:
    if not isinstance(profile, dict):
        return {}
    store = ProfileStore(cache=None)
    hardware_type = store.import_dict(profile)
    if not hardware_type:
        return {}
    result: dict[int, str] = {}
    for role in _ROLES:
        entry = store.find_channel_by_role(hardware_type, role)
        if entry is not None:
            result[int(entry.channel_id)] = role
    return result


def _wall_monotonic_offset(events: Sequence[Mapping[str, Any]]) -> int | None:
    offsets: list[int] = []
    for event in events:
        try:
            wall = datetime.fromisoformat(str(event["wall_time_utc"]).replace("Z", "+00:00"))
            offsets.append(int(wall.timestamp() * 1_000_000_000) - int(event["monotonic_ns"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return int(median(offsets)) if offsets else None


def _to_wall_ns(monotonic_ns: Any, offset_ns: int | None) -> int | None:
    if monotonic_ns is None or offset_ns is None:
        return None
    return int(monotonic_ns) + offset_ns


def _relative_seconds(value_ns: int | None, origin_ns: int | None) -> float | None:
    if value_ns is None or origin_ns is None:
        return None
    return round((value_ns - origin_ns) / 1_000_000_000.0, 6)


def _series_statistics(values: Sequence[tuple[int, float]]) -> dict[str, Any]:
    if not values:
        return {"available": False, "sample_count": 0}
    timestamps = np.asarray([item[0] for item in values], dtype=np.float64)
    samples = np.asarray([item[1] for item in values], dtype=np.float64)
    duration_s = max(0.0, float((timestamps[-1] - timestamps[0]) / 1_000_000_000.0))
    edge_count = max(1, min(len(samples), int(math.ceil(len(samples) * 0.2))))
    start_median = float(np.median(samples[:edge_count]))
    end_median = float(np.median(samples[-edge_count:]))
    delta = end_median - start_median
    slope = 0.0
    if len(samples) >= 2 and duration_s > 0:
        x = (timestamps - timestamps[0]) / 1_000_000_000.0
        slope = float(np.polyfit(x, samples, 1)[0])
    noise = float(np.std(samples))
    tolerance = max(0.05, noise * 0.25)
    trend = "stable" if abs(delta) <= tolerance else ("increasing" if delta > 0 else "decreasing")
    return {
        "available": True,
        "sample_count": int(len(samples)),
        "duration_s": round(duration_s, 6),
        "minimum": float(np.min(samples)),
        "maximum": float(np.max(samples)),
        "mean": float(np.mean(samples)),
        "median": float(np.median(samples)),
        "standard_deviation": noise,
        "start_median": start_median,
        "end_median": end_median,
        "delta": delta,
        "slope_per_second": slope,
        "trend": trend,
    }


def _stage_conclusion(stats: Mapping[str, Any], *, fixture_available: bool) -> str:
    parts: list[str] = []
    snr = stats.get(CHANNEL_ROLE_SNR, {})
    if snr.get("available"):
        direction = {"stable": "总体稳定", "increasing": "呈上升趋势", "decreasing": "呈下降趋势"}[snr["trend"]]
        parts.append(
            f"SNR {direction}，起始/末尾中位数 {snr['start_median']:.2f}/{snr['end_median']:.2f} dB，变化 {snr['delta']:+.2f} dB"
        )
    else:
        parts.append("SNR 数据缺失")
    error = stats.get("attitude_error", {})
    if error.get("available"):
        direction = error["trend"]
        convergence = "收敛" if direction == "decreasing" else ("发散" if direction == "increasing" else "稳定")
        parts.append(
            f"MS6222 姿态误差{convergence}，起始/末尾中位数 {error['start_median']:.2f}/{error['end_median']:.2f}°"
        )
    elif fixture_available:
        parts.append("该时间窗没有 MS6222 姿态误差样本")
    else:
        parts.append("缺少同期 MS6222 比较数据，不能判定姿态精度或收敛/发散")
    parts.append("未配置曲线判定阈值，趋势结论仅作数据描述")
    return "；".join(parts) + "。"


def _overall_summary(
    series: Mapping[str, Any], stages: Sequence[Mapping[str, Any]], fixture_available: bool
) -> str:
    available = [str(item.get("label", role)) for role, item in series.items() if item.get("sample_count")]
    if not available:
        return "未提取到可分析的过程曲线，报告结论只能依据已记录测试项和指标。"
    status_counts: dict[str, int] = {}
    snr_trends: dict[str, int] = {"stable": 0, "increasing": 0, "decreasing": 0}
    error_trends: dict[str, int] = {"stable": 0, "increasing": 0, "decreasing": 0}
    for stage in stages:
        status = str(stage.get("status", "unknown")).upper()
        status_counts[status] = status_counts.get(status, 0) + 1
        snr = stage.get("statistics", {}).get(CHANNEL_ROLE_SNR, {})
        if snr.get("available") and snr.get("trend") in snr_trends:
            snr_trends[str(snr["trend"])] += 1
        error = stage.get("statistics", {}).get("attitude_error", {})
        if error.get("available") and error.get("trend") in error_trends:
            error_trends[str(error["trend"])] += 1
    result_text = "、".join(f"{key} {value} 项" for key, value in sorted(status_counts.items())) or "无测试项"
    trend_text = (
        f"SNR 阶段趋势：稳定 {snr_trends['stable']} 项、上升 {snr_trends['increasing']} 项、下降 {snr_trends['decreasing']} 项。"
    )
    text = (
        "已分析全程曲线："
        + "、".join(available)
        + f"；覆盖 {len(stages)} 个测试项时间窗，自动结果为 {result_text}。"
        + trend_text
    )
    if fixture_available:
        return (
            text
            + " MS6222 姿态误差阶段趋势："
            + f"稳定 {error_trends['stable']} 项、收敛 {error_trends['decreasing']} 项、发散 {error_trends['increasing']} 项。"
        )
    return text + " 当前无同期 MS6222 比较数据，设备姿态曲线仅表示设备输出，不能作为姿态精度结论。"


def _load_fixture_comparison(root: Path, offset_ns: int | None) -> list[tuple[int, float]]:
    if offset_ns is None:
        return []
    candidates = sorted(root.rglob("attitude_comparison.csv"))
    if not candidates:
        return []
    result: list[tuple[int, float]] = []
    for path in candidates:
        try:
            with path.open("r", encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    if str(row.get("valid", "true")).lower() in {"false", "0"}:
                        continue
                    monotonic_ns = int(row["monotonic_ns"])
                    error = float(row["error_angle_deg"])
                    if math.isfinite(error):
                        result.append((monotonic_ns + offset_ns, error))
        except (OSError, KeyError, TypeError, ValueError):
            continue
    return sorted(result)


def _downsample_extrema(values: Sequence[tuple[int, float]], limit: int) -> list[tuple[int, float]]:
    if len(values) <= limit:
        return list(values)
    bucket_count = max(1, limit // 2)
    result: list[tuple[int, float]] = []
    for chunk in np.array_split(np.arange(len(values)), bucket_count):
        if len(chunk) == 0:
            continue
        bucket = [values[int(index)] for index in chunk]
        low = min(bucket, key=lambda item: item[1])
        high = max(bucket, key=lambda item: item[1])
        result.extend(sorted({low, high}, key=lambda item: item[0]))
    return result[:limit]


def _render_chart(
    series: Mapping[str, Any], roles: Sequence[str], title: str, stages: Sequence[Mapping[str, Any]]
) -> bytes:
    width, height = 1600, 720
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setFont(QFont("Arial", 18))
    painter.setPen(QColor("#263238"))
    painter.drawText(QRectF(80, 20, 1440, 40), title)
    plot = QRectF(105, 85, 1425, 540)
    points_by_role = {
        role: [(float(x), float(y)) for x, y in series[role]["points"]]
        for role in roles
        if series.get(role, {}).get("points")
    }
    all_points = [point for values in points_by_role.values() for point in values]
    if not all_points:
        painter.end()
        return b""
    xmin, xmax = min(x for x, _ in all_points), max(x for x, _ in all_points)
    ymin, ymax = min(y for _, y in all_points), max(y for _, y in all_points)
    if xmax <= xmin:
        xmax = xmin + 1.0
    if ymax <= ymin:
        pad = max(1.0, abs(ymin) * 0.05)
        ymin, ymax = ymin - pad, ymax + pad
    else:
        pad = (ymax - ymin) * 0.08
        ymin, ymax = ymin - pad, ymax + pad

    for index, stage in enumerate(stages):
        start = stage.get("start_s")
        end = stage.get("end_s")
        if start is None or end is None or end < xmin or start > xmax:
            continue
        x1 = _scale(float(start), xmin, xmax, plot.left(), plot.right())
        x2 = _scale(float(end), xmin, xmax, plot.left(), plot.right())
        color = QColor("#E3F2FD" if index % 2 == 0 else "#FFF8E1")
        painter.fillRect(QRectF(x1, plot.top(), max(1.0, x2 - x1), plot.height()), color)

    painter.setFont(QFont("Arial", 13))
    for index in range(6):
        fraction = index / 5.0
        y = plot.bottom() - fraction * plot.height()
        value = ymin + fraction * (ymax - ymin)
        painter.setPen(QPen(QColor("#ECEFF1"), 1))
        painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        painter.setPen(QColor("#455A64"))
        painter.drawText(QRectF(5, y - 12, 92, 24), 0x0002 | 0x0080, f"{value:.2f}")
    for index in range(6):
        fraction = index / 5.0
        x = plot.left() + fraction * plot.width()
        value = xmin + fraction * (xmax - xmin)
        painter.setPen(QColor("#455A64"))
        painter.drawText(QRectF(x - 45, plot.bottom() + 8, 90, 25), 0x0004 | 0x0080, f"{value:.1f}")
    painter.setPen(QPen(QColor("#607D8B"), 2))
    painter.drawRect(plot)
    painter.drawText(QRectF(plot.left(), 650, plot.width(), 30), 0x0004 | 0x0080, "Elapsed time (s)")

    legend_x = plot.left()
    for role, values in points_by_role.items():
        color = _COLORS[role]
        path = QPainterPath()
        for index, (x_value, y_value) in enumerate(values):
            x = _scale(x_value, xmin, xmax, plot.left(), plot.right())
            y = _scale(y_value, ymin, ymax, plot.bottom(), plot.top())
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setPen(QPen(color, 3))
        painter.drawPath(path)
        painter.drawLine(QPointF(legend_x, 57), QPointF(legend_x + 35, 57))
        painter.setPen(QColor("#263238"))
        label = f"{series[role]['label']} ({series[role]['unit']})"
        painter.drawText(QRectF(legend_x + 42, 44, 210, 28), label)
        legend_x += 245
    painter.end()
    byte_array = QByteArray()
    buffer = QBuffer(byte_array)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(byte_array)


def _scale(value: float, minimum: float, maximum: float, out_min: float, out_max: float) -> float:
    return out_min + (value - minimum) / (maximum - minimum) * (out_max - out_min)


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["build_report_analysis", "render_report_charts"]
