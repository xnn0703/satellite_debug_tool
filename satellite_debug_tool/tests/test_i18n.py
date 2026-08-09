"""M16 internationalization foundation and runtime-retranslation tests."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CJK_RE = re.compile(r"[\u3400-\u9fff]")


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def i18n_context(qapp, tmp_path, monkeypatch):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.i18n import (
        LANGUAGE_EN_US,
        LANGUAGE_ZH_CN,
        initialize_translation_manager,
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SATELLITE_DEBUG_LOCALE", raising=False)
    settings = Settings()
    settings.set("ui.language", LANGUAGE_EN_US)
    manager = initialize_translation_manager(qapp, settings)
    manager.set_preference(LANGUAGE_EN_US)
    yield settings, manager
    manager.set_preference(LANGUAGE_ZH_CN)
    qapp.processEvents()


def test_locale_resolution_and_environment_override():
    from satellite_debug_tool.i18n import (
        LANGUAGE_AUTO,
        LANGUAGE_EN_US,
        LANGUAGE_ZH_CN,
        normalize_language_preference,
        resolve_effective_locale,
    )

    assert normalize_language_preference("zh-Hans") == LANGUAGE_ZH_CN
    assert normalize_language_preference("zh-TW") == LANGUAGE_ZH_CN
    assert normalize_language_preference("zh_HK") == LANGUAGE_ZH_CN
    assert normalize_language_preference("English") == LANGUAGE_EN_US
    assert normalize_language_preference("en-GB") == LANGUAGE_EN_US
    assert normalize_language_preference("unknown") == LANGUAGE_AUTO
    assert resolve_effective_locale(
        LANGUAGE_AUTO,
        system_locale="zh_TW",
        environment_override="",
    ) == LANGUAGE_ZH_CN
    assert resolve_effective_locale(
        LANGUAGE_AUTO,
        system_locale="en_GB",
        environment_override="",
    ) == LANGUAGE_EN_US
    assert resolve_effective_locale(
        LANGUAGE_ZH_CN,
        system_locale="zh_CN",
        environment_override="en_US",
    ) == LANGUAGE_EN_US


def test_legacy_active_tab_is_migrated_once(tmp_path, monkeypatch):
    from satellite_debug_tool.core.config import Settings

    monkeypatch.setenv("HOME", str(tmp_path))
    config_dir = tmp_path / ".satellite_debug_tool"
    config_dir.mkdir()
    config_path = config_dir / "settings.json"
    config_path.write_text(
        json.dumps({"ui": {"active_tab": "设备"}}),
        encoding="utf-8",
    )

    settings = Settings()

    assert settings.get("ui.active_tab_id") == "device"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["ui"]["active_tab_id"] == "device"
    assert "active_tab" not in saved["ui"]


def test_environment_override_does_not_persist(qapp, tmp_path, monkeypatch):
    from satellite_debug_tool.core.config import Settings
    from satellite_debug_tool.i18n import (
        LANGUAGE_EN_US,
        LANGUAGE_ZH_CN,
        initialize_translation_manager,
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    settings.set("ui.language", LANGUAGE_ZH_CN)
    settings.save()
    monkeypatch.setenv("SATELLITE_DEBUG_LOCALE", LANGUAGE_EN_US)

    manager = initialize_translation_manager(qapp, settings)

    assert manager.effective_locale == LANGUAGE_EN_US
    assert settings.get("ui.language") == LANGUAGE_ZH_CN
    persisted = json.loads(settings._config_file.read_text(encoding="utf-8"))
    assert persisted["ui"]["language"] == LANGUAGE_ZH_CN

    monkeypatch.delenv("SATELLITE_DEBUG_LOCALE")
    manager.apply_preference(settings.get("ui.language"))


def test_settings_language_cancel_and_accept(i18n_context, qapp):
    from satellite_debug_tool.i18n import LANGUAGE_EN_US, LANGUAGE_ZH_CN
    from satellite_debug_tool.ui.settings_dialog import SettingsDialog

    settings, manager = i18n_context
    dialog = SettingsDialog(settings)
    dialog._language_combo.setCurrentIndex(
        dialog._language_combo.findData(LANGUAGE_ZH_CN)
    )
    dialog.reject()

    assert settings.get("ui.language") == LANGUAGE_EN_US
    assert manager.effective_locale == LANGUAGE_EN_US

    dialog = SettingsDialog(settings)
    dialog._language_combo.setCurrentIndex(
        dialog._language_combo.findData(LANGUAGE_ZH_CN)
    )
    dialog._on_accept()
    qapp.processEvents()

    assert settings.get("ui.language") == LANGUAGE_ZH_CN
    assert manager.effective_locale == LANGUAGE_ZH_CN
    assert json.loads(settings._config_file.read_text(encoding="utf-8"))["ui"][
        "language"
    ] == LANGUAGE_ZH_CN
    assert type(settings)().get("ui.language") == LANGUAGE_ZH_CN


def test_english_numerus_fallback_and_chinese_catalog(i18n_context):
    from satellite_debug_tool.i18n import LANGUAGE_EN_US, LANGUAGE_ZH_CN, trn

    _settings, manager = i18n_context
    manager.set_preference(LANGUAGE_EN_US)
    assert trn("%n event(s)", 1) == "1 event"
    assert trn("%n event(s)", 2) == "2 events"

    manager.set_preference(LANGUAGE_ZH_CN)
    assert trn("%n event(s)", 2) == "2 个事件"


def test_runtime_switch_preserves_main_window_state(i18n_context, qapp):
    from satellite_debug_tool.core.protocol import ChannelSample, DataReport
    from satellite_debug_tool.i18n import LANGUAGE_EN_US, LANGUAGE_ZH_CN
    from satellite_debug_tool.ui.main_window import MainWindow

    settings, manager = i18n_context
    manager.set_preference(LANGUAGE_EN_US)
    window = MainWindow(settings=settings)
    live = window._live
    device = window._device
    worker_sentinel = object()
    live._worker = worker_sentinel
    live._is_connected = True
    timers = (live._update_timer, live._heavy_timer, live._handshake_timer)
    splitter_sizes = tuple(live._top_splitter.sizes())

    window._tabs.setCurrentIndex(3)
    live._chart.set_mode("stacked")
    live._channel_panel.add_channel(
        "ch_00",
        "#00AFAF",
        display_label="roll",
        group="__profile_group_0",
    )
    live._channel_panel._rows["ch_00"].set_checked(False)
    live._data_store.update(
        DataReport(timestamp=1000, samples=[ChannelSample(0, 1.25)])
    )
    device._ota_active = True
    device._ota_state = "DATA"
    device._set_ota_status(
        "Transferring... {sequence}/{total} ({percent}%)",
        sequence=1,
        total=4,
        percent=25,
    )
    window.statusBar().showMessage("Temporary status", 15000)

    manager.set_preference(LANGUAGE_ZH_CN)
    qapp.processEvents()

    assert [window._tabs.tabText(i) for i in range(4)] == [
        "实时",
        "回放",
        "Log",
        "设备",
    ]
    assert window._tabs.currentIndex() == 3
    assert live._chart._mode == "stacked"
    assert live._worker is worker_sentinel
    assert timers == (live._update_timer, live._heavy_timer, live._handshake_timer)
    assert tuple(live._top_splitter.sizes()) == splitter_sizes
    assert live._update_timer.isActive()
    assert live._heavy_timer.isActive()
    assert live._channel_panel.is_checked("ch_00") is False
    assert live._data_store.frame_count == 1
    assert device._ota_active is True
    assert device._ota_state == "DATA"
    assert "正在传输" in device._ota_status_label.text()
    assert window.statusBar().currentMessage() == ""

    manager.set_preference(LANGUAGE_EN_US)
    qapp.processEvents()
    assert [window._tabs.tabText(i) for i in range(4)] == [
        "Live",
        "Playback",
        "Log",
        "Device",
    ]
    assert window._tabs.currentIndex() == 3
    assert live._data_store.frame_count == 1
    assert device._ota_status_label.text().startswith("Transferring...")

    live._worker = None
    live._is_connected = False
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_engineering_workspace_restores_four_tabs_and_shortcut_toggles(
    i18n_context,
    qapp,
):
    from satellite_debug_tool.i18n import LANGUAGE_EN_US, LANGUAGE_ZH_CN
    from satellite_debug_tool.ui.main_window import MainWindow

    settings, manager = i18n_context
    manager.set_preference(LANGUAGE_EN_US)
    window = MainWindow(settings=settings)
    window.resize(1280, 800)
    window.show()
    qapp.processEvents()

    engineering_views = tuple(window._tabs.widget(index) for index in range(4))
    assert window._workspace.currentWidget() is window._customer
    assert window._customer.isVisible()
    assert not any(button.isVisible() for button in window._engineering_tab_pills)

    window.unlock_engineering_for_session()
    qapp.processEvents()

    assert window._workspace.currentWidget() is window._tabs
    assert not window._customer.isVisible()
    assert [button.text() for button in window._engineering_tab_pills] == [
        "Live",
        "Playback",
        "Log",
        "Device",
    ]
    assert all(button.isVisible() for button in window._engineering_tab_pills)
    assert not hasattr(window, "_operation_pill")
    assert not hasattr(window, "_engineering_pill")
    assert not hasattr(window, "_exit_engineering_btn")

    for index, button in enumerate(window._engineering_tab_pills):
        button.click()
        qapp.processEvents()
        assert window._tabs.currentIndex() == index
        assert button.isChecked()
    assert tuple(window._tabs.widget(index) for index in range(4)) == engineering_views

    manager.set_preference(LANGUAGE_ZH_CN)
    qapp.processEvents()
    assert [button.text() for button in window._engineering_tab_pills] == [
        "实时",
        "回放",
        "Log",
        "设备",
    ]
    window._engineering_shortcut.activated.emit()
    qapp.processEvents()
    assert window._workspace.currentWidget() is window._customer
    assert window._customer.isVisible()
    assert not window._tab_pillbar.isVisible()

    window._engineering_shortcut.activated.emit()
    qapp.processEvents()
    assert window._workspace.currentWidget() is window._tabs
    assert window._tabs.currentIndex() == 3
    assert tuple(window._tabs.widget(index) for index in range(4)) == engineering_views

    window._engineering_shortcut.activated.emit()
    qapp.processEvents()
    assert window._workspace.currentWidget() is window._customer

    window.close()
    window.deleteLater()
    qapp.processEvents()


def _collect_widget_texts(root) -> list[tuple[str, str]]:
    from PySide6.QtCore import QObject
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (
        QAbstractButton,
        QComboBox,
        QGroupBox,
        QLabel,
        QLineEdit,
        QTabWidget,
        QTableWidget,
        QWidget,
    )

    result: list[tuple[str, str]] = []
    objects = [root, *root.findChildren(QObject)]
    for obj in objects:
        label = type(obj).__name__
        if isinstance(obj, QWidget):
            for name, value in (
                ("windowTitle", obj.windowTitle()),
                ("toolTip", obj.toolTip()),
                ("statusTip", obj.statusTip()),
                ("whatsThis", obj.whatsThis()),
            ):
                if value:
                    result.append((f"{label}.{name}", value))
        if isinstance(obj, (QLabel, QAbstractButton)):
            if obj.text():
                result.append((f"{label}.text", obj.text()))
        if isinstance(obj, QGroupBox) and obj.title():
            result.append((f"{label}.title", obj.title()))
        if isinstance(obj, QLineEdit) and obj.placeholderText():
            result.append((f"{label}.placeholder", obj.placeholderText()))
        if isinstance(obj, QComboBox):
            result.extend(
                (f"{label}.item[{index}]", obj.itemText(index))
                for index in range(obj.count())
                if obj.itemText(index)
            )
        if isinstance(obj, QTabWidget):
            result.extend(
                (f"{label}.tab[{index}]", obj.tabText(index))
                for index in range(obj.count())
                if obj.tabText(index)
            )
        if isinstance(obj, QTableWidget):
            for index in range(obj.columnCount()):
                item = obj.horizontalHeaderItem(index)
                if item is not None and item.text():
                    result.append((f"{label}.header[{index}]", item.text()))
        if isinstance(obj, QAction) and obj.text():
            result.append((f"{label}.text", obj.text()))
    return result


def test_english_major_windows_have_no_first_party_cjk(i18n_context, qapp):
    from satellite_debug_tool.i18n import LANGUAGE_EN_US
    from satellite_debug_tool.ui.main_window import MainWindow

    settings, manager = i18n_context
    manager.set_preference(LANGUAGE_EN_US)
    window = MainWindow(settings=settings)
    qapp.processEvents()

    texts = _collect_widget_texts(window)
    leftovers = [(owner, text) for owner, text in texts if CJK_RE.search(text)]

    assert len(texts) >= 100
    assert leftovers == []
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_english_secondary_windows_have_no_first_party_cjk(i18n_context, qapp):
    from satellite_debug_tool.core.data import GnssStore
    from satellite_debug_tool.core.profile import ProfileStore
    from satellite_debug_tool.i18n import LANGUAGE_EN_US
    from satellite_debug_tool.ui.chart_group_dialog import ChartGroupDialog
    from satellite_debug_tool.ui.connection_dialog import ConnectionDialog
    from satellite_debug_tool.ui.gnss_widget import GnssWidget
    from satellite_debug_tool.ui.settings_dialog import SettingsDialog
    from satellite_debug_tool.ui.update_dialog import UpdateDialog

    settings, manager = i18n_context
    manager.set_preference(LANGUAGE_EN_US)
    roots = [
        SettingsDialog(settings),
        ConnectionDialog(),
        ChartGroupDialog(ProfileStore(), settings, None),
        UpdateDialog(settings, auto_start=False),
        GnssWidget(GnssStore()),
    ]

    leftovers = []
    for root in roots:
        leftovers.extend(
            (type(root).__name__, owner, text)
            for owner, text in _collect_widget_texts(root)
            if CJK_RE.search(text)
        )
    assert leftovers == []

    for root in roots:
        root.close()
        root.deleteLater()
    qapp.processEvents()


def test_minimum_window_right_panel_children_do_not_overlap(i18n_context, qapp):
    from satellite_debug_tool.i18n import LANGUAGE_EN_US
    from satellite_debug_tool.ui.main_window import MainWindow

    settings, manager = i18n_context
    manager.set_preference(LANGUAGE_EN_US)
    window = MainWindow(settings=settings)
    window.resize(1024, 600)
    window.show()
    qapp.processEvents()

    live = window._live
    children = [live._attitude, live._state_panel, live._event_timeline]
    geometries = [child.geometry() for child in children]
    assert geometries[0].bottom() < geometries[1].top()
    assert geometries[1].bottom() < geometries[2].top()
    assert live._type_combo.width() >= 80

    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_customer_workspace_translates_deferred_text_and_fits_minimum_window(
    i18n_context,
    qapp,
):
    from satellite_debug_tool.i18n import LANGUAGE_EN_US, LANGUAGE_ZH_CN
    from satellite_debug_tool.ui.main_window import MainWindow

    settings, manager = i18n_context
    manager.set_preference(LANGUAGE_ZH_CN)
    window = MainWindow(settings=settings)
    window.resize(1920, 1080)
    window.show()
    qapp.processEvents()
    window.resize(1024, 600)
    for _ in range(3):
        qapp.processEvents()

    customer = window._customer
    overview = customer.overview
    overview.refresh()
    overview_scroll = customer._stack.widget(0)

    assert [button.text() for button in customer._nav_buttons] == [
        "总览",
        "射频控制",
        "回放",
        "维护",
    ]
    assert overview._status_values["link"].text().startswith("连接状态:")
    assert overview._beam_values["beam_az"].title.text() == "方位角"
    assert overview._state_group_title.text() == "状态"
    assert overview._data_group_title.text() == "运行数据"
    assert overview._snr_plot.getAxis("bottom").label.toPlainText() == "设备开机时间 (s)"
    assert overview_scroll.horizontalScrollBar().maximum() == 0
    assert overview_scroll.verticalScrollBar().maximum() == 0
    assert overview._density == "dense"
    playback_channels = customer._playback._profile_store.get_channels(
        "customer_playback"
    )
    assert {channel.name for channel in playback_channels} >= {
        "横滚角",
        "波束俯仰角",
        "经度",
    }

    customer.set_page("maintenance")
    qapp.processEvents()
    component_table = customer._maintenance._component_table
    assert component_table.verticalScrollBar().maximum() == 0
    assert component_table.visualItemRect(component_table.item(2, 0)).height() > 0

    manager.set_preference(LANGUAGE_EN_US)
    qapp.processEvents()
    overview.refresh()
    assert customer._nav_buttons[0].text() == "Overview"
    assert overview._status_values["link"].text().startswith("Connection:")
    assert overview._beam_values["beam_az"].title.text() == "Azimuth"
    assert overview._data_group_title.text() == "Runtime data"
    assert overview._snr_plot.getAxis("bottom").label.toPlainText() == "Device uptime (s)"
    playback_channels = customer._playback._profile_store.get_channels(
        "customer_playback"
    )
    assert {channel.name for channel in playback_channels} >= {
        "Roll",
        "Beam elevation",
        "Longitude",
    }

    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_device_payload_text_remains_raw_in_english(i18n_context):
    from satellite_debug_tool.core.protocol import ParaEntry, ParaTableReport, ParaType
    from satellite_debug_tool.i18n import LANGUAGE_EN_US
    from satellite_debug_tool.ui.device_view import DeviceView

    _settings, manager = i18n_context
    manager.set_preference(LANGUAGE_EN_US)
    view = DeviceView()
    view._on_para_table_received(
        ParaTableReport(
            table_ver=1,
            params=[
                ParaEntry(
                    "设备原始参数",
                    int(ParaType.STRING),
                    0,
                    "设备原始值",
                )
            ],
        )
    )

    assert view._para_table.item(0, 0).text() == "设备原始参数"
    assert view._para_table.cellWidget(0, 2).text() == "设备原始值"
    view.deleteLater()


def test_raw_profile_text_survives_runtime_language_switch(i18n_context, qapp):
    from satellite_debug_tool.core.protocol import (
        ChannelDefEntry,
        StateDefEntry,
        StateEnumItem,
    )
    from satellite_debug_tool.i18n import LANGUAGE_EN_US, LANGUAGE_ZH_CN
    from satellite_debug_tool.ui.channel_panel import ChannelPanel
    from satellite_debug_tool.ui.dashboard_widget import (
        EnumStatusChip,
        KpiCard,
        ModeButtonGroup,
    )
    from satellite_debug_tool.ui.live_view import LiveView
    from satellite_debug_tool.ui.state_panel_widget import StateItemRow

    settings, manager = i18n_context
    manager.set_preference(LANGUAGE_ZH_CN)
    live = LiveView(settings=settings)
    live._is_connected = True
    live._set_conn_state(True, dev="设备", detail="状态")
    channel_panel = ChannelPanel()
    channel_panel.add_channel("raw", "#00AFAF", display_label="设备")
    channel = ChannelDefEntry(
        channel_id=0,
        data_type=1,
        group_id=0,
        flags=1,
        name="设备",
        unit="状态",
        display_min=-1.0,
        display_max=1.0,
    )
    state = StateDefEntry(
        state_id=0,
        state_type=1,
        flags=1,
        name="设备",
        enums=[StateEnumItem(0, 0, "状态")],
    )
    kpi = KpiCard(channel)
    status = EnumStatusChip(state)
    status.update_value(0)
    mode = ModeButtonGroup(state)
    row = StateItemRow(state)
    qapp.processEvents()

    manager.set_preference(LANGUAGE_EN_US)
    qapp.processEvents()

    assert live._cs_dev.text() == "设备"
    assert live._cs_stat.text() == "状态"
    assert channel_panel._rows["raw"]._name_label.text() == "设备"
    assert kpi._name_label.text() == "设备"
    assert kpi._unit_label.text() == "状态"
    assert status._name_label.text() == "设备"
    assert status._value_label.text() == "状态"
    assert mode._title.text() == "设备"
    assert mode._buttons[0].text() == "状态"
    assert row._name_label.text() == "设备"

    for widget in (live, channel_panel, kpi, status, mode, row):
        widget.deleteLater()


def test_user_chart_title_is_not_translated(i18n_context, qapp):
    from satellite_debug_tool.core.profile import ProfileStore
    from satellite_debug_tool.core.protocol import ChannelDefEntry
    from satellite_debug_tool.i18n import LANGUAGE_EN_US, LANGUAGE_ZH_CN
    from satellite_debug_tool.ui.chart_group_dialog import ChartGroupDialog

    settings, manager = i18n_context
    profile = ProfileStore()
    profile.apply_channel_define(
        "afd01",
        table_ver=1,
        entries=[
            ChannelDefEntry(0, 1, 0, 0, "roll", "deg", -180.0, 180.0),
        ],
    )
    settings.set(
        "chart.custom_groups",
        {
            "afd01": {
                "0": {
                    "title": "设备自定义",
                    "title_is_default": False,
                    "channels": ["roll"],
                },
            },
        },
    )
    manager.set_preference(LANGUAGE_ZH_CN)
    dialog = ChartGroupDialog(profile, settings, "afd01")

    manager.set_preference(LANGUAGE_EN_US)
    qapp.processEvents()

    assert dialog.current_groups_snapshot()[0]["title"] == "设备自定义"
    assert dialog._group_combo.itemText(0) == "Group 0: 设备自定义"
    dialog.deleteLater()


def test_map_locale_injection_does_not_reload_track(
    i18n_context,
    qapp,
    tmp_path,
    monkeypatch,
):
    from satellite_debug_tool.i18n import LANGUAGE_EN_US, LANGUAGE_ZH_CN
    from satellite_debug_tool.ui.map_widget import MapWidget

    monkeypatch.setattr(MapWidget, "_setup_ui", lambda self: None)
    monkeypatch.setattr(MapWidget, "_load_html", lambda self: None)
    widget = MapWidget(tiles_root=tmp_path)
    calls: list[str] = []
    widget._loaded = True
    widget._call_js = calls.append
    _settings, manager = i18n_context

    manager.set_preference(LANGUAGE_EN_US)
    qapp.processEvents()
    assert any("Offline map tiles were not found." in call for call in calls)

    calls.clear()
    manager.set_preference(LANGUAGE_ZH_CN)
    qapp.processEvents()
    assert any("未找到离线地图瓦片" in call for call in calls)
    assert all("clearAll(" not in call and "setTrack(" not in call for call in calls)

    html = (ROOT / "satellite_debug_tool/ui/assets/map.html").read_text(
        encoding="utf-8"
    )
    assert "window.setLocaleTexts" in html
    assert "startMarker.setPopupContent" in html
    assert "endMarker.setPopupContent" in html
    widget.deleteLater()


def test_translation_catalog_is_complete_and_compiled():
    result = subprocess.run(
        [sys.executable, "scripts/update_translations.py", "check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Translation catalog OK" in result.stdout
