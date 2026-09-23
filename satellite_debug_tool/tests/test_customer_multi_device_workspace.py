"""Customer multi-device directory and endpoint-fixed page UI regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QLabel, QWidget

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.ui.customer_device_dialog import CustomerDeviceDialog
from satellite_debug_tool.ui.customer_device_list import CustomerDeviceSnapshot
from satellite_debug_tool.ui.customer_workspace import CustomerWorkspace


AFD_ENDPOINT = ("192.168.1.13", 4004)
ESA_ENDPOINT = ("192.168.1.12", 4004)


def _snapshot(
    endpoint,
    *,
    identity: str = "",
    phase: str = "DISCONNECTED",
    business_state: str = "",
    recording: bool = False,
    busy: bool = False,
    identity_pending: bool = False,
    identity_conflict: bool = False,
) -> CustomerDeviceSnapshot:
    return CustomerDeviceSnapshot(
        key=f"{endpoint[0]}:{endpoint[1]}",
        endpoint=endpoint,
        display_identity=identity,
        connection_phase=phase,
        business_state=business_state,
        recording_active=recording,
        operation_busy=busy,
        identity_pending=identity_pending,
        identity_conflict=identity_conflict,
    )


class _DirectoryDouble(QObject):
    devices_changed = Signal()
    active_endpoint_changed = Signal(object)
    device_changed = Signal(object)
    attachment_changed = Signal(object, bool)

    def __init__(self, entries=(), active=None) -> None:
        super().__init__()
        self.entries = list(entries)
        self.active = active
        self.select_calls: list[tuple[str, int]] = []

    def devices(self):
        return tuple(self.entries)

    def active_endpoint(self):
        return self.active

    def attachment(self, _endpoint):
        return None

    def select_endpoint(self, endpoint) -> None:
        self.select_calls.append(endpoint)

    def commit_active(self, endpoint) -> None:
        self.active = endpoint
        self.active_endpoint_changed.emit(endpoint)

    def replace(self, entries, active) -> None:
        self.entries = list(entries)
        self.active = active
        self.devices_changed.emit()


class _LifecyclePage(QWidget):
    status_message = Signal(str, int)

    def __init__(self, endpoint, page_id: str) -> None:
        super().__init__()
        self.endpoint = endpoint
        self.page_id = page_id
        self.activations = 0
        self.deactivations = 0
        self.shutdown_count = 0
        self.themes: list[tuple[str, str]] = []
        self.retranslations = 0
        layout_label = QLabel(f"{page_id}:{endpoint[0]}", self)
        layout_label.move(5, 5)

    def activate_view(self) -> None:
        self.activations += 1

    def deactivate_view(self) -> None:
        self.deactivations += 1

    def shutdown(self) -> None:
        self.shutdown_count += 1

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self.themes.append((theme, scale))

    def retranslate_ui(self) -> None:
        self.retranslations += 1


class _PageBundleDouble:
    def __init__(self, endpoint) -> None:
        self.endpoint = endpoint
        self.pages: dict[str, _LifecyclePage] = {}
        self.page_requests: list[str] = []
        self.shutdown_count = 0

    def page(self, page_id: str) -> QWidget:
        self.page_requests.append(page_id)
        page = self.pages.get(page_id)
        if page is None:
            page = _LifecyclePage(self.endpoint, page_id)
            self.pages[page_id] = page
        return page

    def shutdown(self) -> None:
        self.shutdown_count += 1


@pytest.fixture
def customer_settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setenv("HOME", str(tmp_path))
    return Settings()


def _make_workspace(directory, settings):
    factory_calls = []
    bundles = {}

    def factory(endpoint):
        factory_calls.append(endpoint)
        bundle = _PageBundleDouble(endpoint)
        bundles[endpoint] = bundle
        return bundle

    workspace = CustomerWorkspace(
        object(),
        settings,
        lambda: object(),
        device_directory=directory,
        page_bundle_factory=factory,
    )
    return workspace, factory_calls, bundles


def _dispose(workspace: CustomerWorkspace, qapp) -> None:
    workspace.shutdown()
    workspace.close()
    workspace.deleteLater()
    qapp.processEvents()


def test_zero_device_state_does_not_construct_core_pages_and_playback_stays_global(
    qapplication_session,
    customer_settings,
) -> None:
    qapp = qapplication_session
    directory = _DirectoryDouble()
    workspace, factory_calls, _bundles = _make_workspace(
        directory, customer_settings
    )
    add_events = []
    workspace.add_requested.connect(lambda: add_events.append(True))

    workspace.activate_view()
    assert workspace.active_endpoint is None
    assert workspace._overview_pages.empty_state.has_devices is False
    assert factory_calls == []

    workspace.set_page("iperf")
    assert workspace._page_ids[workspace._stack.currentIndex()] == "iperf"
    assert factory_calls == []
    workspace._overview_pages.empty_state._add_button.click()
    assert add_events == [True]

    workspace.set_page("playback")
    playback = workspace.playback
    directory.replace(
        [_snapshot(AFD_ENDPOINT, identity="AFD01C SN-A", phase="ONLINE")],
        AFD_ENDPOINT,
    )
    qapp.processEvents()

    assert workspace._page_ids[workspace._stack.currentIndex()] == "playback"
    assert workspace.playback is playback
    assert factory_calls == []
    _dispose(workspace, qapp)


def test_left_device_list_renders_four_rows_and_emits_only_directory_intents(
    qapplication_session,
    customer_settings,
) -> None:
    qapp = qapplication_session
    endpoints = (
        AFD_ENDPOINT,
        ESA_ENDPOINT,
        ("192.168.1.14", 4004),
        ("192.168.1.15", 4004),
    )
    entries = (
        _snapshot(
            endpoints[0],
            identity="AFD01C SN-A",
            phase="ONLINE",
            recording=True,
            busy=True,
        ),
        _snapshot(
            endpoints[1],
            identity="ESA01 cached",
            phase="WAITING",
            identity_pending=True,
        ),
        _snapshot(
            endpoints[2],
            phase="RECONNECTING",
            identity_conflict=True,
        ),
        _snapshot(
            endpoints[3],
            phase="DISCONNECTED",
            business_state="NOT_READY",
        ),
    )
    directory = _DirectoryDouble(entries, endpoints[0])
    workspace, factory_calls, _bundles = _make_workspace(
        directory, customer_settings
    )
    edit_events = []
    delete_events = []
    workspace.edit_requested.connect(edit_events.append)
    workspace.delete_requested.connect(delete_events.append)
    rows = workspace.device_list.rows

    assert workspace._sidebar.width() == 158
    assert len(rows) == 4
    assert workspace.device_list._add_button.isEnabled() is True
    assert rows[0].isChecked() is True
    assert "AFD01C SN-A" in rows[0].accessibleName()
    assert "192.168.1.13:4004" in rows[0].toolTip()
    assert "192.168.1.12:4004" in rows[1].accessibleName()
    assert "ESA01 cached" not in rows[1].accessibleName()
    assert rows[2].property("severity") == "blocked"
    assert "NOT_READY" in rows[3].toolTip()
    assert factory_calls == []

    add_events = []
    workspace.add_requested.connect(lambda: add_events.append(True))
    workspace.device_list._add_button.click()
    assert add_events == [True]

    rows[1].click()
    assert directory.select_calls == [ESA_ENDPOINT]
    assert workspace.active_endpoint == AFD_ENDPOINT
    directory.commit_active(ESA_ENDPOINT)
    assert workspace.active_endpoint == ESA_ENDPOINT
    assert rows[1].isChecked() is True

    workspace.device_list._edit_button.click()
    workspace.device_list._delete_button.click()
    assert edit_events == [ESA_ENDPOINT]
    assert delete_events == [ESA_ENDPOINT]

    _dispose(workspace, qapp)


def test_endpoint_selection_preserves_subpage_and_reuses_fixed_lazy_pages(
    qapplication_session,
    customer_settings,
) -> None:
    qapp = qapplication_session
    entries = (
        _snapshot(AFD_ENDPOINT, identity="AFD01C SN-A", phase="ONLINE"),
        _snapshot(ESA_ENDPOINT, identity="ESA01 SN-B", phase="ONLINE"),
    )
    directory = _DirectoryDouble(entries, AFD_ENDPOINT)
    workspace, factory_calls, bundles = _make_workspace(directory, customer_settings)

    workspace.activate_view()
    afd_overview = bundles[AFD_ENDPOINT].pages["overview"]
    assert afd_overview.activations == 1
    assert factory_calls == [AFD_ENDPOINT]

    workspace.set_page("rf")
    afd_rf = bundles[AFD_ENDPOINT].pages["rf"]
    assert afd_overview.deactivations == 1
    assert afd_rf.activations == 1
    directory.commit_active(ESA_ENDPOINT)
    esa_rf = bundles[ESA_ENDPOINT].pages["rf"]

    assert workspace._page_ids[workspace._stack.currentIndex()] == "rf"
    assert afd_rf.deactivations == 1
    assert esa_rf.activations == 1
    assert "overview" not in bundles[ESA_ENDPOINT].pages
    assert factory_calls == [AFD_ENDPOINT, ESA_ENDPOINT]

    directory.commit_active(AFD_ENDPOINT)
    assert afd_rf.activations == 2
    assert bundles[AFD_ENDPOINT].page_requests.count("rf") == 1
    assert factory_calls == [AFD_ENDPOINT, ESA_ENDPOINT]

    workspace.set_theme("light")
    directory.commit_active(ESA_ENDPOINT)
    workspace.set_page("maintenance")
    esa_maintenance = bundles[ESA_ENDPOINT].pages["maintenance"]
    assert esa_maintenance.themes[-1] == ("light", "small")

    workspace.retranslate_ui()
    assert afd_overview.retranslations >= 1
    assert afd_rf.retranslations >= 1
    assert esa_rf.retranslations >= 1
    assert esa_maintenance.retranslations >= 1
    _dispose(workspace, qapp)


def test_hidden_endpoint_message_keeps_frozen_scope_and_delete_releases_bundle(
    qapplication_session,
    customer_settings,
) -> None:
    qapp = qapplication_session
    entries = (
        _snapshot(AFD_ENDPOINT, identity="AFD01C SN-A", phase="ONLINE"),
        _snapshot(ESA_ENDPOINT, identity="ESA01 SN-B", phase="ONLINE"),
    )
    directory = _DirectoryDouble(entries, AFD_ENDPOINT)
    workspace, _factory_calls, bundles = _make_workspace(directory, customer_settings)
    scoped_messages = []
    visible_messages = []
    workspace.endpoint_status_message.connect(
        lambda endpoint, message, timeout: scoped_messages.append(
            (endpoint, message, timeout)
        )
    )
    workspace.status_message.connect(
        lambda message, timeout: visible_messages.append((message, timeout))
    )

    workspace.activate_view()
    workspace.set_page("rf")
    directory.commit_active(ESA_ENDPOINT)
    directory.commit_active(AFD_ENDPOINT)
    esa_rf = bundles[ESA_ENDPOINT].pages["rf"]
    esa_rf.status_message.emit("transaction finished", 2500)

    assert scoped_messages[-1] == (ESA_ENDPOINT, "transaction finished", 2500)
    assert "ESA01 SN-B" in visible_messages[-1][0]
    assert "192.168.1.12:4004" in visible_messages[-1][0]
    assert "AFD01C SN-A" not in visible_messages[-1][0]

    workspace.set_page("playback")
    playback = workspace.playback
    directory.replace([entries[0]], AFD_ENDPOINT)
    qapp.processEvents()
    assert bundles[ESA_ENDPOINT].shutdown_count == 1
    assert workspace.playback is playback
    assert workspace._page_ids[workspace._stack.currentIndex()] == "playback"
    assert workspace.active_endpoint == AFD_ENDPOINT
    _dispose(workspace, qapp)


def test_device_rail_remains_operable_at_minimum_window_size(
    qapplication_session,
    customer_settings,
) -> None:
    qapp = qapplication_session
    entries = tuple(
        _snapshot((f"192.168.1.{index}", 4004), phase="DISCONNECTED")
        for index in range(11, 15)
    )
    directory = _DirectoryDouble(entries, entries[0].endpoint)
    workspace, _factory_calls, _bundles = _make_workspace(
        directory, customer_settings
    )
    workspace.resize(1024, 600)
    workspace.show()
    workspace.activate_view()
    for _ in range(3):
        qapp.processEvents()

    assert workspace._sidebar.width() == 158
    assert all(button.isVisible() for button in workspace._nav_buttons)
    assert all(row.isVisible() and row.height() == 46 for row in workspace.device_list.rows)
    assert workspace.device_list.rows[-1].geometry().bottom() <= (
        workspace.device_list.height() - 1
    )
    _dispose(workspace, qapp)


def test_customer_device_dialog_returns_endpoint_intent(
    qapplication_session,
) -> None:
    dialog = CustomerDeviceDialog(("192.168.1.13", 4004))
    dialog._ip_edit.setText(" 192.168.1.12 ")
    dialog._port_edit.setValue(5004)

    assert dialog.endpoint() == ("192.168.1.12", 5004)
    dialog.deleteLater()
