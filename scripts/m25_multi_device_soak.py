#!/usr/bin/env python3
"""M25 four-endpoint Customer-session host soak.

This tool intentionally uses the real UDP broker, endpoint directory, Customer
attachment, Runtime/Core, capture-profile controller and SDB v3 writer.  The
device side is a loopback simulator, so a passing report is host evidence only;
it is never hardware or RF acceptance.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import select
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from typing import Callable, Optional


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# This is a host soak and creates no widgets, but retaining the explicit value
# makes the execution environment unambiguous in the JSON evidence.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import PySide6
from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QThread,
    QTimer,
    Qt,
    Signal,
    Slot,
    qVersion,
)

from satellite_debug_tool.core.comm import UdpEndpointBroker
from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.profile.cache import profile_to_dict
from satellite_debug_tool.core.protocol import (
    CmdType,
    ServiceControlOp,
    ServiceFastState,
    SubCmd,
    build_frame,
)
from satellite_debug_tool.core.session.device_session import DeviceSessionCore
from satellite_debug_tool.core.session.product_controller import CaptureProfileController
from satellite_debug_tool.core.session.registry import EndpointSessionDirectory
from satellite_debug_tool.core.session.runtime import (
    CustomerAttachmentLease,
    CustomerAttachmentScope,
    RuntimeOperationGateState,
)
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.io.data_recorder import DataRecorder
from satellite_debug_tool.io.recording_path_registry import (
    RecordingPathError,
    RecordingPathRegistry,
)
from satellite_debug_tool.io.sdb_schema import SDB_VERSION_V3


DEFAULT_DURATION_S = 1800.0
DEFAULT_ENDPOINT_COUNT = 4
DEFAULT_RATE_HZ = 20.0
DEFAULT_MARKER_INTERVAL_MS = 100
DEFAULT_MAX_P95_LATENCY_MS = 100.0
DEFAULT_MIN_RATE_RATIO = 0.95
_CAPTURE_APPLIED_MASK = 1 << 6


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(float(value) for value in values)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[min(rank, len(ordered) - 1)]


def _endpoint_text(endpoint: tuple[str, int]) -> str:
    return f"{endpoint[0]}:{endpoint[1]}"


def _prefixed(text: str) -> bytes:
    encoded = text.encode("utf-8")
    if len(encoded) > 255:
        raise ValueError("simulator identity field is too long")
    return bytes([len(encoded)]) + encoded


def _machine_model() -> str:
    candidates = [
        platform.machine(),
        platform.node(),
    ]
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "hw.model"],
                check=True,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            candidates.insert(0, result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    if sys.platform.startswith("linux"):
        path = Path("/sys/devices/virtual/dmi/id/product_name")
        try:
            candidates.insert(0, path.read_text(encoding="utf-8").strip())
        except OSError:
            pass
    return next((value for value in candidates if value), "unknown")


def _cpu_description() -> str:
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
                check=True,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            if result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    value = platform.processor().strip()
    if value:
        return value
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if line.lower().startswith("model name") and ":" in line:
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.machine() or "unknown"


def _frame_data(frame: bytes) -> tuple[int, bytes] | None:
    raw = bytes(frame)
    if len(raw) < 9 or raw[:3] != b"\xAA\x55\x0D" or raw[-1] != 0xEE:
        return None
    length = int.from_bytes(raw[4:6], "little")
    if len(raw) != length + 9:
        return None
    return raw[3], raw[6 : 6 + length]


@dataclass(frozen=True)
class _DeviceSpec:
    index: int
    hardware_type: str
    product_identity: str
    service_protocol: int
    serial_number: str

    @property
    def token(self) -> int:
        return self.index + 1


class _LoopbackDeviceSimulator:
    """Small UDP device peer that answers the real host protocol controllers."""

    def __init__(self, spec: _DeviceSpec, rate_hz: float) -> None:
        self.spec = spec
        self.rate_hz = float(rate_hz)
        self._period_s = 1.0 / self.rate_hz
        self._interval_ms = max(1, int(round(1000.0 / self.rate_hz)))
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # macOS does not guarantee that every 127/8 address is configured on
        # the loopback interface.  Distinct ephemeral ports on 127.0.0.1 are
        # still four distinct normalized loopback endpoints and are portable.
        self._socket.bind(("127.0.0.1", 0))
        bound = self._socket.getsockname()
        self.endpoint = (str(bound[0]), int(bound[1]))
        self._socket.setblocking(False)
        self._host_endpoint: tuple[str, int] | None = None
        self._stop = threading.Event()
        self._measurement_active = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_due = 0.0
        self._sequence = 0
        self._lock = threading.Lock()
        self._sent = Counter()
        self._measurement_sent = Counter()
        self._received = Counter()
        self._send_failures = 0
        self._capture_full = False

    @property
    def thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def capture_full(self) -> bool:
        with self._lock:
            return self._capture_full

    @property
    def sent(self) -> dict[str, int]:
        with self._lock:
            return dict(self._sent)

    @property
    def measurement_sent(self) -> dict[str, int]:
        with self._lock:
            return dict(self._measurement_sent)

    @property
    def received(self) -> dict[str, int]:
        with self._lock:
            return dict(self._received)

    @property
    def send_failures(self) -> int:
        with self._lock:
            return self._send_failures

    def start(self, host_port: int) -> None:
        if self._thread is not None:
            raise RuntimeError("simulator is already started")
        self._host_endpoint = ("127.0.0.1", int(host_port))
        self._thread = threading.Thread(
            target=self._run,
            name=f"M25LoopbackDevice-{self.spec.token}",
            daemon=True,
        )
        self._thread.start()

    def begin_measurement(self, start_monotonic: float) -> None:
        with self._lock:
            self._sequence = 0
            self._measurement_sent.clear()
            self._next_due = float(start_monotonic)
        self._measurement_active.set()

    def end_measurement(self) -> None:
        self._measurement_active.clear()

    def stop(self, timeout_s: float = 2.0) -> bool:
        self._measurement_active.clear()
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout_s)))
        alive = bool(thread is not None and thread.is_alive())
        try:
            self._socket.close()
        except OSError:
            pass
        return not alive

    def _run(self) -> None:
        self._send_identity_bundle()
        while not self._stop.is_set():
            self._send_due_fast_frames()
            timeout_s = 0.01
            if self._measurement_active.is_set():
                with self._lock:
                    due = self._next_due
                timeout_s = min(timeout_s, max(0.0, due - time.monotonic()))
            try:
                readable, _, _ = select.select(
                    [self._socket], [], [], max(0.0, timeout_s)
                )
            except (OSError, ValueError):
                break
            if not readable:
                continue
            try:
                frame, source = self._socket.recvfrom(65535)
            except (BlockingIOError, OSError):
                continue
            self._handle_host_frame(bytes(frame), (str(source[0]), int(source[1])))

    def _send_due_fast_frames(self) -> None:
        if not self._measurement_active.is_set():
            return
        # Absolute scheduling prevents cumulative drift.  The bounded catch-up
        # loop keeps a temporarily descheduled simulator from silently lowering
        # its average rate without monopolising the host.
        for _ in range(8):
            now = time.monotonic()
            with self._lock:
                due = self._next_due
                if now < due or not self._measurement_active.is_set():
                    return
                self._sequence += 1
                sequence = self._sequence
                self._next_due += self._period_s
            self._send_frame(self._fast_state_frame(sequence), "fast_state")

    def _handle_host_frame(
        self,
        frame: bytes,
        source: tuple[str, int],
    ) -> None:
        decoded = _frame_data(frame)
        if decoded is None:
            with self._lock:
                self._received["malformed"] += 1
            return
        command, data = decoded
        with self._lock:
            self._received[f"cmd_{command:02x}"] += 1
        if command == int(CmdType.CONTROL):
            self._handle_debug_control(data, source)
            return
        if command == int(CmdType.SERVICE_CONTROL_REQUEST):
            self._handle_service_control(data, source)

    def _handle_debug_control(
        self,
        data: bytes,
        destination: tuple[str, int],
    ) -> None:
        if not data:
            return
        subcommand = int(data[0])
        if subcommand == int(SubCmd.REQUEST_META_INFO):
            self._send_identity_bundle(destination)
        elif subcommand == int(SubCmd.REQUEST_CHANNEL_DEFINE):
            self._send_frame(
                build_frame(CmdType.CHANNEL_DEFINE, b"\x01\x00"),
                "channel_define",
                destination,
            )
        elif subcommand == int(SubCmd.REQUEST_STATE_DEFINE):
            self._send_frame(
                build_frame(CmdType.STATE_DEFINE, b"\x01\x00"),
                "state_define",
                destination,
            )
        elif subcommand == int(SubCmd.REQUEST_EVENT_DEFINE):
            self._send_frame(
                build_frame(CmdType.EVENT_DEFINE, b"\x01\x00"),
                "event_define",
                destination,
            )
        elif subcommand == int(SubCmd.REQUEST_PROFILE_SEMANTICS):
            self._send_frame(
                build_frame(CmdType.PROFILE_SEMANTICS, b"\x01\x00\x00\x00"),
                "profile_semantics",
                destination,
            )

    def _handle_service_control(
        self,
        data: bytes,
        destination: tuple[str, int],
    ) -> None:
        if len(data) < 6:
            return
        schema, request_id, operation = struct.unpack_from("<BIB", data, 0)
        if schema != 1:
            return
        applied_mask = 0
        if operation == int(ServiceControlOp.SET_CAPTURE_PROFILE):
            if len(data) != 7 or data[6] not in (0, 1):
                return
            with self._lock:
                self._capture_full = bool(data[6])
            applied_mask = _CAPTURE_APPLIED_MASK
        elif operation != int(ServiceControlOp.SUBSCRIBE):
            return
        response = struct.pack(
            "<BIBBIBffBBB",
            1,
            int(request_id),
            int(operation),
            0,
            applied_mask,
            0,
            0.0,
            0.0,
            0,
            0,
            0,
        )
        kind = (
            "capture_profile_ack"
            if operation == int(ServiceControlOp.SET_CAPTURE_PROFILE)
            else "subscription_ack"
        )
        self._send_frame(
            build_frame(CmdType.SERVICE_CONTROL_RESPONSE, response),
            kind,
            destination,
        )

    def _send_identity_bundle(
        self,
        destination: tuple[str, int] | None = None,
    ) -> None:
        target = destination or self._host_endpoint
        if target is None:
            return
        timestamp = self.spec.token * 1000
        meta = bytes([2])
        meta += _prefixed(f"sim-{self.spec.service_protocol}.0")
        meta += _prefixed(self.spec.hardware_type)
        meta += _prefixed(self.spec.serial_number)
        identity = struct.pack("<BII", 1, timestamp, 0x1F)
        identity += _prefixed(self.spec.product_identity)
        identity += _prefixed(self.spec.serial_number)
        identity += _prefixed(f"sim-{self.spec.service_protocol}.0")
        identity += _prefixed("sim-boot-1.0")
        identity += bytes([self.spec.service_protocol])
        uid = (
            0xA5000000 | self.spec.token,
            0xB6000000 | self.spec.token,
            0xC7000000 | self.spec.token,
        )
        hardware = struct.pack(
            "<BIIIII6sB",
            1,
            timestamp,
            0x07,
            *uid,
            bytes([0x02, 0x00, 0x00, 0x00, 0x00, self.spec.token]),
            1,
        )
        capabilities = struct.pack(
            "<BII4fBBB",
            1,
            timestamp,
            0xFF,
            17700.0,
            21200.0,
            27500.0,
            31000.0,
            0x0F,
            0x03,
            0x03,
        )
        frames = (
            (CmdType.META_INFO, meta, "meta_info"),
            (CmdType.SERVICE_IDENTITY, identity, "service_identity"),
            (
                CmdType.SERVICE_HARDWARE_IDENTITY,
                hardware,
                "service_hardware_identity",
            ),
            (CmdType.SERVICE_CAPABILITIES, capabilities, "service_capabilities"),
            (CmdType.SERVICE_FAST_STATE, self._fast_state_payload(0), "startup_fast"),
        )
        for command, payload, kind in frames:
            self._send_frame(build_frame(command, payload), kind, target)

    def _fast_state_payload(self, sequence: int) -> bytes:
        timestamp = (self.spec.token << 24) | (
            int(sequence) * self._interval_ms & 0x00FFFFFF
        )
        return struct.pack(
            "<BII6B6f",
            1,
            timestamp,
            0x0FFF,
            0,
            self.spec.token,
            1,
            1,
            3,
            0,
            float(self.spec.token),
            float(sequence % 90),
            float((sequence * 2) % 360),
            float(sequence % 360),
            float(10 + sequence % 70),
            float(8.0 + self.spec.token),
        )

    def _fast_state_frame(self, sequence: int) -> bytes:
        return build_frame(
            CmdType.SERVICE_FAST_STATE,
            self._fast_state_payload(sequence),
        )

    def _send_frame(
        self,
        frame: bytes,
        kind: str,
        destination: tuple[str, int] | None = None,
    ) -> bool:
        target = destination or self._host_endpoint
        if target is None:
            return False
        try:
            sent = self._socket.sendto(frame, target)
        except OSError:
            sent = 0
        ok = sent == len(frame)
        with self._lock:
            if ok:
                self._sent[kind] += 1
                if self._measurement_active.is_set():
                    self._measurement_sent[kind] += 1
            else:
                self._send_failures += 1
        return ok


@dataclass
class _EndpointMetrics:
    spec: _DeviceSpec
    simulator: _LoopbackDeviceSimulator
    endpoint: tuple[str, int]
    attachment: CustomerAttachmentLease | None = None
    controller: CaptureProfileController | None = None
    recorder: DataRecorder | None = None
    recording_active: bool = False
    sdb_path: Path | None = None
    capture_results: list[dict] = field(default_factory=list)
    raw_routed: int = 0
    recorder_enqueued: int = 0
    recorder_enqueue_failures: int = 0
    control_enqueued: int = 0
    control_enqueue_failures: int = 0
    parsed_fast: int = 0
    cross_route_errors: int = 0
    duplicate_parse_errors: int = 0
    _fast_timestamps: set[int] = field(default_factory=set)
    recorder_stop_ok: bool = False
    sdb_valid: bool = False
    sdb_raw_records: int = 0
    sdb_control_records: int = 0
    sdb_measured_fast: int = 0
    sdb_cross_route_errors: int = 0
    sdb_quality: dict = field(default_factory=dict)
    release_ok: bool = False

    def on_datagram(self, datagram: object) -> None:
        if not self.recording_active or self.recorder is None:
            return
        self.raw_routed += 1
        if getattr(datagram, "endpoint", None) != self.endpoint:
            self.cross_route_errors += 1
        data = getattr(datagram, "data", None)
        if data is None:
            self.recorder_enqueue_failures += 1
            return
        accepted = self.recorder.write_frame(
            bytes(data),
            host_timestamp_ns=int(getattr(datagram, "wall_time_ns", time.time_ns())),
        )
        if accepted:
            self.recorder_enqueued += 1
        else:
            self.recorder_enqueue_failures += 1

    def on_sent(self, event: object) -> None:
        if not self.recording_active or self.recorder is None:
            return
        frame = getattr(event, "frame", None)
        if frame is None:
            self.control_enqueue_failures += 1
            return
        accepted = self.recorder.write_control_frame(
            bytes(frame),
            host_timestamp_ns=int(getattr(event, "host_time_ns", time.time_ns())),
        )
        if accepted:
            self.control_enqueued += 1
        else:
            self.control_enqueue_failures += 1

    def on_records(self, _datagram: object, records: object) -> None:
        if not self.recording_active:
            return
        for record in tuple(records):
            if not isinstance(record, ServiceFastState):
                continue
            sequence_ms = int(record.timestamp) & 0x00FFFFFF
            if sequence_ms == 0:
                continue
            self.parsed_fast += 1
            token = int(record.timestamp) >> 24
            if token != self.spec.token:
                self.cross_route_errors += 1
            if int(record.timestamp) in self._fast_timestamps:
                self.duplicate_parse_errors += 1
            self._fast_timestamps.add(int(record.timestamp))

    def on_capture_finished(self, target: bool, ok: bool, result: str) -> None:
        self.capture_results.append(
            {
                "target_support_full": bool(target),
                "confirmed": bool(ok),
                "result": str(result),
                "observed_at_utc": _utc_now(),
            }
        )


class _LatencyProbe(QObject):
    marker = Signal(object)

    def __init__(
        self,
        interval_ms: int,
        sample_resources: Callable[[], None],
    ) -> None:
        super().__init__()
        self._interval_s = int(interval_ms) / 1000.0
        self._sample_resources = sample_resources
        self._active = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.latencies_ms: list[float] = []
        self.thread_violations = 0
        self.marker.connect(self._receive_marker)

    def start(self) -> None:
        self._active = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._emit_loop,
            name="M25QtLatencyMarker",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> bool:
        self._active = False
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout_s)))
        return not bool(thread is not None and thread.is_alive())

    def _emit_loop(self) -> None:
        due = time.monotonic() + self._interval_s
        sequence = 0
        while not self._stop.is_set():
            remaining = due - time.monotonic()
            if remaining > 0 and self._stop.wait(remaining):
                return
            sequence += 1
            self.marker.emit((sequence, time.monotonic_ns()))
            due += self._interval_s

    @Slot(object)
    def _receive_marker(self, payload: object) -> None:
        if not self._active:
            return
        if QThread.currentThread() is not QCoreApplication.instance().thread():
            self.thread_violations += 1
        _sequence, emitted_ns = tuple(payload)
        latency_ms = max(0.0, (time.monotonic_ns() - int(emitted_ns)) / 1_000_000.0)
        self.latencies_ms.append(latency_ms)
        self._sample_resources()


class _SoakRun:
    def __init__(self, args: argparse.Namespace, app: QCoreApplication) -> None:
        self.args = args
        self.app = app
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        if args.output_dir is None:
            self.output_dir = Path(tempfile.mkdtemp(prefix="m25_multi_device_soak_"))
            self.output_dir_kind = "temporary_persistent"
        else:
            self.output_dir = Path(args.output_dir).expanduser().resolve()
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.output_dir_kind = "user_specified"
        self.started_at_utc = _utc_now()
        self.started_monotonic = time.monotonic()
        self.measurement_started = 0.0
        self.measurement_ended = 0.0
        self.broker = UdpEndpointBroker(local_port=0)
        self.directory = EndpointSessionDirectory(self.broker)
        self.devices: list[_EndpointMetrics] = []
        self.latency_probe = _LatencyProbe(
            int(args.marker_interval_ms),
            self._sample_resources,
        )
        self.socket_counts: list[int] = []
        self.runtime_counts: list[int] = []
        self.core_counts: list[int] = []
        self.setup_error = ""
        self.cleanup_errors: list[str] = []
        self.forced_broker_stop = False
        self.directory_shutdown_ok = False
        self.simulator_shutdown_ok = False
        self.latency_shutdown_ok = False
        self._recording_registry = RecordingPathRegistry()

    def execute(self) -> dict:
        try:
            self._setup_sessions()
            self._negotiate_capture(True)
            self._start_recorders()
            self._run_measurement()
            self._stop_recorders()
            # Restore the device capture profile while presence/identity
            # evidence is still being serviced by the Qt event loop.  Long
            # SDB validation is an offline harness concern and must not delay
            # the real terminal ownership handshake past the presence timeout.
            self._negotiate_capture(False)
            self._validate_sdb_files()
        except BaseException as exc:
            self.setup_error = f"{type(exc).__name__}: {exc}"
            self.cleanup_errors.append(traceback.format_exc())
        finally:
            self._cleanup()
        return self._build_report()

    def _device_specs(self) -> tuple[_DeviceSpec, ...]:
        specs: list[_DeviceSpec] = []
        for index in range(int(self.args.endpoint_count)):
            afd = index % 2 == 0
            specs.append(
                _DeviceSpec(
                    index=index,
                    hardware_type="afd01c" if afd else "esa01",
                    product_identity="AFD01C" if afd else "ESA01",
                    service_protocol=8 if afd else 6,
                    serial_number=("AFD01C" if afd else "ESA01") + f"-SOAK-{index + 1:02d}",
                )
            )
        return tuple(specs)

    def _setup_sessions(self) -> None:
        simulators = [
            _LoopbackDeviceSimulator(spec, self.args.rate_hz)
            for spec in self._device_specs()
        ]
        for simulator in simulators:
            endpoint = simulator.endpoint
            core = DeviceSessionCore(
                endpoint=endpoint,
                profile_store=ProfileStore(),
            )
            runtime = self.directory.get_or_create_runtime(endpoint, core=core)
            core.setParent(runtime)
            attachment = self.directory.acquire_customer_attachment(
                endpoint,
                f"m25-soak-customer-{simulator.spec.token}",
                CustomerAttachmentScope(endpoint, 1),
                subscription_hz=int(round(self.args.rate_hz)),
            )
            metrics = _EndpointMetrics(
                simulator.spec,
                simulator,
                endpoint,
                attachment=attachment,
            )
            runtime.datagram_received.connect(metrics.on_datagram)
            runtime.datagram_sent.connect(metrics.on_sent)
            runtime.records_received.connect(metrics.on_records)
            self.devices.append(metrics)

        active_port = self.directory.active_local_port
        if active_port is None:
            raise RuntimeError("UDP broker did not expose an active local port")
        for metrics in self.devices:
            metrics.simulator.start(active_port)

        ready = self._pump_until(
            lambda: all(
                item.attachment is not None
                and item.attachment.runtime.identity_authorization_scope is not None
                and item.attachment.core.product_store.capabilities_record is not None
                and item.attachment.core.product_store.telemetry_ready
                for item in self.devices
            ),
            timeout_s=float(self.args.setup_timeout_s),
        )
        if not ready:
            raise RuntimeError("loopback identity/capability authorization timed out")
        # Drain the startup identity/definition datagrams before recorders become
        # active so their files contain only the measured stream and live control.
        self._pump_for(0.1)
        self._sample_resources()

    def _negotiate_capture(self, support_full: bool) -> None:
        for item in self.devices:
            attachment = item.attachment
            if attachment is None:
                raise RuntimeError("capture negotiation requires a Customer attachment")
            if item.controller is None:
                item.controller = CaptureProfileController(
                    attachment.core,
                    parent=attachment.runtime,
                    operation_gateway=attachment.new_operation_gateway(),
                )
                item.controller.finished.connect(item.on_capture_finished)
            before = len(item.capture_results)
            if not item.controller.request(bool(support_full)):
                if len(item.capture_results) == before:
                    raise RuntimeError(
                        f"capture-profile request was rejected for {_endpoint_text(item.endpoint)}"
                    )

        confirmed = self._pump_until(
            lambda: all(
                item.capture_results
                and item.capture_results[-1]["target_support_full"] is bool(support_full)
                and item.capture_results[-1]["confirmed"]
                for item in self.devices
            ),
            timeout_s=float(self.args.setup_timeout_s),
        )
        if not confirmed:
            target = "support_full" if support_full else "customer_live"
            raise RuntimeError(f"capture-profile {target} confirmation timed out")
        if support_full:
            if not all(
                item.controller is not None
                and item.controller.recording_lease_active
                and item.attachment is not None
                and item.attachment.runtime.operation_gate.state
                is RuntimeOperationGateState.MUTATING
                and item.simulator.capture_full
                for item in self.devices
            ):
                raise RuntimeError("capture-profile ACK did not retain the real recorder/gate owner")
        elif not all(
            item.controller is not None
            and not item.controller.recording_lease_active
            and item.attachment is not None
            and item.attachment.runtime.operation_gate.state
            is RuntimeOperationGateState.IDLE
            and not item.simulator.capture_full
            for item in self.devices
        ):
            raise RuntimeError("customer-live restore did not release recorder/gate ownership")

    def _start_recorders(self) -> None:
        for item in self.devices:
            attachment = item.attachment
            if attachment is None:
                raise RuntimeError("recorder requires a Customer attachment")
            hardware_type = attachment.core.profile_store.current_hw_type()
            profile = (
                attachment.core.profile_store.get_profile(hardware_type)
                if hardware_type is not None
                else None
            )
            profile_dict = None if profile is None else profile_to_dict(profile)
            endpoint_slug = f"{item.endpoint[0].replace('.', '-')}_{item.endpoint[1]}"
            requested = self.output_dir / (
                f"customer_{self.run_id}_{endpoint_slug}.sdb"
            )
            reservation = None
            try:
                reservation = self._recording_registry.reserve_unique(requested)
                recorder = DataRecorder(
                    reservation.path,
                    profile_dict=profile_dict,
                    queue_size=int(self.args.recorder_queue_size),
                    format_version=SDB_VERSION_V3,
                    metadata={
                        "capture_profile": "support_full",
                        "recording_mode": "customer_full_capture",
                        "presentation": "customer",
                        "hardware_type": item.spec.hardware_type,
                        "identity": {
                            "model": item.spec.product_identity,
                            "serial_number": item.spec.serial_number,
                            "product_service_protocol": item.spec.service_protocol,
                        },
                        "endpoint": {
                            "ip": item.endpoint[0],
                            "port": item.endpoint[1],
                        },
                        "evidence_scope": "host_loopback_soak_only",
                        "device_peer": "local_udp_simulator",
                        "hardware_acceptance": False,
                        "capture_negotiation": {
                            "controller": "CaptureProfileController",
                            "request": "SET_CAPTURE_PROFILE support_full=1",
                            "proof": "matching SERVICE_CONTROL_RESPONSE applied_mask bit 6",
                            "confirmed": True,
                            "simulated": True,
                        },
                        "soak_run_id": self.run_id,
                    },
                )
                if not recorder.start(reservation):
                    reservation = None
                    raise RuntimeError("DataRecorder.start() returned false")
                reservation = None
            except (OSError, RecordingPathError, RuntimeError, ValueError):
                if reservation is not None:
                    reservation.discard_failed_file()
                raise
            item.recorder = recorder
            item.sdb_path = recorder.filepath
            item.recording_active = True

    def _run_measurement(self) -> None:
        self.measurement_started = time.monotonic()
        for item in self.devices:
            item.simulator.begin_measurement(self.measurement_started)
        self.latency_probe.start()
        duration_timer = QTimer()
        duration_timer.setSingleShot(True)
        duration_timer.setTimerType(Qt.TimerType.PreciseTimer)
        duration_timer.timeout.connect(self.app.quit)
        duration_timer.start(max(1, int(round(float(self.args.duration) * 1000.0))))
        self.app.exec()
        duration_timer.stop()
        self.measurement_ended = time.monotonic()
        for item in self.devices:
            item.simulator.end_measurement()
        self.latency_shutdown_ok = self.latency_probe.stop()
        # Continue dispatching queued broker datagrams after the senders freeze.
        self._pump_until(
            lambda: all(
                item.parsed_fast
                >= item.simulator.measurement_sent.get("fast_state", 0)
                for item in self.devices
            ),
            timeout_s=1.0,
        )
        self._pump_for(0.05)
        self._sample_resources()

    def _stop_recorders(self) -> None:
        for item in self.devices:
            item.recording_active = False
        for item in self.devices:
            recorder = item.recorder
            item.recorder_stop_ok = bool(recorder is not None and recorder.stop())

    def _validate_sdb_files(self) -> None:
        for item in self.devices:
            if item.sdb_path is None:
                continue
            try:
                sdb = DataImporter.open_sdb(item.sdb_path)
                item.sdb_raw_records = len(sdb.raw_records)
                item.sdb_control_records = len(sdb.control_records)
                item.sdb_quality = dict(sdb.quality)
                for record in sdb.iter_records():
                    if not isinstance(record, ServiceFastState):
                        continue
                    sequence_ms = int(record.timestamp) & 0x00FFFFFF
                    if sequence_ms == 0:
                        continue
                    item.sdb_measured_fast += 1
                    if int(record.timestamp) >> 24 != item.spec.token:
                        item.sdb_cross_route_errors += 1
                item.sdb_valid = bool(
                    sdb.version == SDB_VERSION_V3
                    and sdb.metadata.get("capture_profile") == "support_full"
                    and sdb.metadata.get("device_peer") == "local_udp_simulator"
                    and sdb.metadata.get("hardware_acceptance") is False
                    and sdb.quality.get("complete") is True
                )
            except BaseException as exc:
                item.sdb_quality = {"validation_error": f"{type(exc).__name__}: {exc}"}

    def _cleanup(self) -> None:
        if self.latency_probe._thread is not None and not self.latency_shutdown_ok:
            self.latency_shutdown_ok = self.latency_probe.stop()

        # A failed path may have left a confirmed full-capture owner.  Keep the
        # simulators alive while attempting the one safe terminal restore.
        for item in self.devices:
            item.recording_active = False
            recorder = item.recorder
            if recorder is not None and recorder.is_recording:
                try:
                    item.recorder_stop_ok = recorder.stop()
                except BaseException as exc:
                    self.cleanup_errors.append(
                        f"recorder stop {_endpoint_text(item.endpoint)}: {exc}"
                    )

        pending_restore = [
            item
            for item in self.devices
            if item.controller is not None and item.controller.recording_lease_active
        ]
        for item in pending_restore:
            controller = item.controller
            if controller is not None and controller.pending_request_id is None:
                try:
                    controller.request(False)
                except BaseException as exc:
                    self.cleanup_errors.append(
                        f"capture restore {_endpoint_text(item.endpoint)}: {exc}"
                    )
        if pending_restore:
            self._pump_until(
                lambda: all(
                    item.controller is None
                    or not item.controller.recording_lease_active
                    for item in pending_restore
                ),
                timeout_s=min(3.0, float(self.args.setup_timeout_s)),
            )

        simulator_results = []
        for item in self.devices:
            simulator_results.append(item.simulator.stop())
        self.simulator_shutdown_ok = all(simulator_results) if simulator_results else True

        for item in reversed(self.devices):
            attachment = item.attachment
            if attachment is None:
                item.release_ok = True
                continue
            try:
                item.release_ok = attachment.release()
            except BaseException as exc:
                self.cleanup_errors.append(
                    f"attachment release {_endpoint_text(item.endpoint)}: {exc}"
                )
                item.release_ok = False
        self.app.processEvents()
        try:
            self.directory_shutdown_ok = self.directory.shutdown()
        except BaseException as exc:
            self.cleanup_errors.append(f"directory shutdown: {exc}")
            self.directory_shutdown_ok = False
        if not self.directory_shutdown_ok:
            self.forced_broker_stop = True
            try:
                self.broker.stop_broker()
            except BaseException as exc:
                self.cleanup_errors.append(f"forced broker stop: {exc}")

    def _sample_resources(self) -> None:
        runtimes = self.directory.runtimes()
        self.socket_counts.append(1 if self.directory.active_local_port is not None else 0)
        self.runtime_counts.append(len(runtimes))
        self.core_counts.append(len({id(runtime.core) for runtime in runtimes}))

    def _pump_until(self, predicate: Callable[[], bool], timeout_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.002)
        self.app.processEvents()
        return bool(predicate())

    def _pump_for(self, duration_s: float) -> None:
        deadline = time.monotonic() + max(0.0, float(duration_s))
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _build_report(self) -> dict:
        actual_duration = max(0.0, self.measurement_ended - self.measurement_started)
        latency_values = self.latency_probe.latencies_ms
        latency_p95 = _percentile(latency_values, 0.95)
        latency_max = max(latency_values, default=math.inf)
        latency_p95_json = latency_p95 if math.isfinite(latency_p95) else None
        latency_max_json = latency_max if math.isfinite(latency_max) else None
        per_endpoint = []
        for item in self.devices:
            sent = item.simulator.measurement_sent
            fast_sent = int(sent.get("fast_state", 0))
            total_sent = sum(int(value) for value in sent.values())
            recorder = item.recorder
            written = 0 if recorder is None else int(recorder.written_count)
            written_records = 0 if recorder is None else int(recorder.written_record_count)
            recorder_drops = 0 if recorder is None else int(recorder.dropped_count)
            achieved_rate = fast_sent / actual_duration if actual_duration > 0 else 0.0
            per_endpoint.append(
                {
                    "index": item.spec.index,
                    "endpoint": {
                        "ip": item.endpoint[0],
                        "port": item.endpoint[1],
                    },
                    "hardware_type": item.spec.hardware_type,
                    "product_identity": item.spec.product_identity,
                    "serial_number": item.spec.serial_number,
                    "simulator": {
                        "frames_sent_total": sum(item.simulator.sent.values()),
                        "frames_sent_by_kind": item.simulator.sent,
                        "measurement_frames_sent_total": total_sent,
                        "measurement_frames_sent_by_kind": sent,
                        "host_frames_received_by_kind": item.simulator.received,
                        "send_failures": item.simulator.send_failures,
                        "thread_alive_after_shutdown": item.simulator.thread_alive,
                    },
                    "capture_negotiation": {
                        "mode": "real_CaptureProfileController",
                        "device_peer": "loopback_simulator",
                        "hardware_acceptance": False,
                        "results": item.capture_results,
                    },
                    "frames": {
                        "telemetry_sent": fast_sent,
                        "telemetry_parsed": item.parsed_fast,
                        "raw_datagrams_routed": item.raw_routed,
                        "recorder_rx_enqueued": item.recorder_enqueued,
                        "recorder_rx_written": written,
                        "recorder_records_written": written_records,
                        "recorder_control_enqueued": item.control_enqueued,
                        "recorder_queue_drops": recorder_drops,
                        "recorder_enqueue_failures": item.recorder_enqueue_failures,
                        "control_enqueue_failures": item.control_enqueue_failures,
                        "cross_route_errors": item.cross_route_errors,
                        "duplicate_parse_errors": item.duplicate_parse_errors,
                        "achieved_telemetry_hz": achieved_rate,
                    },
                    "sdb": {
                        "path": None if item.sdb_path is None else str(item.sdb_path),
                        "version": SDB_VERSION_V3,
                        "finalized": item.recorder_stop_ok,
                        "validated": item.sdb_valid,
                        "raw_records": item.sdb_raw_records,
                        "control_records": item.sdb_control_records,
                        "measured_fast_records": item.sdb_measured_fast,
                        "cross_route_errors": item.sdb_cross_route_errors,
                        "quality": item.sdb_quality,
                    },
                    "attachment_released": item.release_ok,
                }
            )

        expected_markers = max(
            1,
            int(float(self.args.duration) * 1000.0 / int(self.args.marker_interval_ms)) - 2,
        )
        expected_count = int(self.args.endpoint_count)
        resource_samples_ok = bool(
            self.socket_counts
            and min(self.socket_counts) == 1
            and max(self.socket_counts) == 1
            and min(self.runtime_counts) == expected_count
            and max(self.runtime_counts) == expected_count
            and min(self.core_counts) == expected_count
            and max(self.core_counts) == expected_count
        )
        min_rate = float(self.args.rate_hz) * float(self.args.min_rate_ratio)
        checks = {
            "setup_completed": not self.setup_error,
            "measurement_duration_reached": (
                actual_duration >= float(self.args.duration)
            ),
            "topology_is_one_broker_four_loopback_endpoints": (
                int(self.args.endpoint_count) == 4 and len(per_endpoint) == 4
            ),
            "one_socket_and_stable_runtime_core_counts": resource_samples_ok,
            "all_capture_profile_start_and_restore_confirmed": all(
                len(item.capture_results) >= 2
                and item.capture_results[0]["target_support_full"]
                and item.capture_results[0]["confirmed"]
                and not item.capture_results[-1]["target_support_full"]
                and item.capture_results[-1]["confirmed"]
                for item in self.devices
            ),
            "each_endpoint_meets_rate": all(
                endpoint["frames"]["achieved_telemetry_hz"] >= min_rate
                for endpoint in per_endpoint
            ),
            "telemetry_sent_parsed_and_sdb_counts_match": all(
                endpoint["frames"]["telemetry_sent"]
                == endpoint["frames"]["telemetry_parsed"]
                == endpoint["sdb"]["measured_fast_records"]
                for endpoint in per_endpoint
            ),
            "raw_routed_enqueued_written_counts_match": all(
                endpoint["frames"]["raw_datagrams_routed"]
                == endpoint["frames"]["recorder_rx_enqueued"]
                == endpoint["frames"]["recorder_rx_written"]
                == endpoint["sdb"]["raw_records"]
                for endpoint in per_endpoint
            ),
            "zero_route_duplicate_and_simulator_errors": all(
                endpoint["frames"]["cross_route_errors"] == 0
                and endpoint["frames"]["duplicate_parse_errors"] == 0
                and endpoint["sdb"]["cross_route_errors"] == 0
                and endpoint["simulator"]["send_failures"] == 0
                for endpoint in per_endpoint
            ),
            "zero_recorder_drops": all(
                endpoint["frames"]["recorder_queue_drops"] == 0
                and endpoint["frames"]["recorder_enqueue_failures"] == 0
                and endpoint["frames"]["control_enqueue_failures"] == 0
                for endpoint in per_endpoint
            ),
            "all_sdb_v3_complete": all(
                endpoint["sdb"]["finalized"]
                and endpoint["sdb"]["validated"]
                for endpoint in per_endpoint
            ),
            "latency_markers_complete": (
                len(latency_values) >= expected_markers
                and self.latency_probe.thread_violations == 0
            ),
            "latency_p95_within_threshold": (
                math.isfinite(latency_p95)
                and latency_p95 <= float(self.args.max_p95_latency_ms)
            ),
            "clean_shutdown": bool(
                self.latency_shutdown_ok
                and self.simulator_shutdown_ok
                and all(item.release_ok for item in self.devices)
                and self.directory_shutdown_ok
                and not self.broker.isRunning()
                and self.directory.active_local_port is None
                and not self.forced_broker_stop
            ),
        }
        passed = bool(checks and all(checks.values()))
        acceptance_duration = float(self.args.duration) >= DEFAULT_DURATION_S
        return {
            "schema_version": 1,
            "tool": "m25_multi_device_soak",
            "result": (
                "PASS_30_MIN_HOST_SOAK"
                if passed and acceptance_duration
                else "PASS_SHORT_SMOKE_NOT_30_MIN_ACCEPTANCE"
                if passed
                else "FAIL"
            ),
            "passed": passed,
            "acceptance_duration_met": acceptance_duration,
            "hardware_acceptance": False,
            "evidence_scope": "host_loopback_soak_only",
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": _utc_now(),
            "run_id": self.run_id,
            "configuration": {
                "duration_requested_s": float(self.args.duration),
                "duration_actual_s": actual_duration,
                "endpoint_count": int(self.args.endpoint_count),
                "rate_hz_per_endpoint": float(self.args.rate_hz),
                "marker_interval_ms": int(self.args.marker_interval_ms),
                "recorder_queue_size": int(self.args.recorder_queue_size),
                "output_dir": str(self.output_dir),
                "output_dir_kind": self.output_dir_kind,
            },
            "environment": {
                "machine_model": _machine_model(),
                "machine": platform.machine(),
                "node": platform.node(),
                "cpu": _cpu_description(),
                "logical_cpu_count": os.cpu_count(),
                "os": platform.platform(),
                "python": sys.version,
                "python_implementation": platform.python_implementation(),
                "pyside6": PySide6.__version__,
                "qt": qVersion(),
                "qt_qpa_platform": os.environ.get("QT_QPA_PLATFORM", ""),
                "offscreen": os.environ.get("QT_QPA_PLATFORM", "").casefold()
                == "offscreen",
                "packaged": bool(getattr(sys, "frozen", False)),
            },
            "topology": {
                "broker_instances": 1,
                "logical_socket_count_min_during_measurement": min(
                    self.socket_counts, default=0
                ),
                "logical_socket_count_max_during_measurement": max(
                    self.socket_counts, default=0
                ),
                "runtime_count_min_during_measurement": min(
                    self.runtime_counts, default=0
                ),
                "runtime_count_max_during_measurement": max(
                    self.runtime_counts, default=0
                ),
                "core_count_min_during_measurement": min(self.core_counts, default=0),
                "core_count_max_during_measurement": max(self.core_counts, default=0),
                "runtime_count_after_shutdown": len(self.directory.runtimes()),
                "broker_statistics": vars(self.broker.statistics),
            },
            "latency": {
                "marker_count": len(latency_values),
                "p95_ms": latency_p95_json,
                "max_ms": latency_max_json,
                "main_thread_violations": self.latency_probe.thread_violations,
            },
            "thresholds": {
                "min_measurement_duration_s": float(self.args.duration),
                "max_p95_latency_ms": float(self.args.max_p95_latency_ms),
                "min_rate_hz_per_endpoint": min_rate,
                "max_route_errors": 0,
                "max_duplicate_parse_errors": 0,
                "max_recorder_drops": 0,
                "logical_socket_count": 1,
                "runtime_and_core_count": expected_count,
            },
            "frames": {
                "telemetry_sent": sum(
                    endpoint["frames"]["telemetry_sent"] for endpoint in per_endpoint
                ),
                "telemetry_parsed": sum(
                    endpoint["frames"]["telemetry_parsed"] for endpoint in per_endpoint
                ),
                "raw_datagrams_routed": sum(
                    endpoint["frames"]["raw_datagrams_routed"]
                    for endpoint in per_endpoint
                ),
                "recorder_rx_written": sum(
                    endpoint["frames"]["recorder_rx_written"]
                    for endpoint in per_endpoint
                ),
                "recorder_records_written": sum(
                    endpoint["frames"]["recorder_records_written"]
                    for endpoint in per_endpoint
                ),
                "recorder_drops": sum(
                    endpoint["frames"]["recorder_queue_drops"]
                    for endpoint in per_endpoint
                ),
                "cross_route_errors": sum(
                    endpoint["frames"]["cross_route_errors"]
                    + endpoint["sdb"]["cross_route_errors"]
                    for endpoint in per_endpoint
                ),
                "duplicate_parse_errors": sum(
                    endpoint["frames"]["duplicate_parse_errors"]
                    for endpoint in per_endpoint
                ),
            },
            "endpoints": per_endpoint,
            "shutdown": {
                "latency_thread_stopped": self.latency_shutdown_ok,
                "simulator_threads_stopped": self.simulator_shutdown_ok,
                "attachments_released": all(item.release_ok for item in self.devices),
                "directory_shutdown": self.directory_shutdown_ok,
                "broker_thread_stopped": not self.broker.isRunning(),
                "socket_count_after_shutdown": (
                    1 if self.directory.active_local_port is not None else 0
                ),
                "forced_broker_stop": self.forced_broker_stop,
                "clean": checks["clean_shutdown"],
            },
            "checks": checks,
            "error": self.setup_error or None,
            "cleanup_errors": self.cleanup_errors,
        }


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive finite number")
    return parsed


def _endpoint_count(value: str) -> int:
    parsed = int(value)
    if not (1 <= parsed <= DEFAULT_ENDPOINT_COUNT):
        raise argparse.ArgumentTypeError("endpoint count must be 1..4")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the M25 real Broker/Directory four-loopback Customer-session host soak."
        )
    )
    parser.add_argument(
        "--duration",
        type=_positive_float,
        default=DEFAULT_DURATION_S,
        help="measurement duration in seconds (default: 1800)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="SDB/report directory; default creates a persistent temporary directory",
    )
    parser.add_argument(
        "--endpoint-count",
        type=_endpoint_count,
        default=DEFAULT_ENDPOINT_COUNT,
        help="diagnostic override, 1..4 (M25 acceptance requires 4)",
    )
    parser.add_argument(
        "--rate-hz",
        type=_positive_float,
        default=DEFAULT_RATE_HZ,
        help="telemetry rate per endpoint (default: 20)",
    )
    parser.add_argument(
        "--marker-interval-ms",
        type=int,
        default=DEFAULT_MARKER_INTERVAL_MS,
        help="Qt main-thread latency marker interval (default: 100)",
    )
    parser.add_argument(
        "--max-p95-latency-ms",
        type=_positive_float,
        default=DEFAULT_MAX_P95_LATENCY_MS,
        help="failure threshold for queued main-thread marker p95 (default: 100)",
    )
    parser.add_argument(
        "--min-rate-ratio",
        type=_positive_float,
        default=DEFAULT_MIN_RATE_RATIO,
        help="minimum achieved/configured telemetry rate ratio (default: 0.95)",
    )
    parser.add_argument(
        "--recorder-queue-size",
        type=int,
        default=10_000,
        help="queue size for each independent DataRecorder (default: 10000)",
    )
    parser.add_argument(
        "--setup-timeout-s",
        type=_positive_float,
        default=5.0,
        help="identity/capability/capture negotiation timeout (default: 5)",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not (1 <= int(args.marker_interval_ms) <= 10_000):
        parser.error("--marker-interval-ms must be 1..10000")
    if not (1 <= int(args.recorder_queue_size) <= 1_000_000):
        parser.error("--recorder-queue-size must be 1..1000000")
    if not (1.0 <= float(args.rate_hz) <= 20.0):
        parser.error("--rate-hz must be 1..20 for Product Service subscription")
    if not (0.0 < float(args.min_rate_ratio) <= 1.0):
        parser.error("--min-rate-ratio must be in (0, 1]")


def _write_report(output_dir: Path, run_id: str, report: dict) -> Path:
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    base = output_dir / f"m25_multi_device_soak_{run_id}.json"
    for index in range(10_000):
        candidate = base if index == 0 else base.with_name(
            f"{base.stem}_{index:03d}{base.suffix}"
        )
        try:
            with candidate.open("x", encoding="utf-8") as stream:
                stream.write(payload)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError("soak report filename sequence is exhausted")


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    app = QCoreApplication.instance() or QCoreApplication([sys.argv[0]])
    run = _SoakRun(args, app)
    report = run.execute()
    try:
        report_path = _write_report(run.output_dir, run.run_id, report)
        report["report_path"] = str(report_path)
        # Rewrite only the just-created, uniquely owned report so stdout and the
        # persisted evidence contain the same self-reference.
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except BaseException as exc:
        report["passed"] = False
        report["result"] = "FAIL"
        report["report_write_error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
