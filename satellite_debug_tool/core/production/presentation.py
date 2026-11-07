"""批次页面的纯领域投影：测试顺序、夹具相关性和设备图槽位。"""

from __future__ import annotations

from typing import Sequence

from satellite_debug_tool.core.production.recipe import ProductionRecipe


TEST_ORDER: tuple[str, ...] = (
    "firmware_verification",
    "parameter_verification",
    "static_acquisition",
    "locked_rocking",
    "power_on_rocking",
    "locked_drive",
    "power_on_drive",
    "gnss",
    "imu_static",
    "external_ins",
    "whole_navigation",
)
SCENARIO_TEST_IDS: tuple[str, ...] = TEST_ORDER[2:7]
SUMMARY_TEST_IDS: tuple[str, ...] = TEST_ORDER[7:]

_BENCH_TEST_IDS: frozenset[str] = frozenset(SCENARIO_TEST_IDS[:3])
_VEHICLE_TEST_IDS: frozenset[str] = frozenset(SCENARIO_TEST_IDS[3:])

_TRACKING_REASON_LABELS = {
    "invalid_tracking_sample": "跟踪状态或 SNR 样本无效",
    "tracking_samples_missing": "缺少跟踪样本",
    "valid_tracking_samples_missing": "缺少有效跟踪样本",
    "observation_start_evidence_missing": "观察窗口起点采样证据缺失",
    "observation_end_evidence_missing": "观察窗口终点采样证据缺失",
    "tracking_sample_gap": "采样间隔超过允许值",
    "acquisition_evidence_incomplete": "搜星阶段证据不完整",
    "acquisition_timeout": "超过搜星时限仍未建立稳定锁定",
    "attitude_reference_missing": "MS-6222 姿态参考缺失或过期",
    "interruption_count_exceeded": "中断次数超过允许值",
    "interruption_duration_exceeded": "单次中断时长超过允许值",
    "interruption_ratio_exceeded": "累计中断时间占比超过允许值",
}


def tracking_reason_text(reason: object) -> str:
    """以统一中文文案呈现判定器原因，未知原因保留原值。"""
    text = str(reason)
    return _TRACKING_REASON_LABELS.get(text, text)


def enabled_production_tests(recipe: ProductionRecipe | None) -> tuple[str, ...]:
    """按流程顺序呈现配方显式启用项；未知项保留其待确认状态。"""
    if recipe is None:
        return ()

    tests = recipe.payload["tests"]
    enabled = {
        test_id for test_id, config in tests.items() if config["enabled"]
    }

    known = tuple(test_id for test_id in TEST_ORDER if test_id in enabled)
    unknown = tuple(sorted(enabled.difference(TEST_ORDER)))
    return known + unknown


def fixture_requirements(recipe: ProductionRecipe | None) -> frozenset[str]:
    """返回当前配方已声明工况需要的夹具类别。

    台架工况以平台反馈和 MS-6222 参考作为启动证据；车辆工况以操作员
    门禁作为启动证据。导航汇总消费各自窗口，不额外声明参考夹具。
    """
    if recipe is None:
        return frozenset()

    enabled = frozenset(enabled_production_tests(recipe))
    required = {"power"}
    if enabled.intersection(_BENCH_TEST_IDS):
        required.update(("motion", "reference"))
    if enabled.intersection(_VEHICLE_TEST_IDS):
        required.add("vehicle")
    return frozenset(required)


def chart_slots(
    target_count: int,
    candidate_slots: Sequence[int],
    participant_slots: Sequence[int] | None = None,
) -> tuple[int | None, ...]:
    """选择实际设备图，未绑定的位置以 ``None`` 独立占位。

    冻结参与者拥有最终设备身份，保留输入顺序和全部实际槽位。冻结前
    按候选优先级稳定去重，取目标数量；本函数保持 endpoint 绑定语义。
    """
    if isinstance(target_count, bool) or not isinstance(target_count, int) or not 1 <= target_count <= 4:
        raise ValueError("target_count must be an integer within 1..4")

    source = participant_slots if participant_slots is not None else candidate_slots
    slots: list[int] = []
    for slot in source:
        if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 4:
            raise ValueError("chart slots must be integers within 1..4")
        if slot not in slots:
            slots.append(slot)

    if participant_slots is not None:
        return tuple(slots)

    selected: tuple[int | None, ...] = tuple(slots[:target_count])
    return selected + (None,) * (target_count - len(selected))
