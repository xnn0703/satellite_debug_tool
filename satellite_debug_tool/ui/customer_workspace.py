"""Customer-facing AFD01 workspace sharing the engineering Live session."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
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
    """Stable customer navigation layered over one shared ``LiveView``."""

    status_message = Signal(str, int)

    _PAGE_DEFS = (
        ("overview", tr_source("Overview"), "grid"),
        ("rf", tr_source("RF control"), "sliders"),
        ("playback", tr_source("Playback"), "history"),
        ("maintenance", tr_source("Maintenance"), "settings"),
    )

    def __init__(self, live_view, settings, device_view, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._live = live_view
        self._settings = settings
        self._device = device_view
        self._theme = "dark"
        self._page_ids: list[str] = []
        self._nav_buttons: list[QPushButton] = []
        self._build_ui()
        register_translatable(self)

    @property
    def overview(self) -> CustomerOverviewView:
        return self._overview

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._sidebar = QFrame()
        self._sidebar.setObjectName("customerSidebar")
        self._sidebar.setFixedWidth(158)
        side = QVBoxLayout(self._sidebar)
        side.setContentsMargins(9, 12, 9, 12)
        side.setSpacing(5)
        self._section_label = QLabel(tr("AFD01 operation"))
        self._section_label.setObjectName("customerSidebarTitle")
        side.addWidget(self._section_label)

        self._stack = QStackedWidget()
        self._overview = CustomerOverviewView(self._live, self._settings)
        self._rf_control = CustomerRfControlView(self._live)
        self._playback = CustomerPlaybackView(self._settings)
        self._maintenance = CustomerMaintenanceView(
            self._live, self._device, self._settings
        )
        overview_scroll = QScrollArea()
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
        rf_scroll.setWidget(self._rf_control)

        pages = (
            overview_scroll,
            rf_scroll,
            self._playback,
            self._maintenance,
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
        self._rf_control.status_message.connect(self.status_message)
        self._playback.status_message.connect(self.status_message)
        self.set_page(0)

    def set_page(self, page: int | str) -> None:
        if isinstance(page, str):
            try:
                index = self._page_ids.index(page)
            except ValueError:
                index = 0
        else:
            index = max(0, min(int(page), self._stack.count() - 1))
        self._stack.setCurrentIndex(index)
        for button_index, button in enumerate(self._nav_buttons):
            button.setChecked(button_index == index)
        self._refresh_nav_icons()

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = theme
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
        )
        self._overview.set_theme(theme, scale)
        self._rf_control.set_theme(theme, scale)
        self._playback.set_theme(theme, scale)
        self._maintenance.set_theme(theme, scale)
        self._refresh_nav_icons()

    def _refresh_nav_icons(self) -> None:
        pal = S.palette(self._theme)
        for button in self._nav_buttons:
            color = pal["accent_ink"] if button.isChecked() else pal["text_2"]
            button.setIcon(
                icons.icon(str(button.property("iconName")), color=color, size=14)
            )

    def retranslate_ui(self) -> None:
        self._section_label.setText(tr("AFD01 operation"))
        for button, (_page_id, label, _icon_name) in zip(
            self._nav_buttons, self._PAGE_DEFS
        ):
            button.setText(tr(label))
