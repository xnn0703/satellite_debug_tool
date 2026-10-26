"""用户固件页的轻量 OTA 呈现接口，共用正式 OTA 状态机和事务租约。"""

from PySide6.QtCore import QObject, Signal, Slot
from satellite_debug_tool.core.session import OtaController
from satellite_debug_tool.core.session.customer_ota_access import CustomerOtaAccess
from satellite_debug_tool.ui.ota_status_text import ota_status_source


class CustomerOtaAdapter(QObject):
    ota_status_changed = Signal(str, object, int, bool)
    ota_artifact_changed = Signal(object)

    def __init__(self, live, binding):
        super().__init__(live)
        self._progress = 0
        self._ota_controller = OtaController(binding.core, parent=self,
                                             operation_gateway=binding.new_operation_gateway())
        self._access = CustomerOtaAccess(binding.core, self._ota_controller, lambda: binding.attached)
        self._ota_controller.status_changed.connect(self._on_status)
        self._ota_controller.progress_changed.connect(self._on_progress)
        self._ota_controller.artifact_changed.connect(self.ota_artifact_changed)
        self._ota_controller.debug_mode_requested.connect(live.request_debug_mode)
        live.debug_request_finished.connect(self._ota_controller.on_debug_request_finished)
        live.debug_state_changed.connect(self._ota_controller.set_debug_state)
        self._ota_controller.set_debug_state(live.is_debug_enabled())

    @Slot(object, object)
    def _on_status(self, status, values):
        self.ota_status_changed.emit(ota_status_source(status, values), dict(values),
                                     self._progress, self._ota_controller.active)

    @Slot(int)
    def _on_progress(self, value):
        self._progress = value

    def customer_ota_available(self, artifact=None):
        return self._access.available(artifact)

    def load_customer_ota_package(self, package):
        return self._access.load(package)

    def start_customer_ota(self, artifact):
        return self._access.start(artifact)

    def abort_customer_ota(self):
        self._ota_controller.abort()
