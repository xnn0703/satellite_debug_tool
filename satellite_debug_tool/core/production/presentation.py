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
MANDATORY_TEST_IDS: frozenset[str] = frozenset(TEST_ORDER[:2])
SCENARIO_TEST_IDS: tuple[str, ...] = TEST_ORDER[2:7]
SUMMARY_TEST_IDS: tuple[str, ...] = TEST_ORDER[7:]

_BENCH_TEST_IDS: frozenset[str] = frozenset(SCENARIO_TEST_IDS[:3])
_VEHICLE_TEST_IDS: frozenset[str] = frozenset(SCENARIO_TEST_IDS[3:])


def enabled_production_tests(recipe: ProductionRecipe | None) -> tuple[str, ...]:
    """按正式流程顺序投影启用项，整机汇总由场景窗口派生。

    固件和参数校验是每份配方的固定入口。未知的显式启用项保留在
    已知流程之后，供界面和执行证据明确呈现其待确认状态。
    """
    if recipe is None:
        return ()

    tests = recipe.payload["tests"]
    explicit = {
        test_id for test_id, config in tests.items() if config["enabled"]
    }
    enabled = explicit | MANDATORY_TEST_IDS
    if explicit.intersection(SCENARIO_TEST_IDS):
        enabled.add("whole_navigation")
    else:
        enabled.discard("whole_navigation")

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
