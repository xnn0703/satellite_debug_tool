"""Small, stable entry points for semantic Qt style properties."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QWidget


def refresh_semantic_style(widget: QWidget) -> None:
    """Re-evaluate QSS selectors after a semantic property changes."""

    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_semantic_property(widget: QWidget, name: str, value: Any) -> bool:
    """Set one QSS property and refresh only when its value changed."""

    if widget.property(name) == value:
        return False
    widget.setProperty(name, value)
    refresh_semantic_style(widget)
    return True


def set_semantic_properties(widget: QWidget, **properties: Any) -> bool:
    """Apply several QSS properties with one style refresh."""

    changed = False
    for name, value in properties.items():
        if widget.property(name) == value:
            continue
        widget.setProperty(name, value)
        changed = True
    if changed:
        refresh_semantic_style(widget)
    return changed


__all__ = [
    "refresh_semantic_style",
    "set_semantic_properties",
    "set_semantic_property",
]
