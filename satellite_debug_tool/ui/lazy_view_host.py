"""Reusable first-activation construction for heavyweight workspace pages."""

from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from satellite_debug_tool.ui.view_lifecycle import activate_view, deactivate_view


class LazyViewHost(QWidget):
    """Construct one child on demand and retain it for the process lifetime."""

    status_message = Signal(str, int)
    view_created = Signal(object)

    def __init__(
        self,
        factory: Callable[[], QWidget],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._factory = factory
        self._view: Optional[QWidget] = None
        self._view_active = False
        self._theme = "dark"
        self._scale = "small"
        self._build_elapsed_ms: Optional[float] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

    @property
    def view(self) -> Optional[QWidget]:
        return self._view

    @property
    def build_elapsed_ms(self) -> Optional[float]:
        return self._build_elapsed_ms

    def ensure_view(self) -> QWidget:
        if self._view is not None:
            return self._view
        started = time.perf_counter()
        view = self._factory()
        if not isinstance(view, QWidget):
            raise TypeError("lazy view factory must return QWidget")
        self._view = view
        self.layout().addWidget(view)
        self._build_elapsed_ms = (time.perf_counter() - started) * 1000.0
        if hasattr(view, "set_theme"):
            view.set_theme(self._theme, self._scale)
        status_signal = getattr(view, "status_message", None)
        if status_signal is not None:
            status_signal.connect(self.status_message)
        self.view_created.emit(view)
        if self._view_active:
            activate_view(view)
        return view

    def activate_view(self) -> None:
        if self._view_active:
            return
        self._view_active = True
        existing = self._view
        view = self.ensure_view()
        if existing is not None:
            activate_view(view)

    def deactivate_view(self) -> None:
        if not self._view_active:
            return
        self._view_active = False
        if self._view is not None:
            deactivate_view(self._view)

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
        self._scale = scale
        if self._view is not None and hasattr(self._view, "set_theme"):
            self._view.set_theme(theme, scale)

    def retranslate_ui(self) -> None:
        if self._view is not None and hasattr(self._view, "retranslate_ui"):
            self._view.retranslate_ui()

    def shutdown(self) -> None:
        self.deactivate_view()
        if self._view is not None and hasattr(self._view, "shutdown"):
            self._view.shutdown()


__all__ = ["LazyViewHost"]
