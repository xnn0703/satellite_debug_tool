"""Application composition for one permanently endpoint-bound Customer session."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal, Slot

from satellite_debug_tool.core.customer.device_directory import (
    CustomerDeviceSupplementalFacts,
    Endpoint,
    normalize_customer_endpoint,
)
from satellite_debug_tool.core.customer.session_binding import (
    CustomerEndpointSessionBinding,
)
from satellite_debug_tool.core.external_power_monitor import (
    ExternalPowerMonitor,
    ExternalPowerStore,
)
from satellite_debug_tool.core.iperf_test import IperfTestController, IperfTestStore
from satellite_debug_tool.ui.customer_iperf_view import CustomerIperfView
from satellite_debug_tool.ui.customer_maintenance_view import CustomerMaintenanceView
from satellite_debug_tool.ui.customer_overview_view import CustomerOverviewView
from satellite_debug_tool.ui.customer_rf_control_view import CustomerRfControlView
from satellite_debug_tool.ui.device_view import DeviceView
from satellite_debug_tool.ui.live_view import LiveView
from satellite_debug_tool.ui.external_power_window import ExternalPowerHistoryWindow
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
        external_power_store=None,
        external_power_presenter: Optional[Callable[[object], None]] = None,
        power_host_claim=None,
        iperf_resource_claim=None,
        iperf_resource_release=None,
    ) -> None:
        self.endpoint = normalize_customer_endpoint(endpoint)
        self._settings = settings
        self._status_sink = status_sink
        self._facts_changed = facts_changed
        self._on_shutdown = on_shutdown
        self._external_power_store = external_power_store
        self._external_power_presenter = external_power_presenter
        self._power_host_claim = power_host_claim
        self._iperf_resource_claim = iperf_resource_claim
        self._iperf_resource_release = iperf_resource_release
        self._customer_active = True
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
        self.device_id = ""
        self._power_profile: dict[str, object] = {}
        self._iperf_profile: dict[str, object] = {}
        self._power_monitor: Optional[ExternalPowerMonitor] = None
        self._power_window: Optional[ExternalPowerHistoryWindow] = None
        self._iperf_store: Optional[IperfTestStore] = None
        self._iperf_controller: Optional[IperfTestController] = None
        record_provider = getattr(device_directory, "device_record", None)
        if callable(record_provider):
            record = record_provider(self.endpoint)
            self.device_id = str(record.get("id", ""))
            self._power_profile = dict(record.get("external_power", {}))
            self._iperf_profile = dict(record.get("iperf", {}))
            self._external_power_store = ExternalPowerStore()
            self._power_monitor = ExternalPowerMonitor(self._external_power_store)
            self._power_monitor.configure(
                str(self._power_profile.get("host", "")),
                float(self._power_profile.get("voltage_set_v", 12.0)),
                float(self._power_profile.get("current_set_a", 12.0)),
            )
            self._power_monitor.set_active(True)
            self._iperf_store = IperfTestStore()
            self._iperf_controller = IperfTestController(
                self._iperf_store,
                self._external_power_store,
                Path(settings.config_directory) / "iperf_sessions" / self.device_id,
                resource_claim=(
                    (lambda config: self._iperf_resource_claim(self.endpoint, config))
                    if self._iperf_resource_claim is not None
                    else None
                ),
                resource_release=(
                    (lambda: self._iperf_resource_release(self.endpoint))
                    if self._iperf_resource_release is not None
                    else None
                ),
            )
            self._iperf_controller.active_changed.connect(
                lambda _active: self._sync_power_activity()
            )
            self._external_power_store.sample_received.connect(
                lambda sample: self.live.record_external_power_sample(
                    sample,
                    device_id=self.device_id,
                    endpoint=self.endpoint,
                )
            )
            self._power_monitor.action_finished.connect(
                lambda outcome: self.live.record_external_power_action(
                    outcome,
                    device_id=self.device_id,
                    endpoint=self.endpoint,
                )
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
        self._iperf: Optional[CustomerIperfView] = None
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

    @property
    def iperf(self):
        return self._create_iperf

    def page(self, page_id: str):
        return {
            "overview": self._create_overview,
            "rf": self._create_rf,
            "maintenance": self._create_maintenance,
            "iperf": self._create_iperf,
        }[page_id]

    def _finish_view(self, view):
        if hasattr(view, "set_theme"):
            view.set_theme(self._theme, self._scale)
        return view

    def _create_overview(self) -> CustomerOverviewView:
        if self._overview is None:
            self._overview = self._finish_view(
                CustomerOverviewView(
                    self.live,
                    self._settings,
                    external_power_store=self._external_power_store,
                    external_power_presenter=self._show_external_power,
                )
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

    def _create_iperf(self) -> CustomerIperfView:
        if self._iperf is None:
            if self._iperf_controller is None or self._iperf_store is None:
                raise RuntimeError("device iperf owner is unavailable")
            self._iperf = self._finish_view(
                CustomerIperfView(
                    self._iperf_controller,
                    self._iperf_store,
                    self._external_power_store,
                    self._settings,
                    device_id=self.device_id,
                    endpoint=self.endpoint,
                    profile_provider=lambda: dict(self._iperf_profile),
                    save_profile=self._save_iperf_profile,
                )
            )
            if self._status_sink is not None:
                self._iperf.status_message.connect(self._status_sink)
        return self._iperf

    def _show_external_power(self, anchor) -> None:
        if self._external_power_store is None:
            return
        if self._power_window is None:
            self._power_window = ExternalPowerHistoryWindow(
                self._external_power_store,
                theme=self._theme,
                monitor=self._power_monitor,
                device_label=f"{self.endpoint[0]}:{self.endpoint[1]}",
                profile=self._power_profile,
                save_profile=self._save_power_profile,
            )
        self._power_window.present_near(anchor)

    def _save_power_profile(self, profile: dict[str, object]) -> None:
        host = str(profile.get("host", "")).strip()
        if self._power_host_claim is not None:
            self._power_host_claim(self.endpoint, host)
        self.binding.directory.update_accessories(
            self.endpoint,
            external_power=profile,
        )
        self._power_profile = dict(profile)

    def _save_iperf_profile(self, profile: dict[str, object]) -> None:
        self.binding.directory.update_accessories(
            self.endpoint,
            iperf=profile,
        )
        self._iperf_profile = dict(profile)

    def set_customer_active(self, active: bool) -> None:
        self._customer_active = bool(active)
        self._sync_power_activity()

    def _sync_power_activity(self) -> None:
        if self._power_monitor is None:
            return
        iperf_active = bool(
            self._iperf_controller is not None and self._iperf_controller.active
        )
        self._power_monitor.set_active(self._customer_active or iperf_active)

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
            self._iperf,
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
            self._iperf,
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
            self._iperf,
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
        if self._iperf_controller is not None and not self._iperf_controller.shutdown():
            return False
        if self._power_monitor is not None and not self._power_monitor.shutdown():
            return False
        if self._power_window is not None:
            self._power_window.close()
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
            self._iperf,
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
        external_power_store=None,
        external_power_presenter: Optional[Callable[[object], None]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._session_directory = session_directory
        self._status_sink = status_sink
        self._external_power_store = external_power_store
        self._external_power_presenter = external_power_presenter
        self._device_directory = None
        self._bundles: dict[Endpoint, CustomerEndpointSessionBundle] = {}
        self._theme = "dark"
        self._scale = "small"
        self._directory_devices_signal = None
        self._customer_active = True
        self._iperf_claims: dict[Endpoint, object] = {}

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
            external_power_store=self._external_power_store,
            external_power_presenter=self._external_power_presenter,
            power_host_claim=self._claim_power_host,
            iperf_resource_claim=self._claim_iperf_resources,
            iperf_resource_release=self._release_iperf_resources,
        )
        bundle.set_customer_active(self._customer_active)
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

    def _claim_power_host(self, owner: Endpoint, host: str) -> None:
        normalized = str(host).strip()
        if not normalized or self._device_directory is None:
            return
        for endpoint in self._device_directory.endpoints():
            if endpoint == owner:
                continue
            record = self._device_directory.device_record(endpoint)
            profile = record.get("external_power", {})
            if isinstance(profile, dict) and str(profile.get("host", "")).strip() == normalized:
                raise ValueError(
                    f"external power {normalized}:2268 is already assigned to "
                    f"{endpoint[0]}:{endpoint[1]}"
                )

    def _claim_iperf_resources(self, owner: Endpoint, config) -> None:
        requested_ports = {
            (config.server, config.protocol.value, config.ul_port)
            if direction == "ul"
            else (config.server, config.protocol.value, config.dl_port)
            for direction in config.direction.members()
        }
        for endpoint, existing in self._iperf_claims.items():
            if endpoint == owner:
                continue
            if existing.local_host == config.local_host:
                raise ValueError(
                    f"local IPv4 {config.local_host} is already used by "
                    f"{endpoint[0]}:{endpoint[1]}"
                )
            existing_ports = {
                (existing.server, existing.protocol.value, existing.ul_port)
                if direction == "ul"
                else (existing.server, existing.protocol.value, existing.dl_port)
                for direction in existing.direction.members()
            }
            conflict = requested_ports & existing_ports
            if conflict:
                server, protocol, port = sorted(conflict)[0]
                raise ValueError(
                    f"iperf3 {server} {protocol} port {port} is already used by "
                    f"{endpoint[0]}:{endpoint[1]}"
                )
        self._iperf_claims[owner] = config

    def _release_iperf_resources(self, owner: Endpoint) -> None:
        self._iperf_claims.pop(owner, None)

    def set_customer_active(self, active: bool) -> None:
        self._customer_active = bool(active)
        for bundle in self._bundles.values():
            bundle.set_customer_active(active)

    @property
    def any_iperf_active(self) -> bool:
        return any(
            bundle._iperf_controller is not None
            and bundle._iperf_controller.active
            for bundle in self._bundles.values()
        )

    @property
    def any_power_action_pending(self) -> bool:
        return any(
            bundle._power_monitor is not None
            and bundle._power_monitor.action_pending
            for bundle in self._bundles.values()
        )

    def confirmed_power_outputs(self) -> tuple[Endpoint, ...]:
        result: list[Endpoint] = []
        for endpoint, bundle in self._bundles.items():
            store = bundle._external_power_store
            sample = store.snapshot.sample if store is not None else None
            if sample is not None and sample.output_enabled:
                result.append(endpoint)
        return tuple(result)

    def disable_confirmed_outputs(self, timeout_ms: int = 6000) -> bool:
        from satellite_debug_tool.core.external_power_monitor import ExternalPowerAction

        for endpoint in self.confirmed_power_outputs():
            bundle = self._bundles[endpoint]
            monitor = bundle._power_monitor
            if monitor is None:
                return False
            loop = QEventLoop()
            outcome_holder: list[object] = []

            def finished(outcome) -> None:
                if outcome.action is ExternalPowerAction.DISABLE:
                    outcome_holder.append(outcome)
                    loop.quit()

            monitor.action_finished.connect(finished)
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(loop.quit)
            try:
                monitor.request_action(ExternalPowerAction.DISABLE)
                timer.start(max(1, int(timeout_ms)))
                loop.exec()
            finally:
                timer.stop()
                try:
                    monitor.action_finished.disconnect(finished)
                except (RuntimeError, TypeError):
                    pass
            if not outcome_holder or not outcome_holder[-1].succeeded:
                return False
        return True

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
                else (
                    "NETWORK_TEST_RUNNING"
                    if bundle is not None
                    and bundle._iperf_controller is not None
                    and bundle._iperf_controller.active
                    else ""
                )
            ),
            recording_active=bool(bundle is not None and bundle.recording_active),
            operation_busy=bool(
                bundle is not None
                and (
                    (
                        bundle._iperf_controller is not None
                        and bundle._iperf_controller.active
                    )
                    or (
                        bundle._external_power_store is not None
                        and bundle._external_power_store.snapshot.sample is not None
                        and bundle._external_power_store.snapshot.sample.output_enabled
                    )
                    or (
                        bundle._power_monitor is not None
                        and bundle._power_monitor.action_pending
                    )
                )
            ),
        )

    def set_theme(self, theme: str, scale: str = "small") -> None:
        self._theme = str(theme)
        self._scale = str(scale)
        for bundle in self._bundles.values():
            bundle.set_theme(theme, scale)

    def retranslate_ui(self) -> None:
        for bundle in self._bundles.values():
            bundle.retranslate_ui()

    @Slot(object)
    def record_external_power_sample(self, sample: object) -> None:
        # M28 samples are connected directly to their fixed endpoint bundle.
        return None

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
