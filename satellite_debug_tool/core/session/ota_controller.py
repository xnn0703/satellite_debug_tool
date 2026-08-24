"""Asynchronous debug-OTA state machine for one device session."""

from __future__ import annotations

from enum import Enum
import time
from typing import Any, Optional
import zlib

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from satellite_debug_tool.core.profile import CapabilitySupport
from satellite_debug_tool.core.protocol import (
    CommandResponse,
    MetaInfo,
    RespCode,
    build_ota_abort,
    build_ota_begin,
    build_ota_data,
    build_ota_end,
    build_request_meta_info,
)

from .device_session import DeviceSessionCore


OTA_CHUNK_SIZE = 512
OTA_CHUNK_TIMEOUT_MS = 2000
OTA_CHUNK_MAX_RETRY = 3
OTA_BEGIN_TIMEOUT_MS = 15000
OTA_END_TIMEOUT_MS = 10000
OTA_REBOOT_TIMEOUT_S = 120.0
OTA_REBOOT_PROBE_MS = 3000


class OtaCapabilityState(str, Enum):
    DISCONNECTED = "disconnected"
    WAITING_PROFILE = "waiting_profile"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class OtaState(str, Enum):
    IDLE = "idle"
    QUIESCE = "quiesce"
    BEGIN = "begin"
    DATA = "data"
    END = "end"
    WAIT_REBOOT = "wait_reboot"


class OtaStatus(str, Enum):
    IDLE = "idle"
    WAITING_PROFILE = "waiting_profile"
    WAITING_CAPABILITY = "waiting_capability"
    UNSUPPORTED = "unsupported"
    NO_FILE = "no_file"
    STOPPING_LIVE_DATA = "stopping_live_data"
    STOP_LIVE_DATA_FAILED = "stop_live_data_failed"
    SENDING_BEGIN = "sending_begin"
    BEGIN_SEND_FAILED = "begin_send_failed"
    BEGIN_REJECTED = "begin_rejected"
    BEGIN_TIMEOUT = "begin_timeout"
    TRANSFERRING = "transferring"
    CHUNK_SEND_FAILED = "chunk_send_failed"
    CHUNK_RETRY = "chunk_retry"
    CHUNK_REJECTED = "chunk_rejected"
    CHUNK_TIMEOUT = "chunk_timeout"
    VERIFYING = "verifying"
    END_SEND_FAILED = "end_send_failed"
    END_REJECTED = "end_rejected"
    END_TIMEOUT = "end_timeout"
    REBOOTING = "rebooting"
    REBOOT_TIMEOUT = "reboot_timeout"
    DEVICE_RETURNED_CHANGED = "device_returned_changed"
    DEVICE_RETURNED_UNCHANGED = "device_returned_unchanged"
    CONNECTION_LOST = "connection_lost"
    ABORTED = "aborted"


class OtaController(QObject):
    """Own the OTA command sequence, exact ACK matching, retries, and reboot wait."""

    capability_changed = Signal(object)
    state_changed = Signal(object)
    status_changed = Signal(object, object)
    progress_changed = Signal(int)
    transaction_active_changed = Signal(bool)
    debug_mode_requested = Signal(bool)

    def __init__(
        self,
        session: DeviceSessionCore,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._connected = session.connected
        self._hardware = session.profile_store.current_hw_type()
        self._capability = OtaCapabilityState.DISCONNECTED
        self._state = OtaState.IDLE
        self._active = False
        self._file: Optional[bytes] = None
        self._filename = ""
        self._crc32 = 0
        self._sequence = 0
        self._total_chunks = 0
        self._retry = 0
        self._start_time = 0.0
        self._known_debug_enabled = False
        self._paused_debug = False
        self._restore_debug = False
        self._firmware_before = ""
        self._firmware_current = (
            session.meta_info.fw_ver if session.meta_info is not None else ""
        )
        self._reboot_started = 0.0
        self._reboot_deadline = 0.0
        self._reboot_meta_not_before = 0.0

        self._response_timer = QTimer(self)
        self._response_timer.setSingleShot(True)
        self._response_timer.timeout.connect(self._on_response_timeout)
        self._reboot_timer = QTimer(self)
        self._reboot_timer.setInterval(OTA_REBOOT_PROBE_MS)
        self._reboot_timer.timeout.connect(self._on_reboot_tick)

        session.connection_changed.connect(self.set_connected)
        session.record_received.connect(self.feed_record)
        session.profile_store.profile_changed.connect(self._on_profile_changed)
        self._refresh_capability()

    @property
    def capability_state(self) -> OtaCapabilityState:
        return self._capability

    @property
    def state(self) -> OtaState:
        return self._state

    @property
    def active(self) -> bool:
        return self._active

    @property
    def supported(self) -> bool:
        return self._capability is OtaCapabilityState.SUPPORTED

    @property
    def has_file(self) -> bool:
        return self._file is not None

    @property
    def filename(self) -> str:
        return self._filename

    @property
    def file_size(self) -> int:
        return len(self._file) if self._file is not None else 0

    @property
    def crc32(self) -> int:
        return self._crc32

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def total_chunks(self) -> int:
        return self._total_chunks

    def configure_file(self, data: bytes, filename: str) -> None:
        self._file = bytes(data)
        self._filename = str(filename)
        self._crc32 = zlib.crc32(self._file) & 0xFFFFFFFF

    def clear_file(self) -> None:
        if self._active:
            return
        self._file = None
        self._filename = ""
        self._crc32 = 0

    def set_debug_state(self, enabled: bool) -> None:
        self._known_debug_enabled = bool(enabled)

    @Slot(bool)
    def set_connected(self, connected: bool) -> None:
        connected = bool(connected)
        if self._connected == connected:
            return
        self._connected = connected
        if not connected:
            self._hardware = None
            if self._active:
                self._finish(OtaStatus.CONNECTION_LOST, restore_debug=False)
        self._refresh_capability()

    @Slot(str)
    def _on_profile_changed(self, hardware: str) -> None:
        if hardware:
            self._hardware = hardware
        self._refresh_capability()

    @Slot(object)
    def feed_record(self, record: object) -> None:
        if isinstance(record, MetaInfo):
            self._hardware = record.hw_type or None
            self._firmware_current = record.fw_ver or ""
            self._refresh_capability()
            if (
                self._active
                and self._state is OtaState.WAIT_REBOOT
                and record.fw_ver
                and time.monotonic() >= self._reboot_meta_not_before
            ):
                before = self._firmware_before or "?"
                status = (
                    OtaStatus.DEVICE_RETURNED_CHANGED
                    if record.fw_ver != before
                    else OtaStatus.DEVICE_RETURNED_UNCHANGED
                )
                self._known_debug_enabled = False
                self._finish(
                    status,
                    restore_debug=False,
                    before=before,
                    after=record.fw_ver,
                    version=record.fw_ver,
                )
        elif isinstance(record, CommandResponse):
            self._apply_response(record)

    def start(self, *, pause_debug: bool) -> bool:
        if self._active or not self._connected or not self.supported:
            self._emit_capability_status()
            return False
        if self._file is None:
            self.status_changed.emit(OtaStatus.NO_FILE, {})
            return False
        self._active = True
        self._sequence = 0
        self._retry = 0
        self._total_chunks = (
            len(self._file) + OTA_CHUNK_SIZE - 1
        ) // OTA_CHUNK_SIZE
        self._start_time = time.monotonic()
        self._paused_debug = bool(pause_debug)
        self._restore_debug = self._paused_debug and self._known_debug_enabled
        self.progress_changed.emit(0)
        self._session.set_handshake_retries_paused(True)
        self.transaction_active_changed.emit(True)
        if self._paused_debug:
            self._set_state(OtaState.QUIESCE)
            self.status_changed.emit(OtaStatus.STOPPING_LIVE_DATA, {})
            self.debug_mode_requested.emit(False)
        else:
            self._send_begin()
        return True

    def on_debug_request_finished(self, target: bool, ok: bool, detail: str) -> None:
        if not self._active or self._state is not OtaState.QUIESCE or target:
            return
        if not ok:
            self._finish(
                OtaStatus.STOP_LIVE_DATA_FAILED,
                detail=detail,
            )
            return
        self._send_begin()

    def abort(self) -> None:
        if not self._active or self._state is OtaState.WAIT_REBOOT:
            return
        self._session.send(build_ota_abort())
        self._finish(OtaStatus.ABORTED)

    def _refresh_capability(self) -> None:
        previous = self._capability
        if not self._connected:
            capability = OtaCapabilityState.DISCONNECTED
        elif not self._hardware:
            capability = OtaCapabilityState.WAITING_PROFILE
        else:
            profile = self._session.profile_store
            ota = profile.capability_status(self._hardware, "ota")
            context = profile.capability_status(
                self._hardware,
                "command_response_context",
            )
            if all(status is CapabilitySupport.SUPPORTED for status in (ota, context)):
                capability = OtaCapabilityState.SUPPORTED
            elif CapabilitySupport.UNKNOWN in {ota, context}:
                capability = OtaCapabilityState.WAITING_PROFILE
            else:
                capability = OtaCapabilityState.UNSUPPORTED
        self._capability = capability
        if previous is not capability:
            self.capability_changed.emit(capability)
        if not self._active:
            self._emit_capability_status()

    def _emit_capability_status(self) -> None:
        status = {
            OtaCapabilityState.DISCONNECTED: OtaStatus.IDLE,
            OtaCapabilityState.WAITING_PROFILE: (
                OtaStatus.WAITING_PROFILE
                if not self._hardware
                else OtaStatus.WAITING_CAPABILITY
            ),
            OtaCapabilityState.SUPPORTED: OtaStatus.IDLE,
            OtaCapabilityState.UNSUPPORTED: OtaStatus.UNSUPPORTED,
        }[self._capability]
        self.status_changed.emit(status, {})

    def _send_begin(self) -> None:
        if not self._active or self._file is None:
            return
        self._set_state(OtaState.BEGIN)
        self.status_changed.emit(OtaStatus.SENDING_BEGIN, {})
        if not self._session.send(build_ota_begin(len(self._file), self._filename)):
            self._finish(OtaStatus.BEGIN_SEND_FAILED)
            return
        self._response_timer.start(OTA_BEGIN_TIMEOUT_MS)

    def _send_current_chunk(self) -> None:
        if not self._active or self._file is None:
            return
        if self._sequence >= self._total_chunks:
            self._send_end()
            return
        offset = self._sequence * OTA_CHUNK_SIZE
        chunk = self._file[offset:offset + OTA_CHUNK_SIZE]
        self._set_state(OtaState.DATA)
        if not self._session.send(build_ota_data(self._sequence, chunk)):
            self._finish(
                OtaStatus.CHUNK_SEND_FAILED,
                sequence=self._sequence,
            )
            return
        self._response_timer.start(OTA_CHUNK_TIMEOUT_MS)

    def _send_end(self) -> None:
        if not self._active:
            return
        self._set_state(OtaState.END)
        self.status_changed.emit(OtaStatus.VERIFYING, {})
        if not self._session.send(build_ota_end(self._crc32)):
            self._finish(OtaStatus.END_SEND_FAILED)
            return
        self._response_timer.start(OTA_END_TIMEOUT_MS)

    def _apply_response(self, response: CommandResponse) -> None:
        if not self._active:
            return
        if self._state is OtaState.BEGIN:
            self._apply_begin_response(response)
        elif self._state is OtaState.DATA:
            self._apply_data_response(response)
        elif self._state is OtaState.END:
            self._apply_end_response(response)

    def _apply_begin_response(self, response: CommandResponse) -> None:
        if int(response.code) == int(RespCode.SUCCESS):
            if (response.msg or "").strip() != "OTA_BEGIN=READY":
                return
            self._response_timer.stop()
            self._retry = 0
            QTimer.singleShot(0, self._send_current_chunk)
            return
        self._finish(
            OtaStatus.BEGIN_REJECTED,
            detail=response.msg or str(response.code),
        )

    def _apply_data_response(self, response: CommandResponse) -> None:
        expected = f"OTA_DATA={self._sequence}"
        if int(response.code) == int(RespCode.SUCCESS):
            if (response.msg or "").strip() != expected:
                return
            self._response_timer.stop()
            self._sequence += 1
            self._retry = 0
            self._emit_progress()
            QTimer.singleShot(0, self._send_current_chunk)
            return
        self._finish(
            OtaStatus.CHUNK_REJECTED,
            sequence=self._sequence,
            detail=response.msg or str(response.code),
        )

    def _apply_end_response(self, response: CommandResponse) -> None:
        if int(response.code) == int(RespCode.SUCCESS):
            if (response.msg or "").strip() != "OTA_END=VERIFIED":
                return
            self._enter_wait_reboot()
            return
        self._finish(
            OtaStatus.END_REJECTED,
            detail=response.msg or str(response.code),
        )

    def _emit_progress(self) -> None:
        if self._total_chunks <= 0 or self._file is None:
            return
        percent = int(self._sequence * 100 / self._total_chunks)
        self.progress_changed.emit(percent)
        elapsed = time.monotonic() - self._start_time
        transferred = min(self._sequence * OTA_CHUNK_SIZE, len(self._file))
        values: dict[str, Any] = {
            "sequence": self._sequence,
            "total": self._total_chunks,
            "percent": percent,
        }
        if elapsed > 0.1 and transferred > 0:
            speed = transferred / elapsed
            values["speed"] = speed / 1024.0
            values["remaining"] = int(max(0, len(self._file) - transferred) / speed)
        self.status_changed.emit(OtaStatus.TRANSFERRING, values)

    def _enter_wait_reboot(self) -> None:
        self._response_timer.stop()
        self._set_state(OtaState.WAIT_REBOOT)
        self.progress_changed.emit(100)
        self._session.set_handshake_retries_paused(False)
        self._firmware_before = self._firmware_current
        self._reboot_started = time.monotonic()
        self._reboot_deadline = self._reboot_started + OTA_REBOOT_TIMEOUT_S
        self._reboot_meta_not_before = self._reboot_started + 1.0
        self.status_changed.emit(OtaStatus.REBOOTING, {"elapsed": 0})
        self._reboot_timer.start()

    @Slot()
    def _on_reboot_tick(self) -> None:
        if not self._active or self._state is not OtaState.WAIT_REBOOT:
            return
        now = time.monotonic()
        if now > self._reboot_deadline:
            self._finish(OtaStatus.REBOOT_TIMEOUT, restore_debug=False)
            return
        self.status_changed.emit(
            OtaStatus.REBOOTING,
            {"elapsed": int(now - self._reboot_started)},
        )
        self._session.send(build_request_meta_info())

    @Slot()
    def _on_response_timeout(self) -> None:
        if not self._active:
            return
        if self._state is OtaState.DATA:
            if self._retry < OTA_CHUNK_MAX_RETRY:
                self._retry += 1
                self.status_changed.emit(
                    OtaStatus.CHUNK_RETRY,
                    {
                        "sequence": self._sequence,
                        "retry": self._retry,
                        "maximum": OTA_CHUNK_MAX_RETRY,
                    },
                )
                self._send_current_chunk()
            else:
                self._finish(
                    OtaStatus.CHUNK_TIMEOUT,
                    sequence=self._sequence,
                )
        elif self._state is OtaState.BEGIN:
            self._finish(OtaStatus.BEGIN_TIMEOUT)
        elif self._state is OtaState.END:
            self._finish(OtaStatus.END_TIMEOUT)

    def _finish(
        self,
        status: OtaStatus,
        *,
        restore_debug: bool = True,
        **values: Any,
    ) -> None:
        should_restore = restore_debug and self._restore_debug and self._connected
        self._active = False
        self._response_timer.stop()
        self._reboot_timer.stop()
        self._session.set_handshake_retries_paused(False)
        self._set_state(OtaState.IDLE)
        self.transaction_active_changed.emit(False)
        self.status_changed.emit(status, values)
        self._paused_debug = False
        self._restore_debug = False
        if should_restore:
            QTimer.singleShot(0, lambda: self.debug_mode_requested.emit(True))

    def _set_state(self, state: OtaState) -> None:
        if self._state is state:
            return
        self._state = state
        self.state_changed.emit(state)


__all__ = [
    "OTA_BEGIN_TIMEOUT_MS",
    "OTA_CHUNK_MAX_RETRY",
    "OTA_CHUNK_SIZE",
    "OTA_CHUNK_TIMEOUT_MS",
    "OTA_END_TIMEOUT_MS",
    "OtaCapabilityState",
    "OtaController",
    "OtaState",
    "OtaStatus",
]
