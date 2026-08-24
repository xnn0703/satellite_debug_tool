"""Shared lifecycle dispatch for views with visibility-scoped rendering."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ViewLifecycle(Protocol):
    """A view whose presentation work follows explicit workspace visibility."""

    def activate_view(self) -> None:
        """Refresh once and start presentation work for the visible view."""

    def deactivate_view(self) -> None:
        """Stop presentation work while preserving the underlying session."""


def activate_view(view: object) -> None:
    callback = getattr(view, "activate_view", None)
    if callable(callback):
        callback()


def deactivate_view(view: object) -> None:
    callback = getattr(view, "deactivate_view", None)
    if callable(callback):
        callback()


__all__ = ["ViewLifecycle", "activate_view", "deactivate_view"]
