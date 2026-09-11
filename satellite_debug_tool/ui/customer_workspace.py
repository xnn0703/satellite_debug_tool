"""Customer-facing product workspace sharing the engineering Live session."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.i18n import register_translatable, tr, tr_source
from satellite_debug_tool.ui import icons, styles as S
from satellite_debug_tool.ui.customer_overview_view import CustomerOverviewView
from satellite_debug_tool.ui.customer_maintenance_view import CustomerMaintenanceView
from satellite_debug_tool.ui.customer_playback_view import CustomerPlaybackView
from satellite_debug_tool.ui.customer_rf_control_view import CustomerRfControlView
from satellite_debug_tool.ui.customer_device_list import (
    CustomerDeviceList,
    CustomerDeviceSnapshot,
    Endpoint,
    coerce_endpoint,
    format_endpoint,
)
from satellite_debug_tool.ui.customer_endpoint_pages import (
    CustomerEndpointPageStack,
    CustomerPageBundleRegistry,
)
from satellite_debug_tool.ui.lazy_view_host import LazyViewHost
from satellite_debug_tool.ui.view_lifecycle import activate_view, deactivate_view


class _ViewportFitScrollArea(QScrollArea):
    """Fill the viewport when the page can fit; scroll only below its minimum."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._fit_pending = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_page_height()
        if not self._fit_pending:
            self._fit_pending = True
            QTimer.singleShot(0, self._finish_fit_page_height)

    def _finish_fit_page_height(self) -> None:
        self._fit_pending = False
        self._fit_page_height()

    def _fit_page_height(self) -> None:
        page = self.widget()
        if page is None:
            return
        page.setMinimumHeight(0)
        page.setMaximumHeight(16777215)
        viewport_height = self.viewport().height()
        minimum_height = page.minimumSizeHint().height()
        if viewport_height >= minimum_height:
            page.setFixedHeight(viewport_height)
        else:
            page.setMinimumHeight(minimum_height)
            page.resize(page.width(), minimum_height)


class _PendingCustomerPage(QWidget):
    def __init__(self, state_text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._state_source = state_text
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        self._state = QLabel(tr(state_text))
        self._state.setObjectName("customerPendingState")
        self._state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._state)
        layout.addStretch(1)

    def retranslate_ui(self) -> None:
        self._state.setText(tr(self._state_source))


class CustomerWorkspace(QWidget):
    """Stable navigation over endpoint-fixed Customer session bundles."""

    status_message = Signal(str, int)
    endpoint_status_message = Signal(object, str, int)
    add_requested = Signal()
    edit_requested = Signal(object)
    delete_requested = Signal(object)

    _PAGE_DEFS = (
        ("overview", tr_source("Overview"), "grid"),
        ("rf", tr_source("RF control"), "sliders"),
        ("playback", tr_source("Playback"), "history"),
        ("maintenance", tr_source("Maintenance"), "settings"),
    )

    def __init__(
        self,
        live_view,
        settings,
        device_view,
        parent: Optional[QWidget] = None,
        *,
        device_directory=None,
        page_bundle_factory=None,
    ) -> None:
        super().__init__(parent)
        if (device_directory is None) != (page_bundle_factory is None):
            raise TypeError(
                "device_directory and page_bundle_factory must be injected together"
            )
        self._live = live_view
        self._settings = settings
        self._device_provider = device_view if callable(device_view) else lambda: device_view
        self._device_directory = device_directory
        self._page_bundle_factory = page_bundle_factory
        self._multi_device_enabled = device_directory is not None
        self._theme = "dark"
        self._scale = "small"
        self._page_ids: list[str] = []
        self._nav_buttons: list[QPushButton] = []
        self._page_views: list[QWidget] = []
        self._view_active = False
        self._shutdown_done = False
        self._build_ui()
        if self._multi_device_enabled:
            registry = self._bundle_registry
            self.destroyed.connect(
                lambda _object=None, registry=registry: registry.shutdown_all()
            )
        register_translatable(self)

    @property
    def overview(self) -> Optional[QWidget]:
        if self._multi_device_enabled:
            self._overview = self._overview_pages.ensure_active_view()
        return self._overview

    @property
    def rf_control(self) -> Optional[QWidget]:
        if self._multi_device_enabled:
            self._rf_control = self._rf_pages.ensure_active_view()
            return self._rf_control
        return self._rf_host.ensure_view()

    @property
    def playback(self) -> CustomerPlaybackView:
        return self._playback_host.ensure_view()

    @property
    def maintenance(self) -> Optional[QWidget]:
        if self._multi_device_enabled:
            self._maintenance = self._maintenance_pages.ensure_active_view()
            return self._maintenance
        return self._maintenance_host.ensure_view()

    @property
    def active_endpoint(self) -> Optional[Endpoint]:
        if not self._multi_device_enabled:
            return None
        return self._active_endpoint

    @property
    def device_list(self) -> Optional[CustomerDeviceList]:
        return self._device_list if self._multi_device_enabled else None

    def _create_rf_control(self) -> CustomerRfControlView:
        self._rf_control = CustomerRfControlView(self._live)
        return self._rf_control

    def _create_playback(self) -> CustomerPlaybackView:
        self._playback = CustomerPlaybackView(self._settings)
        return self._playback

    def _create_maintenance(self) -> CustomerMaintenanceView:
        self._maintenance = CustomerMaintenanceView(
            self._live,
            self._device_provider(),
            self._settings,
        )
        return self._maintenance

    def _build_ui(self) -> None:
        if self._multi_device_enabled:
            self._build_multi_device_ui()
        else:
            self._build_legacy_ui()

    def _build_legacy_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._sidebar = QFrame()
        self._sidebar.setObjectName("customerSidebar")
        self._sidebar.setFixedWidth(158)
        side = QVBoxLayout(self._sidebar)
        side.setContentsMargins(9, 12, 9, 12)
        side.setSpacing(5)
        self._section_label = QLabel(tr("Product operation"))
        self._section_label.setObjectName("customerSidebarTitle")
        side.addWidget(self._section_label)

        self._stack = QStackedWidget()
        self._overview = CustomerOverviewView(self._live, self._settings)
        self._rf_control: Optional[CustomerRfControlView] = None
        self._playback: Optional[CustomerPlaybackView] = None
        self._maintenance: Optional[CustomerMaintenanceView] = None
        self._rf_host = LazyViewHost(self._create_rf_control)
        self._playback_host = LazyViewHost(self._create_playback)
        self._maintenance_host = LazyViewHost(self._create_maintenance)
        overview_scroll = _ViewportFitScrollArea()
        overview_scroll.setObjectName("customerOverviewScroll")
        overview_scroll.setWidgetResizable(True)
        overview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        overview_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        overview_scroll.setWidget(self._overview)

        rf_scroll = QScrollArea()
        rf_scroll.setObjectName("customerRfScroll")
        rf_scroll.setWidgetResizable(True)
        rf_scroll.setFrameShape(QFrame.Shape.NoFrame)
        rf_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        rf_scroll.setWidget(self._rf_host)

        maintenance_scroll = QScrollArea()
        maintenance_scroll.setObjectName("customerMaintenanceScroll")
        maintenance_scroll.setWidgetResizable(True)
        maintenance_scroll.setFrameShape(QFrame.Shape.NoFrame)
        maintenance_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        maintenance_scroll.setWidget(self._maintenance_host)

        pages = (
            overview_scroll,
            rf_scroll,
            self._playback_host,
            maintenance_scroll,
        )
        self._page_views.extend(
            (
                self._overview,
                self._rf_host,
                self._playback_host,
                self._maintenance_host,
            )
        )
        for index, ((page_id, label, icon_name), page) in enumerate(
            zip(self._PAGE_DEFS, pages)
        ):
            button = QPushButton(tr(label))
            button.setObjectName("customerNavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty("iconName", icon_name)
            button.clicked.connect(lambda _checked, i=index: self.set_page(i))
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            side.addWidget(button)
            self._nav_buttons.append(button)
            self._page_ids.append(page_id)
            self._stack.addWidget(page)
        side.addStretch(1)

        root.addWidget(self._sidebar)
        root.addWidget(self._stack, 1)
        self._overview.status_message.connect(self.status_message)
        self._rf_host.status_message.connect(self.status_message)
        self._playback_host.status_message.connect(self.status_message)
        self._maintenance_host.status_message.connect(self.status_message)
        self.set_page(0)

    def _build_multi_device_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._sidebar = QFrame()
        self._sidebar.setObjectName("customerSidebar")
        self._sidebar.setFixedWidth(158)
        side = QVBoxLayout(self._sidebar)
        side.setContentsMargins(9, 12, 9, 12)
        side.setSpacing(5)

        self._snapshots: tuple[CustomerDeviceSnapshot, ...] = ()
        self._configured_endpoints: tuple[Endpoint, ...] = ()
        self._active_endpoint: Optional[Endpoint] = None
        self._device_list = CustomerDeviceList()
        self._device_list.endpoint_selected.connect(self._request_endpoint_selection)
        self._device_list.add_requested.connect(self.add_requested)
        self._device_list.edit_requested.connect(self.edit_requested)
        self._device_list.delete_requested.connect(self.delete_requested)
        side.addWidget(self._device_list)

        self._section_label = QLabel(tr("Product operation"))
        self._section_label.setObjectName("customerSidebarTitle")
        side.addWidget(self._section_label)

        self._bundle_registry = CustomerPageBundleRegistry(self._page_bundle_factory)
        self._overview_pages = CustomerEndpointPageStack(
            "overview", self._bundle_registry
        )
        self._rf_pages = CustomerEndpointPageStack("rf", self._bundle_registry)
        self._maintenance_pages = CustomerEndpointPageStack(
            "maintenance", self._bundle_registry
        )
        for page_stack in (
            self._overview_pages,
            self._rf_pages,
            self._maintenance_pages,
        ):
            page_stack.status_message.connect(self._forward_endpoint_status)
            page_stack.view_created.connect(self._on_endpoint_view_created)
            page_stack.add_requested.connect(self.add_requested)

        self._overview: Optional[QWidget] = None
        self._rf_control: Optional[QWidget] = None
        self._playback: Optional[CustomerPlaybackView] = None
        self._maintenance: Optional[QWidget] = None
        self._playback_host = LazyViewHost(self._create_playback)

        overview_scroll = _ViewportFitScrollArea()
        overview_scroll.setObjectName("customerOverviewScroll")
        overview_scroll.setWidgetResizable(True)
        overview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        overview_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        overview_scroll.setWidget(self._overview_pages)

        rf_scroll = QScrollArea()
        rf_scroll.setObjectName("customerRfScroll")
        rf_scroll.setWidgetResizable(True)
        rf_scroll.setFrameShape(QFrame.Shape.NoFrame)
        rf_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        rf_scroll.setWidget(self._rf_pages)

        maintenance_scroll = QScrollArea()
        maintenance_scroll.setObjectName("customerMaintenanceScroll")
        maintenance_scroll.setWidgetResizable(True)
        maintenance_scroll.setFrameShape(QFrame.Shape.NoFrame)
        maintenance_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        maintenance_scroll.setWidget(self._maintenance_pages)

        self._stack = QStackedWidget()
        pages = (
            overview_scroll,
            rf_scroll,
            self._playback_host,
            maintenance_scroll,
        )
        self._page_views.extend(
            (
                self._overview_pages,
                self._rf_pages,
                self._playback_host,
                self._maintenance_pages,
            )
        )
        for index, ((page_id, label, icon_name), page) in enumerate(
            zip(self._PAGE_DEFS, pages)
        ):
            button = QPushButton(tr(label))
            button.setObjectName("customerNavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty("iconName", icon_name)
            button.clicked.connect(lambda _checked, i=index: self.set_page(i))
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            side.addWidget(button)
            self._nav_buttons.append(button)
            self._page_ids.append(page_id)
            self._stack.addWidget(page)
        side.addStretch(1)

        root.addWidget(self._sidebar)
        root.addWidget(self._stack, 1)
        self._playback_host.status_message.connect(self.status_message)
        self._connect_directory_signal("devices_changed", self._refresh_directory)
        self._connect_directory_signal(
            "active_endpoint_changed", self._on_active_endpoint_changed
        )
        self._connect_directory_signal("device_changed", self._refresh_directory)
        self._refresh_directory()
        self.set_page(0)

    def _connect_directory_signal(self, name: str, slot) -> None:
        signal = getattr(self._device_directory, name, None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(slot)

    def _directory_devices(self) -> tuple[CustomerDeviceSnapshot, ...]:
        provider = getattr(self._device_directory, "devices", None)
        if not callable(provider):
            raise TypeError("customer device_directory must provide devices()")
        return tuple(CustomerDeviceSnapshot.from_entry(entry) for entry in provider())

    def _directory_active_endpoint(self) -> Optional[Endpoint]:
        provider = getattr(self._device_directory, "active_endpoint", None)
        if not callable(provider):
            raise TypeError("customer device_directory must provide active_endpoint()")
        endpoint = provider()
        return coerce_endpoint(endpoint) if endpoint is not None else None

    def _refresh_directory(self, *_args) -> None:
        if self._shutdown_done:
            return
        snapshots = self._directory_devices()
        endpoints = tuple(snapshot.endpoint for snapshot in snapshots)
        if len(set(endpoints)) != len(endpoints):
            raise ValueError("customer device_directory returned duplicate endpoints")
        old_endpoints = self._configured_endpoints
        self._snapshots = snapshots
        self._configured_endpoints = endpoints
        for page_stack in (
            self._overview_pages,
            self._rf_pages,
            self._maintenance_pages,
        ):
            page_stack.set_endpoints(endpoints)
        for endpoint in old_endpoints:
            if endpoint not in endpoints:
                self._bundle_registry.discard(endpoint)
        active = self._directory_active_endpoint()
        if active not in endpoints:
            active = None
        self._apply_active_endpoint(active)

    def _on_active_endpoint_changed(self, endpoint=None) -> None:
        if self._shutdown_done:
            return
        if endpoint is None:
            endpoint = self._directory_active_endpoint()
        else:
            endpoint = coerce_endpoint(endpoint)
        if endpoint not in self._configured_endpoints:
            endpoint = None
        self._apply_active_endpoint(endpoint)

    def _apply_active_endpoint(self, endpoint: Optional[Endpoint]) -> None:
        self._active_endpoint = endpoint
        self._device_list.set_devices(self._snapshots, endpoint)
        for page_stack in (
            self._overview_pages,
            self._rf_pages,
            self._maintenance_pages,
        ):
            page_stack.set_active_endpoint(endpoint)
        self._overview = self._overview_pages.active_view
        self._rf_control = self._rf_pages.active_view
        self._maintenance = self._maintenance_pages.active_view

    def _request_endpoint_selection(self, endpoint: object) -> None:
        if self._shutdown_done:
            return
        endpoint = coerce_endpoint(endpoint)
        selector = getattr(self._device_directory, "select_endpoint", None)
        if not callable(selector):
            raise TypeError("customer device_directory must provide select_endpoint(endpoint)")
        selector(endpoint)

    def _on_endpoint_view_created(
        self,
        endpoint: object,
        page_id: str,
        view: QWidget,
    ) -> None:
        if endpoint != self._active_endpoint:
            return
        if page_id == "overview":
            self._overview = view
        elif page_id == "rf":
            self._rf_control = view
        elif page_id == "maintenance":
            self._maintenance = view

    def _forward_endpoint_status(
        self,
        endpoint: object,
        message: str,
        timeout: int,
    ) -> None:
        if self._shutdown_done:
            return
        endpoint = coerce_endpoint(endpoint)
        self.endpoint_status_message.emit(endpoint, message, timeout)
        snapshot = next(
            (item for item in self._snapshots if item.endpoint == endpoint),
            None,
        )
        endpoint_text = format_endpoint(endpoint)
        owner = ""
        if snapshot and not (snapshot.identity_pending or snapshot.identity_conflict):
            owner = snapshot.display_identity
        prefix = f"{owner} · {endpoint_text}" if owner else endpoint_text
        self.status_message.emit(f"{prefix}: {message}", timeout)

    def session_page(self, page_id: str, *, ensure: bool = False) -> Optional[QWidget]:
        """Return the selected endpoint's fixed page without changing selection."""

        if not self._multi_device_enabled:
            return None
        stacks = {
            "overview": self._overview_pages,
            "rf": self._rf_pages,
            "maintenance": self._maintenance_pages,
        }
        if page_id not in stacks:
            return None
        stack = stacks[page_id]
        return stack.ensure_active_view() if ensure else stack.active_view

    def set_page(self, page: int | str) -> None:
        if isinstance(page, str):
            try:
                index = self._page_ids.index(page)
            except ValueError:
                index = 0
        else:
            index = max(0, min(int(page), self._stack.count() - 1))
        previous = self._stack.currentIndex()
        if self._view_active and previous != index and 0 <= previous < len(self._page_views):
            deactivate_view(self._page_views[previous])
        self._stack.setCurrentIndex(index)
        if self._view_active and previous != index:
            activate_view(self._page_views[index])
        for button_index, button in enumerate(self._nav_buttons):
            button.setChecked(button_index == index)
        self._refresh_nav_icons()

    def activate_view(self) -> None:
        if self._view_active or self._shutdown_done:
            return
        self._view_active = True
        activate_view(self._page_views[self._stack.currentIndex()])

    def deactivate_view(self) -> None:
        if not self._view_active:
            return
        self._view_active = False
        deactivate_view(self._page_views[self._stack.currentIndex()])

    def shutdown(self) -> bool:
        if self._shutdown_done:
            return True
        if not self._multi_device_enabled:
            self.deactivate_view()
            for host in (
                self._rf_host,
                self._playback_host,
                self._maintenance_host,
            ):
                host.shutdown()
            self._shutdown_done = True
            return True
        # Endpoint-bound bundles own their recorder and operation leases.  They
        # must release them before page hosts are dismantled.
        if not self._bundle_registry.shutdown_all():
            return False
        factory_shutdown = getattr(self._page_bundle_factory, "shutdown_all", None)
        if callable(factory_shutdown) and factory_shutdown() is False:
            return False
        self.deactivate_view()
        for page_stack in (
            self._overview_pages,
            self._rf_pages,
            self._maintenance_pages,
        ):
            page_stack.shutdown()
        self._playback_host.shutdown()
        self._shutdown_done = True
        return True

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
        self._scale = scale
        pal = S.palette(theme)
        self.setStyleSheet(
            f"CustomerWorkspace {{ background: {pal['bg']}; }}"
            f"#customerSidebar {{ background: {pal['panel']}; "
            f"border-right: 1px solid {pal['border']}; }}"
            f"#customerSidebarTitle {{ color: {pal['text_muted']}; font-weight: 600; "
            f"padding: 3px 7px 7px 7px; }}"
            f"#customerNavButton {{ background: transparent; border: 0; border-radius: 5px; "
            f"color: {pal['text_2']}; text-align: left; padding: 8px 9px; }}"
            f"#customerNavButton:hover {{ background: {pal['card_2']}; color: {pal['text']}; }}"
            f"#customerNavButton:checked {{ background: {pal['accent_dim']}; "
            f"color: {pal['accent_ink']}; font-weight: 600; }}"
            f"#customerPendingState {{ color: {pal['text_muted']}; }}"
            f"#customerOverviewScroll {{ background: {pal['bg']}; }}"
            f"#customerMaintenanceScroll {{ background: {pal['bg']}; }}"
        )
        if self._multi_device_enabled:
            self._device_list.set_theme(theme, scale)
            self._overview_pages.set_theme(theme, scale)
            self._rf_pages.set_theme(theme, scale)
            self._playback_host.set_theme(theme, scale)
            self._maintenance_pages.set_theme(theme, scale)
        else:
            self._overview.set_theme(theme, scale)
            self._rf_host.set_theme(theme, scale)
            self._playback_host.set_theme(theme, scale)
            self._maintenance_host.set_theme(theme, scale)
        self._refresh_nav_icons()

    def _refresh_nav_icons(self) -> None:
        pal = S.palette(self._theme)
        for button in self._nav_buttons:
            color = pal["accent_ink"] if button.isChecked() else pal["text_2"]
            button.setIcon(
                icons.icon(str(button.property("iconName")), color=color, size=14)
            )

    def retranslate_ui(self) -> None:
        self._section_label.setText(tr("Product operation"))
        for button, (_page_id, label, _icon_name) in zip(
            self._nav_buttons, self._PAGE_DEFS
        ):
            button.setText(tr(label))
        if self._multi_device_enabled:
            self._device_list.retranslate_ui()
            self._overview_pages.retranslate_ui()
            self._rf_pages.retranslate_ui()
            self._playback_host.retranslate_ui()
            self._maintenance_pages.retranslate_ui()
