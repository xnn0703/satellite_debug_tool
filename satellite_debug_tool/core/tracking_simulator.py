"""Pure Tracking scenario interpolation, link model, and wire encoding."""

from __future__ import annotations

import json
import math
import random
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from satellite_debug_tool.core.protocol.codec_v2 import build_frame
from satellite_debug_tool.core.protocol.frame_v2 import CmdType

_KA256_LOSS_BANDS = (
    (17.70, (-0.00254605020511577, -0.0133214183611333, 0.00267734982645733, -5.80138509981172e-05, 5.5170530405621e-07)),
    (18.20, (0.00644428178367438, 0.0314159761642518, -0.000526537250885152, 2.09066530518207e-05, -4.24602158700433e-08)),
    (18.70, (-0.00468944164593505, -0.0278099729398239, 0.00269993359996836, -4.99572300795382e-05, 4.93337183880441e-07)),
    (19.20, (-0.00361954658227432, 0.0120836044504878, 0.000289417749214218, -9.7750842996721e-07, 1.7314019555398e-07)),
    (19.45, (0.00628158909623104, -0.00976377666166093, 0.00150595849737438, -2.54265015995595e-05, 3.2764280844923e-07)),
    (19.70, (0.00623371031359759, 0.00165670116422824, 0.00140570482168282, -2.88411132411371e-05, 3.61405667457336e-07)),
    (20.20, (0.00769545199114653, 0.0049360332175681, 0.000536870744677989, 5.96624844746407e-07, 1.55844693604099e-07)),
    (20.70, (-0.00677174884099492, 0.0165902919405121, -0.00171117658699807, 6.17406433492205e-05, -2.82960924552614e-07)),
    (21.20, (0.00667323768553916, 0.0187618215191605, -0.00236847234929502, 8.55556350834417e-05, -5.34453949642317e-07)),
)


@dataclass(frozen=True)
class PoseKeyframe:
    time_s: float
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    yaw_deg: float
    pitch_deg: float
    roll_deg: float


@dataclass(frozen=True)
class LinkKeyframe:
    time_s: float
    base_snr_db: float
    obstruction_loss_db: float = 0.0
    rain_loss_db: float = 0.0
    noise_std_db: float = 0.0
    rx_online: bool = True


@dataclass(frozen=True)
class TrackingScenario:
    name: str
    duration_s: float
    random_seed: int
    satellite_id: int
    satellite_longitude_deg: float
    rx_frequency_mhz: float
    tx_frequency_mhz: float
    rx_polarization: int
    tx_polarization: int
    mount_yaw_deg: float
    mount_pitch_deg: float
    mount_roll_deg: float
    hpbw_deg: float
    hard_limit_deg: float
    pose_keyframes: tuple[PoseKeyframe, ...]
    link_keyframes: tuple[LinkKeyframe, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TrackingScenario":
        if int(raw.get("schema", 0)) != 1:
            raise ValueError("tracking scenario schema must be 1")
        poses = tuple(PoseKeyframe(**item) for item in raw["pose_keyframes"])
        links = tuple(LinkKeyframe(**item) for item in raw["link_keyframes"])
        scenario = cls(
            name=str(raw["name"]),
            duration_s=float(raw["duration_s"]),
            random_seed=int(raw.get("random_seed", 1)),
            satellite_id=int(raw["satellite_id"]),
            satellite_longitude_deg=float(raw["satellite_longitude_deg"]),
            rx_frequency_mhz=float(raw["rx_frequency_mhz"]),
            tx_frequency_mhz=float(raw["tx_frequency_mhz"]),
            rx_polarization=int(raw["rx_polarization"]),
            tx_polarization=int(raw["tx_polarization"]),
            mount_yaw_deg=float(raw.get("mount_yaw_deg", 0.0)),
            mount_pitch_deg=float(raw.get("mount_pitch_deg", 0.0)),
            mount_roll_deg=float(raw.get("mount_roll_deg", 0.0)),
            hpbw_deg=float(raw["hpbw_deg"]),
            hard_limit_deg=float(raw["hard_limit_deg"]),
            pose_keyframes=poses,
            link_keyframes=links,
        )
        scenario.validate()
        return scenario

    @classmethod
    def load(cls, path: str | Path) -> "TrackingScenario":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict[str, Any]:
        def fields(value: object) -> dict[str, Any]:
            return dict(vars(value))

        return {
            "schema": 1,
            "name": self.name,
            "duration_s": self.duration_s,
            "random_seed": self.random_seed,
            "satellite_id": self.satellite_id,
            "satellite_longitude_deg": self.satellite_longitude_deg,
            "rx_frequency_mhz": self.rx_frequency_mhz,
            "tx_frequency_mhz": self.tx_frequency_mhz,
            "rx_polarization": self.rx_polarization,
            "tx_polarization": self.tx_polarization,
            "mount_yaw_deg": self.mount_yaw_deg,
            "mount_pitch_deg": self.mount_pitch_deg,
            "mount_roll_deg": self.mount_roll_deg,
            "hpbw_deg": self.hpbw_deg,
            "hard_limit_deg": self.hard_limit_deg,
            "pose_keyframes": [fields(item) for item in self.pose_keyframes],
            "link_keyframes": [fields(item) for item in self.link_keyframes],
        }

    def validate(self) -> None:
        if not self.name or not math.isfinite(self.duration_s) or self.duration_s <= 0.0:
            raise ValueError("scenario name and duration are required")
        if self.satellite_id < 0 or self.hpbw_deg <= 0.0 or self.hard_limit_deg <= 0.0:
            raise ValueError("satellite id, HPBW, and hard limit are invalid")
        _validate_keyframes(self.pose_keyframes, self.duration_s)
        _validate_keyframes(self.link_keyframes, self.duration_s)


@dataclass(frozen=True)
class SimulationSample:
    time_s: float
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    velocity_n_mps: float
    velocity_e_mps: float
    velocity_d_mps: float
    yaw_deg: float
    pitch_deg: float
    roll_deg: float
    body_rate_x_dps: float
    body_rate_y_dps: float
    body_rate_z_dps: float
    raw_snr_db: float
    normalized_snr_db: float
    offaxis_deg: float
    scan_loss_db: float
    snr_valid: bool
    normalized_snr_valid: bool
    rx_online: bool


def _validate_keyframes(items: Sequence[object], duration_s: float) -> None:
    if not items:
        raise ValueError("scenario requires keyframes")
    times = [float(getattr(item, "time_s")) for item in items]
    if times != sorted(times) or times[0] < 0.0 or times[-1] > duration_s:
        raise ValueError("keyframe times must be sorted inside scenario duration")


def _segment(items: Sequence[object], time_s: float) -> tuple[object, object, float, float]:
    if time_s <= float(getattr(items[0], "time_s")):
        return items[0], items[0], 0.0, 1.0
    for left, right in zip(items, items[1:]):
        start = float(getattr(left, "time_s"))
        end = float(getattr(right, "time_s"))
        if time_s <= end:
            duration = max(end - start, 1e-9)
            return left, right, (time_s - start) / duration, duration
    return items[-1], items[-1], 0.0, 1.0


def _linear(left: float, right: float, ratio: float) -> float:
    return left + (right - left) * ratio


def _angle_delta(left: float, right: float) -> float:
    return (right - left + 180.0) % 360.0 - 180.0


def calculate_snr(
    *,
    base_snr_db: float,
    scan_loss_db: float,
    pointing_error_deg: float,
    hpbw_deg: float,
    obstruction_loss_db: float,
    rain_loss_db: float,
    noise_db: float,
) -> tuple[float, float]:
    pointing_loss = 12.0 * (pointing_error_deg / hpbw_deg) ** 2
    raw = max(
        0.0,
        base_snr_db
        - scan_loss_db
        - pointing_loss
        - obstruction_loss_db
        - rain_loss_db
        + noise_db,
    )
    return raw, raw + scan_loss_db


def ka256_scan_loss_db(rx_frequency_mhz: float, offaxis_deg: float) -> float:
    """Evaluate the firmware KA256 nearest-band fourth-order loss table."""

    frequency_ghz = float(rx_frequency_mhz) / 1000.0
    angle = min(abs(float(offaxis_deg)), 70.0)
    _frequency, coefficients = min(
        _KA256_LOSS_BANDS, key=lambda item: abs(item[0] - frequency_ghz)
    )
    result = coefficients[4]
    for coefficient in reversed(coefficients[:4]):
        result = result * angle + coefficient
    return max(0.0, result)


class ScenarioEngine:
    def __init__(self, scenario: TrackingScenario) -> None:
        self.scenario = scenario
        self._rng = random.Random(scenario.random_seed)

    def sample(
        self,
        time_s: float,
        *,
        pointing_error_deg: float,
        offaxis_deg: float,
        scan_loss_db: float,
        beam_fresh: bool,
    ) -> SimulationSample:
        t = min(max(float(time_s), 0.0), self.scenario.duration_s)
        p0, p1, ratio, duration = _segment(self.scenario.pose_keyframes, t)
        l0, l1, link_ratio, _ = _segment(self.scenario.link_keyframes, t)
        yaw_delta = _angle_delta(p0.yaw_deg, p1.yaw_deg)
        yaw = (p0.yaw_deg + yaw_delta * ratio) % 360.0
        pitch = _linear(p0.pitch_deg, p1.pitch_deg, ratio)
        roll = _linear(p0.roll_deg, p1.roll_deg, ratio)
        noise_std = _linear(l0.noise_std_db, l1.noise_std_db, link_ratio)
        noise = self._rng.gauss(0.0, noise_std) if noise_std > 0.0 else 0.0
        raw, normalized = calculate_snr(
            base_snr_db=_linear(l0.base_snr_db, l1.base_snr_db, link_ratio),
            scan_loss_db=scan_loss_db,
            pointing_error_deg=pointing_error_deg,
            hpbw_deg=self.scenario.hpbw_deg,
            obstruction_loss_db=_linear(l0.obstruction_loss_db, l1.obstruction_loss_db, link_ratio),
            rain_loss_db=_linear(l0.rain_loss_db, l1.rain_loss_db, link_ratio),
            noise_db=noise,
        )
        return SimulationSample(
            time_s=t,
            latitude_deg=_linear(p0.latitude_deg, p1.latitude_deg, ratio),
            longitude_deg=_linear(p0.longitude_deg, p1.longitude_deg, ratio),
            altitude_m=_linear(p0.altitude_m, p1.altitude_m, ratio),
            velocity_n_mps=(p1.latitude_deg - p0.latitude_deg) * 111_320.0 / duration,
            velocity_e_mps=(p1.longitude_deg - p0.longitude_deg) * 111_320.0 * math.cos(math.radians(p0.latitude_deg)) / duration,
            velocity_d_mps=-(p1.altitude_m - p0.altitude_m) / duration,
            yaw_deg=yaw,
            pitch_deg=pitch,
            roll_deg=roll,
            body_rate_x_dps=(p1.roll_deg - p0.roll_deg) / duration,
            body_rate_y_dps=(p1.pitch_deg - p0.pitch_deg) / duration,
            body_rate_z_dps=yaw_delta / duration,
            raw_snr_db=raw,
            normalized_snr_db=normalized,
            offaxis_deg=offaxis_deg,
            scan_loss_db=scan_loss_db,
            snr_valid=beam_fresh and abs(offaxis_deg) <= self.scenario.hard_limit_deg and bool(l0.rx_online),
            normalized_snr_valid=beam_fresh and abs(offaxis_deg) <= self.scenario.hard_limit_deg,
            rx_online=bool(l0.rx_online),
        )


def build_tracking_simulation_frame(
    operation: int,
    session_id: int,
    scenario: TrackingScenario,
    sample: SimulationSample | None = None,
) -> bytes:
    if session_id <= 0 or session_id > 0xFFFFFFFF:
        raise ValueError("session_id must be a non-zero uint32")
    if operation == 2:
        payload = struct.pack("<BBI", 1, operation, session_id)
    else:
        if operation not in (0, 1) or sample is None:
            raise ValueError("START/SAMPLE require a sample")
        flags = int(sample.snr_valid) | (int(sample.normalized_snr_valid) << 1) | (int(sample.rx_online) << 2)
        payload = struct.pack(
            "<BBIBdd11fI4f",
            1,
            operation,
            session_id,
            flags,
            sample.latitude_deg,
            sample.longitude_deg,
            sample.altitude_m,
            sample.velocity_n_mps,
            sample.velocity_e_mps,
            sample.velocity_d_mps,
            sample.yaw_deg,
            sample.pitch_deg,
            sample.roll_deg,
            sample.body_rate_x_dps,
            sample.body_rate_y_dps,
            sample.body_rate_z_dps,
            scenario.satellite_longitude_deg,
            scenario.satellite_id,
            sample.raw_snr_db,
            sample.normalized_snr_db,
            sample.offaxis_deg,
            sample.scan_loss_db,
        )
    return build_frame(CmdType.TRACKING_SIMULATION, payload)
