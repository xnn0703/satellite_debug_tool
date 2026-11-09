"""两阶段批次：台架完成挂起、恢复续测车辆段的协调层合同。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.production import (
    AttemptStatus,
    BatchStatus,
    ProductionRecipe,
    ProductionResultStore,
    ResultStoreError,
)
from satellite_debug_tool.core.production.coordinators import BatchCoordinator
from satellite_debug_tool.core.production.fixture_profile import FixtureControlLease
from satellite_debug_tool.core.production.run_coordinator import ProductionRunState
from satellite_debug_tool.tests.test_production_recipe import valid_recipe


def _bind(coordinator: BatchCoordinator, store, output: Path) -> None:
    """模拟 create() 之后的绑定状态（既有测试惯例：直连内部事实）。"""

    coordinator._store = store
    coordinator._batch = store.get_batch("BATCH-RESUME")
    coordinator._output_dir = output


def _bench_finished_store(tmp_path: Path) -> ProductionResultStore:
    payload = valid_recipe()
    payload["tests"]["locked_drive"] = {"enabled": True, "duration_s": 30}
    payload["tests"]["power_on_drive"] = {"enabled": True, "duration_s": 30}
    store = ProductionResultStore(tmp_path / "batch.sqlite3")
    store.create_batch(
        "BATCH-RESUME",
        ProductionRecipe.from_mapping(payload),
        operator="测试员甲",
        output_dir=tmp_path / "out",
    )
    serial = "AFD01C-001"
    store.register_device(
        "BATCH-RESUME",
        serial_number=serial,
        slot=1,
        endpoint_ip="192.168.1.12",
        endpoint_port=4004,
        hardware_type="AFD01C",
        device_uid="111111112222222233333333",
        mac_address="02:00:00:00:00:01",
        metadata={},
    )
    store.start_batch(
        "BATCH-RESUME",
        (serial,),
        ("static_acquisition", "locked_rocking", "power_on_rocking",
         "locked_drive", "power_on_drive"),
    )
    for attempt in store.list_attempts("BATCH-RESUME"):
        if attempt["test_id"] in ("locked_drive", "power_on_drive"):
            if attempt["test_id"] == "locked_drive":
                for status in (
                    AttemptStatus.WAITING_PREREQUISITE, AttemptStatus.ARMED,
                    AttemptStatus.RUNNING, AttemptStatus.ANALYZING, AttemptStatus.PASS,
                ):
                    store.transition_attempt(
                        int(attempt["attempt_id"]), status,
                        result={"reason": "sample"} if status is AttemptStatus.PASS else None,
                    )
            continue
        for status in (
            AttemptStatus.WAITING_PREREQUISITE, AttemptStatus.ARMED,
            AttemptStatus.RUNNING, AttemptStatus.ANALYZING, AttemptStatus.PASS,
        ):
            store.transition_attempt(
                int(attempt["attempt_id"]), status,
                result={"reason": "sample"} if status is AttemptStatus.PASS else None,
            )
    return store


def test_bench_completion_suspends_batch_and_resume_completes(tmp_path: Path) -> None:
    """台架全终态 + 车辆 PENDING → complete 自动挂起；恢复补终态后才转完成。"""
    output = tmp_path / "out"
    store = _bench_finished_store(tmp_path)
    coordinator = BatchCoordinator(FixtureControlLease())
    _bind(coordinator, store, output)
    try:
        suspended = coordinator.complete()
        assert suspended["status"] == BatchStatus.SUSPENDED.value
        statuses = {
            row["test_id"]: row["status"]
            for row in store.list_attempts("BATCH-RESUME")
        }
        assert statuses["locked_drive"] == AttemptStatus.PASS.value
        assert statuses["power_on_drive"] == AttemptStatus.PENDING.value
        assert statuses["static_acquisition"] == AttemptStatus.PASS.value

        # 挂起批次不能跳过车载段直接完成。
        with pytest.raises(ResultStoreError):
            store.transition_batch("BATCH-RESUME", BatchStatus.COMPLETED)

        resumed = coordinator.resume_vehicle_phase()
        assert resumed["status"] == BatchStatus.RUNNING.value

        for attempt in store.list_attempts("BATCH-RESUME"):
            if attempt["test_id"] == "power_on_drive":
                for status in (
                    AttemptStatus.WAITING_PREREQUISITE, AttemptStatus.ARMED,
                    AttemptStatus.RUNNING, AttemptStatus.ANALYZING, AttemptStatus.PASS,
                ):
                    store.transition_attempt(
                        int(attempt["attempt_id"]), status,
                        result={"reason": "sample"} if status is AttemptStatus.PASS else None,
                    )
        completed = coordinator.complete()
        assert completed["status"] == BatchStatus.COMPLETED.value
    finally:
        coordinator.close()


def test_resume_requires_suspended_and_rejects_active_workspace(tmp_path: Path) -> None:
    """恢复只接受挂起批次；工作区已有活动批次时拒绝接续。"""
    store = _bench_finished_store(tmp_path)
    coordinator = BatchCoordinator(FixtureControlLease())
    _bind(coordinator, store, tmp_path / "out")
    try:
        coordinator.complete()
        assert coordinator._batch["status"] == BatchStatus.SUSPENDED.value

        # 已绑定批次的工作区不能再接续；空工作区恢复挂起批次成功。
        with pytest.raises(ResultStoreError):
            coordinator.adopt_suspended(
                store=store, batch_id="BATCH-RESUME", output_dir=tmp_path / "out"
            )
        restored = BatchCoordinator(FixtureControlLease())
        try:
            batch = restored.adopt_suspended(
                store=store, batch_id="BATCH-RESUME", output_dir=tmp_path / "out"
            )
            assert batch["status"] == BatchStatus.SUSPENDED.value
            assert restored._output_dir == tmp_path / "out"
        finally:
            restored.close()
    finally:
        coordinator.close()


def _drive_store(tmp_path: Path, *, pending: str, completed: str) -> ProductionResultStore:
    """车辆段单待测项批次：pending 项保持 PENDING，completed 项预置 PASS。"""
    payload = valid_recipe()
    payload["tests"]["locked_drive"] = {"enabled": True, "duration_s": 30}
    payload["tests"]["power_on_drive"] = {"enabled": True, "duration_s": 30}
    store = ProductionResultStore(tmp_path / "batch.sqlite3")
    store.create_batch(
        "BATCH-RESUME", ProductionRecipe.from_mapping(payload),
        operator="测试员甲", output_dir=tmp_path / "out",
    )
    serial = "AFD01C-001"
    store.register_device(
        "BATCH-RESUME", serial_number=serial, slot=1,
        endpoint_ip="192.168.1.12", endpoint_port=4004, hardware_type="AFD01C",
        device_uid="111111112222222233333333", mac_address="02:00:00:00:00:01",
        metadata={},
    )
    store.start_batch("BATCH-RESUME", (serial,), ("locked_drive", "power_on_drive"))
    for attempt in store.list_attempts("BATCH-RESUME"):
        target = AttemptStatus.PASS if attempt["test_id"] == completed else None
        if target is None:
            continue
        for status in (
            AttemptStatus.WAITING_PREREQUISITE, AttemptStatus.ARMED,
            AttemptStatus.RUNNING, AttemptStatus.ANALYZING, AttemptStatus.PASS,
        ):
            store.transition_attempt(
                int(attempt["attempt_id"]), status,
                result={"reason": "sample"} if status is target else None,
            )
    return store


def _vehicle_workspace(tmp_path, store):
    import time as time_module
    import pytest as _pytest
    from satellite_debug_tool.core.config import Settings as _Settings
    from satellite_debug_tool.core.production import ProductionRecipe
    from satellite_debug_tool.core.production.execution import ProductionExecution
    from satellite_debug_tool.ui.production_workspace import ProductionWorkspace

    with _pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(tmp_path / "home"))
        ws_settings = _Settings()
    workspace = ProductionWorkspace(ws_settings)
    workspace.append_event = lambda message: None

    class _Core(QObject):
        record_received = Signal(object)

    core = _Core()
    core.generation = 1
    core.product_store = SimpleNamespace(telemetry_epoch=1)
    session = SimpleNamespace(
        serial_number="AFD01C-001", is_online=lambda: True, capture_ready=True,
        core=core, endpoint=("127.0.0.1", 4004),
    )
    payload = valid_recipe()
    payload["tests"]["locked_drive"] = {"enabled": True, "duration_s": 30}
    payload["tests"]["power_on_drive"] = {"enabled": True, "duration_s": 30}
    # 真实时钟：与 time.monotonic_ns() 同源，上电锚点的 min() 语义与生产一致。
    execution = ProductionExecution(
        store, "BATCH-RESUME", ProductionRecipe.from_mapping(payload),
        SimpleNamespace(send=lambda *a, **k: True), [session],
        clock=time_module.monotonic,
    )
    workspace._execution = execution
    # 镜像 _start_execution(vehicle_phase=True) 的车辆段接管：无台架夹具依赖。
    execution.fixture_ready = lambda: (True, "")
    return workspace, execution, session, time_module


def test_vehicle_locked_drive_confirms_immediately(qapplication_session, tmp_path):
    """行驶项：硬件请求即确认，窗口启动、判定器为姿态模式。"""
    store = _drive_store(tmp_path, pending="locked_drive", completed="power_on_drive")
    coordinator = BatchCoordinator(FixtureControlLease())
    coordinator._store = store
    coordinator._batch = store.get_batch("BATCH-RESUME")
    coordinator._output_dir = tmp_path / "out"
    coordinator.complete()
    coordinator.resume_vehicle_phase()
    workspace, execution, _session, _time = _vehicle_workspace(tmp_path, store)
    try:
        execution.start()
        request = SimpleNamespace(test_id="locked_drive", batch_id="BATCH-RESUME",
                                  participant_serials=("AFD01C-001",), ordinal=1)
        workspace._on_vehicle_hardware_requested(request)
        assert execution.run.state is ProductionRunState.RUNNING
        assert execution._hardware_ready
        assert execution._evaluators["AFD01C-001"].attitude_mode
    finally:
        execution.stop()
        coordinator.close()
        store.close()


def test_vehicle_power_on_drive_awaits_offline_then_online(qapplication_session, tmp_path):
    """动态上电项：等全离线再等全回在线，确认锚点为回在线时刻。"""
    store = _drive_store(tmp_path, pending="power_on_drive", completed="locked_drive")
    coordinator = BatchCoordinator(FixtureControlLease())
    coordinator._store = store
    coordinator._batch = store.get_batch("BATCH-RESUME")
    coordinator._output_dir = tmp_path / "out"
    coordinator.complete()
    coordinator.resume_vehicle_phase()
    workspace, execution, session, time_module = _vehicle_workspace(tmp_path, store)
    try:
        execution.start()
        request = SimpleNamespace(test_id="power_on_drive", batch_id="BATCH-RESUME",
                                  participant_serials=("AFD01C-001",), ordinal=1)
        workspace._on_vehicle_hardware_requested(request)
        assert execution._power_cycle_pending
        assert workspace._vehicle_power_await_off is True

        session.is_online = lambda: False
        workspace._maybe_confirm_vehicle_power_cycle()
        assert workspace._vehicle_power_await_on is True
        assert execution._power_cycle_pending

        session.is_online = lambda: True
        before = time_module.monotonic_ns()
        workspace._maybe_confirm_vehicle_power_cycle()
        after = time_module.monotonic_ns()
        # 全在线且采集就绪后窗口立即启动：锚点经恢复供电时刻进入判定器。
        assert execution._power_cycle_pending is False
        assert execution.run.state is ProductionRunState.RUNNING
        anchor_s = execution._evaluators["AFD01C-001"].power_on_monotonic_s
        anchor_ns = anchor_s * 1e9
        assert before - 10**6 <= anchor_ns <= after + 10**6
    finally:
        execution.stop()
        coordinator.close()
        store.close()


def test_retest_verification_replays_failed_item(tmp_path: Path) -> None:
    """核验补测：INCOMPLETE 批次回到运行态，新建 attempt 完成后批次完成。"""
    store = ProductionResultStore(tmp_path / "batch.sqlite3")
    payload = valid_recipe()
    payload["tests"] = {
        "parameter_verification": {"enabled": True},
        "static_acquisition": {"enabled": True, "duration_s": 600},
    }
    store.create_batch(
        "BATCH-RETEST", ProductionRecipe.from_mapping(payload),
        operator="测试员甲", output_dir=tmp_path / "out",
    )
    serial = "AFD01C-001"
    store.register_device("BATCH-RETEST", serial_number=serial, slot=1)
    store.start_batch("BATCH-RETEST", (serial,),
                      ("parameter_verification", "static_acquisition"))
    for attempt in store.list_attempts("BATCH-RETEST"):
        verdict = AttemptStatus.FAIL if attempt["test_id"] == "parameter_verification" else AttemptStatus.PASS
        for status in (
            AttemptStatus.WAITING_PREREQUISITE, AttemptStatus.ARMED,
            AttemptStatus.RUNNING, AttemptStatus.ANALYZING, verdict,
        ):
            store.transition_attempt(
                int(attempt["attempt_id"]), status,
                result={"reason": "sample"} if status is verdict else None,
            )
    store.transition_batch("BATCH-RETEST", BatchStatus.INCOMPLETE)
    coordinator = BatchCoordinator(FixtureControlLease())
    coordinator._store = store
    coordinator._batch = store.get_batch("BATCH-RETEST")
    coordinator._output_dir = tmp_path / "out"
    try:
        resumed = coordinator.resume_retest()
        assert resumed["status"] == BatchStatus.RUNNING.value
        store.create_attempt("BATCH-RETEST", serial, "parameter_verification")
        new_attempt = [
            row for row in store.list_attempts("BATCH-RETEST")
            if row["test_id"] == "parameter_verification" and row["status"] == AttemptStatus.PENDING.value
        ][0]
        for status in (
            AttemptStatus.WAITING_PREREQUISITE, AttemptStatus.ARMED,
            AttemptStatus.RUNNING, AttemptStatus.ANALYZING, AttemptStatus.PASS,
        ):
            store.transition_attempt(
                int(new_attempt["attempt_id"]), status,
                result={"reason": "retest pass"} if status is AttemptStatus.PASS else None,
            )
        completed = coordinator.complete()
        assert completed["status"] == BatchStatus.COMPLETED.value
    finally:
        coordinator.close()
        store.close()
