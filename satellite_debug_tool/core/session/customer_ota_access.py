"""客户 OTA 的唯一准入接口；两种界面共用同一设备控制器合同。"""

from __future__ import annotations
from typing import Callable, TYPE_CHECKING
if TYPE_CHECKING:
    from satellite_debug_tool.core.session import DeviceSessionCore, OtaController, OtaArtifactToken
    from satellite_debug_tool.core.security import FirmwarePackage


class CustomerOtaAccess:
    def __init__(self, session: DeviceSessionCore, controller: OtaController, connected: Callable[[], bool]) -> None:
        self.session = session
        self.controller = controller
        self.connected = connected

    def available(self, artifact: OtaArtifactToken | None = None) -> bool:
        return bool(self.connected() and self.controller.supported
                    and not self.controller.active and not self.session.device_transaction_active
                    and (artifact is None or self.controller.verified_artifact_matches(artifact)))

    def load(self, package: FirmwarePackage) -> OtaArtifactToken | None:
        return self.controller.configure_verified_package(package) if self.available() else None

    def start(self, artifact: OtaArtifactToken) -> bool:
        if not self.available(artifact):
            return False
        self.controller.start(pause_debug=True, required_artifact=artifact)
        return self.controller.active
