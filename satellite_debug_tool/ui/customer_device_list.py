"""Customer device-directory presentation without owning session state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.i18n import tr
from satellite_debug_tool.ui import icons, styles as S


Endpoint = tuple[str, int]
MAX_CUSTOMER_DEVICES = 4


def format_endpoint(endpoint: Endpoint) -> str:
    """Return the canonical user-facing endpoint representation."""

    return f"{endpoint[0]}:{endpoint[1]}"


def _field(entry: object, name: str, default: Any = None) -> Any:
    if isinstance(entry, Mapping):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _fact_text(value: object, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, Enum):
        raw = value.value
        if isinstance(raw, str):
            return raw
        return value.name
    return str(value)


def coerce_endpoint(value: object) -> Endpoint:
    """Validate the UI boundary; canonicalization remains Directory-owned."""

    if not isinstance(value, tuple) or len(value) != 2:
        raise TypeError("customer endpoint must be a (IPv4, port) tuple")
    host, port = value
    if not isinstance(host, str) or not host:
        raise TypeError("customer endpoint IPv4 must be a non-empty string")
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError("customer endpoint port must be an integer")
    return host, port


@dataclass(frozen=True)
class CustomerDeviceSnapshot:
    """Immutable UI facts supplied by the customer Directory adapter.

    The widget never derives connection, identity authorization, or
    operation ownership.  It only renders this snapshot and emits user intent.
    """

    key: object
    endpoint: Endpoint
    display_identity: str = ""
    connection_phase: str = "DISCONNECTED"
    business_state: str = ""
    recording_active: bool = False
    operation_busy: bool = False
    identity_pending: bool = False
    identity_conflict: bool = False

    @classmethod
    def from_entry(cls, entry: object) -> "CustomerDeviceSnapshot":
        if isinstance(entry, cls):
            return entry
        endpoint = coerce_endpoint(_field(entry, "endpoint"))
        return cls(
            key=_field(entry, "key", endpoint),
            endpoint=endpoint,
            display_identity=_fact_text(_field(entry, "display_identity", "")),
            connection_phase=_fact_text(
                _field(entry, "connection_phase", "DISCONNECTED"),
                "DISCONNECTED",
            ).upper(),
            business_state=_fact_text(_field(entry, "business_state", "")),
            recording_active=bool(_field(entry, "recording_active", False)),
            operation_busy=bool(_field(entry, "operation_busy", False)),
            identity_pending=bool(_field(entry, "identity_pending", False)),
            identity_conflict=bool(_field(entry, "identity_conflict", False)),
        )


_PHASE_SOURCES = {
    "DISCONNECTED": "Disconnected",
    "WAITING": "Waiting",
    "ONLINE": "Online",
    "RECONNECTING": "Reconnecting",
}

class CustomerDeviceRow(QPushButton):
    """Checkable, two-line device selector with elision and full semantics."""

    def __init__(
        self,
        snapshot: CustomerDeviceSnapshot,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._snapshot = snapshot
        self._primary_full = ""
        self._secondary_full = ""
        self.setObjectName("customerDeviceRow")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(46)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 5, 7, 5)
        layout.setSpacing(1)
        self._primary = QLabel()
        self._primary.setObjectName("customerDevicePrimary")
        self._primary.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._secondary = QLabel()
        self._secondary.setObjectName("customerDeviceSecondary")
        self._secondary.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._primary)
        layout.addWidget(self._secondary)
        self._apply_snapshot()

    @property
    def endpoint(self) -> Endpoint:
        return self._snapshot.endpoint

    @property
    def snapshot(self) -> CustomerDeviceSnapshot:
        return self._snapshot

    def set_snapshot(self, snapshot: CustomerDeviceSnapshot) -> None:
        if snapshot.endpoint != self.endpoint:
            raise ValueError("a customer device row cannot be rebound to another endpoint")
        self._snapshot = snapshot
        self._apply_snapshot()

    def _phase_text(self) -> str:
        source = _PHASE_SOURCES.get(
            self._snapshot.connection_phase,
            self._snapshot.connection_phase,
        )
        return tr(source)

    def _state_facts(self) -> list[str]:
        facts: list[str] = []
        if self._snapshot.business_state:
            if self._snapshot.business_state == "CAPTURE_PROFILE_RESYNC_REQUIRED":
                facts.append(tr("Capture profile resync required"))
            else:
                facts.append(tr(self._snapshot.business_state))
        if self._snapshot.recording_active:
            facts.append(tr("Recording"))
        if self._snapshot.operation_busy:
            facts.append(tr("Operation busy"))
        if self._snapshot.identity_pending:
            facts.append(tr("Waiting for current-session identity"))
        if self._snapshot.identity_conflict:
            facts.append(tr("Identity conflict"))
        return facts

    def _apply_snapshot(self) -> None:
        snapshot = self._snapshot
        endpoint_text = format_endpoint(snapshot.endpoint)
        identity_is_current = bool(snapshot.display_identity) and not (
            snapshot.identity_pending or snapshot.identity_conflict
        )
        self._primary_full = snapshot.display_identity if identity_is_current else endpoint_text
        phase_text = self._phase_text()
        facts = self._state_facts()
        self._secondary_full = " · ".join((endpoint_text, phase_text, *facts))
        accessible_facts = "; ".join(facts)
        accessible = f"{self._primary_full}; {endpoint_text}; {phase_text}"
        if accessible_facts:
            accessible = f"{accessible}; {accessible_facts}"
        self.setAccessibleName(accessible)
        self.setToolTip(accessible)
        severity = "normal"
        if snapshot.identity_conflict:
            severity = "blocked"
        elif snapshot.identity_pending or snapshot.operation_busy:
            severity = "attention"
        self.setProperty("phase", snapshot.connection_phase.lower())
        self.setProperty("severity", severity)
        self._secondary.setProperty("phase", snapshot.connection_phase.lower())
        self._secondary.setProperty("severity", severity)
        for widget in (self, self._secondary):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self._elide_text()

    def _elide_text(self) -> None:
        width = max(16, self.width() - 17)
        self._primary.setText(
            self._primary.fontMetrics().elidedText(
                self._primary_full,
                Qt.TextElideMode.ElideRight,
                width,
            )
        )
        self._secondary.setText(
            self._secondary.fontMetrics().elidedText(
                self._secondary_full,
                Qt.TextElideMode.ElideRight,
                width,
            )
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide_text()

    def retranslate_ui(self) -> None:
        self._apply_snapshot()


class CustomerDeviceList(QWidget):
    """Left-rail directory view; all mutations are outward intent signals."""

    endpoint_selected = Signal(object)
    add_requested = Signal()
    edit_requested = Signal(object)
    delete_requested = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("customerDeviceList")
        self._theme = "dark"
        self._scale = "small"
        self._active_endpoint: Optional[Endpoint] = None
        self._rows: dict[Endpoint, CustomerDeviceRow] = {}
        self._snapshots: tuple[CustomerDeviceSnapshot, ...] = ()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(7, 0, 2, 0)
        header.setSpacing(2)
        self._title = QLabel(tr("Device"))
        self._title.setObjectName("customerDeviceSectionTitle")
        header.addWidget(self._title, 1)
        self._add_button = self._tool_button("plus", self._emit_add)
        self._edit_button = self._tool_button("pencil", self._emit_edit)
        self._delete_button = self._tool_button("trash", self._emit_delete)
        header.addWidget(self._add_button)
        header.addWidget(self._edit_button)
        header.addWidget(self._delete_button)
        layout.addLayout(header)

        self._rows_widget = QWidget()
        self._rows_widget.setObjectName("customerDeviceRows")
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(3)
        layout.addWidget(self._rows_widget)

        self._empty = QWidget()
        empty_layout = QVBoxLayout(self._empty)
        empty_layout.setContentsMargins(5, 7, 5, 7)
        empty_layout.setSpacing(5)
        self._empty_label = QLabel()
        self._empty_label.setObjectName("customerDeviceEmptyText")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_add = QPushButton()
        self._empty_add.setObjectName("customerEmptyAddButton")
        self._empty_add.clicked.connect(self._emit_add)
        empty_layout.addWidget(self._empty_label)
        empty_layout.addWidget(self._empty_add)
        layout.addWidget(self._empty)

        self.retranslate_ui()
        self.set_devices((), None)

    def _tool_button(self, icon_name: str, callback) -> QToolButton:
        button = QToolButton()
        button.setObjectName("customerDeviceToolButton")
        button.setProperty("iconName", icon_name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedSize(24, 24)
        button.clicked.connect(callback)
        return button

    @property
    def rows(self) -> tuple[CustomerDeviceRow, ...]:
        return tuple(self._rows[snapshot.endpoint] for snapshot in self._snapshots)

    @property
    def active_endpoint(self) -> Optional[Endpoint]:
        return self._active_endpoint

    def snapshot_for(self, endpoint: Endpoint) -> Optional[CustomerDeviceSnapshot]:
        row = self._rows.get(endpoint)
        return row.snapshot if row is not None else None

    def set_devices(
        self,
        entries: tuple[object, ...] | list[object],
        active_endpoint: Optional[Endpoint],
    ) -> None:
        snapshots = tuple(CustomerDeviceSnapshot.from_entry(entry) for entry in entries)
        if len(snapshots) > MAX_CUSTOMER_DEVICES:
            raise ValueError("customer device directory exceeds the four-device UI contract")
        endpoints = tuple(snapshot.endpoint for snapshot in snapshots)
        if len(set(endpoints)) != len(endpoints):
            raise ValueError("customer device directory contains duplicate endpoints")
        active = coerce_endpoint(active_endpoint) if active_endpoint is not None else None
        if active not in endpoints:
            active = None

        for endpoint in tuple(self._rows):
            if endpoint in endpoints:
                continue
            row = self._rows.pop(endpoint)
            self._rows_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()

        ordered_rows: list[CustomerDeviceRow] = []
        for snapshot in snapshots:
            row = self._rows.get(snapshot.endpoint)
            if row is None:
                row = CustomerDeviceRow(snapshot, self._rows_widget)
                row.clicked.connect(
                    lambda _checked=False, endpoint=snapshot.endpoint: self.endpoint_selected.emit(
                        endpoint
                    )
                )
                self._rows[snapshot.endpoint] = row
            else:
                row.set_snapshot(snapshot)
            self._rows_layout.removeWidget(row)
            self._rows_layout.addWidget(row)
            ordered_rows.append(row)

        self._snapshots = snapshots
        self._active_endpoint = active
        for row in ordered_rows:
            row.setChecked(row.endpoint == active)
        has_devices = bool(snapshots)
        self._rows_widget.setVisible(has_devices)
        self._empty.setVisible(not has_devices)
        # Keep Add actionable at capacity so the composition layer can present
        # the Directory's explicit four-device rejection instead of a silent
        # disabled control.
        self._add_button.setEnabled(True)
        self._empty_add.setEnabled(True)
        has_selection = active is not None
        self._edit_button.setEnabled(has_selection)
        self._delete_button.setEnabled(has_selection)
        self._refresh_icons()

    def _emit_add(self) -> None:
        self.add_requested.emit()

    def _emit_edit(self) -> None:
        if self._active_endpoint is not None:
            self.edit_requested.emit(self._active_endpoint)

    def _emit_delete(self) -> None:
        if self._active_endpoint is not None:
            self.delete_requested.emit(self._active_endpoint)

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
        self._scale = scale
        pal = S.palette(theme)
        self.setStyleSheet(
            f"#customerDeviceSectionTitle {{ color: {pal['text_muted']}; font-weight: 600; }}"
            f"#customerDeviceToolButton {{ background: transparent; border: 0; "
            f"border-radius: 4px; color: {pal['text_2']}; }}"
            f"#customerDeviceToolButton:hover {{ background: {pal['card_2']}; }}"
            f"#customerDeviceToolButton:disabled {{ color: {pal['text_faint']}; }}"
            f"#customerDeviceRow {{ background: transparent; border: 1px solid transparent; "
            f"border-radius: 5px; text-align: left; }}"
            f"#customerDeviceRow:hover {{ background: {pal['card_2']}; }}"
            f"#customerDeviceRow:checked {{ background: {pal['accent_dim']}; "
            f"border-color: {pal['accent']}; }}"
            f"#customerDevicePrimary {{ color: {pal['text']}; font-weight: 600; }}"
            f"#customerDeviceSecondary {{ color: {pal['text_muted']}; }}"
            f"#customerDeviceSecondary[phase='online'] {{ color: {pal['success']}; }}"
            f"#customerDeviceSecondary[phase='waiting'], "
            f"#customerDeviceSecondary[phase='reconnecting'] {{ color: {pal['warning']}; }}"
            f"#customerDeviceSecondary[severity='blocked'] {{ color: {pal['error']}; }}"
            f"#customerDeviceEmptyText {{ color: {pal['text_muted']}; }}"
            f"#customerEmptyAddButton {{ background: {pal['card_2']}; color: {pal['text']}; "
            f"border: 1px solid {pal['border']}; border-radius: 4px; padding: 4px; }}"
        )
        self._refresh_icons()

    def _refresh_icons(self) -> None:
        pal = S.palette(self._theme)
        for button in (self._add_button, self._edit_button, self._delete_button):
            color = pal["text_2"] if button.isEnabled() else pal["text_faint"]
            button.setIcon(
                icons.icon(str(button.property("iconName")), color=color, size=13)
            )

    def retranslate_ui(self) -> None:
        self._title.setText(tr("Device"))
        self._add_button.setToolTip(tr("Add device"))
        self._add_button.setAccessibleName(tr("Add device"))
        self._edit_button.setToolTip(tr("Edit selected device"))
        self._edit_button.setAccessibleName(tr("Edit selected device"))
        self._delete_button.setToolTip(tr("Delete selected device"))
        self._delete_button.setAccessibleName(tr("Delete selected device"))
        self._empty_label.setText(tr("No customer devices"))
        self._empty_add.setText(tr("Add device"))
        self._empty_add.setAccessibleName(tr("Add device"))
        for row in self._rows.values():
            row.retranslate_ui()


__all__ = [
    "CustomerDeviceList",
    "CustomerDeviceRow",
    "CustomerDeviceSnapshot",
    "Endpoint",
    "MAX_CUSTOMER_DEVICES",
    "coerce_endpoint",
    "format_endpoint",
]
