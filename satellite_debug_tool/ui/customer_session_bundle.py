"""Application composition for one permanently endpoint-bound Customer session."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.customer.device_directory import (
    CustomerDeviceSupplementalFacts,
    Endpoint,
    normalize_customer_endpoint,
)
from satellite_debug_tool.core.customer.session_binding import (
    CustomerEndpointSessionBinding,
)
from satellite_debug_tool.ui.customer_maintenance_view import CustomerMaintenanceView
from satellite_debug_tool.ui.customer_overview_view import CustomerOverviewView
from satellite_debug_tool.ui.customer_rf_control_view import CustomerRfControlView
from satellite_debug_tool.ui.device_view import DeviceView
from satellite_debug_tool.ui.live_view import LiveView
from satellite_debug_tool.ui.tracking_simulator_view import TrackingSimulatorView
from satellite_debug_tool.ui.view_lifecycle import deactivate_view


class CustomerEndpointSessionBundle:
    """Own fixed Live/Device/Tracking/controllers for exactly one endpoint."""

    def __init__(
        self,
        endpoint: Endpoint,
        *,
        settings,
        device_directory,
        session_directory,
        status_sink: Optional[Callable[[str, int], None]] = None,
        facts_changed: Optional[Callable[[Endpoint], None]] = None,
        on_shutdown: Optional[Callable[[Endpoint, object], None]] = None,
    ) -> None:
        self.endpoint = normalize_customer_endpoint(endpoint)
        self._settings = settings
        self._status_sink = status_sink
        self._facts_changed = facts_changed
        self._on_shutdown = on_shutdown
        self._theme = "dark"
        self._scale = "small"
        self._shutdown = False
        self.binding = CustomerEndpointSessionBinding(
            device_directory,
            self.endpoint,
        )
        self.live = LiveView(
            settings,
            session_registry=session_directory,
            customer_binding=self.binding,
            defer_presentation=True,
        )
        self._live_status_slot = status_sink
        self._recording_facts_slot = None
        self._connection_facts_slot = None
        self._resync_facts_slot = None
        if status_sink is not None:
            self.live.status_message.connect(status_sink)
        if facts_changed is not None:
            self._recording_facts_slot = (
                lambda _active, _path: facts_changed(self.endpoint)
            )
            self._connection_facts_slot = (
                lambda _attached: facts_changed(self.endpoint)
            )
            self._resync_facts_slot = (
                lambda _required: facts_changed(self.endpoint)
            )
            self.live.recording_state_changed.connect(self._recording_facts_slot)
            self.live.capture_profile_resync_changed.connect(
                self._resync_facts_slot
            )
            self.binding.connection_state_changed.connect(
                self._connection_facts_slot
            )
        self._overview: Optional[CustomerOverviewView] = None
        self._rf: Optional[CustomerRfControlView] = None
        self._maintenance: Optional[CustomerMaintenanceView] = None
        self._device: Optional[DeviceView] = None
        self._tracking: Optional[TrackingSimulatorView] = None

    @property
    def overview(self):
        return self._create_overview

    @property
    def rf(self):
        return self._create_rf

    @property
    def maintenance(self):
        return self._create_maintenance

    def page(self, page_id: str):
        return {
            "overview": self._create_overview,
            "rf": self._create_rf,
            "maintenance": self._create_maintenance,
        }[page_id]

    def _finish_view(self, view):
        if hasattr(view, "set_theme"):
            view.set_theme(self._theme, self._scale)
        return view

    def _create_overview(self) -> CustomerOverviewView:
        if self._overview is None:
            self._overview = self._finish_view(
                CustomerOverviewView(self.live, self._settings)
            )
        return self._overview

    def _create_rf(self) -> CustomerRfControlView:
        if self._rf is None:
            self._rf = self._finish_view(
                CustomerRfControlView(
                    self.live,
                    operation_gateway_factory=self.binding.new_operation_gateway,
                )
            )
        return self._rf

    def device_view(self) -> DeviceView:
        if self._device is None:
            device = DeviceView(
                settings=self._settings,
                session_core=self.binding.core,
                operation_gateway_factory=self.binding.new_operation_gateway,
                connection_state_provider=lambda: self.binding.attached,
                command_sender=self.binding.command_sender,
            )
            device.debug_mode_requested.connect(self.live.request_debug_mode)
            self.live.debug_request_finished.connect(device.on_debug_request_finished)
            self.live.debug_state_changed.connect(device.set_debug_state)
            self.binding.connection_state_changed.connect(
                device._apply_connection_state
            )
            device._apply_connection_state(self.binding.attached)
            device.set_debug_state(self.live.is_debug_enabled())
            self._device = self._finish_view(device)
        return self._device

    def _create_maintenance(self) -> CustomerMaintenanceView:
        if self._maintenance is None:
            self._maintenance = self._finish_view(
                CustomerMaintenanceView(
                    self.live,
                    self.device_view(),
                    self._settings,
                    operation_gateway_factory=self.binding.new_operation_gateway,
                )
            )
        return self._maintenance

    def tracking_view(self) -> TrackingSimulatorView:
        if self._tracking is None:
            self._tracking = self._finish_view(
                TrackingSimulatorView(
                    self.binding.core,
                    operation_gateway_factory=self.binding.new_operation_gateway,
                )
            )
        return self._tracking

    @property
    def recording_active(self) -> bool:
        return self.live.is_recording()

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = str(theme)
        self._scale = str(scale)
        self.live.set_theme(theme, scale)
        for view in (
            self._overview,
            self._rf,
            self._maintenance,
            self._device,
            self._tracking,
        ):
            if view is not None and hasattr(view, "set_theme"):
                view.set_theme(theme, scale)

    def retranslate_ui(self) -> None:
        for view in (
            self.live,
            self._overview,
            self._rf,
            self._maintenance,
            self._device,
            self._tracking,
        ):
            if view is not None and hasattr(view, "retranslate_ui"):
                view.retranslate_ui()

    def shutdown(self) -> bool:
        if self._shutdown:
            return True
        for view in (
            self._overview,
            self._rf,
            self._maintenance,
            self.live,
            self._device,
            self._tracking,
        ):
            if view is not None:
                deactivate_view(view)
        if not self.live.shutdown():
            if not self.live.prepare_session_shutdown():
                return False
            if not self.live.shutdown():
                return False
        self._shutdown = True
        if self._live_status_slot is not None:
            self._disconnect_signal(
                self.live.status_message,
                self._live_status_slot,
            )
        if self._recording_facts_slot is not None:
            self._disconnect_signal(
                self.live.recording_state_changed,
                self._recording_facts_slot,
            )
        if self._connection_facts_slot is not None:
            self._disconnect_signal(
                self.binding.connection_state_changed,
                self._connection_facts_slot,
            )
        if self._resync_facts_slot is not None:
            self._disconnect_signal(
                self.live.capture_profile_resync_changed,
                self._resync_facts_slot,
            )
        self.binding.shutdown()
        if self._on_shutdown is not None:
            self._on_shutdown(self.endpoint, self)
        seen: set[int] = set()
        for view in (
            self._overview,
            self._rf,
            self._maintenance,
            self.live,
            self._device,
            self._tracking,
        ):
            if view is None or id(view) in seen:
                continue
            seen.add(id(view))
            view.deleteLater()
        self.binding.deleteLater()
        return True

    @staticmethod
    def _disconnect_signal(signal: object, slot: object) -> None:
        try:
            signal.disconnect(slot)
        except (RuntimeError, TypeError):
            pass


class CustomerEndpointSessionBundleFactory(QObject):
    """Lazy bundle owner and supplemental-fact provider for Customer Directory."""

    device_changed = Signal(object)

    def __init__(
        self,
        *,
        settings,
        session_directory,
        status_sink: Optional[Callable[[str, int], None]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._session_directory = session_directory
        self._status_sink = status_sink
        self._device_directory = None
        self._bundles: dict[Endpoint, CustomerEndpointSessionBundle] = {}
        self._theme = "dark"
        self._scale = "small"
        self._directory_devices_signal = None

    def bind_device_directory(self, device_directory) -> None:
        if self._device_directory is not None:
            raise RuntimeError("customer bundle factory already has a device directory")
        self._device_directory = device_directory
        devices_signal = getattr(device_directory, "devices_changed", None)
        if devices_signal is not None and hasattr(devices_signal, "connect"):
            devices_signal.connect(self._reconcile_bundles)
            self._directory_devices_signal = devices_signal

    def __call__(self, *args):
        if len(args) == 1:
            return self.bundle(args[0])
        if len(args) == 2:
            return self.facts(args[0], args[1])
        raise TypeError("customer bundle factory expects endpoint[, runtime]")

    def bundle(self, endpoint: Endpoint) -> CustomerEndpointSessionBundle:
        normalized = normalize_customer_endpoint(endpoint)
        if self._device_directory is None:
            raise RuntimeError("customer bundle factory is not bound")
        runtime = self._device_directory.runtime(normalized)
        if runtime is None:
            raise ValueError("customer endpoint has no configured runtime")
        existing = self._bundles.get(normalized)
        if existing is not None:
            if existing.binding.runtime is runtime and not existing.binding.closed:
                return existing
            if not existing.shutdown():
                raise RuntimeError(
                    "customer bundle still owns an active local resource"
                )
        bundle = CustomerEndpointSessionBundle(
            normalized,
            settings=self._settings,
            device_directory=self._device_directory,
            session_directory=self._session_directory,
            status_sink=self._status_sink,
            facts_changed=self.device_changed.emit,
            on_shutdown=self._forget_bundle,
        )
        bundle.set_theme(self._theme, self._scale)
        self._bundles[normalized] = bundle
        return bundle

    def existing_bundle(
        self,
        endpoint: Endpoint,
    ) -> Optional[CustomerEndpointSessionBundle]:
        return self._bundles.get(normalize_customer_endpoint(endpoint))

    def _forget_bundle(self, endpoint: Endpoint, bundle: object) -> None:
        if self._bundles.get(endpoint) is bundle:
            self._bundles.pop(endpoint, None)

    def _reconcile_bundles(self) -> None:
        directory = self._device_directory
        if directory is None:
            return
        configured = set(directory.endpoints())
        for endpoint, bundle in tuple(self._bundles.items()):
            runtime = directory.runtime(endpoint) if endpoint in configured else None
            if runtime is bundle.binding.runtime and not bundle.binding.closed:
                continue
            bundle.shutdown()

    def facts(self, endpoint: Endpoint, runtime) -> CustomerDeviceSupplementalFacts:
        normalized = normalize_customer_endpoint(endpoint)
        bundle = self._bundles.get(normalized)
        return CustomerDeviceSupplementalFacts(
            business_state=(
                "CAPTURE_PROFILE_RESYNC_REQUIRED"
                if bundle is not None and bundle.live.capture_profile_resync_required
                else ""
            ),
            recording_active=bool(bundle is not None and bundle.recording_active),
        )

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = str(theme)
        self._scale = str(scale)
        for bundle in self._bundles.values():
            bundle.set_theme(theme, scale)

    def retranslate_ui(self) -> None:
        for bundle in self._bundles.values():
            bundle.retranslate_ui()

    def shutdown_all(self) -> bool:
        for bundle in tuple(self._bundles.values()):
            if not bundle.shutdown():
                return False
        signal = self._directory_devices_signal
        self._directory_devices_signal = None
        if signal is not None:
            try:
                signal.disconnect(self._reconcile_bundles)
            except (RuntimeError, TypeError):
                pass
        return True


__all__ = [
    "CustomerEndpointSessionBundle",
    "CustomerEndpointSessionBundleFactory",
]
