"""Lazy, permanently endpoint-bound customer page presentation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from satellite_debug_tool.i18n import tr
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.customer_device_list import Endpoint, coerce_endpoint
from satellite_debug_tool.ui.lazy_view_host import LazyViewHost
from satellite_debug_tool.ui.view_lifecycle import activate_view, deactivate_view


_SESSION_PAGE_IDS = frozenset(("overview", "rf", "maintenance"))
_PAGE_ATTRIBUTE_ALIASES = {
    "overview": ("overview", "overview_view"),
    "rf": ("rf", "rf_control", "rf_view", "rf_control_view"),
    "maintenance": ("maintenance", "maintenance_view"),
}


@dataclass(frozen=True)
class CustomerEndpointPageBundle:
    """Small canonical bundle accepted by :class:`CustomerWorkspace`.

    Each page value may be a ``QWidget`` or a zero-argument factory.  The
    registry calls it at most once and never rebinds the resulting widget.
    """

    endpoint: Endpoint
    overview: object
    rf: object
    maintenance: object

    def page(self, page_id: str) -> object:
        if page_id not in _SESSION_PAGE_IDS:
            raise KeyError(page_id)
        return getattr(self, page_id)


class CustomerPageBundleAdapter:
    """Duck-type adapter for application-owned endpoint page bundles."""

    def __init__(self, endpoint: Endpoint, bundle: object) -> None:
        self.endpoint = coerce_endpoint(endpoint)
        self.bundle = bundle
        self._views: dict[str, QWidget] = {}
        self._shutdown = False
        declared_endpoint = self._read_declared_endpoint(bundle)
        if declared_endpoint is not None and coerce_endpoint(declared_endpoint) != endpoint:
            raise ValueError("customer page bundle endpoint does not match its registry key")

    @staticmethod
    def _read_declared_endpoint(bundle: object) -> Optional[object]:
        if isinstance(bundle, Mapping):
            return bundle.get("endpoint")
        return getattr(bundle, "endpoint", None)

    def _page_source(self, page_id: str) -> object:
        if page_id not in _SESSION_PAGE_IDS:
            raise KeyError(page_id)
        page_method = getattr(self.bundle, "page", None)
        if callable(page_method):
            return page_method(page_id)
        if isinstance(self.bundle, Mapping):
            for name in _PAGE_ATTRIBUTE_ALIASES[page_id]:
                if name in self.bundle:
                    return self.bundle[name]
        else:
            for name in _PAGE_ATTRIBUTE_ALIASES[page_id]:
                if hasattr(self.bundle, name):
                    return getattr(self.bundle, name)
        raise AttributeError(f"customer page bundle has no {page_id!r} page")

    def page(self, page_id: str) -> QWidget:
        cached = self._views.get(page_id)
        if cached is not None:
            return cached
        if self._shutdown:
            raise RuntimeError("customer page bundle is already shut down")
        source = self._page_source(page_id)
        view = source() if callable(source) and not isinstance(source, QWidget) else source
        if not isinstance(view, QWidget):
            raise TypeError(f"customer {page_id} page factory must return QWidget")
        self._views[page_id] = view
        return view

    @property
    def views(self) -> tuple[QWidget, ...]:
        return tuple(self._views.values())

    def shutdown(self) -> bool:
        if self._shutdown:
            return True
        bundle_shutdown = getattr(self.bundle, "shutdown", None)
        if callable(bundle_shutdown):
            result = bundle_shutdown()
            if result is False:
                return False
            self._shutdown = True
            return True
        for view in self._views.values():
            shutdown = getattr(view, "shutdown", None)
            if callable(shutdown):
                result = shutdown()
                if result is False:
                    return False
        self._shutdown = True
        return True


class CustomerPageBundleRegistry:
    """Own one application bundle per endpoint and reject widget reuse."""

    def __init__(self, factory: Callable[[Endpoint], object]) -> None:
        if not callable(factory):
            raise TypeError("page_bundle_factory must be callable")
        self._factory = factory
        self._bundles: dict[Endpoint, CustomerPageBundleAdapter] = {}
        self._widget_owners: dict[int, tuple[Endpoint, str]] = {}

    def _adapter(self, endpoint: Endpoint) -> CustomerPageBundleAdapter:
        endpoint = coerce_endpoint(endpoint)
        adapter = self._bundles.get(endpoint)
        if adapter is None:
            adapter = CustomerPageBundleAdapter(endpoint, self._factory(endpoint))
            self._bundles[endpoint] = adapter
        return adapter

    def page(self, endpoint: Endpoint, page_id: str) -> QWidget:
        endpoint = coerce_endpoint(endpoint)
        view = self._adapter(endpoint).page(page_id)
        owner = (endpoint, page_id)
        previous = self._widget_owners.get(id(view))
        if previous is not None and previous != owner:
            raise ValueError(
                "a customer page QWidget cannot be shared between endpoints or page roles"
            )
        self._widget_owners[id(view)] = owner
        return view

    def has_bundle(self, endpoint: Endpoint) -> bool:
        return coerce_endpoint(endpoint) in self._bundles

    @property
    def bundle_count(self) -> int:
        return len(self._bundles)

    def discard(self, endpoint: Endpoint) -> bool:
        endpoint = coerce_endpoint(endpoint)
        adapter = self._bundles.get(endpoint)
        if adapter is None:
            return True
        if not adapter.shutdown():
            return False
        self._bundles.pop(endpoint, None)
        for view in adapter.views:
            self._widget_owners.pop(id(view), None)
        return True

    def shutdown_all(self) -> bool:
        for endpoint in tuple(self._bundles):
            if not self.discard(endpoint):
                return False
        return True


class CustomerSessionEmptyState(QWidget):
    """Shared empty state for pages that require a selected endpoint."""

    add_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("customerSessionEmptyState")
        self._has_devices = False
        self._theme = "dark"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        self._title = QLabel()
        self._title.setObjectName("customerSessionEmptyTitle")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setWordWrap(True)
        layout.addWidget(self._title)
        self._add_button = QPushButton()
        self._add_button.setObjectName("customerSessionEmptyAdd")
        self._add_button.setMaximumWidth(180)
        self._add_button.clicked.connect(self.add_requested)
        layout.addWidget(self._add_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        self.retranslate_ui()

    @property
    def has_devices(self) -> bool:
        return self._has_devices

    def set_has_devices(self, has_devices: bool) -> None:
        self._has_devices = bool(has_devices)
        self._add_button.setVisible(not self._has_devices)
        self.retranslate_ui()

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
        pal = S.palette(theme)
        self.setStyleSheet(
            f"#customerSessionEmptyState {{ background: {pal['bg']}; }}"
            f"#customerSessionEmptyTitle {{ color: {pal['text_muted']}; }}"
            f"#customerSessionEmptyAdd {{ background: {pal['card_2']}; "
            f"color: {pal['text']}; border: 1px solid {pal['border']}; "
            f"border-radius: 5px; padding: 7px 12px; }}"
        )

    def retranslate_ui(self) -> None:
        if self._has_devices:
            self._title.setText(tr("Select a customer device from the list"))
        else:
            self._title.setText(tr("No customer devices. Add a device to continue."))
        self._add_button.setText(tr("Add device"))
        self._add_button.setAccessibleName(tr("Add device"))


class CustomerEndpointPageStack(QWidget):
    """Switch one session-bound page role among endpoint-fixed lazy hosts."""

    status_message = Signal(object, str, int)
    view_created = Signal(object, str, object)
    add_requested = Signal()

    def __init__(
        self,
        page_id: str,
        registry: CustomerPageBundleRegistry,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        if page_id not in _SESSION_PAGE_IDS:
            raise ValueError(f"unsupported customer session page: {page_id}")
        self.page_id = page_id
        self._registry = registry
        self._endpoints: tuple[Endpoint, ...] = ()
        self._active_endpoint: Optional[Endpoint] = None
        self._hosts: dict[Endpoint, LazyViewHost] = {}
        self._view_active = False
        self._theme = "dark"
        self._scale = "small"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._stack = QStackedWidget()
        layout.addWidget(self._stack)
        self._empty = CustomerSessionEmptyState()
        self._empty.add_requested.connect(self.add_requested)
        self._stack.addWidget(self._empty)
        self._stack.setCurrentWidget(self._empty)

    @property
    def endpoints(self) -> tuple[Endpoint, ...]:
        return self._endpoints

    @property
    def active_endpoint(self) -> Optional[Endpoint]:
        return self._active_endpoint

    @property
    def active_view(self) -> Optional[QWidget]:
        if self._active_endpoint is None:
            return None
        host = self._hosts.get(self._active_endpoint)
        return host.view if host is not None else None

    @property
    def current_host(self) -> Optional[LazyViewHost]:
        if self._active_endpoint is None:
            return None
        return self._hosts.get(self._active_endpoint)

    @property
    def empty_state(self) -> CustomerSessionEmptyState:
        return self._empty

    def set_endpoints(self, endpoints: tuple[Endpoint, ...]) -> tuple[Endpoint, ...]:
        normalized = tuple(coerce_endpoint(endpoint) for endpoint in endpoints)
        if len(set(normalized)) != len(normalized):
            raise ValueError("customer endpoint page stack received duplicate endpoints")
        removed = tuple(endpoint for endpoint in self._endpoints if endpoint not in normalized)
        for endpoint in removed:
            host = self._hosts.pop(endpoint, None)
            if host is None:
                continue
            deactivate_view(host)
            self._stack.removeWidget(host)
            host.setParent(None)
            host.deleteLater()
        self._endpoints = normalized
        self._empty.set_has_devices(bool(normalized))
        if self._active_endpoint not in normalized:
            self.set_active_endpoint(None)
        return removed

    def _host_for(self, endpoint: Endpoint) -> LazyViewHost:
        host = self._hosts.get(endpoint)
        if host is not None:
            return host
        host = LazyViewHost(
            lambda endpoint=endpoint: self._registry.page(endpoint, self.page_id)
        )
        host.set_theme(self._theme, self._scale)
        host.status_message.connect(
            lambda message, timeout, endpoint=endpoint: self.status_message.emit(
                endpoint,
                message,
                timeout,
            )
        )
        host.view_created.connect(
            lambda view, endpoint=endpoint: self.view_created.emit(
                endpoint,
                self.page_id,
                view,
            )
        )
        self._hosts[endpoint] = host
        self._stack.addWidget(host)
        return host

    def set_active_endpoint(self, endpoint: Optional[Endpoint]) -> None:
        normalized = coerce_endpoint(endpoint) if endpoint is not None else None
        if normalized not in self._endpoints:
            normalized = None
        target = self._empty if normalized is None else self._host_for(normalized)
        previous = self._stack.currentWidget()
        if previous is target and self._active_endpoint == normalized:
            return
        if self._view_active:
            deactivate_view(previous)
        self._active_endpoint = normalized
        self._stack.setCurrentWidget(target)
        if self._view_active:
            activate_view(target)

    def ensure_active_view(self) -> Optional[QWidget]:
        if self._active_endpoint is None:
            return None
        return self._host_for(self._active_endpoint).ensure_view()

    def activate_view(self) -> None:
        if self._view_active:
            return
        self._view_active = True
        activate_view(self._stack.currentWidget())

    def deactivate_view(self) -> None:
        if not self._view_active:
            return
        self._view_active = False
        deactivate_view(self._stack.currentWidget())

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
        self._scale = scale
        self._empty.set_theme(theme, scale)
        for host in self._hosts.values():
            host.set_theme(theme, scale)

    def retranslate_ui(self) -> None:
        self._empty.retranslate_ui()
        for host in self._hosts.values():
            host.retranslate_ui()

    def shutdown(self) -> None:
        self.deactivate_view()
        for host in tuple(self._hosts.values()):
            deactivate_view(host)
            self._stack.removeWidget(host)
            host.setParent(None)
            host.deleteLater()
        self._hosts.clear()
        self._endpoints = ()
        self._active_endpoint = None
        self._empty.set_has_devices(False)
        self._stack.setCurrentWidget(self._empty)


__all__ = [
    "CustomerEndpointPageBundle",
    "CustomerEndpointPageStack",
    "CustomerPageBundleAdapter",
    "CustomerPageBundleRegistry",
    "CustomerSessionEmptyState",
]
