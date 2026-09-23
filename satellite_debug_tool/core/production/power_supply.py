"""GW Instek PSW 80-27 TCP/SCPI adapter with closed-loop safety checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import socket
import threading
import time
from typing import Callable, Optional, Protocol


class PowerSupplyError(RuntimeError):
    pass


class PowerSupplyState(str, Enum):
    DISCONNECTED = "disconnected"
    IDENTIFIED = "identified"
    READY_OFF = "ready_off"
    TURNING_ON = "turning_on"
    ON_CONFIRMED = "on_confirmed"
    TURNING_OFF = "turning_off"
    OFF_CONFIRMED = "off_confirmed"
    PROTECTION_TRIPPED = "protection_tripped"
    UNKNOWN = "unknown"


class PowerEvidenceLevel(str, Enum):
    NONE = "none"
    COMMAND_SENT = "command_sent"
    OUTPUT_STATE_CONFIRMED = "output_state_confirmed"
    VOLTAGE_CONFIRMED = "voltage_confirmed"
    DUT_BOOT_OBSERVED = "dut_boot_observed"


@dataclass(frozen=True)
class PowerValidationPolicy:
    voltage_setpoint_tolerance_v: float
    current_setpoint_tolerance_a: float
    output_voltage_min_v: float
    output_voltage_max_v: float
    off_voltage_max_v: float


def _validate_psw80_27_setpoints(voltage: float, current: float) -> None:
    if not math.isfinite(voltage) or not (0.0 < voltage <= 80.0):
        raise PowerSupplyError("PSW80-27 voltage setpoint must be within 0..80 V")
    if not math.isfinite(current) or not (0.0 < current <= 27.0):
        raise PowerSupplyError("PSW80-27 current setpoint must be within 0..27 A")
    if voltage * current > 720.0:
        raise PowerSupplyError("PSW80-27 requested power exceeds 720 W")


def psw80_27_validation_policy(
    voltage_set_v: float,
    current_set_a: float,
) -> PowerValidationPolicy:
    """Derive PSW80-27 limits from its published accuracy and resolution."""
    voltage = float(voltage_set_v)
    current = float(current_set_a)
    _validate_psw80_27_setpoints(voltage, current)

    # PSW80-27 voltage programming and measurement accuracy are each
    # 0.1 % + 10 mV. Include two 2 mV remote-resolution steps so the
    # comparison remains stable at quantization boundaries.
    output_tolerance_v = 0.002 * voltage + 0.024
    return PowerValidationPolicy(
        voltage_setpoint_tolerance_v=0.002,
        current_setpoint_tolerance_a=0.002,
        output_voltage_min_v=max(0.0, voltage - output_tolerance_v),
        output_voltage_max_v=min(80.0, voltage + output_tolerance_v),
        off_voltage_max_v=0.050,
    )


@dataclass(frozen=True)
class PowerSupplyConfig:
    host: str
    voltage_set_v: float
    current_set_a: float
    port: int = 2268
    expected_manufacturer: str = "GW-INSTEK"
    expected_model: str = "PSW 80-27"
    expected_serial: str = ""
    voltage_setpoint_tolerance_v: float = 0.002
    current_setpoint_tolerance_a: float = 0.002
    output_voltage_min_v: float = 13.5
    output_voltage_max_v: float = 14.5
    off_voltage_max_v: float = 0.5
    command_timeout_s: float = 2.0
    connect_timeout_s: float = 3.0
    output_settle_timeout_s: float = 3.0
    max_line_bytes: int = 1024

    def validate(self) -> None:
        try:
            socket.inet_aton(self.host)
        except OSError as exc:
            raise PowerSupplyError("power-supply host must be an IPv4 address") from exc
        if not (1 <= int(self.port) <= 65535):
            raise PowerSupplyError("power-supply port is out of range")
        if not self.expected_manufacturer.strip() or not self.expected_model.strip():
            raise PowerSupplyError("expected manufacturer and model are required")
        for name, value in (
            ("voltage_set_v", self.voltage_set_v),
            ("current_set_a", self.current_set_a),
            ("voltage_setpoint_tolerance_v", self.voltage_setpoint_tolerance_v),
            ("current_setpoint_tolerance_a", self.current_setpoint_tolerance_a),
            ("output_voltage_min_v", self.output_voltage_min_v),
            ("output_voltage_max_v", self.output_voltage_max_v),
            ("off_voltage_max_v", self.off_voltage_max_v),
            ("command_timeout_s", self.command_timeout_s),
            ("connect_timeout_s", self.connect_timeout_s),
            ("output_settle_timeout_s", self.output_settle_timeout_s),
        ):
            if not math.isfinite(float(value)) or float(value) < 0:
                raise PowerSupplyError(f"{name} must be finite and non-negative")
        _validate_psw80_27_setpoints(
            float(self.voltage_set_v),
            float(self.current_set_a),
        )
        if self.output_settle_timeout_s <= 0:
            raise PowerSupplyError("output_settle_timeout_s must be positive")
        if self.output_voltage_min_v > self.output_voltage_max_v:
            raise PowerSupplyError("output voltage window is invalid")
        if not (64 <= int(self.max_line_bytes) <= 65536):
            raise PowerSupplyError("max_line_bytes must be 64..65536")


@dataclass(frozen=True)
class PowerIdentity:
    manufacturer: str
    model: str
    serial_number: str
    firmware: str
    raw: str


@dataclass(frozen=True)
class PowerMeasurement:
    voltage_v: float
    current_a: float


@dataclass(frozen=True)
class PowerCommandRecord:
    command: str
    response: str
    is_query: bool
    wall_time_ns: int
    monotonic_ns: int
    connection_generation: int
    fixture_action_id: str


@dataclass(frozen=True)
class PowerActionResult:
    state: PowerSupplyState
    evidence_level: PowerEvidenceLevel
    output_enabled: bool
    measurement: PowerMeasurement
    operation_condition: int
    questionable_condition: int
    protection_tripped: bool


class ScpiLineCodec:
    """Split an arbitrary TCP byte stream into strict LF-terminated lines."""

    def __init__(self, max_line_bytes: int = 1024) -> None:
        if int(max_line_bytes) < 1:
            raise ValueError("max_line_bytes must be positive")
        self._max_line_bytes = int(max_line_bytes)
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> tuple[str, ...]:
        if data:
            self._buffer.extend(data)
        lines: list[str] = []
        while True:
            marker = self._buffer.find(b"\n")
            if marker < 0:
                break
            raw = bytes(self._buffer[:marker])
            del self._buffer[: marker + 1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            try:
                lines.append(raw.decode("ascii", errors="strict"))
            except UnicodeDecodeError as exc:
                self.reset()
                raise PowerSupplyError("SCPI response is not ASCII") from exc
        if len(self._buffer) > self._max_line_bytes:
            self.reset()
            raise PowerSupplyError("SCPI response exceeds maximum line length")
        return tuple(lines)


class ScpiTransport(Protocol):
    def connect(self) -> None: ...

    def close(self) -> None: ...

    def write(self, data: bytes) -> None: ...

    def read_line(self) -> str: ...


class SocketScpiTransport:
    def __init__(self, config: PowerSupplyConfig) -> None:
        self._config = config
        self._socket: Optional[socket.socket] = None
        self._codec = ScpiLineCodec(config.max_line_bytes)

    def connect(self) -> None:
        self.close()
        sock = socket.create_connection(
            (self._config.host, self._config.port),
            timeout=self._config.connect_timeout_s,
        )
        sock.settimeout(self._config.command_timeout_s)
        self._socket = sock
        self._codec.reset()

    def close(self) -> None:
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._codec.reset()

    def write(self, data: bytes) -> None:
        sock = self._socket
        if sock is None:
            raise PowerSupplyError("power supply is not connected")
        sock.sendall(data)

    def read_line(self) -> str:
        sock = self._socket
        if sock is None:
            raise PowerSupplyError("power supply is not connected")
        while True:
            chunk = sock.recv(1024)
            if not chunk:
                raise PowerSupplyError("power supply closed the TCP connection")
            lines = self._codec.feed(chunk)
            if lines:
                if len(lines) != 1 or self._codec.buffered_bytes:
                    self._codec.reset()
                    raise PowerSupplyError("unexpected extra SCPI response data")
                return lines[0]


class GwInstekPswAdapter:
    """Serialize PSW access and never replay OUTPUT ON after reconnect."""

    def __init__(
        self,
        config: PowerSupplyConfig,
        *,
        transport_factory: Optional[Callable[[PowerSupplyConfig], ScpiTransport]] = None,
        wall_clock_ns: Callable[[], int] = time.time_ns,
        monotonic_clock_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        config.validate()
        self._config = config
        self._transport_factory = transport_factory or SocketScpiTransport
        self._wall_clock_ns = wall_clock_ns
        self._monotonic_clock_ns = monotonic_clock_ns
        self._sleep = sleep
        self._transport: Optional[ScpiTransport] = None
        self._lock = threading.RLock()
        self._generation = 0
        self._records: list[PowerCommandRecord] = []
        self._identity: Optional[PowerIdentity] = None
        self._state = PowerSupplyState.DISCONNECTED
        self._evidence_level = PowerEvidenceLevel.NONE

    @property
    def state(self) -> PowerSupplyState:
        return self._state

    @property
    def evidence_level(self) -> PowerEvidenceLevel:
        return self._evidence_level

    @property
    def identity(self) -> Optional[PowerIdentity]:
        return self._identity

    @property
    def records(self) -> tuple[PowerCommandRecord, ...]:
        return tuple(self._records)

    @property
    def connection_generation(self) -> int:
        return self._generation

    def connect(self) -> PowerIdentity:
        with self._lock:
            self.close()
            transport = self._transport_factory(self._config)
            try:
                transport.connect()
            except (OSError, PowerSupplyError) as exc:
                self._state = PowerSupplyState.DISCONNECTED
                raise PowerSupplyError(f"cannot connect to power supply: {exc}") from exc
            self._transport = transport
            self._generation += 1
            self._state = PowerSupplyState.UNKNOWN
            self._evidence_level = PowerEvidenceLevel.NONE
            try:
                identity = parse_identity(self.query("*IDN?"))
                self._validate_identity(identity)
            except Exception:
                self.close()
                raise
            self._identity = identity
            self._state = PowerSupplyState.IDENTIFIED
            return identity

    def close(self) -> None:
        with self._lock:
            transport = self._transport
            self._transport = None
            if transport is not None:
                transport.close()
            self._identity = None
            self._state = PowerSupplyState.DISCONNECTED
            self._evidence_level = PowerEvidenceLevel.NONE

    def _command(self, command: str, *, fixture_action_id: str = "") -> None:
        with self._lock:
            normalized = _normalize_command(command)
            transport = self._require_transport()
            try:
                transport.write((normalized + "\n").encode("ascii"))
            except (OSError, PowerSupplyError) as exc:
                self._mark_unknown()
                raise PowerSupplyError(f"SCPI command failed: {exc}") from exc
            self._record(normalized, "", False, fixture_action_id)

    def query(self, command: str, *, fixture_action_id: str = "") -> str:
        with self._lock:
            normalized = _normalize_command(command)
            if "?" not in normalized:
                raise PowerSupplyError("SCPI query command must contain '?'")
            transport = self._require_transport()
            try:
                transport.write((normalized + "\n").encode("ascii"))
                response = transport.read_line().strip()
            except (OSError, PowerSupplyError) as exc:
                self._mark_unknown()
                raise PowerSupplyError(f"SCPI query failed: {exc}") from exc
            if not response:
                self._mark_unknown()
                raise PowerSupplyError("SCPI query returned an empty response")
            self._record(normalized, response, True, fixture_action_id)
            return response

    def inspect(self, *, fixture_action_id: str = "") -> PowerActionResult:
        with self._lock:
            self._require_identity()
            output = parse_bool(self.query("OUTP?", fixture_action_id=fixture_action_id))
            measurement = parse_measurement(
                self.query("MEAS:ALL?", fixture_action_id=fixture_action_id)
            )
            operation = parse_int(
                self.query("STAT:OPER:COND?", fixture_action_id=fixture_action_id)
            )
            questionable = parse_int(
                self.query("STAT:QUES:COND?", fixture_action_id=fixture_action_id)
            )
            tripped = parse_bool(
                self.query("OUTP:PROT:TRIP?", fixture_action_id=fixture_action_id)
            )
            if tripped or questionable:
                self._state = PowerSupplyState.PROTECTION_TRIPPED
                self._evidence_level = PowerEvidenceLevel.NONE
            elif output and self._voltage_in_output_window(measurement.voltage_v):
                self._state = PowerSupplyState.ON_CONFIRMED
                self._evidence_level = PowerEvidenceLevel.VOLTAGE_CONFIRMED
            elif not output and measurement.voltage_v <= self._config.off_voltage_max_v:
                self._state = PowerSupplyState.OFF_CONFIRMED
                self._evidence_level = PowerEvidenceLevel.OUTPUT_STATE_CONFIRMED
            else:
                self._state = PowerSupplyState.UNKNOWN
                self._evidence_level = PowerEvidenceLevel.NONE
            return self._result(output, measurement, operation, questionable, tripped)

    def prepare_output_off(self, *, fixture_action_id: str = "") -> PowerActionResult:
        """Force OFF, verify discharge, configure setpoints, and drain errors."""
        with self._lock:
            self._require_identity()
            self._state = PowerSupplyState.TURNING_OFF
            self._command("OUTP 0", fixture_action_id=fixture_action_id)
            self._evidence_level = PowerEvidenceLevel.COMMAND_SENT
            measurement = self._wait_for_output_off(fixture_action_id)
            self._evidence_level = PowerEvidenceLevel.OUTPUT_STATE_CONFIRMED
            self._command(
                f"SOUR:VOLT {_format_number(self._config.voltage_set_v)}",
                fixture_action_id=fixture_action_id,
            )
            self._command(
                f"SOUR:CURR {_format_number(self._config.current_set_a)}",
                fixture_action_id=fixture_action_id,
            )
            if parse_int(self.query("*OPC?", fixture_action_id=fixture_action_id)) != 1:
                self._state = PowerSupplyState.UNKNOWN
                raise PowerSupplyError("power supply did not complete setpoint configuration")
            voltage_set = parse_float(
                self.query("SOUR:VOLT?", fixture_action_id=fixture_action_id)
            )
            current_set = parse_float(
                self.query("SOUR:CURR?", fixture_action_id=fixture_action_id)
            )
            self._verify_setpoint(
                "voltage",
                voltage_set,
                self._config.voltage_set_v,
                self._config.voltage_setpoint_tolerance_v,
            )
            self._verify_setpoint(
                "current",
                current_set,
                self._config.current_set_a,
                self._config.current_setpoint_tolerance_a,
            )
            self._verify_no_scpi_error(fixture_action_id)
            operation = parse_int(
                self.query("STAT:OPER:COND?", fixture_action_id=fixture_action_id)
            )
            questionable = parse_int(
                self.query("STAT:QUES:COND?", fixture_action_id=fixture_action_id)
            )
            tripped = parse_bool(
                self.query("OUTP:PROT:TRIP?", fixture_action_id=fixture_action_id)
            )
            if tripped or questionable:
                self._state = PowerSupplyState.PROTECTION_TRIPPED
                raise PowerSupplyError("power supply reports a protection or questionable state")
            self._state = PowerSupplyState.READY_OFF
            return self._result(False, measurement, operation, questionable, tripped)

    def enable_output(self, *, fixture_action_id: str) -> PowerActionResult:
        with self._lock:
            if self._state != PowerSupplyState.READY_OFF:
                raise PowerSupplyError("power supply must be READY_OFF before enabling output")
            if not fixture_action_id.strip():
                raise PowerSupplyError("fixture_action_id is required for OUTPUT ON")
            self._state = PowerSupplyState.TURNING_ON
            self._command("OUTP 1", fixture_action_id=fixture_action_id)
            self._evidence_level = PowerEvidenceLevel.COMMAND_SENT
            deadline_ns = self._monotonic_clock_ns() + int(
                self._config.output_settle_timeout_s * 1_000_000_000
            )
            while True:
                output = parse_bool(
                    self.query("OUTP?", fixture_action_id=fixture_action_id)
                )
                measurement = parse_measurement(
                    self.query("MEAS:ALL?", fixture_action_id=fixture_action_id)
                )
                operation = parse_int(
                    self.query("STAT:OPER:COND?", fixture_action_id=fixture_action_id)
                )
                questionable = parse_int(
                    self.query("STAT:QUES:COND?", fixture_action_id=fixture_action_id)
                )
                tripped = parse_bool(
                    self.query("OUTP:PROT:TRIP?", fixture_action_id=fixture_action_id)
                )
                if not output:
                    self._state = PowerSupplyState.UNKNOWN
                    raise PowerSupplyError("power supply did not confirm OUTPUT ON")
                self._evidence_level = PowerEvidenceLevel.OUTPUT_STATE_CONFIRMED
                if tripped or questionable:
                    self._state = PowerSupplyState.PROTECTION_TRIPPED
                    raise PowerSupplyError(
                        "power supply reports a protection or questionable state"
                    )
                if self._voltage_in_output_window(measurement.voltage_v):
                    self._evidence_level = PowerEvidenceLevel.VOLTAGE_CONFIRMED
                    self._state = PowerSupplyState.ON_CONFIRMED
                    return self._result(
                        True,
                        measurement,
                        operation,
                        questionable,
                        tripped,
                    )
                remaining_ns = deadline_ns - self._monotonic_clock_ns()
                if remaining_ns <= 0:
                    self._state = PowerSupplyState.UNKNOWN
                    raise PowerSupplyError(
                        "power output did not enter the configured ON window within "
                        f"{self._config.output_settle_timeout_s:.3f} s: measured "
                        f"{measurement.voltage_v:.4f} V, expected "
                        f"{self._config.output_voltage_min_v:.4f}.."
                        f"{self._config.output_voltage_max_v:.4f} V"
                    )
                self._sleep(min(0.1, remaining_ns / 1_000_000_000.0))

    def disable_output(self, *, fixture_action_id: str = "") -> PowerActionResult:
        with self._lock:
            self._require_identity()
            self._state = PowerSupplyState.TURNING_OFF
            self._command("OUTP 0", fixture_action_id=fixture_action_id)
            self._evidence_level = PowerEvidenceLevel.COMMAND_SENT
            measurement = self._wait_for_output_off(fixture_action_id)
            operation = parse_int(
                self.query("STAT:OPER:COND?", fixture_action_id=fixture_action_id)
            )
            questionable = parse_int(
                self.query("STAT:QUES:COND?", fixture_action_id=fixture_action_id)
            )
            tripped = parse_bool(
                self.query("OUTP:PROT:TRIP?", fixture_action_id=fixture_action_id)
            )
            self._evidence_level = PowerEvidenceLevel.OUTPUT_STATE_CONFIRMED
            self._state = (
                PowerSupplyState.PROTECTION_TRIPPED
                if tripped or questionable
                else PowerSupplyState.OFF_CONFIRMED
            )
            return self._result(False, measurement, operation, questionable, tripped)

    def _wait_for_output_off(self, fixture_action_id: str) -> PowerMeasurement:
        deadline_ns = self._monotonic_clock_ns() + int(
            self._config.output_settle_timeout_s * 1_000_000_000
        )
        while True:
            output = parse_bool(
                self.query("OUTP?", fixture_action_id=fixture_action_id)
            )
            measurement = parse_measurement(
                self.query("MEAS:ALL?", fixture_action_id=fixture_action_id)
            )
            if output:
                self._state = PowerSupplyState.UNKNOWN
                raise PowerSupplyError("power supply did not confirm OUTPUT OFF")
            if measurement.voltage_v <= self._config.off_voltage_max_v:
                return measurement
            remaining_ns = deadline_ns - self._monotonic_clock_ns()
            if remaining_ns <= 0:
                self._state = PowerSupplyState.UNKNOWN
                raise PowerSupplyError(
                    "power output did not fall below the configured OFF threshold within "
                    f"{self._config.output_settle_timeout_s:.3f} s: measured "
                    f"{measurement.voltage_v:.4f} V, expected ≤"
                    f"{self._config.off_voltage_max_v:.4f} V"
                )
            self._sleep(min(0.1, remaining_ns / 1_000_000_000.0))

    def release_local(self) -> None:
        """Return panel control without changing or inferring output state."""
        self._command("SYST:COMM:RLST LOC")

    def _require_transport(self) -> ScpiTransport:
        if self._transport is None:
            raise PowerSupplyError("power supply is not connected")
        return self._transport

    def _require_identity(self) -> PowerIdentity:
        if self._identity is None:
            raise PowerSupplyError("power-supply identity has not been verified")
        return self._identity

    def _validate_identity(self, identity: PowerIdentity) -> None:
        expected_maker = _normalize_identity_field(self._config.expected_manufacturer)
        actual_maker = _normalize_identity_field(identity.manufacturer)
        expected_model = _normalize_identity_field(self._config.expected_model)
        actual_model = _normalize_identity_field(identity.model)
        if expected_maker not in actual_maker:
            raise PowerSupplyError(f"unexpected power-supply manufacturer: {identity.manufacturer}")
        if expected_model not in actual_model:
            raise PowerSupplyError(f"unexpected power-supply model: {identity.model}")
        expected_serial = self._config.expected_serial.strip()
        if expected_serial and identity.serial_number.strip() != expected_serial:
            raise PowerSupplyError(f"unexpected power-supply serial: {identity.serial_number}")

    def _verify_setpoint(
        self,
        name: str,
        actual: float,
        expected: float,
        tolerance: float,
    ) -> None:
        if abs(actual - expected) > tolerance:
            self._state = PowerSupplyState.UNKNOWN
            raise PowerSupplyError(f"power-supply {name} setpoint readback does not match")

    def _verify_no_scpi_error(self, fixture_action_id: str) -> None:
        response = self.query("SYST:ERR?", fixture_action_id=fixture_action_id)
        code_text = response.partition(",")[0].strip()
        try:
            code = int(code_text)
        except ValueError as exc:
            self._state = PowerSupplyState.UNKNOWN
            raise PowerSupplyError(f"invalid SCPI error response: {response}") from exc
        if code != 0:
            self._state = PowerSupplyState.UNKNOWN
            raise PowerSupplyError(f"power supply SCPI error: {response}")

    def _voltage_in_output_window(self, voltage_v: float) -> bool:
        return self._config.output_voltage_min_v <= voltage_v <= self._config.output_voltage_max_v

    def _result(
        self,
        output: bool,
        measurement: PowerMeasurement,
        operation: int,
        questionable: int,
        tripped: bool,
    ) -> PowerActionResult:
        return PowerActionResult(
            state=self._state,
            evidence_level=self._evidence_level,
            output_enabled=output,
            measurement=measurement,
            operation_condition=operation,
            questionable_condition=questionable,
            protection_tripped=tripped,
        )

    def _record(
        self,
        command: str,
        response: str,
        is_query: bool,
        fixture_action_id: str,
    ) -> None:
        self._records.append(
            PowerCommandRecord(
                command=command,
                response=response,
                is_query=is_query,
                wall_time_ns=self._wall_clock_ns(),
                monotonic_ns=self._monotonic_clock_ns(),
                connection_generation=self._generation,
                fixture_action_id=str(fixture_action_id),
            )
        )

    def _mark_unknown(self) -> None:
        self._state = PowerSupplyState.UNKNOWN
        self._evidence_level = PowerEvidenceLevel.NONE


def parse_identity(response: str) -> PowerIdentity:
    fields = [field.strip() for field in str(response).split(",")]
    if len(fields) < 4 or any(not field for field in fields[:2]):
        raise PowerSupplyError(f"invalid *IDN? response: {response}")
    return PowerIdentity(fields[0], fields[1], fields[2], ",".join(fields[3:]), str(response))


def parse_float(response: str) -> float:
    try:
        value = float(str(response).strip())
    except ValueError as exc:
        raise PowerSupplyError(f"invalid numeric SCPI response: {response}") from exc
    if not math.isfinite(value):
        raise PowerSupplyError(f"non-finite SCPI response: {response}")
    return value


def parse_int(response: str) -> int:
    value = parse_float(response)
    integer = int(value)
    if value != integer:
        raise PowerSupplyError(f"SCPI response is not an integer: {response}")
    return integer


def parse_bool(response: str) -> bool:
    normalized = str(response).strip().upper()
    if normalized in {"1", "ON"}:
        return True
    if normalized in {"0", "OFF"}:
        return False
    raise PowerSupplyError(f"invalid boolean SCPI response: {response}")


def parse_measurement(response: str) -> PowerMeasurement:
    fields = [field.strip() for field in str(response).replace(";", ",").split(",")]
    if len(fields) != 2:
        raise PowerSupplyError(f"invalid MEAS:ALL? response: {response}")
    return PowerMeasurement(parse_float(fields[0]), parse_float(fields[1]))


def _normalize_command(command: str) -> str:
    normalized = str(command).strip()
    if not normalized:
        raise PowerSupplyError("SCPI command cannot be empty")
    if "\r" in normalized or "\n" in normalized:
        raise PowerSupplyError("SCPI command cannot contain a line terminator")
    try:
        normalized.encode("ascii")
    except UnicodeEncodeError as exc:
        raise PowerSupplyError("SCPI command must be ASCII") from exc
    return normalized


def _format_number(value: float) -> str:
    return f"{float(value):.9f}".rstrip("0").rstrip(".")


def _normalize_identity_field(value: str) -> str:
    return "".join(character for character in str(value).upper() if character.isalnum())


__all__ = [
    "GwInstekPswAdapter",
    "PowerActionResult",
    "PowerCommandRecord",
    "PowerEvidenceLevel",
    "PowerIdentity",
    "PowerMeasurement",
    "PowerSupplyConfig",
    "PowerSupplyError",
    "PowerSupplyState",
    "PowerValidationPolicy",
    "ScpiLineCodec",
    "ScpiTransport",
    "SocketScpiTransport",
    "parse_bool",
    "parse_float",
    "parse_identity",
    "parse_int",
    "parse_measurement",
    "psw80_27_validation_policy",
]
