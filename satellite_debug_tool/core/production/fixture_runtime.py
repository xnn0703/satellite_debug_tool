"""Process-level ownership root for motion-platform and MS-6222 resources."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QObject, Signal

from .fixture_analysis import FixtureCalibrationStore
from .fixture_profile import FixtureControlLease, FixtureProfileStore
from .fixture_telemetry import FixtureTelemetryBus
from .ms6222_debug import Ms6222ControlLease


class FixtureRuntimeState(str, Enum):
    NEEDS_BINDING = "needs_binding"
    CONFIGURED = "configured"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    READY = "ready"
    RUNNING = "running"
    FAULT = "fault"


@dataclass(frozen=True)
class FixtureRuntimeFact:
    fixture: str
    state: FixtureRuntimeState
    evidence: str = ""


class FixtureRuntime(QObject):
    """Own the single set of fixture leases and persistent stores for the process."""

    state_changed = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        profile_store: FixtureProfileStore | None = None,
        calibration_store: FixtureCalibrationStore | None = None,
    ) -> None:
        super().__init__(parent)
        self.fixture_lease = FixtureControlLease()
        self.reference_lease = Ms6222ControlLease()
        self.profile_store = profile_store or FixtureProfileStore()
        self.calibration_store = calibration_store or FixtureCalibrationStore()
        self.telemetry = FixtureTelemetryBus(self)
        self._facts = {
            "motion": FixtureRuntimeFact("motion", FixtureRuntimeState.NEEDS_BINDING),
            "reference": FixtureRuntimeFact("reference", FixtureRuntimeState.NEEDS_BINDING),
        }
        self._motion_profile_id = ""
        self._reference_profile_id = ""

    def bind_configuration(self, configuration: object) -> None:
        motion = getattr(configuration, "motion_profile", None)
        reference = getattr(configuration, "reference_profile", None)
        if motion is None or reference is None:
            self.clear_bindings()
            return
        self._motion_profile_id = str(motion.profile_id)
        self._reference_profile_id = str(reference.profile_id)
        self.set_state(
            "motion",
            FixtureRuntimeState.CONFIGURED,
            f"{motion.controller_ipv4}:{motion.command_port} | "
            f"feedback {motion.local_ipv4}:{motion.feedback_port}",
        )
        self.set_state(
            "reference",
            FixtureRuntimeState.CONFIGURED,
            f"{reference.serial_port} @ {reference.baud_rate}",
        )

    def clear_bindings(self) -> None:
        self._motion_profile_id = ""
        self._reference_profile_id = ""
        self.set_state("motion", FixtureRuntimeState.NEEDS_BINDING, "")
        self.set_state("reference", FixtureRuntimeState.NEEDS_BINDING, "")

    def set_state(
        self,
        fixture: str,
        state: FixtureRuntimeState,
        evidence: str = "",
    ) -> None:
        key = str(fixture)
        if key not in self._facts:
            raise ValueError(f"unsupported fixture runtime fact: {key}")
        fact = FixtureRuntimeFact(key, FixtureRuntimeState(state), str(evidence))
        if self._facts[key] == fact:
            return
        self._facts[key] = fact
        self.state_changed.emit(fact)

    def fact(self, fixture: str) -> FixtureRuntimeFact:
        try:
            return self._facts[str(fixture)]
        except KeyError as exc:
            raise ValueError(f"unsupported fixture runtime fact: {fixture}") from exc

    @property
    def motion_owner(self) -> str:
        return self.fixture_lease.owner

    @property
    def reference_owner(self) -> str:
        return self.reference_lease.owner

    def ownership_snapshot(self) -> dict[str, str]:
        return {
            "motion_owner": self.motion_owner,
            "reference_owner": self.reference_owner,
        }


__all__ = [
    "FixtureRuntime",
    "FixtureRuntimeFact",
    "FixtureRuntimeState",
]
