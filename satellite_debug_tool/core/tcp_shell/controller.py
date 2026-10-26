"""Shell session controller: YMODEM admission lease and session ownership (M37).

Admission rules (confirmed with the user):

- Shell observe/input never takes the device transaction lease.
- YMODEM requires the device transaction lease when the target endpoint has a
  managed runtime: customer/engineering endpoints go through the attachment
  operation gateway with purpose ``tcp-shell-ymodem``; endpoints that only
  exist in the shared session directory (e.g. production devices outside a
  batch) are admitted while the runtime operation gate is idle and rejected
  while any operation or batch freeze holds it.
- Hand-typed TCP targets and serial targets transfer directly; the evidence
  records ``managed_session: false``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
from typing import Optional

from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.protocol.frame_v2 import MetaInfo
from satellite_debug_tool.core.session.runtime import RuntimeOperationGateState

from .worker import TcpShellWorker, TcpShellWorkerState
from .ymodem_sender import YmodemFileSpec

YMODEM_PURPOSE = "tcp-shell-ymodem"

_VERSION_PATTERN = re.compile(r"_(\d+\.\d+\.\d+(?:_(?:release|beta))?)\.bin$")
_APP_BIN_PATTERN = re.compile(
    r"^[a-z0-9]+_app_\d+\.\d+\.\d+(?:_(?:release|beta))?\.bin$"
)


class YmodemAdmission(str, Enum):
    MANAGED = "managed"
    IDLE_RUNTIME = "idle-runtime"
    UNMANAGED = "unmanaged"
    DEVICE_BUSY = "device-busy"


@dataclass(frozen=True)
class YmodemAdmissionResult:
    admission: YmodemAdmission
    allowed: bool
    detail: str = ""

    @property
    def managed_session(self) -> bool:
        return self.admission in (YmodemAdmission.MANAGED, YmodemAdmission.IDLE_RUNTIME)


def parse_target_version(filename: str) -> str:
    match = _VERSION_PATTERN.search(filename)
    return match.group(1) if match else ""


def ymodem_route_hint(filename: str) -> str:
    """Mirror the firmware filename routing as a UI hint (device decides)."""

    name = filename.strip().lower()
    if name == "app_factory.bin":
        return "factory"
    if _APP_BIN_PATTERN.match(name):
        return "download"
    return "filesystem"


class TcpShellController(QObject):
    """Own the single TCP shell session and its YMODEM admission lease."""

    connected = Signal(str)
    connect_failed = Signal(str)
    disconnected = Signal(str)
    state_changed = Signal(str)
    rx_payload = Signal(bytes)
    ymodem_progress = Signal(object)
    ymodem_finished = Signal(object)
    session_started = Signal(str)
    version_readback = Signal(object)

    def __init__(
        self,
        *,
        device_directory=None,
        session_directory=None,
        session_root: Optional[Path] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._device_directory = device_directory
        self._session_directory = session_directory
        self._session_root = session_root
        self._lease_owner = object()
        self._gateway = None
        self._worker: Optional[TcpShellWorker] = None
        self._endpoint = None
        self._target_version = ""
        self._readback_active = False
        self._readback_runtime = None

    @property
    def worker(self) -> Optional[TcpShellWorker]:
        return self._worker

    @property
    def state(self) -> TcpShellWorkerState:
        return self._worker.state if self._worker is not None else TcpShellWorkerState.DISCONNECTED

    # -- session lifecycle ------------------------------------------------------

    def connect_device(
        self,
        target: dict,
        *,
        endpoint=None,
        operator: str = "",
    ) -> bool:
        """Connect one shell target: ``{"transport": "tcp"|"serial", ...}``.

        ``endpoint`` binds the session to a managed device runtime (UDP device
        directory hit, TCP target only); serial targets and hand-typed TCP
        addresses stay unmanaged.
        """
        self.disconnect_device("reconnect")
        worker = TcpShellWorker(
            target=target,
            managed_session=endpoint is not None,
            operator=operator,
            session_root=self._session_root,
        )
        self._bind_worker(worker)
        self._endpoint = endpoint
        worker.submit_connect()
        return True

    def disconnect_device(self, reason: str = "operator") -> None:
        self._release_lease()
        worker = self._worker
        if worker is not None:
            worker.stop_and_join()
            self._unbind_worker(worker)
        self._worker = None
        self._endpoint = None

    def send_text(self, text: str) -> bool:
        return self.send_raw(text.encode("utf-8") + b"\r")

    def send_raw(self, data: bytes) -> bool:
        if not data:
            return False
        worker = self._worker
        if worker is None or worker.state is not TcpShellWorkerState.CONNECTED:
            return False
        worker.submit_send_raw(data)
        return True

    def shutdown(self) -> None:
        self.disconnect_device("shutdown")

    # -- YMODEM admission and transfer -------------------------------------------

    def admit_ymodem(self, endpoint=None) -> YmodemAdmissionResult:
        if endpoint is None:
            return YmodemAdmissionResult(YmodemAdmission.UNMANAGED, True)
        attachment = None
        if self._device_directory is not None:
            attachment = self._device_directory.attachment(endpoint)
        if attachment is not None:
            gateway = attachment.new_operation_gateway()
            if gateway.try_acquire_operation(self._lease_owner, purpose=YMODEM_PURPOSE):
                self._gateway = gateway
                return YmodemAdmissionResult(YmodemAdmission.MANAGED, True)
            return YmodemAdmissionResult(
                YmodemAdmission.DEVICE_BUSY,
                False,
                "device transaction is held by another operation",
            )
        runtime = (
            self._session_directory.runtime(endpoint)
            if self._session_directory is not None
            else None
        )
        if runtime is None:
            return YmodemAdmissionResult(YmodemAdmission.UNMANAGED, True)
        if runtime.operation_gate.state is not RuntimeOperationGateState.IDLE:
            return YmodemAdmissionResult(
                YmodemAdmission.DEVICE_BUSY,
                False,
                "device operation gate is not idle",
            )
        return YmodemAdmissionResult(YmodemAdmission.IDLE_RUNTIME, True)

    def start_ymodem(self, file_path, *, endpoint=None) -> YmodemAdmissionResult:
        worker = self._worker
        if worker is None or worker.state is not TcpShellWorkerState.CONNECTED:
            return YmodemAdmissionResult(
                YmodemAdmission.DEVICE_BUSY, False, "tcp shell is not connected"
            )
        try:
            spec = YmodemFileSpec.from_path(Path(file_path))
        except Exception as exc:  # noqa: BLE001 - surface file errors to the UI
            return YmodemAdmissionResult(
                YmodemAdmission.DEVICE_BUSY, False, f"{type(exc).__name__}: {exc}"
            )
        admission = self.admit_ymodem(endpoint)
        if not admission.allowed:
            return admission
        self._target_version = parse_target_version(spec.filename)
        self._readback_active = False
        self._subscribe_readback(endpoint)
        worker.submit_start_ymodem(spec)
        return admission

    def cancel_ymodem(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.cancel_ymodem()

    # -- internals -----------------------------------------------------------------

    def _bind_worker(self, worker: TcpShellWorker) -> None:
        worker.connected.connect(self.connected)
        worker.connect_failed.connect(self.connect_failed)
        worker.disconnected.connect(self._on_disconnected)
        worker.state_changed.connect(self.state_changed)
        worker.rx_payload.connect(self.rx_payload)
        worker.ymodem_progress.connect(self.ymodem_progress)
        worker.ymodem_finished.connect(self._on_ymodem_finished)
        worker.session_started.connect(self.session_started)
        self._worker = worker
        worker.start()

    def _unbind_worker(self, worker: TcpShellWorker) -> None:
        for signal in (
            worker.connected,
            worker.connect_failed,
            worker.disconnected,
            worker.state_changed,
            worker.rx_payload,
            worker.ymodem_progress,
            worker.ymodem_finished,
            worker.session_started,
        ):
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass

    def _on_disconnected(self, _reason: str) -> None:
        self._release_lease()
        self._readback_active = False

    def _on_ymodem_finished(self, payload: dict) -> None:
        self._release_lease()
        self._readback_active = payload.get("outcome") == "completed"
        if self._readback_active:
            self._report_readback(self._current_meta_info())

    def _subscribe_readback(self, endpoint) -> None:
        self._readback_runtime = None
        if endpoint is None or self._session_directory is None:
            return
        runtime = self._session_directory.runtime(endpoint)
        if runtime is None:
            return
        self._readback_runtime = runtime
        try:
            runtime.core.record_received.connect(self._on_record_received)
        except (RuntimeError, TypeError):
            self._readback_runtime = None

    def _on_record_received(self, record) -> None:
        if self._readback_active and isinstance(record, MetaInfo):
            self._report_readback(record)

    def _current_meta_info(self) -> Optional[MetaInfo]:
        runtime = self._readback_runtime
        return getattr(runtime, "core", None) and getattr(runtime.core, "meta_info", None)

    def _report_readback(self, meta) -> None:
        if meta is None:
            return
        fw_ver = str(getattr(meta, "fw_ver", "")).strip()
        if not fw_ver:
            return
        self.version_readback.emit(
            {
                "fw_ver": fw_ver,
                "device_sn": str(getattr(meta, "device_sn", "")).strip(),
                "hw_type": str(getattr(meta, "hw_type", "")).strip(),
                "target_version": self._target_version,
                "matches_target": bool(self._target_version) and fw_ver == self._target_version,
            }
        )

    def _release_lease(self) -> None:
        gateway, self._gateway = self._gateway, None
        if gateway is not None:
            try:
                gateway.release_operation(self._lease_owner)
            except Exception:  # noqa: BLE001 - release must never raise upstream
                pass


__all__ = [
    "TcpShellController",
    "YmodemAdmission",
    "YmodemAdmissionResult",
    "parse_target_version",
    "ymodem_route_hint",
]
