"""M48 批次布局、多语言与启动准备生命周期回归。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.production import BatchStatus, ProductionRecipe, ProductionResultStore
from satellite_debug_tool.core.production.start_preparation import PreparationState
from satellite_debug_tool.i18n import initialize_translation_manager, tr
from satellite_debug_tool.tests.test_production_recipe import valid_recipe
from satellite_debug_tool.ui.production_workspace import ProductionWorkspace


@pytest.fixture
def language_manager(qapplication_session, monkeypatch):
    """切换真实 QM；退出时恢复原偏好和环境覆盖。"""
    manager = initialize_translation_manager(qapplication_session)
    original_preference = manager.preference
    original_override = os.environ.get("SATELLITE_DEBUG_LOCALE")
    monkeypatch.delenv("SATELLITE_DEBUG_LOCALE", raising=False)
    try:
        manager.set_preference("zh_CN")
        yield manager
    finally:
        if original_override is None:
            monkeypatch.delenv("SATELLITE_DEBUG_LOCALE", raising=False)
        else:
            monkeypatch.setenv("SATELLITE_DEBUG_LOCALE", original_override)
        manager.set_preference(original_preference)
        qapplication_session.processEvents()


def _workspace_with_ready_batch(tmp_path: Path, monkeypatch, *, count: int = 1) -> ProductionWorkspace:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    settings.set("production.discovery_cidr", "127.0.0.1/32")
    settings.set("production.local_port", 0)
    workspace = ProductionWorkspace(settings)
    payload = valid_recipe()
    payload["target_device_count"] = count
    recipe = ProductionRecipe.from_mapping(payload)
    store = ProductionResultStore(tmp_path / "layout.sqlite3")
    batch = store.create_batch("BATCH-LAYOUT", recipe, operator="QA", output_dir=tmp_path / "output")
    workspace._batch_coordinator._store = store
    workspace._batch_coordinator._batch = batch
    workspace._recipe = recipe
    workspace._refresh_recipe_presentation()
    workspace._refresh_batch_state()
    return workspace


def _close_workspace(workspace: ProductionWorkspace, qapplication_session) -> None:
    workspace.close()
    workspace.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapplication_session.processEvents()


def test_power_preparation_and_admission_keep_real_reason_across_languages(
    qapplication_session, language_manager, tmp_path, monkeypatch,
):
    workspace = _workspace_with_ready_batch(tmp_path, monkeypatch)
    try:
        workspace._preparation.begin()
        workspace.retranslate_ui()
        preparing_source = "Production power preparation started."
        assert workspace._execution_summary.text() == tr(preparing_source)
        assert workspace._execution_summary.text() != preparing_source
        assert workspace._progress_panel.remaining_label.text() == "—"

        language_manager.set_preference("en_US")
        assert workspace._preparation.state is PreparationState.PREPARING_POWER
        assert workspace._execution_summary.text() == preparing_source
        workspace._preparation.confirm_power()
        assert workspace._preparation.state is PreparationState.WAITING_DEVICES
        admission_source = (
            "Exactly {expected} identified {product} device(s) are required; currently ready: {actual}."
        )
        english_reason = admission_source.format(expected=1, product="AFD01", actual=0)
        assert workspace._execution_summary.text() == (
            "Power ready. Waiting for device admission: " + english_reason
        )

        language_manager.set_preference("zh_CN")
        chinese_reason = tr(admission_source, expected=1, product="AFD01", actual=0)
        assert chinese_reason != english_reason
        assert workspace._execution_summary.text() == tr(
            "Power ready. Waiting for device admission: {reason}", reason=chinese_reason,
        )
        assert workspace._preparation.state is PreparationState.WAITING_DEVICES
        assert workspace._progress_panel.remaining_label.text() == "—"
        language_manager.set_preference("en_US")
        assert workspace._execution_summary.text().endswith(english_reason)
        assert "has not started" not in workspace._execution_summary.text()
    finally:
        _close_workspace(workspace, qapplication_session)


@pytest.mark.parametrize("status,source", [
    (BatchStatus.READY, "Batch ready"),
    (BatchStatus.RUNNING, "Batch running"),
    (BatchStatus.INCOMPLETE, "Batch incomplete"),
    (BatchStatus.ABORTED, "Batch aborted"),
    (BatchStatus.COMPLETED, "Batch completed"),
])
def test_batch_status_retranslates_from_store_fact(
    qapplication_session, language_manager, tmp_path, monkeypatch, status, source,
):
    workspace = _workspace_with_ready_batch(tmp_path, monkeypatch)
    try:
        store = workspace.result_store
        if status in (BatchStatus.RUNNING, BatchStatus.INCOMPLETE, BatchStatus.COMPLETED):
            store.transition_batch("BATCH-LAYOUT", BatchStatus.RUNNING)
        if status in (BatchStatus.INCOMPLETE, BatchStatus.ABORTED, BatchStatus.COMPLETED):
            store.transition_batch("BATCH-LAYOUT", status)
        workspace._batch_coordinator._batch = store.get_batch("BATCH-LAYOUT")
        workspace._refresh_batch_state()
        chinese_text = workspace._batch_state.text()
        assert chinese_text == tr(source)
        assert chinese_text != source
        language_manager.set_preference("en_US")
        assert workspace._batch_state.text() == source
        assert workspace.batch["status"] == status.value
        language_manager.set_preference("zh_CN")
        assert workspace._batch_state.text() == chinese_text
    finally:
        _close_workspace(workspace, qapplication_session)


def test_four_charts_keep_minimum_height_in_1280_by_800_workspace(
    qapplication_session, tmp_path, monkeypatch,
):
    workspace = _workspace_with_ready_batch(tmp_path, monkeypatch, count=4)
    try:
        workspace.resize(1280, 800)
        workspace.show()
        qapplication_session.processEvents()
        assert workspace.width() == 1280
        assert workspace.height() == 800
        assert workspace._devices_layout.count() == 4
        assert workspace._visible_chart_slots == (None, None, None, None)
        for position in range(4):
            panel = workspace._devices_layout.itemAt(position).widget()
            assert panel.minimumHeight() >= 180
            assert panel.height() >= 180
            assert panel._plot.geometry().top() > panel._test_label.geometry().bottom()
            assert panel._plot.height() >= 90
        assert workspace._devices_layout.getItemPosition(3) == (1, 1, 1, 1)
        scrollbar = workspace._charts_scroll.verticalScrollBar()
        assert scrollbar.maximum() > 0
        scrollbar.setValue(scrollbar.maximum())
        qapplication_session.processEvents()
        assert workspace._abort_button.isVisible()
        assert workspace._abort_button.geometry().bottom() <= workspace._abort_button.parentWidget().height()
        assert workspace._progress_panel.isVisible()
    finally:
        _close_workspace(workspace, qapplication_session)


@pytest.mark.parametrize("count", (1, 2))
def test_running_chart_uses_available_height_without_scrolling_whole_page(
    qapplication_session, tmp_path, monkeypatch, count,
):
    """图表偏好高度不能把事件区推到首屏之外，实际卡片保留可读尺寸。"""
    from satellite_debug_tool.ui import qss

    workspace = _workspace_with_ready_batch(tmp_path, monkeypatch, count=count)
    original_stylesheet = qapplication_session.styleSheet()
    try:
        qapplication_session.setStyleSheet(qss.build("light", "small"))
        workspace.set_theme("light")
        workspace._progress_panel.set_progress(
            title_source="Static acquisition", phase_source="Effective observation",
            ordinal=3, total=len(workspace._progress_panel.node_ids), elapsed_s=173,
            remaining_s=1627,
        )
        workspace.resize(1280, 800)
        workspace.show()
        qapplication_session.processEvents()
        assert workspace._batch_scroll.verticalScrollBar().maximum() == 0
        assert workspace._charts_scroll.verticalScrollBar().maximum() == 0
        assert workspace._latest_event.isVisibleTo(workspace)
        assert workspace._abort_button.isVisibleTo(workspace)
        for index in range(count):
            panel = workspace._devices_layout.itemAt(index).widget()
            assert panel.height() >= 180
            assert panel._plot.height() >= 90
        workspace._details_button.setChecked(True)
        workspace._events_button.setChecked(True)
        qapplication_session.processEvents()
        assert workspace.size().height() == 800
        assert workspace._abort_button.mapTo(workspace, workspace._abort_button.rect().bottomRight()).y() < 800
    finally:
        _close_workspace(workspace, qapplication_session)
        qapplication_session.setStyleSheet(original_stylesheet)
