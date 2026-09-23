"""Process-wide iperf3 client orchestration and crash-resilient evidence."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, replace
from enum import Enum
import ipaddress
import json
import math
from pathlib import Path
import re
import subprocess
import time
from typing import Optional

from PySide6.QtCore import QObject, QProcess, QTimer, Signal, Slot

from satellite_debug_tool.core.external_power_monitor import (
    ExternalPowerPhase,
    ExternalPowerSample,
    ExternalPowerStore,
)


MIN_IPERF_VERSION = (3, 18, 0)
SEGMENT_SECONDS = 600
RETRY_SECONDS = 10
HISTORY_SECONDS = 30 * 60
_RATE_RE = re.compile(r"^[1-9][0-9]*(?:\.[0-9]+)?[KMG]?$", re.IGNORECASE)
_VERSION_RE = re.compile(r"iperf\s+(\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)


class IperfValidationError(ValueError):
    pass


class IperfProtocol(str, Enum):
    UDP = "udp"
    TCP = "tcp"


class IperfDirection(str, Enum):
    UL = "ul"
    DL = "dl"
    BOTH = "both"

    def members(self) -> tuple[str, ...]:
        if self is IperfDirection.BOTH:
            return ("ul", "dl")
        return (self.value,)


class IperfTestPhase(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class IperfLanePhase(str, Enum):
    DISABLED = "disabled"
    STARTING = "starting"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    STOPPED = "stopped"


@dataclass(frozen=True)
class IperfTestConfig:
    executable: str
    server: str = "60.205.157.141"
    local_host: str = ""
    protocol: IperfProtocol = IperfProtocol.UDP
    direction: IperfDirection = IperfDirection.BOTH
    ul_port: int = 5201
    dl_port: int = 5202
    ul_rate: str = "491K"
    dl_rate: str = "200K"
    continuous: bool = True
    duration_seconds: int = 0

    def validate(self) -> None:
        executable = Path(self.executable).expanduser()
        if not executable.is_file():
            raise IperfValidationError("iperf3 executable does not exist")
        try:
            server = ipaddress.ip_address(self.server.strip())
            local = ipaddress.ip_address(self.local_host.strip())
        except ValueError as exc:
            raise IperfValidationError("server and local host must be IPv4 addresses") from exc
        if server.version != 4 or local.version != 4:
            raise IperfValidationError("server and local host must be IPv4 addresses")
        if local.is_unspecified or local.is_multicast:
            raise IperfValidationError("local host must be a usable IPv4 address")
        for name, value in (("ul_port", self.ul_port), ("dl_port", self.dl_port)):
            if not 1 <= int(value) <= 65535:
                raise IperfValidationError(f"{name} must be 1..65535")
        if self.ul_port == self.dl_port and self.direction is IperfDirection.BOTH:
            raise IperfValidationError("simultaneous UL and DL require different ports")
        for name, value in (("ul_rate", self.ul_rate), ("dl_rate", self.dl_rate)):
            if not _RATE_RE.fullmatch(str(value).strip()):
                raise IperfValidationError(f"{name} must be a positive bitrate such as 491K")
        if not self.continuous and int(self.duration_seconds) <= 0:
            raise IperfValidationError("finite duration must be positive")


@dataclass(frozen=True)
class IperfMeasurement:
    timestamp_ns: int
    elapsed_seconds: float
    direction: str
    protocol: str
    bits_per_second: float
    bytes_transferred: int
    jitter_ms: Optional[float] = None
    lost_packets: Optional[int] = None
    packets: Optional[int] = None
    lost_percent: Optional[float] = None
    out_of_order: Optional[int] = None
    retransmits: Optional[int] = None


@dataclass(frozen=True)
class IperfLaneSnapshot:
    phase: IperfLanePhase = IperfLanePhase.DISABLED
    local_host: str = ""
    bits_per_second: float = 0.0
    total_bytes: int = 0
    jitter_ms: Optional[float] = None
    lost_packets: Optional[int] = None
    packets: Optional[int] = None
    lost_percent: Optional[float] = None
    out_of_order: Optional[int] = None
    retransmits: Optional[int] = None
    sessions: int = 0
    retries: int = 0
    error: str = ""


@dataclass(frozen=True)
class IperfTestSnapshot:
    phase: IperfTestPhase = IperfTestPhase.IDLE
    session_id: str = ""
    session_directory: str = ""
    started_ns: int = 0
    finished_ns: int = 0
    continuous: bool = True
    duration_seconds: int = 0
    ul: IperfLaneSnapshot = IperfLaneSnapshot()
    dl: IperfLaneSnapshot = IperfLaneSnapshot()
    message: str = ""

    @property
    def active(self) -> bool:
        return self.phase in {
            IperfTestPhase.STARTING,
            IperfTestPhase.RUNNING,
            IperfTestPhase.DEGRADED,
            IperfTestPhase.STOPPING,
        }


class IperfTestStore(QObject):
    updated = Signal()
    measurement_received = Signal(object)
    event_received = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._snapshot = IperfTestSnapshot()
        self._measurements: list[IperfMeasurement] = []
        self._power_samples: list[ExternalPowerSample] = []
        self._events: list[dict[str, object]] = []

    @property
    def snapshot(self) -> IperfTestSnapshot:
        return self._snapshot

    def begin(self, snapshot: IperfTestSnapshot) -> None:
        self._snapshot = snapshot
        self._measurements.clear()
        self._power_samples.clear()
        self._events.clear()
        self.updated.emit()

    def set_snapshot(self, snapshot: IperfTestSnapshot) -> None:
        self._snapshot = snapshot
        self.updated.emit()

    def add_measurement(self, measurement: IperfMeasurement) -> None:
        self._measurements.append(measurement)
        cutoff = measurement.timestamp_ns - int(HISTORY_SECONDS * 1e9)
        while self._measurements and self._measurements[0].timestamp_ns < cutoff:
            self._measurements.pop(0)
        self.measurement_received.emit(measurement)
        self.updated.emit()

    def add_power_sample(self, sample: ExternalPowerSample) -> None:
        self._power_samples.append(sample)
        cutoff = sample.host_timestamp_ns - int(HISTORY_SECONDS * 1e9)
        while self._power_samples and self._power_samples[0].host_timestamp_ns < cutoff:
            self._power_samples.pop(0)
        self.updated.emit()

    def add_event(self, event: dict[str, object]) -> None:
        self._events.append(dict(event))
        self._events = self._events[-500:]
        self.event_received.emit(dict(event))
        self.updated.emit()

    def measurements(self) -> tuple[IperfMeasurement, ...]:
        return tuple(self._measurements)

    def power_samples(self) -> tuple[ExternalPowerSample, ...]:
        return tuple(self._power_samples)

    def events(self) -> tuple[dict[str, object], ...]:
        return tuple(self._events)


class IperfSessionWriter:
    _MEASUREMENT_FIELDS = (
        "timestamp_ns", "kind", "direction", "protocol", "elapsed_seconds",
        "bits_per_second", "bytes_transferred", "jitter_ms", "lost_packets",
        "packets", "lost_percent", "out_of_order", "retransmits",
        "voltage_v", "current_a", "power_w", "power_phase",
    )

    def __init__(self, root: Path, session_id: str, config: IperfTestConfig) -> None:
        self.directory = root / session_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self._config = config
        self._counts = {"ul": 0, "dl": 0, "power": 0, "events": 0}
        self._bps_sum = {"ul": 0.0, "dl": 0.0}
        self._power_sum = {"voltage_v": 0.0, "current_a": 0.0, "power_w": 0.0}
        self._power_min = {key: math.inf for key in self._power_sum}
        self._power_max = {key: -math.inf for key in self._power_sum}
        self._started_ns = time.time_ns()
        self._write_json("config.json", self._config_payload(config))
        self._write_json("summary.json", self._summary_payload("incomplete", ""))
        with (self.directory / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=self._MEASUREMENT_FIELDS).writeheader()

    @staticmethod
    def _config_payload(config: IperfTestConfig) -> dict[str, object]:
        payload = asdict(config)
        payload["protocol"] = config.protocol.value
        payload["direction"] = config.direction.value
        return payload

    def raw(self, direction: str, payload: dict[str, object]) -> None:
        self._append_jsonl(f"iperf_{direction}.raw.jsonl", payload)

    def event(self, level: str, code: str, message: str, **extra: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "timestamp_ns": time.time_ns(),
            "level": level,
            "code": code,
            "message": message,
        }
        payload.update(extra)
        self._append_jsonl("events.jsonl", payload)
        self._counts["events"] += 1
        return payload

    def measurement(self, item: IperfMeasurement) -> None:
        self._counts[item.direction] += 1
        self._bps_sum[item.direction] += item.bits_per_second
        row = {field: "" for field in self._MEASUREMENT_FIELDS}
        row.update(asdict(item))
        row["kind"] = "iperf"
        self._append_csv(row)

    def power(self, sample: ExternalPowerSample) -> None:
        self._counts["power"] += 1
        for key, value in (
            ("voltage_v", sample.voltage_v),
            ("current_a", sample.current_a),
            ("power_w", sample.power_w),
        ):
            self._power_sum[key] += value
            self._power_min[key] = min(self._power_min[key], value)
            self._power_max[key] = max(self._power_max[key], value)
        row = {field: "" for field in self._MEASUREMENT_FIELDS}
        row.update({
            "timestamp_ns": sample.host_timestamp_ns,
            "kind": "power",
            "voltage_v": sample.voltage_v,
            "current_a": sample.current_a,
            "power_w": sample.power_w,
            "power_phase": ExternalPowerPhase.ONLINE.value,
        })
        self._append_csv(row)

    def finalize(
        self,
        outcome: str,
        message: str,
        snapshot: Optional[IperfTestSnapshot] = None,
    ) -> None:
        summary = self._summary_payload(outcome, message, snapshot)
        self._write_json("summary.json", summary)
        with (self.directory / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("metric", "value"))
            for key, value in summary.items():
                if not isinstance(value, (dict, list)):
                    writer.writerow((key, value))
            for direction in ("ul", "dl"):
                writer.writerow((f"{direction}_samples", self._counts[direction]))
                count = self._counts[direction]
                writer.writerow((
                    f"{direction}_average_bits_per_second",
                    self._bps_sum[direction] / count if count else 0.0,
                ))
                lane = summary.get(direction)
                if isinstance(lane, dict):
                    for key, value in lane.items():
                        writer.writerow((f"{direction}_{key}", value))
            power = summary.get("power")
            if isinstance(power, dict):
                for key, value in power.items():
                    writer.writerow((f"power_{key}", value))

    def _summary_payload(
        self,
        outcome: str,
        message: str,
        snapshot: Optional[IperfTestSnapshot] = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": 1,
            "outcome": outcome,
            "message": message,
            "started_ns": self._started_ns,
            "finished_ns": time.time_ns() if outcome != "incomplete" else 0,
            "ul_samples": self._counts["ul"],
            "dl_samples": self._counts["dl"],
            "power_samples": self._counts["power"],
            "events": self._counts["events"],
        }
        if snapshot is not None:
            for direction, lane in (("ul", snapshot.ul), ("dl", snapshot.dl)):
                lane_payload = asdict(lane)
                lane_payload["phase"] = lane.phase.value
                payload[direction] = lane_payload
        power_count = self._counts["power"]
        payload["power"] = {
            f"{key}_{suffix}": value
            for key in self._power_sum
            for suffix, value in (
                ("average", self._power_sum[key] / power_count if power_count else None),
                ("minimum", self._power_min[key] if power_count else None),
                ("maximum", self._power_max[key] if power_count else None),
            )
        }
        return payload

    def _append_csv(self, row: dict[str, object]) -> None:
        with (self.directory / "measurements.csv").open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=self._MEASUREMENT_FIELDS).writerow(row)

    def _append_jsonl(self, name: str, payload: dict[str, object]) -> None:
        with (self.directory / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()

    def _write_json(self, name: str, payload: dict[str, object]) -> None:
        target = self.directory / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)


class IperfTestController(QObject):
    active_changed = Signal(bool)

    def __init__(
        self,
        store: IperfTestStore,
        power_store: ExternalPowerStore,
        root: Path,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self._power_store = power_store
        self._root = Path(root)
        self._config: Optional[IperfTestConfig] = None
        self._writer: Optional[IperfSessionWriter] = None
        self._processes: dict[str, QProcess] = {}
        self._buffers = {"ul": bytearray(), "dl": bytearray()}
        self._retry_timers = {"ul": QTimer(self), "dl": QTimer(self)}
        self._stopping = False
        self._finalized = True
        self._monotonic_started = 0.0
        self._power_phase = ""
        for direction, timer in self._retry_timers.items():
            timer.setSingleShot(True)
            timer.timeout.connect(lambda d=direction: self._start_lane(d))
        power_store.sample_received.connect(self._on_power_sample)
        power_store.updated.connect(self._on_power_updated)

    @property
    def active(self) -> bool:
        return self.store.snapshot.active

    @staticmethod
    def inspect_executable(executable: str) -> tuple[int, int, int]:
        path = str(Path(executable).expanduser())
        if not Path(path).is_file():
            raise IperfValidationError("iperf3 executable does not exist")
        try:
            version_result = subprocess.run(
                [path, "--version"], capture_output=True, text=True,
                timeout=5, check=False,
            )
            help_result = subprocess.run(
                [path, "--help"], capture_output=True, text=True,
                timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise IperfValidationError(f"cannot execute iperf3: {exc}") from exc
        version_text = version_result.stdout + version_result.stderr
        match = _VERSION_RE.search(version_text)
        if version_result.returncode != 0 or match is None:
            raise IperfValidationError("cannot determine iperf3 version")
        version = tuple(int(value or 0) for value in match.groups())
        if version < MIN_IPERF_VERSION:
            raise IperfValidationError("iperf3 3.18 or newer is required")
        help_text = help_result.stdout + help_result.stderr
        if help_result.returncode != 0 or "--json-stream" not in help_text:
            raise IperfValidationError("iperf3 does not support --json-stream")
        return version

    def start(self, config: IperfTestConfig) -> None:
        if self.active:
            raise IperfValidationError("an iperf3 test is already active")
        config.validate()
        self.inspect_executable(config.executable)
        session_id = time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1_000_000_000:09d}"
        writer = IperfSessionWriter(self._root, session_id, config)
        self._config = config
        self._writer = writer
        self._stopping = False
        self._finalized = False
        self._monotonic_started = time.monotonic()
        self._power_phase = ""
        enabled = set(config.direction.members())
        snapshot = IperfTestSnapshot(
            phase=IperfTestPhase.STARTING,
            session_id=session_id,
            session_directory=str(writer.directory),
            started_ns=time.time_ns(),
            continuous=config.continuous,
            duration_seconds=config.duration_seconds,
            ul=IperfLaneSnapshot(
                IperfLanePhase.STARTING if "ul" in enabled else IperfLanePhase.DISABLED
            ),
            dl=IperfLaneSnapshot(
                IperfLanePhase.STARTING if "dl" in enabled else IperfLanePhase.DISABLED
            ),
            message="starting",
        )
        self.store.begin(snapshot)
        self._emit_event("info", "test_started", "iperf3 test started")
        self.active_changed.emit(True)
        self._on_power_updated()
        for direction in config.direction.members():
            self._start_lane(direction)

    def stop(self, reason: str = "stopped by user") -> None:
        if not self.active or self._stopping:
            return
        self._stopping = True
        for timer in self._retry_timers.values():
            timer.stop()
        self.store.set_snapshot(replace(
            self.store.snapshot,
            phase=IperfTestPhase.STOPPING,
            message=reason,
        ))
        self._emit_event("info", "test_stopping", reason)
        running = False
        for process in self._processes.values():
            if process.state() != QProcess.ProcessState.NotRunning:
                running = True
                process.terminate()
        if running:
            QTimer.singleShot(3000, self._force_stop)
        else:
            self._finish(IperfTestPhase.STOPPED, reason)

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        if self.active:
            self.stop("application shutdown")
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        for process in self._processes.values():
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if process.state() != QProcess.ProcessState.NotRunning and not process.waitForFinished(remaining):
                process.kill()
                process.waitForFinished(1000)
        if not self._finalized:
            self._finish(IperfTestPhase.STOPPED, "application shutdown")
        return all(
            process.state() == QProcess.ProcessState.NotRunning
            for process in self._processes.values()
        )

    def _start_lane(self, direction: str) -> None:
        if self._stopping or self._config is None or not self.active:
            return
        remaining = self._remaining_seconds()
        if remaining is not None and remaining <= 0:
            self._set_lane(direction, phase=IperfLanePhase.COMPLETED, error="")
            self._finish_if_complete()
            return
        duration = SEGMENT_SECONDS if remaining is None else min(SEGMENT_SECONDS, max(1, math.ceil(remaining)))
        old = self._processes.get(direction)
        if old is not None:
            old.deleteLater()
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(lambda d=direction: self._read_stdout(d))
        process.readyReadStandardError.connect(lambda d=direction: self._read_stderr(d))
        process.errorOccurred.connect(lambda error, d=direction: self._on_process_error(d, error))
        process.finished.connect(lambda code, status, d=direction: self._on_finished(d, code, status))
        self._processes[direction] = process
        self._buffers[direction].clear()
        self._set_lane(direction, phase=IperfLanePhase.STARTING, error="")
        process.start(self._config.executable, self._arguments(direction, duration))

    def _arguments(self, direction: str, duration: int) -> list[str]:
        assert self._config is not None
        config = self._config
        port = config.ul_port if direction == "ul" else config.dl_port
        rate = config.ul_rate if direction == "ul" else config.dl_rate
        args = [
            "-c", config.server, "-p", str(port), "-B", config.local_host,
            "-b", rate, "-t", str(duration), "-i", "1",
            "--json-stream", "--forceflush",
        ]
        if config.protocol is IperfProtocol.UDP:
            args.append("-u")
        if direction == "dl":
            args.append("-R")
        return args

    def _read_stdout(self, direction: str) -> None:
        process = self._processes.get(direction)
        if process is None:
            return
        buffer = self._buffers[direction]
        buffer.extend(bytes(process.readAllStandardOutput()))
        while b"\n" in buffer:
            raw, _, tail = buffer.partition(b"\n")
            buffer[:] = tail
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                self._emit_event("warning", "invalid_json", line, direction=direction)
                continue
            if not isinstance(payload, dict):
                continue
            if self._writer is not None:
                self._writer.raw(direction, payload)
            self._handle_payload(direction, payload)

    def _read_stderr(self, direction: str) -> None:
        process = self._processes.get(direction)
        if process is None:
            return
        text = bytes(process.readAllStandardError()).decode("utf-8", errors="replace").strip()
        if text:
            self._emit_event("warning", "iperf_stderr", text, direction=direction)

    def _handle_payload(self, direction: str, payload: dict[str, object]) -> None:
        event = str(payload.get("event", ""))
        data = payload.get("data")
        if event == "error" and isinstance(data, str):
            self._set_lane(direction, error=data)
            self._emit_event("error", "iperf_error", data, direction=direction)
            return
        if not isinstance(data, dict):
            return
        if event == "start":
            connected = data.get("connected")
            local_host = ""
            if isinstance(connected, list) and connected and isinstance(connected[0], dict):
                local_host = str(connected[0].get("local_host", ""))
            if self._config is None or local_host != self._config.local_host:
                self._set_lane(
                    direction,
                    phase=IperfLanePhase.BLOCKED,
                    local_host=local_host,
                    error="reported local address does not match the selected satellite address",
                )
                self._emit_event(
                    "error", "local_host_mismatch",
                    "reported local address does not match the selected satellite address",
                    direction=direction, reported=local_host,
                )
                process = self._processes.get(direction)
                if process is not None:
                    process.kill()
                return
            self._set_lane(direction, phase=IperfLanePhase.RUNNING, local_host=local_host, error="")
            self._update_overall_phase()
        elif event == "interval":
            summary = data.get("sum")
            if isinstance(summary, dict):
                self._record_interval(direction, summary)
        elif event == "end":
            summary = data.get("sum_received") or data.get("sum")
            if isinstance(summary, dict):
                quality = dict(summary)
                sent = data.get("sum_sent")
                if isinstance(sent, dict) and "retransmits" in sent:
                    quality["retransmits"] = sent["retransmits"]
                streams = data.get("streams")
                if isinstance(streams, list) and streams and isinstance(streams[0], dict):
                    udp = streams[0].get("udp")
                    if isinstance(udp, dict):
                        quality.update(udp)
                self._record_quality(direction, quality)
        elif event == "error":
            message = str(data.get("error", payload.get("error", "iperf3 error")))
            self._set_lane(direction, error=message)
            self._emit_event("error", "iperf_error", message, direction=direction)

    def _record_interval(self, direction: str, summary: dict[str, object]) -> None:
        snapshot = self.store.snapshot
        lane = snapshot.ul if direction == "ul" else snapshot.dl
        measurement = IperfMeasurement(
            timestamp_ns=time.time_ns(),
            elapsed_seconds=max(0.0, time.monotonic() - self._monotonic_started),
            direction=direction,
            protocol=self._config.protocol.value if self._config else "",
            bits_per_second=float(summary.get("bits_per_second", 0.0) or 0.0),
            bytes_transferred=int(summary.get("bytes", 0) or 0),
            jitter_ms=self._optional_float(summary, "jitter_ms"),
            lost_packets=self._optional_int(summary, "lost_packets"),
            packets=self._optional_int(summary, "packets"),
            lost_percent=self._optional_float(summary, "lost_percent"),
            out_of_order=self._optional_int(summary, "out_of_order"),
            retransmits=self._optional_int(summary, "retransmits"),
        )
        lane = replace(
            lane,
            phase=IperfLanePhase.RUNNING,
            bits_per_second=measurement.bits_per_second,
            total_bytes=lane.total_bytes + measurement.bytes_transferred,
            jitter_ms=measurement.jitter_ms,
            lost_packets=measurement.lost_packets,
            packets=measurement.packets,
            lost_percent=measurement.lost_percent,
            out_of_order=measurement.out_of_order,
            retransmits=measurement.retransmits,
            error="",
        )
        self._replace_lane(direction, lane)
        self.store.add_measurement(measurement)
        if self._writer is not None:
            self._writer.measurement(measurement)

    def _record_quality(self, direction: str, summary: dict[str, object]) -> None:
        snapshot = self.store.snapshot
        lane = snapshot.ul if direction == "ul" else snapshot.dl
        self._replace_lane(direction, replace(
            lane,
            jitter_ms=self._optional_float(summary, "jitter_ms"),
            lost_packets=self._optional_int(summary, "lost_packets"),
            packets=self._optional_int(summary, "packets"),
            lost_percent=self._optional_float(summary, "lost_percent"),
            out_of_order=self._optional_int(summary, "out_of_order"),
            retransmits=self._optional_int(summary, "retransmits"),
        ))

    def _on_finished(self, direction: str, exit_code: int, _status) -> None:
        if self._finalized:
            return
        if self._stopping:
            if all(p.state() == QProcess.ProcessState.NotRunning for p in self._processes.values()):
                self._finish(IperfTestPhase.STOPPED, self.store.snapshot.message)
            return
        lane = self.store.snapshot.ul if direction == "ul" else self.store.snapshot.dl
        if lane.phase is IperfLanePhase.RETRYING and self._retry_timers[direction].isActive():
            return
        if lane.phase is IperfLanePhase.BLOCKED:
            self._update_overall_phase()
            self._finish_if_complete()
            return
        if exit_code == 0:
            self._set_lane(direction, sessions=lane.sessions + 1, error="")
            remaining = self._remaining_seconds()
            if remaining is not None and remaining <= 0:
                self._set_lane(direction, phase=IperfLanePhase.COMPLETED)
                self._finish_if_complete()
            else:
                self._start_lane(direction)
            return
        self._schedule_retry(direction, f"iperf3 exited with code {exit_code}")

    def _on_process_error(self, direction: str, error) -> None:
        process = self._processes.get(direction)
        if process is None or self._stopping or self._finalized:
            return
        if error == QProcess.ProcessError.FailedToStart:
            self._schedule_retry(direction, process.errorString())
            return
        self._set_lane(direction, error=process.errorString())

    def _schedule_retry(self, direction: str, message: str) -> None:
        if self._finalized or self._stopping or self._retry_timers[direction].isActive():
            return
        lane = self.store.snapshot.ul if direction == "ul" else self.store.snapshot.dl
        self._set_lane(
            direction,
            phase=IperfLanePhase.RETRYING,
            retries=lane.retries + 1,
            bits_per_second=0.0,
            error=message,
        )
        self._emit_event("warning", "retry_scheduled", message, direction=direction)
        self._update_overall_phase()
        self._retry_timers[direction].start(RETRY_SECONDS * 1000)

    def _remaining_seconds(self) -> Optional[float]:
        if self._config is None or self._config.continuous:
            return None
        return self._config.duration_seconds - (time.monotonic() - self._monotonic_started)

    def _set_lane(self, direction: str, **changes: object) -> None:
        snapshot = self.store.snapshot
        lane = snapshot.ul if direction == "ul" else snapshot.dl
        self._replace_lane(direction, replace(lane, **changes))

    def _replace_lane(self, direction: str, lane: IperfLaneSnapshot) -> None:
        snapshot = self.store.snapshot
        self.store.set_snapshot(
            replace(snapshot, ul=lane) if direction == "ul" else replace(snapshot, dl=lane)
        )

    def _update_overall_phase(self) -> None:
        snapshot = self.store.snapshot
        enabled = [lane for lane in (snapshot.ul, snapshot.dl) if lane.phase is not IperfLanePhase.DISABLED]
        if any(lane.phase in {IperfLanePhase.RETRYING, IperfLanePhase.BLOCKED} for lane in enabled):
            phase = IperfTestPhase.DEGRADED
        elif any(lane.phase is IperfLanePhase.RUNNING for lane in enabled):
            phase = IperfTestPhase.RUNNING
        else:
            phase = IperfTestPhase.STARTING
        self.store.set_snapshot(replace(snapshot, phase=phase, message=phase.value))

    def _finish_if_complete(self) -> None:
        snapshot = self.store.snapshot
        enabled = [lane for lane in (snapshot.ul, snapshot.dl) if lane.phase is not IperfLanePhase.DISABLED]
        if enabled and all(lane.phase in {IperfLanePhase.COMPLETED, IperfLanePhase.BLOCKED} for lane in enabled):
            phase = (
                IperfTestPhase.FAILED
                if any(lane.phase is IperfLanePhase.BLOCKED for lane in enabled)
                else IperfTestPhase.COMPLETED
            )
            self._finish(phase, phase.value)

    def _force_stop(self) -> None:
        if not self._stopping:
            return
        for process in self._processes.values():
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
        QTimer.singleShot(100, lambda: self._finish(IperfTestPhase.STOPPED, self.store.snapshot.message))

    def _finish(self, phase: IperfTestPhase, message: str) -> None:
        if self._finalized:
            return
        self._finalized = True
        self._stopping = False
        for timer in self._retry_timers.values():
            timer.stop()
        current = self.store.snapshot
        ul = current.ul
        dl = current.dl
        if phase is IperfTestPhase.STOPPED:
            if ul.phase not in {IperfLanePhase.DISABLED, IperfLanePhase.COMPLETED, IperfLanePhase.BLOCKED}:
                ul = replace(ul, phase=IperfLanePhase.STOPPED, bits_per_second=0.0)
            if dl.phase not in {IperfLanePhase.DISABLED, IperfLanePhase.COMPLETED, IperfLanePhase.BLOCKED}:
                dl = replace(dl, phase=IperfLanePhase.STOPPED, bits_per_second=0.0)
        snapshot = replace(
            current,
            phase=phase,
            finished_ns=time.time_ns(),
            ul=ul,
            dl=dl,
            message=message,
        )
        self.store.set_snapshot(snapshot)
        if self._writer is not None:
            self._writer.event("info", "test_finished", message, outcome=phase.value)
            self._writer.finalize(phase.value, message, snapshot)
        self.active_changed.emit(False)

    def _emit_event(self, level: str, code: str, message: str, **extra: object) -> None:
        if self._writer is not None:
            event = self._writer.event(level, code, message, **extra)
        else:
            event = {"timestamp_ns": time.time_ns(), "level": level, "code": code, "message": message, **extra}
        self.store.add_event(event)

    @Slot(object)
    def _on_power_sample(self, sample: ExternalPowerSample) -> None:
        if not self.active:
            return
        self.store.add_power_sample(sample)
        if self._writer is not None:
            self._writer.power(sample)

    @Slot()
    def _on_power_updated(self) -> None:
        if not self.active:
            return
        phase = self._power_store.snapshot.phase.value
        if phase == self._power_phase:
            return
        self._power_phase = phase
        if phase != ExternalPowerPhase.ONLINE.value:
            self._emit_event(
                "warning", "power_unavailable",
                self._power_store.snapshot.error or f"external power phase: {phase}",
                power_phase=phase,
            )

    @staticmethod
    def _optional_float(payload: dict[str, object], key: str) -> Optional[float]:
        value = payload.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def _optional_int(payload: dict[str, object], key: str) -> Optional[int]:
        value = payload.get(key)
        return int(value) if isinstance(value, (int, float)) else None


__all__ = [
    "HISTORY_SECONDS", "MIN_IPERF_VERSION", "RETRY_SECONDS", "SEGMENT_SECONDS",
    "IperfDirection", "IperfLanePhase", "IperfLaneSnapshot", "IperfMeasurement",
    "IperfProtocol", "IperfSessionWriter", "IperfTestConfig", "IperfTestController",
    "IperfTestPhase", "IperfTestSnapshot", "IperfTestStore", "IperfValidationError",
]
