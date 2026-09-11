"""Stable Engineering tab host for serial or the selected Customer UDP session."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.i18n import tr
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.view_lifecycle import activate_view, deactivate_view


ENGINEERING_SERIAL = "serial"
ENGINEERING_SHARED_UDP = "shared_udp"


class EngineeringSessionHost(QWidget):
    """Keep endpoint widgets fixed while switching only the visible owner."""

    status_message = Signal(str, int)

    def __init__(
        self,
        page_kind: str,
        *,
        serial_provider: Callable[[], QWidget],
        device_directory,
        bundle_factory,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        if page_kind not in {"live", "device", "tracking"}:
            raise ValueError("unsupported Engineering session page")
        self._page_kind = page_kind
        self._serial_provider = serial_provider
        self._device_directory = device_directory
        self._bundle_factory = bundle_factory
        self._mode = ENGINEERING_SERIAL
        self._serial_view: Optional[QWidget] = None
        self._endpoint_views: dict[tuple[str, int], QWidget] = {}
        self._active_view: Optional[QWidget] = None
        self._view_active = False
        self._theme = "dark"
        self._scale = "small"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget()
        self._empty = QLabel()
        self._empty.setObjectName("engineeringSessionEmpty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._stack.addWidget(self._empty)
        layout.addWidget(self._stack)

        device_directory.active_endpoint_changed.connect(
            lambda _endpoint: self.refresh_target()
        )
        device_directory.devices_changed.connect(self._on_devices_changed)
        device_directory.attachment_changed.connect(
            lambda _endpoint, _attached: self.refresh_target()
        )
        self.retranslate_ui()
        self.refresh_target()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def active_view(self) -> Optional[QWidget]:
        return self._active_view

    def set_mode(self, mode: str) -> None:
        normalized = str(mode)
        if normalized not in {ENGINEERING_SERIAL, ENGINEERING_SHARED_UDP}:
            raise ValueError("unknown Engineering session mode")
        if normalized == self._mode:
            return
        self._mode = normalized
        self.refresh_target()

    def _serial(self) -> QWidget:
        if self._serial_view is None:
            self._serial_view = self._serial_provider()
            self._adopt_view(self._serial_view)
        return self._serial_view

    def _endpoint_view(self, endpoint: tuple[str, int]) -> QWidget:
        view = self._endpoint_views.get(endpoint)
        if view is not None:
            return view
        bundle = self._bundle_factory.bundle(endpoint)
        if self._page_kind == "live":
            view = bundle.live
        elif self._page_kind == "device":
            view = bundle.device_view()
        else:
            view = bundle.tracking_view()
        self._endpoint_views[endpoint] = view
        self._adopt_view(view)
        return view

    def _adopt_view(self, view: QWidget) -> None:
        if self._stack.indexOf(view) < 0:
            self._stack.addWidget(view)
        status = getattr(view, "status_message", None)
        if status is not None and hasattr(status, "connect"):
            status.connect(self.status_message)
        if hasattr(view, "set_theme"):
            view.set_theme(self._theme, self._scale)

    def refresh_target(self) -> None:
        previous = self._active_view
        if self._mode == ENGINEERING_SERIAL:
            target: Optional[QWidget] = self._serial()
            empty_source = ""
        else:
            endpoint = self._device_directory.active_endpoint()
            attached = bool(
                endpoint is not None
                and self._device_directory.attachment(endpoint) is not None
            )
            target = self._endpoint_view(endpoint) if attached else None
            empty_source = (
                "Select a customer device"
                if endpoint is None
                else "Connect the selected customer device first"
            )
        if previous is target:
            return
        if self._view_active and previous is not None:
            deactivate_view(previous)
        self._active_view = target
        if target is None:
            self._empty.setProperty("sourceText", empty_source)
            self._empty.setText(tr(empty_source))
            self._stack.setCurrentWidget(self._empty)
        else:
            self._stack.setCurrentWidget(target)
            if self._view_active:
                activate_view(target)

    def _on_devices_changed(self) -> None:
        configured = set(self._device_directory.endpoints())
        for endpoint, view in tuple(self._endpoint_views.items()):
            if endpoint in configured:
                continue
            if self._active_view is view:
                self._active_view = None
            self._endpoint_views.pop(endpoint, None)
            self._stack.removeWidget(view)
        self.refresh_target()

    def activate_view(self) -> None:
        if self._view_active:
            return
        self._view_active = True
        self.refresh_target()
        if self._active_view is not None:
            activate_view(self._active_view)

    def deactivate_view(self) -> None:
        if not self._view_active:
            return
        self._view_active = False
        if self._active_view is not None:
            deactivate_view(self._active_view)

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = str(theme)
        self._scale = str(scale)
        palette = S.palette(theme)
        self._empty.setStyleSheet(f"color: {palette['text_muted']};")
        for view in (
            *((self._serial_view,) if self._serial_view is not None else ()),
            *self._endpoint_views.values(),
        ):
            if hasattr(view, "set_theme"):
                view.set_theme(theme, scale)

    def retranslate_ui(self) -> None:
        source = str(self._empty.property("sourceText") or "")
        self._empty.setText(tr(source))


__all__ = [
    "ENGINEERING_SERIAL",
    "ENGINEERING_SHARED_UDP",
    "EngineeringSessionHost",
]
