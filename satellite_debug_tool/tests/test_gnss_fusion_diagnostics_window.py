"""GNSS 融合实时诊断窗：kind2 schema2 逐包统计与忽略合同。"""
from __future__ import annotations

from satellite_debug_tool.core.protocol.frame_v2 import InsTraceRecord
from satellite_debug_tool.ui.gnss_fusion_diagnostics_window import GnssFusionDiagnosticsWindow


def _record(*, stage: int = 3, timestamp_us: int = 1_500_000, pre_filter_us: int = 1_502_000,
            timeline_reject: int = 0, pv_reject: int = 0, attempted: int = 0x05, accepted: int = 0x05,
            source_seq: int = 1, flags: int = 0x4, schema: int = 2, kind: int = 2) -> InsTraceRecord:
    values: dict[str, object] = {
        "processing_stage": stage, "timeline_reject": timeline_reject, "pv_reject": pv_reject,
        "pre_filter_state_us": pre_filter_us, "group_attempted_mask": attempted,
        "group_accepted_mask": accepted, "gps_week": 2400, "gps_tow_s": 100.5,
        "driver_received_timestamp_us": timestamp_us + 17_000, "velocity_type": 8,
        "velocity_latency_s": -0.004, "time_quality": 1,
        "pv_result_flags": 0x3FF, "filter_time_reject_count": 0,
        "group_status": (1, 0, 1, 0), "processing_complete_us": timestamp_us + 20_000,
        "lat_deg": 31.0, "lon_deg": 118.0,
    }
    return InsTraceRecord(schema=schema, kind=kind, source=3, health=1, seq=source_seq,
                          source_seq=source_seq, timestamp_us=timestamp_us,
                          received_timestamp_us=timestamp_us + 17_000, flags=flags, values=values)


def test_window_counts_stages_rejects_and_gate_deltas(qapplication_session):
    window = GnssFusionDiagnosticsWindow()
    window.feed_record(_record(stage=2, timeline_reject=8, timestamp_us=1_000_000))
    window.feed_record(_record(stage=3, source_seq=2, timestamp_us=1_100_000, pre_filter_us=1_102_000))
    window.feed_record(_record(stage=3, source_seq=3, timestamp_us=1_200_000, pre_filter_us=1_211_000))
    window.feed_record(_record(stage=0, source_seq=4, timestamp_us=1_300_000))

    # 无关记录（kind1 / schema1 kind2）必须被忽略。
    window.feed_record(_record(kind=1))
    window.feed_record(_record(schema=1))

    window._refresh()
    stats = window._stats_label.text()
    assert "融合摘要: 4 包" in stats
    assert "已执行准入 2" in stats
    assert "超前×1" in stats          # stage2 的 timeline FUTURE 拒因
    assert "超8ms 1" in stats         # 11ms 偏差计入超门
    assert "HP 100%" in stats and "VV —" in stats
    # kind1 忽略；schema1 帧入实测明细但不进融合摘要。
    assert window._samples == 4
    assert window._table.rowCount() == 5
    # 实测值卡显示最新包的速度与定位。
    assert window._velocity_card._values[0].text() == "有效"  # 最新包（schema1）flags 含速度位
    assert "—" not in window._position_card._values[2].text()


def test_window_reset_clears_statistics(qapplication_session):
    window = GnssFusionDiagnosticsWindow()
    window.feed_record(_record(stage=3))
    assert window._samples == 1
    window.reset_statistics()
    assert window._samples == 0
    assert window._table.rowCount() == 0
