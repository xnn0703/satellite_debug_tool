"""M37 TCP shell controller：YMODEM 准入租约与最小版本回读回归。"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.protocol.frame_v2 import MetaInfo
from satellite_debug_tool.core.session.runtime import RuntimeOperationGateState
from satellite_debug_tool.core.tcp_shell.controller import (
    TcpShellController,
    YmodemAdmission,
    parse_target_version,
    ymodem_route_hint,
)

ENDPOINT = ("192.168.1.50", 4004)


class FakeCore(QObject):
    record_received = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.meta_info = MetaInfo(
            protocol_ver=2, fw_ver="0.0.131_beta", hw_type="afd01c", device_sn="SN001"
        )


class FakeGate:
    def __init__(self, state=RuntimeOperationGateState.IDLE) -> None:
        self.state = state


class FakeRuntime:
    def __init__(self, state=RuntimeOperationGateState.IDLE) -> None:
        self.operation_gate = FakeGate(state)
        self.core = FakeCore()


class FakeGateway:
    def __init__(self, *, allow_acquire: bool = True) -> None:
        self.allow_acquire = allow_acquire
        self.acquired = []
        self.released = []

    def try_acquire_operation(self, owner, *, purpose, **_kwargs) -> bool:
        if not self.allow_acquire:
            return False
        self.acquired.append(purpose)
        return True

    def release_operation(self, owner) -> bool:
        self.released.append(owner)
        return True


class FakeAttachment:
    def __init__(self, gateway: FakeGateway) -> None:
        self.gateway = gateway

    def new_operation_gateway(self) -> FakeGateway:
        return self.gateway


class FakeDeviceDirectory:
    def __init__(self, attachment=None) -> None:
        self._attachment = attachment

    def attachment(self, endpoint):
        return self._attachment


class FakeSessionDirectory:
    def __init__(self, runtime=None) -> None:
        self._runtime = runtime

    def runtime(self, endpoint):
        return self._runtime


def make_controller(*, attachment=None, runtime=None) -> TcpShellController:
    return TcpShellController(
        device_directory=FakeDeviceDirectory(attachment),
        session_directory=FakeSessionDirectory(runtime),
    )


class TestAdmission:
    def test_unmanaged_endpoint_allows_direct_transfer(self):
        result = make_controller().admit_ymodem(None)
        assert result.allowed and result.admission is YmodemAdmission.UNMANAGED
        assert result.managed_session is False

    def test_attachment_gateway_grants_managed_lease(self):
        gateway = FakeGateway(allow_acquire=True)
        controller = make_controller(attachment=FakeAttachment(gateway))
        result = controller.admit_ymodem(ENDPOINT)
        assert result.allowed and result.admission is YmodemAdmission.MANAGED
        assert gateway.acquired == ["tcp-shell-ymodem"]
        controller._release_lease()
        assert len(gateway.released) == 1

    def test_attachment_gateway_busy_rejects(self):
        controller = make_controller(
            attachment=FakeAttachment(FakeGateway(allow_acquire=False))
        )
        result = controller.admit_ymodem(ENDPOINT)
        assert not result.allowed and result.admission is YmodemAdmission.DEVICE_BUSY

    def test_runtime_without_attachment_allows_when_gate_idle(self):
        controller = make_controller(runtime=FakeRuntime(RuntimeOperationGateState.IDLE))
        result = controller.admit_ymodem(ENDPOINT)
        assert result.allowed and result.admission is YmodemAdmission.IDLE_RUNTIME
        assert result.managed_session is True

    def test_runtime_busy_gate_rejects(self):
        controller = make_controller(
            runtime=FakeRuntime(RuntimeOperationGateState.PRODUCTION_FROZEN)
        )
        result = controller.admit_ymodem(ENDPOINT)
        assert not result.allowed and result.admission is YmodemAdmission.DEVICE_BUSY

    def test_unknown_endpoint_without_runtime_is_unmanaged(self):
        result = make_controller().admit_ymodem(ENDPOINT)
        assert result.allowed and result.admission is YmodemAdmission.UNMANAGED


class TestFilenameHelpers:
    def test_parse_target_version(self):
        assert (
            parse_target_version("afd01c_app_0.0.131_beta.bin") == "0.0.131_beta"
        )
        assert parse_target_version("app_factory.bin") == ""
        assert parse_target_version("notes.txt") == ""

    def test_route_hint_mirrors_firmware_routing(self):
        assert ymodem_route_hint("afd01c_app_0.0.131_beta.bin") == "download"
        assert ymodem_route_hint("app_factory.bin") == "factory"
        assert ymodem_route_hint("readme.txt") == "filesystem"


class TestVersionReadback:
    def test_completed_transfer_reports_readback_after_meta(self, qapplication_session):
        runtime = FakeRuntime()
        controller = make_controller(runtime=runtime)
        readbacks = []
        controller.version_readback.connect(readbacks.append)
        controller._target_version = "0.0.131_beta"
        controller._subscribe_readback(ENDPOINT)
        controller._on_ymodem_finished({"outcome": "completed"})
        # 完成时刻的既有 meta 立即上报一次，重启后的新 meta 再次上报
        assert len(readbacks) == 1
        assert readbacks[0]["fw_ver"] == "0.0.131_beta"
        assert readbacks[0]["matches_target"] is True
        runtime.core.meta_info = MetaInfo(
            protocol_ver=2, fw_ver="0.0.130_beta", hw_type="afd01c", device_sn="SN001"
        )
        runtime.core.record_received.emit(runtime.core.meta_info)
        assert len(readbacks) == 2
        assert readbacks[1]["matches_target"] is False

    def test_readback_stays_silent_before_completion(self, qapplication_session):
        runtime = FakeRuntime()
        controller = make_controller(runtime=runtime)
        readbacks = []
        controller.version_readback.connect(readbacks.append)
        controller._subscribe_readback(ENDPOINT)
        runtime.core.record_received.emit(runtime.core.meta_info)
        assert readbacks == []
